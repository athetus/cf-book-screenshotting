"""Screenshot automation.

Subcommands:
  dispatch  Read the control sheet, find jobs that should run now, create
            per-sheet subfolders in Drive, and emit a JSON matrix of jobs
            to GITHUB_OUTPUT.
  run       Process a single data sheet: visit each URL, screenshot
            viewport when the keyword is found, upload to the given Drive
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
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from playwright.async_api import async_playwright

# Give every socket (including Drive API HTTP calls) a hard 120-second
# read/write deadline.  Without this, a stalled upload can hang forever and
# eventually raise the "read operation timed out" error seen in run #5382.
socket.setdefaulttimeout(120)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive",
]

VIEWPORT = {"width": 1440, "height": 900}
NAV_TIMEOUT_MS = 45000
POST_LOAD_WAIT_MS = 2500
DEFAULT_RUN_HOURS = [9, 19]
ZOOM_OUT_FACTOR = "70%"


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


# ---------- Drive ----------

def find_or_create_subfolder(drive_service, parent_id, name):
    safe_name = name.replace("'", "\\'")
    q = (
        f"name = '{safe_name}' "
        f"and mimeType = 'application/vnd.google-apps.folder' "
        f"and '{parent_id}' in parents and trashed = false"
    )
    res = drive_service.files().list(
        q=q,
        fields="files(id,name)",
        includeItemsFromAllDrives=True,
        supportsAllDrives=True,
    ).execute(num_retries=3)
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
    ).execute(num_retries=3)
    return created["id"]


def upload_to_drive(drive_service, folder_id, name, data):
    media = MediaIoBaseUpload(io.BytesIO(data), mimetype="image/png", resumable=False)
    drive_service.files().create(
        body={"name": name, "parents": [folder_id]},
        media_body=media,
        fields="id",
        supportsAllDrives=True,
    ).execute(num_retries=3)


# ---------- URL banner ----------

URL_BAR_HEIGHT = 48
URL_BAR_BG = (222, 225, 230)
URL_PILL_BG = (255, 255, 255)
URL_PILL_BORDER = (200, 203, 209)
URL_TEXT_COLOR = (60, 64, 67)


def _load_font(size):
    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "C:/Windows/Fonts/arial.ttf",
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
    safe_key = key_slug.replace("'", "\\'")
    q = (
        f"'{filtered_folder_id}' in parents and trashed = false and "
        f"name contains '{safe_key}'"
    )
    res = drive_service.files().list(
        q=q,
        fields="files(id,name)",
        includeItemsFromAllDrives=True,
        supportsAllDrives=True,
        pageSize=10,
    ).execute(num_retries=3)
    entries = []
    for f in res.get("files", []):
        m = FILTERED_NAME_RE.match(f["name"])
        if m:
            entries.append((f["id"], int(m.group(1))))
    return entries


def delete_from_drive(drive_service, file_id):
    drive_service.files().delete(fileId=file_id, supportsAllDrives=True).execute(num_retries=3)


# ---------- Sheets ----------

def read_data_sheet(sheets_service, sheet_id, range_="A:D"):
    res = sheets_service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=range_
    ).execute()
    rows = res.get("values", [])
    out = []
    for row in rows[1:]:
        row = (row + [""] * 4)[:4]
        category, url, country, keyword = (c.strip() for c in row)
        if url and keyword:
            out.append(
                {"category": category, "url": url, "country": country, "keyword": keyword}
            )
    return out


def get_sheet_title(sheets_service, sheet_id):
    meta = sheets_service.spreadsheets().get(
        spreadsheetId=sheet_id, fields="properties.title"
    ).execute()
    return meta["properties"]["title"]


def read_control_sheet(sheets_service, control_id):
    jobs_res = sheets_service.spreadsheets().values().get(
        spreadsheetId=control_id, range="Jobs!A:D"
    ).execute()
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
            print(f"  Jobs row {i}: bad date/timezone ({e})", file=sys.stderr, flush=True)
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

    run_hours = list(DEFAULT_RUN_HOURS)
    try:
        s_res = sheets_service.spreadsheets().values().get(
            spreadsheetId=control_id, range="Settings!A:B"
        ).execute()
        for srow in s_res.get("values", [])[1:]:
            if len(srow) < 2:
                continue
            key = srow[0].strip().lower()
            val = srow[1].strip()
            if key == "run_hours" and val:
                try:
                    run_hours = [int(h) for h in val.split(",") if h.strip()]
                except ValueError as e:
                    print(f"  bad run_hours value '{val}': {e}", file=sys.stderr, flush=True)
    except Exception as e:
        print(f"  could not read Settings tab (using defaults): {e}", file=sys.stderr, flush=True)

    return jobs, run_hours


def is_active_now(job, run_hours):
    now_in_tz = datetime.now(job["tz"])
    return job["start"] <= now_in_tz <= job["stop"] and now_in_tz.hour in run_hours


# ---------- dispatch ----------

def cmd_dispatch():
    output_path = os.environ.get("GITHUB_OUTPUT")
    active = []

    def write_outputs():
        if active:
            matrix_value = active
            has_jobs = "true"
        else:
            matrix_value = [{"sheet_id": "__noop__", "subfolder_id": "__noop__", "filtered_subfolder_id": "__noop__", "sheet_name": "__noop__"}]
            has_jobs = "false"
        payload = json.dumps(matrix_value)
        if output_path:
            with open(output_path, "a", encoding="utf-8") as f:
                f.write(f"matrix={payload}\n")
                f.write(f"has_jobs={has_jobs}\n")
        else:
            print(payload)

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

        jobs, run_hours = read_control_sheet(sheets_service, control_id)
        print(f"Run hours: {run_hours}", flush=True)
        print(f"Jobs in control sheet: {len(jobs)}", flush=True)

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
            try:
                title = get_sheet_title(sheets_service, job["sheet_id"])
            except Exception as e:
                print(f"  row {job['row']}: cannot read sheet metadata ({e})", file=sys.stderr, flush=True)
                continue
            try:
                subfolder_id = find_or_create_subfolder(drive_service, drive_folder_id, title)
                filtered_subfolder_id = find_or_create_subfolder(
                    drive_service, drive_folder_id, f"{title} filtered"
                )
            except Exception as e:
                print(f"  row {job['row']}: cannot create subfolder for '{title}' ({e})", file=sys.stderr, flush=True)
                continue
            active.append(
                {
                    "sheet_id": job["sheet_id"],
                    "subfolder_id": subfolder_id,
                    "filtered_subfolder_id": filtered_subfolder_id,
                    "sheet_name": title,
                }
            )
            print(
                f"  ACTIVE: '{title}' -> {subfolder_id} | filtered -> {filtered_subfolder_id}",
                flush=True,
            )

        print(f"Active jobs to dispatch: {len(active)}", flush=True)
    finally:
        write_outputs()


# ---------- run ----------

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


async def process_row(page, row, drive_service, drive_folder_id, filtered_folder_id, sheet_label):
    url = row["url"]
    keyword = row["keyword"]
    print(f"[{sheet_label}] {url} :: '{keyword}'", flush=True)

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        await page.wait_for_timeout(POST_LOAD_WAIT_MS)
    except Exception as e:
        print(f"  load failed: {e}", flush=True)
        return False

    try:
        body_text = await page.evaluate(
            "() => document.body ? document.body.innerText : ''"
        )
    except Exception as e:
        print(f"  text read failed: {e}", flush=True)
        return False

    if keyword.lower() not in body_text.lower():
        print("  keyword not found", flush=True)
        return False

    rank = await extract_rank(page, keyword)
    print(f"  rank: {rank if rank is not None else 'not found'}", flush=True)

    try:
        await page.evaluate(
            "(z) => { document.documentElement.style.zoom = z; }", ZOOM_OUT_FACTOR
        )
        await page.wait_for_timeout(300)
    except Exception as e:
        print(f"  zoom failed: {e}", flush=True)

    try:
        await page.get_by_text(keyword, exact=False).first.scroll_into_view_if_needed(
            timeout=4000
        )
        await page.wait_for_timeout(400)
    except Exception:
        pass

    raw_png = await page.screenshot(full_page=False)
    captured_at = datetime.now(timezone.utc)
    try:
        png = add_meta_banner(add_url_banner(raw_png, page.url), row["category"], captured_at)
    except Exception as e:
        print(f"  banner failed ({e}); uploading without it", flush=True)
        png = raw_png
    ts = captured_at.strftime("%Y%m%d-%H%M%S")
    name = (
        f"{ts}_{slugify(row['category'])}_"
        f"{slugify(row['country'])}_{slugify(keyword)}.png"
    )
    upload_to_drive(drive_service, drive_folder_id, name, png)
    print(f"  uploaded {name}", flush=True)

    if filtered_folder_id:
        if rank is None:
            print("  rank not found; skipping filtered folder update", flush=True)
        else:
            try:
                url_key = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
                readable = (
                    f"{slugify(row['category'])}_{slugify(row['country'])}_{slugify(keyword)}"
                )
                existing = find_filtered_entries(drive_service, filtered_folder_id, url_key)
                best_existing_rank = min((r for _, r in existing), default=None)
                if best_existing_rank is None or rank < best_existing_rank:
                    for file_id, _ in existing:
                        delete_from_drive(drive_service, file_id)
                    filtered_name = f"rank{rank:04d}_{url_key}_{readable}_{ts}.png"
                    upload_to_drive(drive_service, filtered_folder_id, filtered_name, png)
                    print(
                        f"  uploaded {filtered_name} to filtered "
                        f"(rank #{rank}, previous best {best_existing_rank})",
                        flush=True,
                    )
                else:
                    print(
                        f"  rank #{rank} not better than existing best #{best_existing_rank}; "
                        f"skipping filtered upload",
                        flush=True,
                    )
            except Exception as e:
                print(f"  filtered rank update failed: {e}", flush=True)

    return True


async def cmd_run(sheet_id, drive_folder_id, filtered_folder_id, sheet_name):
    creds = get_credentials()
    sheets_service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    drive_service = build("drive", "v3", credentials=creds, cache_discovery=False)

    rows = read_data_sheet(sheets_service, sheet_id)
    label = sheet_name or sheet_id[:8]
    print(f"=== Sheet '{label}' ({len(rows)} rows) ===", flush=True)

    found = 0
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport=VIEWPORT)
        page = await context.new_page()
        for row in rows:
            if await process_row(page, row, drive_service, drive_folder_id, filtered_folder_id, label):
                found += 1
        await browser.close()
    print(f"Done: {found}/{len(rows)} screenshots uploaded.", flush=True)


# ---------- entry ----------

def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("dispatch", help="Read control sheet, output matrix")
    p_run = sub.add_parser("run", help="Process a single data sheet")
    p_run.add_argument("--sheet-id", required=True)
    p_run.add_argument("--drive-folder-id", required=True)
    p_run.add_argument("--filtered-folder-id", default="")
    p_run.add_argument("--sheet-name", default="")

    args = parser.parse_args()
    if args.cmd == "dispatch":
        cmd_dispatch()
    elif args.cmd == "run":
        asyncio.run(
            cmd_run(
                args.sheet_id,
                args.drive_folder_id,
                args.filtered_folder_id,
                args.sheet_name,
            )
        )


if __name__ == "__main__":
    main()
