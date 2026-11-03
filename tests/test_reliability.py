import asyncio
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import screenshot_runner as sr
from fakes import FakeDrive, FakeSheets

UTC = timezone.utc


# ---------- upload_to_drive ----------

def test_upload_retries_after_failure():
    drive = FakeDrive()
    drive.files().create_failures = [TimeoutError("read operation timed out")]
    fid = sr.upload_to_drive(drive, "folder", "a.png", b"x", sleep=lambda s: None)
    assert drive.files().files[fid]["name"] == "a.png"
    assert len(drive.files().files) == 1


def test_upload_does_not_duplicate_when_failed_create_actually_landed():
    drive = FakeDrive()
    f = drive.files()
    f.create_failures = [TimeoutError("read operation timed out")]
    f.create_lands_then_fails = True
    sr.upload_to_drive(drive, "folder", "a.png", b"x", sleep=lambda s: None)
    assert [v["name"] for v in f.files.values()] == ["a.png"]


def test_upload_gives_up_after_attempts():
    drive = FakeDrive()
    drive.files().create_failures = [TimeoutError("t")] * sr.UPLOAD_ATTEMPTS
    with pytest.raises(TimeoutError):
        sr.upload_to_drive(drive, "folder", "a.png", b"x", sleep=lambda s: None)
    assert drive.files().files == {}


def test_find_or_create_subfolder_reuses_existing_and_quotes_names():
    drive = FakeDrive()
    fid = drive.files().add("Bob's Book", "root", "application/vnd.google-apps.folder")
    assert sr.find_or_create_subfolder(drive, "root", "Bob's Book") == fid
    new = sr.find_or_create_subfolder(drive, "root", "Other")
    assert new != fid and drive.files().files[new]["name"] == "Other"


# ---------- control sheet ----------

def test_parse_run_hours_maps_24_to_midnight_and_drops_invalid():
    assert sr.parse_run_hours("2, 4,24,25,-1,4") == [2, 4, 0]


def test_read_control_sheet_parses_jobs_and_settings():
    sheets = FakeSheets({
        ("ctl", "Jobs!A:D"): [
            ["sheet", "start", "stop", "tz"],
            ["https://docs.google.com/spreadsheets/d/ABC_123/edit", "2026-11-01 10:00", "2026-11-03 20:00", "Asia/Kolkata"],
            ["", "", "", ""],
            ["XYZ", "bad date", "2026-11-03 20:00", "UTC"],
        ],
        ("ctl", "Settings!A:B"): [["key", "value"], ["run_hours", "1,3,24"], ["chunk_rows", "50"]],
    })
    jobs, settings = sr.read_control_sheet(sheets, "ctl")
    assert [j["sheet_id"] for j in jobs] == ["ABC_123"]
    assert jobs[0]["tz"] == ZoneInfo("Asia/Kolkata")
    assert settings == {"run_hours": [1, 3, 0], "chunk_rows": 50}


def test_read_control_sheet_keeps_defaults_when_run_hours_all_invalid():
    sheets = FakeSheets({
        ("ctl", "Jobs!A:D"): [["h"]],
        ("ctl", "Settings!A:B"): [["k", "v"], ["run_hours", "25,99"]],
    })
    _, settings = sr.read_control_sheet(sheets, "ctl")
    assert settings["run_hours"] == sr.DEFAULT_RUN_HOURS


def test_read_control_sheet_defaults_when_settings_missing():
    sheets = FakeSheets({("ctl", "Jobs!A:D"): [["h"]]})
    _, settings = sr.read_control_sheet(sheets, "ctl")
    assert settings == {"run_hours": sr.DEFAULT_RUN_HOURS, "chunk_rows": sr.DEFAULT_CHUNK_ROWS}


def test_read_data_sheet_skips_header_and_incomplete_rows():
    sheets = FakeSheets({("d", "A:D"): [
        ["Category", "URL", "Country", "Keyword"],
        ["Fiction", "https://a", "US", "Book"],
        ["Fiction", "", "US", "Book"],
        [" Business ", " https://b ", "UK", " Book "],
    ]})
    rows = sr.read_data_sheet(sheets, "d")
    assert [r["url"] for r in rows] == ["https://a", "https://b"]
    assert rows[1]["category"] == "Business" and rows[1]["keyword"] == "Book"


