"""Screenshot automation.

Subcommands:
  dispatch  Read the control sheet, find jobs that should run now, create
            per-sheet subfolders in Drive, split each sheet into row chunks
            and emit a matrix of chunks to GITHUB_OUTPUT.
  run       Process one chunk of a data sheet: visit each URL, screenshot
            the viewport when the keyword is found, upload to the given Drive
            subfolder.
"""

import argparse
import asyncio
import hashlib
import io
import json
import os
import re
import socket
import sys
import time
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload
from playwright.async_api import async_playwright

# Every socket (Drive and Sheets HTTP calls) gets a hard 120-second deadline,
# so a stalled request raises instead of hanging until the job timeout.
socket.setdefaulttimeout(120)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive",
]

VIEWPORT = {"width": 1440, "height": 900}
NAV_TIMEOUT_MS = 45000
POST_LOAD_WAIT_MS = 2500
DEFAULT_RUN_HOURS = [9, 19]
# Rows per matrix job. ~7 s per row, so 120 rows is ~15 minutes, well inside
# the job's timeout. Override with Settings!chunk_rows in the control sheet.
DEFAULT_CHUNK_ROWS = 120
# Stop starting new rows after this many seconds, well before the job's
# 45-minute timeout (setup runs first), so a chunk slowed down by blocked pages still ends
# cleanly; the next trigger this hour picks up the rest.
RUN_BUDGET_S = 33 * 60  # plus ~5 min setup and one slow row stays under 45
MAX_CHUNK_ROWS = 200  # larger chunks risk the job timeout
MAX_MATRIX_JOBS = 256  # GitHub's limit per workflow run
# Read-only Drive/Sheets calls are safe to retry. Uploads are not (a retry
# after a timeout can create a second file), see upload_to_drive.
READ_RETRIES = 3
UPLOAD_ATTEMPTS = 3
# Shrink the page before scrolling to the keyword so more of the page (ideally
# including context above the keyword) fits into the viewport screenshot.
ZOOM_OUT_FACTOR = "70%"
# Amazon serves these instead of the list when it blocks or throttles a client.
BLOCKED_MARKERS = (
    "enter the characters you see below",
    "sorry, we just need to make sure you're not a robot",
    "request was throttled",
    "to discuss automated access to amazon data",
    "click the button below to continue shopping",
)
MATRIX_SEP = "|"
# Drive folder (inside DRIVE_FOLDER_ID) holding one small marker file per
# finished chunk and local hour.
MARKERS_FOLDER = "_screenshot_run_markers"


def get_credentials():
    key_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "credentials.json")
    return Credentials.from_service_account_file(key_path, scopes=SCOPES)


def slugify(s, max_len=60):
    s = re.sub(r"[^\w\-]+", "_", (s or "").strip())[:max_len]
    return s.strip("_") or "x"


def extract_sheet_id(url_or_id):
    s = (url_or_id or "").strip()
    m = re.search(r"/d/([a-zA-Z0-9_-]+)", s)
    return m.group(1) if m else s


def public_text(value, limit=300):
    """Text safe for the public run logs: URLs (which can carry book titles)
    are replaced, and long messages cut."""
    text = str(value)
    # Playwright puts the whole URL in quotes ("navigating to \"...\""),
    # and a URL can contain spaces: hide quoted URLs whole first.
    text = re.sub(r"([\"'])https?://.*?\1", r"\1<url>\1", text)
    text = re.sub(r"https?://\S+", "<url>", text)
    # Links written without a scheme (www.amazon.com/Book-Title/...).
    text = re.sub(r"\b(?:[\w-]+\.)+[a-z]{2,}(?::\d+)?/\S*", "<url>", text, flags=re.I)
    # Drive/Sheets query strings name files (category, country, book title).
    text = re.sub(r"\bq=\S+", "q=<query>", text)
    # Quoted names in Drive/Sheets errors (name contains 'Book Title').
    text = re.sub(r"'[^']{3,}'", "'<text>'", text)
    return text[:limit]


def _drive_quote(s):
    return s.replace("\\", "\\\\").replace("'", "\\'")


# ---------- Slack ----------

def notify_slack(text):
    """Post to SLACK_WEBHOOK_URL if it is set. Never raises."""
    url = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if not url:
        return False
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps({"text": text}).encode(),
            headers={"Content-type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=20).read()
        return True
    except Exception as e:
        print(f"  slack notify failed: {public_text(e)}", file=sys.stderr, flush=True)
        return False


# ---------- Drive ----------

def find_or_create_subfolder(drive_service, parent_id, name):
    q = (
        f"name = '{_drive_quote(name)}' "
        f"and mimeType = 'application/vnd.google-apps.folder' "
        f"and '{parent_id}' in parents and trashed = false"
    )
    res = drive_service.files().list(
        q=q,
        fields="files(id,name)",
        includeItemsFromAllDrives=True,
        supportsAllDrives=True,
    ).execute(num_retries=READ_RETRIES)
    if res.get("files"):
        return res["files"][0]["id"]
    created = drive_service.files().create(
        body={
            "name": name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_id],
        },
        fields="id",
        supportsAllDrives=True,
    ).execute()
    return created["id"]


def find_file_by_name(drive_service, folder_id, name):
    q = f"name = '{_drive_quote(name)}' and '{folder_id}' in parents and trashed = false"
    res = drive_service.files().list(
        q=q,
        fields="files(id)",
        includeItemsFromAllDrives=True,
        supportsAllDrives=True,
        pageSize=1,
    ).execute(num_retries=READ_RETRIES)
    files = res.get("files", [])
    return files[0]["id"] if files else None


def upload_to_drive(drive_service, folder_id, name, data, sleep=None):
    """Upload a PNG; returns the file id.

    A create that times out may still have landed, so before trying again we
    look for the file by its (timestamped, unique) name instead of blindly
    re-sending it, which would leave a duplicate.
    """
    sleep = sleep or time.sleep
    last_exc = None
    for attempt in range(1, UPLOAD_ATTEMPTS + 1):
        if attempt > 1:
            sleep(2 ** attempt)
            try:
                existing = find_file_by_name(drive_service, folder_id, name)
            except Exception as e:
                existing = None
                print(f"  upload check failed: {public_text(e)}", flush=True)
            if existing:
                return existing
        try:
            media = MediaIoBaseUpload(io.BytesIO(data), mimetype="image/png", resumable=False)
            created = drive_service.files().create(
                body={"name": name, "parents": [folder_id]},
                media_body=media,
                fields="id",
                supportsAllDrives=True,
            ).execute()
            return created["id"]
        except Exception as e:
            last_exc = e
            print(f"  upload attempt {attempt} failed: {public_text(e)}", flush=True)
    try:
        existing = find_file_by_name(drive_service, folder_id, name)
    except Exception:
        existing = None
    if existing:
        return existing
    raise last_exc


def _is_not_found(exc):
    status = getattr(exc, "status_code", None) or getattr(getattr(exc, "resp", None), "status", None)
    return str(status) == "404"


def delete_from_drive(drive_service, file_id):
    """Delete a file; a file that is already gone counts as deleted."""
    try:
        drive_service.files().delete(fileId=file_id, supportsAllDrives=True).execute(
            num_retries=READ_RETRIES
        )
    except HttpError as e:
        if _is_not_found(e):
            return
        raise


# ---------- URL banner ----------

URL_BAR_HEIGHT = 48
URL_BAR_BG = (222, 225, 230)
URL_PILL_BG = (255, 255, 255)
URL_PILL_BORDER = (200, 203, 209)
URL_TEXT_COLOR = (60, 64, 67)