# ---------- dispatch helpers ----------

def test_chunk_ranges_cover_all_rows():
    assert sr.chunk_ranges(840, 120) == [(i, i + 120) for i in range(0, 840, 120)]
    assert sr.chunk_ranges(250, 120) == [(0, 120), (120, 240), (240, 250)]
    assert sr.chunk_ranges(0, 120) == []


def test_matrix_entry_round_trip_and_no_braces():
    e = sr.encode_matrix_entry("sheet-1_A", "sub", "filt", 120, 240, "mk", "20261102-21")
    assert "{" not in e and "}" not in e
    assert sr.decode_matrix_entry(e) == ("sheet-1_A", "sub", "filt", 120, 240, "mk", "20261102-21")


def test_hour_key_uses_local_hour_for_half_hour_timezone():
    t = datetime(2026, 11, 2, 15, 29, tzinfo=UTC).astimezone(ZoneInfo("Asia/Kolkata"))  # 20:59 IST
    assert sr.hour_key(t) == "20261102T1430Z"
    t = datetime(2026, 11, 2, 15, 30, tzinfo=UTC).astimezone(ZoneInfo("Asia/Kolkata"))  # 21:00 IST
    assert sr.hour_key(t) == "20261102T1530Z"


def test_hour_key_start_round_trip():
    assert sr.hour_key_start("20261102T1530Z") == datetime(2026, 11, 2, 15, 30, tzinfo=UTC)


def test_taken_since_lists_only_this_hours_screenshots():
    drive = FakeDrive()
    f = drive.files()
    since = datetime(2026, 11, 2, 15, 30, tzinfo=UTC)
    f.add("20261102-153500_Fiction_US_Book_abc123.png", "main", created=since + timedelta(minutes=5))
    f.add("20261102-143500_Old_US_Book.png", "main", created=since - timedelta(minutes=55))
    f.add("20261102-153600_Other_UK_Book.png", "elsewhere", created=since + timedelta(minutes=6))
    assert sr.taken_since(drive, "main", since) == {"Fiction_US_Book_abc123.png"}


def test_chunk_markers_round_trip():
    drive = FakeDrive()
    assert not sr.chunk_done(drive, "mk", "S", "20261102-21", 0, 120)
    sr.mark_chunk_done(drive, "mk", "S", "20261102-21", 0, 120, "summary")
    assert sr.chunk_done(drive, "mk", "S", "20261102-21", 0, 120)
    assert not sr.chunk_done(drive, "mk", "S", "20261102-22", 0, 120)
    assert not sr.chunk_done(drive, "mk", "S", "20261102-21", 120, 240)


# ---------- cmd_dispatch end to end (fakes) ----------