def _load_font(size):
    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",          # ubuntu runner
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "C:/Windows/Fonts/arial.ttf",                                # local dev
    ):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def add_url_banner(png_bytes, url):
    """Stamp a browser-style address bar above the screenshot showing the URL."""
    img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    banner = Image.new("RGB", (img.width, img.height + URL_BAR_HEIGHT), URL_BAR_BG)
    banner.paste(img, (0, URL_BAR_HEIGHT))
    draw = ImageDraw.Draw(banner)

    pad = 12
    pill = (pad, 8, img.width - pad, URL_BAR_HEIGHT - 8)
    draw.rounded_rectangle(pill, radius=16, fill=URL_PILL_BG, outline=URL_PILL_BORDER)

    font = _load_font(16)
    text_x = pill[0] + 14
    max_w = pill[2] - text_x - 14
    text = url
    if draw.textlength(text, font=font) > max_w:
        while text and draw.textlength(text + "…", font=font) > max_w:
            text = text[:-1]
        text += "…"
    draw.text((text_x, URL_BAR_HEIGHT // 2), text, fill=URL_TEXT_COLOR, font=font, anchor="lm")

    out = io.BytesIO()
    banner.save(out, format="PNG")
    return out.getvalue()


META_BAR_HEIGHT = 28
META_BAR_BG = (241, 243, 244)
META_TEXT_COLOR = (32, 33, 36)


def add_meta_banner(png_bytes, category, captured_at):
    """Stamp a status line above the image showing category and capture time.

    Keeps that identifying info visible on the image itself even when the
    keyword sits below the fold and the visible viewport no longer shows the
    page's own category/breadcrumb.
    """
    img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    banner = Image.new("RGB", (img.width, img.height + META_BAR_HEIGHT), META_BAR_BG)
    banner.paste(img, (0, META_BAR_HEIGHT))
    draw = ImageDraw.Draw(banner)

    font = _load_font(14)
    text = (
        f"Category: {category or 'N/A'}    "
        f"Captured: {captured_at.strftime('%Y-%m-%d %H:%M:%S UTC')}"
    )
    draw.text((14, META_BAR_HEIGHT // 2), text, fill=META_TEXT_COLOR, font=font, anchor="lm")

    out = io.BytesIO()
    banner.save(out, format="PNG")
    return out.getvalue()


FILTERED_NAME_RE = re.compile(r"^rank(\d+)_")


def find_filtered_entries(drive_service, filtered_folder_id, key_slug):
    """Return [(file_id, rank), ...] of existing filtered entries for this book."""
    q = (
        f"'{filtered_folder_id}' in parents and trashed = false and "
        f"name contains '{_drive_quote(key_slug)}'"
    )
    entries = []
    page_token = None
    while True:
        kwargs = dict(
            q=q,
            fields="nextPageToken, files(id,name)",
            includeItemsFromAllDrives=True,
            supportsAllDrives=True,
            pageSize=100,
        )
        if page_token:
            kwargs["pageToken"] = page_token
        res = drive_service.files().list(**kwargs).execute(num_retries=READ_RETRIES)
        for f in res.get("files", []):
            m = FILTERED_NAME_RE.match(f["name"])
            if m and key_slug in f["name"]:
                entries.append((f["id"], int(m.group(1))))
        page_token = res.get("nextPageToken")
        if not page_token:
            return entries


def update_filtered_best(drive_service, filtered_folder_id, url, row, rank, ts, png):
    """Keep only the best (lowest) rank screenshot per URL in the filtered folder.

    Uploads the new best first and only then removes the old ones, so a failed
    upload never leaves the book without its previous best screenshot.
    Returns a short status string for the log.
    """
    # Keyed on the URL itself (not the sheet's label text) so a
    # category/country/keyword edit in the sheet can't orphan the
    # previous best-rank file for this book.
    url_key = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
    readable = f"{slugify(row['category'])}_{slugify(row['country'])}_{slugify(row['keyword'])}"
    existing = find_filtered_entries(drive_service, filtered_folder_id, url_key)
    best_existing_rank = min((r for _, r in existing), default=None)
    if best_existing_rank is not None and rank >= best_existing_rank:
        # Two chunks running at once can each upload a "best" for the same
        # URL; keep one file at the best rank and remove the rest.
        keep = next(fid for fid, r in existing if r == best_existing_rank)
        for file_id, _ in existing:
            if file_id != keep:
                delete_from_drive(drive_service, file_id)
        return f"rank #{rank} not better than existing best #{best_existing_rank}; skipping filtered upload"
    filtered_name = f"rank{rank:04d}_{url_key}_{readable}_{ts}.png"
    new_id = upload_to_drive(drive_service, filtered_folder_id, filtered_name, png)
    for file_id, _ in existing:
        if file_id != new_id:
            delete_from_drive(drive_service, file_id)
    return f"uploaded to filtered (rank #{rank}, previous best {best_existing_rank})"


# ---------- Sheets ----------

def read_data_sheet(sheets_service, sheet_id, range_="A:D"):
    res = sheets_service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=range_
    ).execute(num_retries=READ_RETRIES)
    rows = res.get("values", [])
    out = []
    for row in rows[1:]:  # skip header
        row = (row + [""] * 4)[:4]
        category, url, country, keyword = (c.strip() for c in row)
        if url and keyword:
            out.append(
                {"category": category, "url": url, "country": country, "keyword": keyword}
            )
    return out


def unique_rows(rows):
    """Drop repeated rows (same URL and labels), keeping the first. Dispatch
    and run both use this, so chunk ranges point at the same rows."""
    seen = set()
    out = []
    for r in rows:
        key = row_suffix(r)
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def load_rows(sheets_service, sheet_id):
    return unique_rows(read_data_sheet(sheets_service, sheet_id))


def get_sheet_title(sheets_service, sheet_id):
    meta = sheets_service.spreadsheets().get(
        spreadsheetId=sheet_id, fields="properties.title"
    ).execute(num_retries=READ_RETRIES)
    return meta["properties"]["title"]


def parse_run_hours(val):
    """Parse '2,4,...' into valid hours 0-23. 24 means midnight (0)."""
    hours = []
    for part in val.split(","):
        part = part.strip()
        if not part:
            continue
        h = int(part)
        if h == 24:
            h = 0
        if not 0 <= h <= 23:
            print(f"  run_hours: ignoring invalid hour {part}", file=sys.stderr, flush=True)
            continue
        if h not in hours:
            hours.append(h)
    return hours


def read_control_sheet(sheets_service, control_id):
    """Return (jobs, settings) where settings has run_hours and chunk_rows."""
    jobs_res = sheets_service.spreadsheets().values().get(
        spreadsheetId=control_id, range="Jobs!A:D"
    ).execute(num_retries=READ_RETRIES)
    jobs_rows = jobs_res.get("values", [])

    jobs = []
    for i, row in enumerate(jobs_rows[1:], start=2):
        row = (row + [""] * 4)[:4]
        sheet_url, start_str, stop_str, tz_str = (c.strip() for c in row)
        if not sheet_url:
            continue
        try:
            tz = ZoneInfo(tz_str) if tz_str else ZoneInfo("UTC")
            start = datetime.strptime(start_str, "%Y-%m-%d %H:%M").replace(tzinfo=tz)
            stop = datetime.strptime(stop_str, "%Y-%m-%d %H:%M").replace(tzinfo=tz)
        except Exception as e:
            print(f"  Jobs row {i}: bad date/timezone ({public_text(e)})", file=sys.stderr, flush=True)
            continue
        jobs.append(
            {
                "row": i,
                "sheet_id": extract_sheet_id(sheet_url),
                "start": start,
                "stop": stop,
                "tz": tz,
                "tz_str": tz_str or "UTC",
            }
        )

    settings = {"run_hours": list(DEFAULT_RUN_HOURS), "chunk_rows": DEFAULT_CHUNK_ROWS}
    try:
        s_res = sheets_service.spreadsheets().values().get(
            spreadsheetId=control_id, range="Settings!A:B"
        ).execute(num_retries=READ_RETRIES)
        for srow in s_res.get("values", [])[1:]:
            if len(srow) < 2:
                continue
            key = srow[0].strip().lower()
            val = srow[1].strip()
            if not val:
                continue
            try:
                if key == "run_hours":
                    hours = parse_run_hours(val)
                    if hours:
                        settings["run_hours"] = hours
                    else:
                        print(f"::error::run_hours '{val}' has no valid hour; using {settings['run_hours']}", flush=True)
                elif key == "chunk_rows":
                    settings["chunk_rows"] = min(MAX_CHUNK_ROWS, max(10, int(val)))
            except ValueError as e:
                print(f"  bad {key} value '{val}': {public_text(e)}", file=sys.stderr, flush=True)
    except Exception as e:
        print(f"  could not read Settings tab (using defaults): {public_text(e)}", file=sys.stderr, flush=True)

    return jobs, settings


# ---------- dispatch ----------

def chunk_ranges(n_rows, chunk_rows):
    """[(start, end), ...] half-open row ranges covering n_rows."""
    return [(s, min(s + chunk_rows, n_rows)) for s in range(0, n_rows, chunk_rows)]


def encode_matrix_entry(sheet_id, subfolder_id, filtered_id, start, end, markers_id, hour_key):
    # Plain strings only: no braces or sheet titles in the job output, so
    # GitHub's secret masking can't match it and drop the whole matrix
    # ("Skip output 'matrix' since it may contain secret").
    return MATRIX_SEP.join(
        [sheet_id, subfolder_id, filtered_id, str(start), str(end), markers_id, hour_key]
    )


def decode_matrix_entry(entry):
    sheet_id, subfolder_id, filtered_id, start, end, markers_id, hour_key = entry.split(MATRIX_SEP)
    return sheet_id, subfolder_id, filtered_id, int(start), int(end), markers_id, hour_key


def local_hour_start(now_in_tz):
    return now_in_tz.replace(minute=0, second=0, microsecond=0)


def hour_key(now_in_tz):
    """Start of the job's local hour as a UTC instant, e.g. 20261102T1530Z
    for 21:00 in Asia/Kolkata. Used to mark finished chunks and to find
    screenshots already taken this hour."""
    return local_hour_start(now_in_tz).astimezone(timezone.utc).strftime("%Y%m%dT%H%MZ")


def hour_key_start(key):
    return datetime.strptime(key, "%Y%m%dT%H%MZ").replace(tzinfo=timezone.utc)


TS_PREFIX_LEN = len("YYYYMMDD-HHMMSS_")


def row_suffix(row):
    """File name of a row's screenshot without its timestamp prefix. Ends with
    a short hash of the URL, so two pages with the same labels never share a
    name, while a duplicate row (same URL and labels) does."""
    url_key = hashlib.sha1(row["url"].encode("utf-8")).hexdigest()[:6]
    return (
        f"{slugify(row['category'])}_{slugify(row['country'])}_"
        f"{slugify(row['keyword'])}_{url_key}.png"
    )


def taken_since(drive_service, folder_id, since_utc):
    """Name suffixes (see row_suffix) of screenshots in folder_id created
    since since_utc, so a re-run in the same hour skips rows already done."""
    q = (
        f"'{folder_id}' in parents and trashed = false and "
        f"createdTime >= '{since_utc.strftime('%Y-%m-%dT%H:%M:%SZ')}'"
    )
    out = set()
    page_token = None
    while True:
        kwargs = dict(
            q=q,
            fields="nextPageToken, files(name)",
            includeItemsFromAllDrives=True,
            supportsAllDrives=True,
            pageSize=1000,
        )
        if page_token:
            kwargs["pageToken"] = page_token
        res = drive_service.files().list(**kwargs).execute(num_retries=READ_RETRIES)
        for f in res.get("files", []):
            if len(f["name"]) > TS_PREFIX_LEN:
                out.add(f["name"][TS_PREFIX_LEN:])
        page_token = res.get("nextPageToken")
        if not page_token:
            return out


def marker_name(sheet_id, hour_key_, start, end):
    return f"done_{sheet_id}_{hour_key_}_{start}-{end}"


def chunk_done(drive_service, markers_id, sheet_id, hour_key_, start, end):
    return find_file_by_name(drive_service, markers_id, marker_name(sheet_id, hour_key_, start, end)) is not None


def mark_chunk_done(drive_service, markers_id, sheet_id, hour_key_, start, end, summary):
    """Record in Drive that this chunk was done for this local hour, so a
    second trigger in the same hour (or a retry after another chunk failed)
    skips it instead of taking the same screenshots again."""
    name = marker_name(sheet_id, hour_key_, start, end)
    media = MediaIoBaseUpload(io.BytesIO(summary.encode("utf-8")), mimetype="text/plain", resumable=False)
    drive_service.files().create(
        body={"name": name, "parents": [markers_id]},
        media_body=media,
        fields="id",
        supportsAllDrives=True,
    ).execute()


def cmd_dispatch():
    output_path = os.environ.get("GITHUB_OUTPUT")
    active = []
    problems = []

    def write_outputs():
        # Matrix must always have at least one entry, otherwise GitHub Actions
        # errors on strategy expansion. Use a sentinel when there's no work.
        matrix_value = active if active else ["__noop__"]
        has_jobs = "true" if active else "false"
        payload = json.dumps(matrix_value)
        if output_path:
            with open(output_path, "a", encoding="utf-8") as f:
                f.write(f"matrix={payload}\n")
                f.write(f"has_jobs={has_jobs}\n")
                f.write(f"problems={'true' if problems else 'false'}\n")
        print(f"Matrix entries: {len(active)}", flush=True)

    try:
        control_id = os.environ.get("CONTROL_SHEET_ID", "").strip()
        drive_folder_id = os.environ.get("DRIVE_FOLDER_ID", "").strip()
        if not control_id:
            raise RuntimeError("CONTROL_SHEET_ID env var is empty or missing")
        if not drive_folder_id:
            raise RuntimeError("DRIVE_FOLDER_ID env var is empty or missing")

        creds = get_credentials()
        sheets_service = build("sheets", "v4", credentials=creds, cache_discovery=False)
        drive_service = build("drive", "v3", credentials=creds, cache_discovery=False)

        jobs, settings = read_control_sheet(sheets_service, control_id)
        run_hours = settings["run_hours"]
        print(f"Run hours: {run_hours}; chunk rows: {settings['chunk_rows']}", flush=True)
        print(f"Jobs in control sheet: {len(jobs)}", flush=True)

        due = []
        for job in jobs:
            now_in_tz = datetime.now(job["tz"])
            in_window = job["start"] <= now_in_tz <= job["stop"]
            hour_ok = now_in_tz.hour in run_hours
            if not (in_window and hour_ok):
                print(
                    f"  skip row {job['row']} ({job['sheet_id'][:8]}) "
                    f"now={now_in_tz.strftime('%Y-%m-%d %H:%M')} {job['tz_str']}, "
                    f"start={job['start'].strftime('%Y-%m-%d %H:%M')}, "
                    f"stop={job['stop'].strftime('%Y-%m-%d %H:%M')}, "
                    f"in_window={in_window}, hour_in_run_hours={hour_ok}",
                    flush=True,
                )
                continue
            due.append((job, hour_key(now_in_tz)))

        force = os.environ.get("FORCE_RERUN", "").strip().lower() == "true"
        markers_id = None
        if due:
            markers_id = find_or_create_subfolder(drive_service, drive_folder_id, MARKERS_FOLDER)
        seen = set()
        # Logs are public while the repo is public: they name sheets by an id
        # prefix only, never by title (client and book names). Slack gets titles.
        titles = {}
        for job, hkey in due:
            tag = job["sheet_id"][:8]
            if job["sheet_id"] in seen:
                print(f"  skip row {job['row']}: sheet already dispatched by another row", flush=True)
                continue
            seen.add(job["sheet_id"])
            try:
                title = get_sheet_title(sheets_service, job["sheet_id"])
                n_rows = len(load_rows(sheets_service, job["sheet_id"]))
            except Exception as e:
                print(f"  row {job['row']}: cannot read sheet ({public_text(e)})", file=sys.stderr, flush=True)
                problems.append(f"control row {job['row']}: cannot read data sheet")
                continue
            try:
                subfolder_id = find_or_create_subfolder(drive_service, drive_folder_id, title)
                filtered_subfolder_id = find_or_create_subfolder(
                    drive_service, drive_folder_id, f"{title} filtered"
                )
            except Exception as e:
                print(f"  row {job['row']} ({tag}): cannot create subfolder ({public_text(e)})", file=sys.stderr, flush=True)
                problems.append(f"sheet {tag}: cannot create Drive folder")
                titles[tag] = title
                continue
            if n_rows == 0:
                problems.append(f"sheet {tag}: no rows with both URL and keyword")
                titles[tag] = title
                continue
            ranges = chunk_ranges(n_rows, settings["chunk_rows"])
            skipped = 0
            for start, end in ranges:
                if not force and chunk_done(drive_service, markers_id, job["sheet_id"], hkey, start, end):
                    skipped += 1
                    continue
                active.append(
                    encode_matrix_entry(job["sheet_id"], subfolder_id, filtered_subfolder_id,
                                        start, end, markers_id, hkey)
                )
            if skipped:
                print(f"  sheet {tag}: {skipped} chunk(s) already done this hour ({hkey}); skipped", flush=True)
            print(
                f"  ACTIVE: sheet {tag} ({n_rows} rows, {len(ranges)} chunks) -> {subfolder_id} "
                f"| filtered -> {filtered_subfolder_id}",
                flush=True,
            )

        if len(active) > MAX_MATRIX_JOBS:
            problems.append(
                f"{len(active)} chunks exceed GitHub's {MAX_MATRIX_JOBS}; raise chunk_rows"
            )
            del active[MAX_MATRIX_JOBS:]

        print(f"Active chunks to dispatch: {len(active)}", flush=True)
        if problems:
            print("::error::Screenshot dispatch problems: " + "; ".join(problems), flush=True)
            names = "; ".join(f"{k} = {v}" for k, v in titles.items())
            notify_slack(":warning: Screenshot dispatch problems: " + "; ".join(problems)
                         + (f" ({names})" if names else ""))
            if not active:
                # Work was due but nothing could be dispatched: fail so the
                # run shows red instead of passing with no screenshots.
                raise RuntimeError("work was due but none could be dispatched: " + "; ".join(problems))
    except Exception as e:
        notify_slack(f":rotating_light: Screenshot dispatch failed: {e}")
        raise
    finally:
        write_outputs()


# ---------- run ----------

# Finds the "#N" rank badge belonging to the matched book by walking up from
# its DOM element until an ancestor's text contains exactly one such badge.
# Structure-agnostic (no dependence on Amazon's auto-generated CSS class
# names) since it keys off ancestor scope narrowing to a single badge rather
# than a specific selector.
_RANK_BADGE_JS = """
(el) => {
    const rx = /#\\s?\\d{1,4}\\b/g;
    let cur = el;
    for (let i = 0; i < 20 && cur; i++) {
        const matches = (cur.innerText || '').match(rx) || [];
        if (matches.length === 1) return matches[0];
        if (matches.length > 1) return null;
        cur = cur.parentElement;
    }
    return null;
}
"""


async def extract_rank(page, keyword):
    try:
        matched_text = await page.get_by_text(keyword, exact=False).first.evaluate(_RANK_BADGE_JS)
    except Exception:
        return None
    if not matched_text:
        return None
    digits = re.sub(r"\D", "", matched_text)
    return int(digits) if digits else None


def is_blocked_page(body_text):
    low = (body_text or "").lower()
    return any(m in low for m in BLOCKED_MARKERS)


def looks_like_ranked_list(body_text):
    """Bestseller pages show rank badges like '#1' and '#2'."""
    return len(re.findall(r"#\s?\d{1,3}\b", body_text or "")) >= 2


def is_allowed_url(url):
    """Only public web pages: http(s), not localhost or private addresses."""
    url = (url or "").strip()
    if "\\" in url:
        return False  # browsers read a backslash as a slash
    m = re.match(r"^https?://(?:[^/\s@]*@)?(\[[^\]]+\]|[^/\s:?#]+)", url, re.I)
    if not m:
        return False
    host = m.group(1).lower().strip("[]").rstrip(".")
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".internal"):
        return False
    import ipaddress
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        pass
    # Numbers-only hosts (2130706433, 0x7f.1, 127.1) are other spellings of
    # IP addresses that browsers accept; no real site is named that way.
    if re.fullmatch(r"(0x[0-9a-f]+|[0-9]+)(\.(0x[0-9a-f]+|[0-9]+)){0,3}", host):
        return False
    return True


class RowError(Exception):
    """A row could not be processed (blocked, load failure, upload failure)."""


async def _load(page, url):
    await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    await page.wait_for_timeout(POST_LOAD_WAIT_MS)
    return await page.evaluate("() => document.body ? document.body.innerText : ''")


async def process_row(page, row, drive_service, drive_folder_id, filtered_folder_id, sheet_label):
    """Returns True if a screenshot was uploaded, False if the keyword was not
    on the page. Raises RowError when the row could not be checked."""
    url = row["url"]
    keyword = row["keyword"]
    print(f"[{sheet_label}] {row['category'][:60]} / {row['country']}", flush=True)

    if not is_allowed_url(url):
        raise RowError("not an http(s) URL (check the sheet's URL column)")

    try:
        body_text = await _load(page, url)
        if is_blocked_page(body_text):
            print("  blocked/throttled by the site; retrying once in 20 s", flush=True)
            await page.wait_for_timeout(20000)
            body_text = await _load(page, url)
    except Exception as e:
        raise RowError(f"load failed: {e}") from e

    if is_blocked_page(body_text):
        raise RowError("blocked/throttled by the site")

    if keyword.lower() not in body_text.lower():
        if not looks_like_ranked_list(body_text):
            # Not a normal bestseller list (changed layout, redirect, error
            # page): an error, so it alerts and is retried, instead of a
            # quiet "not found".
            raise RowError("page does not look like a bestseller list")
        print("  keyword not found", flush=True)
        return False

    rank = await extract_rank(page, keyword)
    print(f"  rank: {rank if rank is not None else 'not found'}", flush=True)

    try:
        # Zoom out before locating the keyword so the element position is
        # computed against the shrunk layout, giving more context around it.
        await page.evaluate(
            "(z) => { document.documentElement.style.zoom = z; }", ZOOM_OUT_FACTOR
        )
        await page.wait_for_timeout(300)
    except Exception as e:
        print(f"  zoom failed: {public_text(e)}", flush=True)

    try:
        await page.get_by_text(keyword, exact=False).first.scroll_into_view_if_needed(
            timeout=4000
        )
        await page.wait_for_timeout(400)
    except Exception:
        pass

    try:
        raw_png = await page.screenshot(full_page=False)
    except Exception as e:
        raise RowError(f"screenshot failed: {e}") from e
    captured_at = datetime.now(timezone.utc)
    try:
        png = add_meta_banner(add_url_banner(raw_png, page.url), row["category"], captured_at)
    except Exception as e:
        print(f"  banner failed ({public_text(e)}); uploading without it", flush=True)
        png = raw_png
    ts = captured_at.strftime("%Y%m%d-%H%M%S")
    name = f"{ts}_{row_suffix(row)}"
    try:
        upload_to_drive(drive_service, drive_folder_id, name, png)
    except Exception as e:
        raise RowError(f"upload failed: {e}") from e
    print("  uploaded screenshot", flush=True)

    if filtered_folder_id:
        if rank is None:
            print("  rank not found; skipping filtered folder update", flush=True)
        else:
            try:
                status = update_filtered_best(
                    drive_service, filtered_folder_id, url, row, rank, ts, png
                )
                print(f"  {status}", flush=True)
            except Exception as e:
                print(f"  filtered rank update failed: {public_text(e)}", flush=True)

    return True


def summarize(label, start, end, found, not_found, errors, skipped=0):
    total = end - start
    return (
        f"sheet {label} rows {start + 1}-{end}: {found} screenshots, "
        f"{not_found} keyword not found, {skipped} already done this hour, "
        f"{len(errors)} errors (of {total})"
    )


def should_fail(found, errors, attempted=None):
    """Fail the job (red run, Slack alert) when nothing worked or more than a
    few rows errored. A retry in the same hour only redoes rows without a
    screenshot, so failing is safe."""
    if not errors:
        return False
    if found == 0:
        return True
    attempted = attempted if attempted is not None else found + len(errors)
    return len(errors) > max(2, attempted // 10)


async def cmd_run(matrix_entry):
    sheet_id, drive_folder_id, filtered_folder_id, start, end, markers_id, hkey = decode_matrix_entry(matrix_entry)
    creds = get_credentials()
    sheets_service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    drive_service = build("drive", "v3", credentials=creds, cache_discovery=False)

    label = sheet_id[:8]  # public logs: id prefix only
    try:
        title = get_sheet_title(sheets_service, sheet_id)
    except Exception:
        title = label
    rows = load_rows(sheets_service, sheet_id)[start:end]
    last = start + len(rows)  # the sheet may have shrunk since dispatch
    print(f"=== Sheet '{label}' rows {start + 1}-{last} ===", flush=True)
    if not rows:
        print(f"::warning::chunk rows {start + 1}-{end} is empty (sheet shrank since dispatch)", flush=True)

    force = os.environ.get("FORCE_RERUN", "").strip().lower() == "true"
    try:
        done = set() if force else taken_since(drive_service, drive_folder_id, hour_key_start(hkey))
    except Exception as e:
        done = set()
        print(f"::warning::could not list this hour's screenshots (no row skipping): {public_text(e)}", flush=True)

    found = 0
    not_found = 0
    skipped = 0
    deferred = 0
    errors = []
    started = time.monotonic()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport=VIEWPORT)
        page = await context.new_page()
        for i, row in enumerate(rows, start=start + 1):
            if time.monotonic() - started > RUN_BUDGET_S:
                deferred = last - i + 1
                print(f"::warning::time budget used; {deferred} rows left for the next run", flush=True)
                break
            if row_suffix(row) in done:
                # Already captured this hour (an earlier run that was cut off,
                # or a duplicate row in the sheet).
                skipped += 1
                continue
            try:
                if await process_row(page, row, drive_service, drive_folder_id, filtered_folder_id, label):
                    found += 1
                    done.add(row_suffix(row))
                else:
                    not_found += 1
            except Exception as e:
                errors.append(f"row {i}: {e}")
                print(f"  ERROR row {i}: {public_text(e)}", flush=True)
                # A fresh page after any failure, so one stuck or crashed
                # page can't take down the rest of the chunk.
                try:
                    await page.close()
                except Exception:
                    pass
                page = await context.new_page()
        await browser.close()

    summary = summarize(label, start, last, found, not_found, errors, skipped)
    print(f"Done: {summary}", flush=True)
    if deferred:
        summary += f", {deferred} left for the next run"
        notify_slack(f":warning: Screenshots for {title}: {summary} (the time budget ran out; "
                     f"the next run this hour continues if the hour is in run_hours).")
    for err in errors[:20]:
        print(f"  - {public_text(err)}", flush=True)
    if errors:
        notify_slack(f":warning: Screenshots for {title}: {summary}. First error: {errors[0][:200]}")
    elif found == 0 and not_found > 0:
        notify_slack(f":warning: Screenshots for {title}: {summary}. The book was not found on any page "
                     f"in this chunk; check the sheet's keywords or Amazon's pages.")
    if should_fail(found, errors, found + not_found + len(errors)):
        print("::error::" + summary, flush=True)
        sys.exit(1)
    if errors or deferred:
        # No marker: the next trigger this hour retries this chunk, and
        # skips the rows that already have a screenshot.
        print("::warning::" + summary, flush=True)
        return
    try:
        mark_chunk_done(drive_service, markers_id, sheet_id, hkey, start, end, summary)
    except Exception as e:
        print(f"::warning::could not write done-marker: {public_text(e)}", flush=True)
        notify_slack(f":warning: Screenshot chunk done but its marker failed ({title}): {e}")


# ---------- entry ----------

def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("dispatch", help="Read control sheet, output matrix")
    p_run = sub.add_parser("run", help="Process one chunk of a data sheet")
    p_run.add_argument("--job", default=os.environ.get("MATRIX_JOB", ""),
                       help="matrix entry from dispatch (default: $MATRIX_JOB)")

    args = parser.parse_args()
    if args.cmd == "run" and not args.job:
        parser.error("run needs --job or MATRIX_JOB")
    try:
        if args.cmd == "dispatch":
            cmd_dispatch()
        else:
            asyncio.run(cmd_run(args.job))
    except SystemExit:
        raise
    except Exception as e:
        # No raw traceback in the public log: error class and filtered text.
        print(f"::error::{type(e).__name__}: {public_text(e)}", flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