def _setup_dispatch(monkeypatch, tmp_path, n_rows):
    tz = ZoneInfo("Asia/Kolkata")
    now = datetime.now(tz)
    start = (now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
    stop = (now + timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
    data = [["c", "u", "co", "k"]] + [["Fiction", f"https://a/{i}", "US", "Book"] for i in range(n_rows)]
    sheets = FakeSheets(
        {
            ("ctl", "Jobs!A:D"): [["h"], ["DATA1", start, stop, "Asia/Kolkata"], ["DATA1", start, stop, "Asia/Kolkata"]],
            ("ctl", "Settings!A:B"): [["k", "v"], ["run_hours", ",".join(str(h) for h in range(24))], ["chunk_rows", "100"]],
            ("DATA1", "A:D"): data,
        },
        titles={"DATA1": "Jane Doe - {Book} \"$(id)\""},
    )
    drive = FakeDrive()
    monkeypatch.setattr(sr, "get_credentials", lambda: None)
    monkeypatch.setattr(sr, "build", lambda api, v, **k: sheets if api == "sheets" else drive)
    monkeypatch.setenv("CONTROL_SHEET_ID", "ctl")
    monkeypatch.setenv("DRIVE_FOLDER_ID", "root")
    monkeypatch.delenv("FORCE_RERUN", raising=False)
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    out = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    return out, drive


def _outputs(path):
    lines = dict(l.split("=", 1) for l in path.read_text().splitlines())
    return json.loads(lines["matrix"]), lines["has_jobs"]


def test_dispatch_splits_big_sheet_into_chunks(monkeypatch, tmp_path):
    out, drive = _setup_dispatch(monkeypatch, tmp_path, 250)
    sr.cmd_dispatch()
    matrix, has_jobs = _outputs(out)
    assert has_jobs == "true"
    assert [sr.decode_matrix_entry(e)[3:5] for e in matrix] == [(0, 100), (100, 200), (200, 250)]
    assert all("{" not in e and "$" not in e for e in matrix)
    folders = sorted(f["name"] for f in drive.files().files.values())
    assert folders == ['Jane Doe - {Book} "$(id)"', 'Jane Doe - {Book} "$(id)" filtered', sr.MARKERS_FOLDER]


def test_dispatch_skips_chunks_already_done_this_hour(monkeypatch, tmp_path):
    out, drive = _setup_dispatch(monkeypatch, tmp_path, 250)
    sr.cmd_dispatch()
    first, _ = _outputs(out)
    _, _, _, start, end, markers_id, hkey = sr.decode_matrix_entry(first[1])
    sr.mark_chunk_done(drive, markers_id, "DATA1", hkey, start, end, "done")
    out.write_text("")
    sr.cmd_dispatch()
    second, has_jobs = _outputs(out)
    assert has_jobs == "true"
    assert [sr.decode_matrix_entry(e)[3:5] for e in second] == [(0, 100), (200, 250)]


def test_dispatch_force_ignores_markers(monkeypatch, tmp_path):
    out, drive = _setup_dispatch(monkeypatch, tmp_path, 50)
    sr.cmd_dispatch()
    first, _ = _outputs(out)
    _, _, _, start, end, markers_id, hkey = sr.decode_matrix_entry(first[0])
    sr.mark_chunk_done(drive, markers_id, "DATA1", hkey, start, end, "done")
    out.write_text("")
    sr.cmd_dispatch()
    assert _outputs(out) == (["__noop__"], "false")
    out.write_text("")
    monkeypatch.setenv("FORCE_RERUN", "true")
    sr.cmd_dispatch()
    assert _outputs(out)[1] == "true"


def test_dispatch_writes_outputs_even_when_it_fails(monkeypatch, tmp_path):
    out, _ = _setup_dispatch(monkeypatch, tmp_path, 10)
    monkeypatch.setenv("CONTROL_SHEET_ID", "")
    with pytest.raises(RuntimeError):
        sr.cmd_dispatch()
    assert _outputs(out)[1] == "false"


# ---------- run helpers ----------

def test_is_blocked_page():
    assert sr.is_blocked_page("Sorry, we just need to make sure you're not a robot.")
    assert sr.is_blocked_page("Request was throttled. Please wait a moment")
    assert not sr.is_blocked_page("Best Sellers in Kindle Store #1 Some Book")


def test_is_allowed_url_blocks_local_addresses():
    assert not sr.is_allowed_url("http://localhost:8080/x")
    assert not sr.is_allowed_url("http://127.0.0.1/x")
    assert not sr.is_allowed_url("http://169.254.169.254/latest/meta-data")
    assert not sr.is_allowed_url("http://10.1.2.3/")
    assert not sr.is_allowed_url("http://[::1]/")
    assert not sr.is_allowed_url("http://2130706433/")
    assert not sr.is_allowed_url("http://0x7f.1/")
    assert not sr.is_allowed_url("http://127.1/")
    assert sr.is_allowed_url("https://www.amazon.de/gp/bestsellers")
    assert sr.is_allowed_url("https://deadbeef.example.com/")


def test_is_allowed_url():
    assert sr.is_allowed_url("https://www.amazon.com/gp/bestsellers")
    assert sr.is_allowed_url("http://amazon.co.uk/x")
    assert not sr.is_allowed_url("file:///home/runner/credentials.json")
    assert not sr.is_allowed_url("javascript:alert(1)")
    assert not sr.is_allowed_url("")


def test_should_fail_when_nothing_worked_or_many_errors():
    assert sr.should_fail(0, ["row 1: x"])
    assert not sr.should_fail(5, ["row 1: x"])
    assert not sr.should_fail(0, [])
    assert not sr.should_fail(100, ["e"] * 12, 120)
    assert sr.should_fail(100, ["e"] * 13, 120)
    assert sr.should_fail(1, ["e"] * 3, 4)


def test_notify_slack_without_webhook_is_noop(monkeypatch):
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    assert sr.notify_slack("hi") is False


def test_notify_slack_never_raises(monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "http://127.0.0.1:9/nothing")
    assert sr.notify_slack("hi") is False


# ---------- process_row with a fake page ----------

class FakePage:
    def __init__(self, texts, rank_text="#4"):
        self._texts = list(texts)
        self.url = "https://www.amazon.com/x"
        self.gotos = 0
        self._rank_text = rank_text

    async def goto(self, url, **kw):
        self.gotos += 1

    async def wait_for_timeout(self, ms):
        pass

    async def evaluate(self, js, *args):
        if "innerText" in js:
            return self._texts.pop(0) if len(self._texts) > 1 else self._texts[0]
        return None

    def get_by_text(self, keyword, exact=False):
        page = self

        class L:
            first = None

            async def evaluate(self, js):
                return page._rank_text

            async def scroll_into_view_if_needed(self, timeout=None):
                pass

        loc = L()
        loc.first = loc
        return loc

    async def screenshot(self, full_page=False):
        from PIL import Image
        import io

        b = io.BytesIO()
        Image.new("RGB", (40, 30)).save(b, format="PNG")
        return b.getvalue()


ROW = {"category": "Fiction", "url": "https://www.amazon.com/x", "country": "US", "keyword": "Some Book"}


def test_process_row_uploads_and_updates_filtered():
    drive = FakeDrive()
    page = FakePage(["#4 Some Book by Jane"])
    assert asyncio.run(sr.process_row(page, ROW, drive, "main", "filt", "L")) is True
    names = sorted(f["name"] for f in drive.files().files.values())
    assert len(names) == 2 and names[1].startswith("rank0004_")


def test_process_row_keyword_missing_returns_false():
    page = FakePage(["#1 Other Book #2 Another Book"])
    assert asyncio.run(sr.process_row(page, ROW, FakeDrive(), "main", "filt", "L")) is False


def test_process_row_retries_once_when_blocked_then_succeeds():
    page = FakePage(["Request was throttled", "#4 Some Book"])
    assert asyncio.run(sr.process_row(page, ROW, FakeDrive(), "main", "", "L")) is True
    assert page.gotos == 2


def test_process_row_raises_when_still_blocked():
    page = FakePage(["Request was throttled"])
    with pytest.raises(sr.RowError):
        asyncio.run(sr.process_row(page, ROW, FakeDrive(), "main", "", "L"))


def test_process_row_rejects_file_url():
    row = dict(ROW, url="file:///etc/passwd")
    with pytest.raises(sr.RowError):
        asyncio.run(sr.process_row(FakePage(["x"]), row, FakeDrive(), "main", "", "L"))


def test_process_row_upload_failure_is_row_error():
    drive = FakeDrive()
    drive.files().create_failures = [TimeoutError("t")] * 3
    page = FakePage(["#4 Some Book"])
    orig = sr.time.sleep
    sr.time.sleep = lambda s: None
    try:
        with pytest.raises(sr.RowError):
            asyncio.run(sr.process_row(page, ROW, drive, "main", "", "L"))
    finally:
        sr.time.sleep = orig


# ---------- cmd_run end to end (fake browser, fake Google) ----------

class _FakeContext:
    def __init__(self, texts):
        self._texts = texts
        self.pages = 0

    async def new_page(self):
        self.pages += 1
        return _RunPage(self._texts)


class _RunPage(FakePage):
    def __init__(self, texts):
        super().__init__(["x"])
        self._map = texts

    async def goto(self, url, **kw):
        self.gotos += 1
        self._url = url
        if self._map.get(url) == "RAISE":
            raise TimeoutError("nav timeout")

    async def evaluate(self, js, *args):
        if "innerText" in js:
            return self._map.get(self._url, "")
        return None

    async def close(self):
        pass


class _FakeBrowser:
    def __init__(self, ctx):
        self._ctx = ctx

    async def new_context(self, viewport=None):
        return self._ctx

    async def close(self):
        pass


def _fake_playwright(ctx):
    class Chromium:
        async def launch(self, headless=True):
            return _FakeBrowser(ctx)

    class P:
        chromium = Chromium()

    class CM:
        async def __aenter__(self):
            return P()

        async def __aexit__(self, *a):
            return False

    return lambda: CM()


def _setup_run(monkeypatch, rows, texts):
    data = [["c", "u", "co", "k"]] + rows
    sheets = FakeSheets({("S", "A:D"): data}, titles={"S": "Book"})
    drive = FakeDrive()
    ctx = _FakeContext(texts)
    monkeypatch.setattr(sr, "get_credentials", lambda: None)
    monkeypatch.setattr(sr, "build", lambda api, v, **k: sheets if api == "sheets" else drive)
    monkeypatch.setattr(sr, "async_playwright", _fake_playwright(ctx))
    monkeypatch.setattr(sr.time, "sleep", lambda s: None)
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("FORCE_RERUN", raising=False)
    hkey = sr.hour_key(datetime.now(UTC))
    entry = sr.encode_matrix_entry("S", "main", "filt", 0, len(rows), "mk", hkey)
    return drive, entry, hkey


def _pngs(drive, folder):
    return [f["name"] for f in drive.files().files.values() if f["parents"] == [folder]]


def test_run_marks_chunk_done_when_all_rows_worked(monkeypatch):
    rows = [["Fiction", "https://a/1", "US", "Some Book"], ["Drama", "https://a/2", "UK", "Some Book"]]
    drive, entry, hkey = _setup_run(monkeypatch, rows, {"https://a/1": "#1 Some Book", "https://a/2": "#3 Some Book"})
    asyncio.run(sr.cmd_run(entry))
    assert len(_pngs(drive, "main")) == 2
    assert sr.chunk_done(drive, "mk", "S", hkey, 0, 2)


def test_run_skips_rows_already_taken_this_hour_and_duplicate_rows(monkeypatch):
    rows = [
        ["Fiction", "https://a/1", "US", "Some Book"],
        ["Drama", "https://a/2", "UK", "Some Book"],
        ["Drama", "https://a/2", "UK", "Some Book"],
    ]
    drive, entry, _ = _setup_run(monkeypatch, rows, {"https://a/1": "#1 Some Book", "https://a/2": "#3 Some Book"})
    drive.files().add("20261102-153500_" + sr.row_suffix({"category": "Fiction", "country": "US", "keyword": "Some Book", "url": "https://a/1"}), "main")
    asyncio.run(sr.cmd_run(entry))
    names = _pngs(drive, "main")
    assert len(names) == 2  # the pre-existing one + one Drama, no duplicates
    assert sum("Drama_UK_Some_Book_" in n for n in names) == 1


def test_row_suffix_separates_urls_but_matches_duplicate_rows():
    a = {"category": "Fiction", "country": "US", "keyword": "Book", "url": "https://a/1"}
    assert sr.row_suffix(a) == sr.row_suffix(dict(a))
    assert sr.row_suffix(a) != sr.row_suffix(dict(a, url="https://a/2"))


def test_dispatch_fails_when_due_sheet_unreadable(monkeypatch, tmp_path):
    out, _ = _setup_dispatch(monkeypatch, tmp_path, 0)
    with pytest.raises(RuntimeError):
        sr.cmd_dispatch()
    assert _outputs(out)[1] == "false"


def test_run_with_one_error_leaves_no_marker_and_does_not_fail(monkeypatch):
    rows = [["Fiction", f"https://a/{i}", "US", "Some Book"] for i in range(5)]
    texts = {f"https://a/{i}": "#1 Some Book" for i in range(5)}
    texts["https://a/4"] = "RAISE"
    drive, entry, hkey = _setup_run(monkeypatch, rows, texts)
    asyncio.run(sr.cmd_run(entry))  # must not raise SystemExit
    assert len(_pngs(drive, "main")) == 4
    assert not sr.chunk_done(drive, "mk", "S", hkey, 0, 5)


def test_run_with_many_errors_fails(monkeypatch):
    rows = [["Fiction", f"https://a/{i}", "US", "Some Book"] for i in range(4)]
    texts = {"https://a/0": "#1 Some Book", "https://a/1": "RAISE", "https://a/2": "RAISE", "https://a/3": "RAISE"}
    drive, entry, hkey = _setup_run(monkeypatch, rows, texts)
    with pytest.raises(SystemExit):
        asyncio.run(sr.cmd_run(entry))
    assert len(_pngs(drive, "main")) == 1
    assert not sr.chunk_done(drive, "mk", "S", hkey, 0, 4)


def test_run_fails_when_every_row_errors(monkeypatch):
    rows = [["Fiction", "https://a/1", "US", "Some Book"]]
    drive, entry, hkey = _setup_run(monkeypatch, rows, {"https://a/1": "RAISE"})
    with pytest.raises(SystemExit):
        asyncio.run(sr.cmd_run(entry))
    assert not sr.chunk_done(drive, "mk", "S", hkey, 0, 1)


def test_run_force_retakes_rows_taken_this_hour(monkeypatch):
    rows = [["Fiction", "https://a/1", "US", "Some Book"]]
    drive, entry, _ = _setup_run(monkeypatch, rows, {"https://a/1": "#1 Some Book"})
    drive.files().add("20261102-153500_" + sr.row_suffix({"category": "Fiction", "country": "US", "keyword": "Some Book", "url": "https://a/1"}), "main")
    monkeypatch.setenv("FORCE_RERUN", "true")
    asyncio.run(sr.cmd_run(entry))
    assert len(_pngs(drive, "main")) == 2


def test_logs_never_show_sheet_title_or_book_title(monkeypatch, capsys, tmp_path):
    rows = [["Fiction", "https://a/1", "US", "Secret Book Title"]]
    drive, entry, _ = _setup_run(monkeypatch, rows, {"https://a/1": "#1 Secret Book Title"})
    asyncio.run(sr.cmd_run(entry))
    out, _ = _setup_dispatch(monkeypatch, tmp_path, 5)
    sr.cmd_dispatch()
    logs = capsys.readouterr()
    text = logs.out + logs.err
    assert "Secret Book Title" not in text and "Secret_Book_Title" not in text
    assert "Jane Doe" not in text and "Book'" not in text


# ---------- reaudit round (3 Nov) ----------

def test_unique_rows_drops_repeats_keeps_order():
    a = {"category": "F", "country": "US", "keyword": "B", "url": "https://a/1"}
    b = dict(a, url="https://a/2")
    assert sr.unique_rows([a, b, dict(a), b]) == [a, b]


def test_dispatch_counts_unique_rows_only(monkeypatch, tmp_path):
    out, _ = _setup_dispatch(monkeypatch, tmp_path, 3)
    monkeypatch.setattr(sr, "read_data_sheet", lambda s, i: [
        {"category": "F", "country": "US", "keyword": "B", "url": f"https://a/{n % 2}"} for n in range(250)
    ])
    sr.cmd_dispatch()
    matrix, _ = _outputs(out)
    assert [sr.decode_matrix_entry(e)[3:5] for e in matrix] == [(0, 2)]


def test_public_text_hides_urls():
    assert sr.public_text("Page.goto: timeout https://www.amazon.com/My-Book-Title/dp/1 x") == "Page.goto: timeout <url> x"


def test_is_allowed_url_blocks_trailing_dot_and_backslash_tricks():
    assert not sr.is_allowed_url("http://localhost./")
    assert not sr.is_allowed_url("http://127.0.0.1./")
    assert not sr.is_allowed_url("http://\\127.0.0.1/")
    assert not sr.is_allowed_url("http://metadata.google.internal./")


def test_upload_final_check_finds_file_that_landed_on_last_attempt():
    drive = FakeDrive()
    f = drive.files()
    f.create_failures = [TimeoutError("t")] * sr.UPLOAD_ATTEMPTS
    f.create_lands_then_fails = True
    fid = sr.upload_to_drive(drive, "folder", "a.png", b"x", sleep=lambda s: None)
    assert fid in f.files and len(f.files) == 1


def test_dispatch_problems_output_set_when_one_sheet_fails(monkeypatch, tmp_path):
    out, _ = _setup_dispatch(monkeypatch, tmp_path, 5)
    real = sr.read_control_sheet

    def two_jobs(sheets, cid):
        jobs, settings = real(sheets, cid)
        broken = dict(jobs[0], row=9, sheet_id="MISSING1")
        return [jobs[0], broken], settings

    monkeypatch.setattr(sr, "read_control_sheet", two_jobs)
    sr.cmd_dispatch()
    lines = dict(l.split("=", 1) for l in out.read_text().splitlines())
    assert lines["has_jobs"] == "true"
    assert lines["problems"] == "true"


def test_run_stops_at_time_budget_and_leaves_no_marker(monkeypatch):
    rows = [["Fiction", f"https://a/{i}", "US", "Some Book"] for i in range(3)]
    drive, entry, hkey = _setup_run(monkeypatch, rows, {f"https://a/{i}": "#1 Some Book" for i in range(3)})
    monkeypatch.setattr(sr, "RUN_BUDGET_S", -1)
    asyncio.run(sr.cmd_run(entry))
    assert _pngs(drive, "main") == []
    assert not sr.chunk_done(drive, "mk", "S", hkey, 0, 3)


def test_run_with_no_book_found_still_marks_done(monkeypatch):
    rows = [["Fiction", "https://a/1", "US", "Some Book"]]
    drive, entry, hkey = _setup_run(monkeypatch, rows, {"https://a/1": "#1 Other Book #2 Another Book"})
    asyncio.run(sr.cmd_run(entry))
    assert sr.chunk_done(drive, "mk", "S", hkey, 0, 1)


# ---------- reaudit round 2 ----------

def test_public_text_hides_schemeless_links_and_drive_queries():
    assert "Book" not in sr.public_text("goto www.amazon.com/Book-Title/dp/1 failed")
    t = sr.public_text('<HttpError 500 when requesting https://www.googleapis.com/drive/v3/files?q=name%3D%27Book_Title%27 returned "x">')
    assert "Book_Title" not in t
    assert "Book_Title" not in sr.public_text("bad request q=name='Book_Title' and x")


def test_update_filtered_best_removes_extra_entries_from_parallel_chunks():
    import hashlib
    drive = FakeDrive()
    f = drive.files()
    url = "https://www.amazon.com/x"
    key = hashlib.sha1(url.encode()).hexdigest()[:10]
    a = f.add(f"rank0003_{key}_a.png", "filt")
    f.add(f"rank0005_{key}_b.png", "filt")
    f.add(f"rank0003_{key}_c.png", "filt")
    row = {"category": "F", "country": "US", "keyword": "B"}
    sr.update_filtered_best(drive, "filt", url, row, 7, "ts", b"png")
    left = [(i, v["name"]) for i, v in f.files.items()]
    assert len(left) == 1 and left[0][1].startswith("rank0003_")


def test_process_row_non_list_page_is_an_error():
    page = FakePage(["Welcome to the new Amazon homepage"])
    with pytest.raises(sr.RowError):
        asyncio.run(sr.process_row(page, ROW, FakeDrive(), "main", "", "L"))


def test_chunk_rows_is_capped(monkeypatch):
    sheets = FakeSheets({
        ("ctl", "Jobs!A:D"): [["h"]],
        ("ctl", "Settings!A:B"): [["k", "v"], ["chunk_rows", "1000"]],
    })
    _, settings = sr.read_control_sheet(sheets, "ctl")
    assert settings["chunk_rows"] == sr.MAX_CHUNK_ROWS
