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
    assert (settings["run_hours"], settings["chunk_rows"]) == ([1, 3, 0], 50)


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
    assert (settings["run_hours"], settings["chunk_rows"]) == (sr.DEFAULT_RUN_HOURS, sr.DEFAULT_CHUNK_ROWS)


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

    async def close(self):
        self.closed = True


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

    async def new_context(self, **kw):
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


# ---------- reaudit round 3 ----------

def test_public_text_hides_quoted_url_with_spaces_and_quoted_names():
    t = sr.public_text('Page.goto: Timeout navigating to "https://www.amazon.com/gp/x/Secret Book Title/ref=x"')
    assert "Secret" not in t and "Book Title" not in t
    t = sr.public_text("Invalid query: name contains 'Secret Book' and parents")
    assert "Secret Book" not in t


def test_soft_block_page_is_detected():
    assert sr.is_blocked_page("Click the button below to continue shopping")


def test_bad_url_error_does_not_echo_cell():
    row = dict(ROW, url="My Secret Book Title - Kindle")
    with pytest.raises(sr.RowError) as e:
        asyncio.run(sr.process_row(FakePage(["x"]), row, FakeDrive(), "main", "", "L"))
    assert "Secret" not in str(e.value)


def test_main_prints_filtered_error_not_traceback(monkeypatch, capsys):
    def boom():
        raise RuntimeError("failed https://www.googleapis.com/drive/v3/files?q=name='Secret Book'")
    monkeypatch.setattr(sr, "cmd_dispatch", boom)
    monkeypatch.setattr(sr.sys, "argv", ["screenshot_runner.py", "dispatch"])
    with pytest.raises(SystemExit):
        sr.main()
    out = capsys.readouterr()
    assert "Secret" not in out.out + out.err and "Traceback" not in out.out + out.err


# ---------- reliability alerts ----------

def test_control_sheet_bad_job_row_is_reported_as_problem():
    sheets = FakeSheets({
        ("ctl", "Jobs!A:D"): [["h"], ["XYZ", "zzcell", "2026-11-03 20:00", "UTC"]],
    })
    jobs, settings = sr.read_control_sheet(sheets, "ctl")
    assert jobs == []
    assert any("Jobs row 2" in p for p in settings["problems"])
    assert not any("zzcell" in p or "XYZ" in p for p in settings["problems"])


def test_control_sheet_bad_settings_are_reported_as_problems():
    sheets = FakeSheets({
        ("ctl", "Jobs!A:D"): [["h"]],
        ("ctl", "Settings!A:B"): [["k", "v"], ["run_hours", "25,99"], ["chunk_rows", "abc"]],
    })
    _, settings = sr.read_control_sheet(sheets, "ctl")
    assert settings["run_hours"] == sr.DEFAULT_RUN_HOURS
    assert settings["chunk_rows"] == sr.DEFAULT_CHUNK_ROWS
    assert len(settings["problems"]) == 2


def test_control_sheet_unreadable_settings_tab_is_a_problem():
    sheets = FakeSheets({("ctl", "Jobs!A:D"): [["h"]]})
    _, settings = sr.read_control_sheet(sheets, "ctl")
    assert settings["run_hours"] == sr.DEFAULT_RUN_HOURS
    assert any("Settings" in p for p in settings["problems"])


def test_control_sheet_clean_has_no_problems():
    sheets = FakeSheets({
        ("ctl", "Jobs!A:D"): [["h"]],
        ("ctl", "Settings!A:B"): [["k", "v"], ["run_hours", "1,3"]],
    })
    _, settings = sr.read_control_sheet(sheets, "ctl")
    assert settings["problems"] == []


def test_dispatch_sets_problems_for_bad_job_row(monkeypatch, tmp_path):
    out, _ = _setup_dispatch(monkeypatch, tmp_path, 5)
    sheets = sr.build("sheets", "v4")
    sheets.spreadsheets()._values._ranges[("ctl", "Jobs!A:D")].append(["X", "nope", "nope", "UTC"])
    sr.cmd_dispatch()
    lines = dict(l.split("=", 1) for l in out.read_text().splitlines())
    assert lines["problems"] == "true"


def test_keyword_match_ignores_curly_vs_straight_quotes():
    assert sr.keyword_in_text("Bob's Book", "#1 Bob\u2019s Book by Jane")
    assert sr.keyword_in_text("Bob\u2019s \u201cBook\u201d", "#1 bob's \"book\"")
    assert not sr.keyword_in_text("Bob's Book", "#1 Other #2 More")


def test_process_row_finds_keyword_with_curly_apostrophe():
    row = dict(ROW, keyword="Bob's Book")
    page = FakePage(["#4 Bob\u2019s Book by Jane"])
    assert asyncio.run(sr.process_row(page, row, FakeDrive(), "main", "", "L")) is True


VALID_PAGE = "#1 Some Book #2 Another Book"


def test_process_row_hung_evaluate_is_row_error(monkeypatch):
    # After the hang the page would look fine, so only the timeout can fail the row.
    class Hung(FakePage):
        async def evaluate(self, js, *args):
            await asyncio.sleep(0.5)
            return VALID_PAGE

    monkeypatch.setattr(sr, "EVAL_TIMEOUT_S", 0.05)
    with pytest.raises(sr.RowError, match="load failed"):
        asyncio.run(sr.process_row(Hung(["x"]), ROW, FakeDrive(), "main", "", "L"))


def test_run_row_with_hung_row_times_out_as_row_error(monkeypatch):
    class Slow(FakePage):
        async def goto(self, url, **kw):
            await asyncio.sleep(0.5)

    monkeypatch.setattr(sr, "ROW_TIMEOUT_S", 0.05)
    with pytest.raises(sr.RowError, match=r"row timed out after 0\.05 s"):
        asyncio.run(sr.process_row_limited(Slow([VALID_PAGE]), ROW, FakeDrive(), "main", "", "L"))


def test_process_row_zoom_hang_times_out_and_row_still_succeeds(monkeypatch, capsys):
    class ZoomHang(FakePage):
        async def evaluate(self, js, *args):
            if "zoom" in js:
                await asyncio.sleep(30)
            return await super().evaluate(js, *args)

    async def go():
        return await asyncio.wait_for(
            sr.process_row(ZoomHang([VALID_PAGE]), ROW, FakeDrive(), "main", "", "L"), 5
        )

    monkeypatch.setattr(sr, "EVAL_TIMEOUT_S", 0.05)
    assert asyncio.run(go()) is True
    assert "zoom failed" in capsys.readouterr().out


def test_run_page_close_hang_does_not_stall_the_chunk(monkeypatch, capsys):
    class HangClose(_RunPage):
        async def close(self):
            await asyncio.sleep(30)

    class Ctx(_FakeContext):
        async def new_page(self):
            self.pages += 1
            return HangClose(self._texts)

    rows = [["Fiction", "https://a/1", "US", "Some Book"], ["Drama", "https://a/2", "UK", "Some Book"]]
    drive, entry, _ = _setup_run(monkeypatch, rows, {"https://a/1": "RAISE", "https://a/2": "#1 Some Book"})
    monkeypatch.setattr(sr, "async_playwright", _fake_playwright(Ctx({"https://a/1": "RAISE", "https://a/2": "#1 Some Book"})))
    monkeypatch.setattr(sr, "PAGE_CLOSE_TIMEOUT_S", 0.05)
    asyncio.run(asyncio.wait_for(sr.cmd_run(entry), 5))
    assert len(_pngs(drive, "main")) == 1


def test_run_summary_includes_not_found_rows(monkeypatch, capsys):
    monkeypatch.setattr(sr, "notify_slack", lambda t: None)
    rows = [["Fiction", "https://a/1", "US", "Some Book"], ["Drama", "https://a/2", "UK", "Some Book"]]
    drive, entry, _ = _setup_run(monkeypatch, rows, {"https://a/1": "#1 Some Book", "https://a/2": "#1 Other #2 More"})
    asyncio.run(sr.cmd_run(entry))
    assert "not found: row 2" in capsys.readouterr().out


def test_keyword_pattern_collapses_whitespace_like_playwright_text_match():
    pat = sr.keyword_pattern("Some Book  Title")
    assert pat.search("#1 Some\nBook Title")
    assert pat.search("#1 Some\u00a0Book\u00a0\u00a0Title")
    assert pat.search("#1 Some   Book \n Title")
    assert not pat.search("#1 SomeBook Title")
    assert not pat.search("#1 Other Book Title")


def test_keyword_pattern_keeps_quotes_case_and_escaping():
    pat = sr.keyword_pattern("Bob's \"Book\" (2)")
    assert pat.search("bob\u2019s \u201cbook\u201d (2)")
    assert pat.search("BOB'S \"BOOK\" (2)")
    assert not pat.search("Bobs Book 2")


def test_summary_lists_not_found_rows_without_alerting(monkeypatch):
    sent = []
    monkeypatch.setattr(sr, "notify_slack", lambda t: sent.append(t))
    rows = [["Fiction", "https://a/1", "US", "Some Book"], ["Drama", "https://a/2", "UK", "Some Book"]]
    drive, entry, hkey = _setup_run(monkeypatch, rows, {"https://a/1": "#1 Some Book", "https://a/2": "#1 Other #2 More"})
    asyncio.run(sr.cmd_run(entry))
    assert sent == []
    assert "not found: row 2" in sr.summarize("S", 0, 2, 1, 1, [], not_found_rows=[2])


def test_summary_without_not_found_rows_is_unchanged():
    assert "not found:" not in sr.summarize("S", 0, 2, 2, 0, [])


# ---------- retry runs: rows already done this hour count as successes ----------

def _row(i, cat="Fiction"):
    return {"category": cat, "country": "US", "keyword": "Some Book", "url": f"https://a/{i}"}


def _mark_done(drive, *idx):
    for i in idx:
        drive.files().add("20261102-153500_" + sr.row_suffix(_row(i)), "main")


def test_should_fail_counts_already_done_rows_as_successes():
    # retry: 7 rows already done, one blocked row
    assert not sr.should_fail(0 + 7, ["row 8: x"], 7 + 0 + 1)
    # first run, nothing worked: still fails
    assert sr.should_fail(0, ["row 1: x"], 1)
    # retry where most remaining rows still error: still fails
    assert sr.should_fail(1, ["e"] * 20, 21)


def test_retry_with_one_blocked_row_does_not_fail_or_alert(monkeypatch, capsys):
    sent = []
    monkeypatch.setattr(sr, "notify_slack", lambda t: sent.append(t))
    rows = [["Fiction", f"https://a/{i}", "US", "Some Book"] for i in range(8)]
    texts = {f"https://a/{i}": "#1 Some Book" for i in range(8)}
    texts["https://a/7"] = "RAISE"
    drive, entry, hkey = _setup_run(monkeypatch, rows, texts)
    _mark_done(drive, 0, 1, 2, 3, 4, 5, 6)
    asyncio.run(sr.cmd_run(entry))  # must not raise SystemExit
    assert sent == []
    assert "::warning::" in capsys.readouterr().out
    assert not sr.chunk_done(drive, "mk", "S", hkey, 0, 8)


def test_first_run_with_all_rows_erroring_fails_and_alerts(monkeypatch):
    sent = []
    monkeypatch.setattr(sr, "notify_slack", lambda t: sent.append(t))
    rows = [["Fiction", "https://a/1", "US", "Some Book"]]
    drive, entry, _ = _setup_run(monkeypatch, rows, {"https://a/1": "RAISE"})
    with pytest.raises(SystemExit):
        asyncio.run(sr.cmd_run(entry))
    assert len(sent) == 1 and "First error" in sent[0] and "recovered" not in sent[0]


def test_retry_that_recovers_some_rows_alerts_with_recovered_count(monkeypatch):
    sent = []
    monkeypatch.setattr(sr, "notify_slack", lambda t: sent.append(t))
    rows = [["Fiction", f"https://a/{i}", "US", "Some Book"] for i in range(5)]
    texts = {f"https://a/{i}": "#1 Some Book" for i in range(5)}
    texts["https://a/4"] = "RAISE"
    drive, entry, _ = _setup_run(monkeypatch, rows, texts)
    _mark_done(drive, 0, 1)
    asyncio.run(sr.cmd_run(entry))
    assert len(sent) == 1
    assert "recovered 2" in sent[0] and "1 still fail" in sent[0] and "First error" in sent[0]


def test_retry_not_found_alert_not_sent_when_rows_already_done(monkeypatch):
    sent = []
    monkeypatch.setattr(sr, "notify_slack", lambda t: sent.append(t))
    rows = [["Fiction", "https://a/1", "US", "Some Book"], ["Drama", "https://a/2", "UK", "Some Book"]]
    drive, entry, _ = _setup_run(monkeypatch, rows, {"https://a/1": "#1 Some Book", "https://a/2": "#1 Other #2 More"})
    _mark_done(drive, 1)
    asyncio.run(sr.cmd_run(entry))
    assert sent == []


def test_first_run_with_only_not_found_rows_still_alerts(monkeypatch):
    sent = []
    monkeypatch.setattr(sr, "notify_slack", lambda t: sent.append(t))
    rows = [["Drama", "https://a/2", "UK", "Some Book"]]
    drive, entry, _ = _setup_run(monkeypatch, rows, {"https://a/2": "#1 Other #2 More"})
    asyncio.run(sr.cmd_run(entry))
    assert len(sent) == 1 and "not found on any page" in sent[0]


# ---------- one browser context per locale ----------

@pytest.mark.parametrize("host,expected", [
    ("www.amazon.ca", "en-CA"),
    ("amazon.co.uk", "en-GB"),
    ("www.amazon.com.au", "en-AU"),
    ("www.amazon.in", "en-IN"),
    ("www.amazon.sg", "en-SG"),
    ("www.amazon.com", None),
    ("www.amazon.de", None),
    ("www.amazon.fr", None),
    ("www.amazon.co.jp", None),
    ("WWW.AMAZON.CA", "en-CA"),
    ("", None),
    (None, None),
    ("example.sin", None),
    ("example.xco.uk", None),
])
def test_locale_for_host(host, expected):
    assert sr.locale_for_host(host) == expected


class _LocaleBrowser:
    def __init__(self, texts):
        self.texts = texts
        self.made = []  # (kwargs, context)

    async def new_context(self, **kw):
        ctx = _FakeContext(self.texts)
        ctx.closed = False
        self.made.append((kw, ctx))
        return ctx

    async def close(self):
        pass


def _locale_run(monkeypatch, urls, texts):
    rows = [["Fiction", u, "US", "Some Book"] for u in urls]
    drive, entry, _ = _setup_run(monkeypatch, rows, texts)
    browser = _LocaleBrowser(texts)

    class Chromium:
        async def launch(self, headless=True):
            return browser

    class P:
        chromium = Chromium()

    class CM:
        async def __aenter__(self):
            return P()

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(sr, "async_playwright", lambda: CM())
    return drive, entry, browser


def test_run_uses_one_context_per_locale(monkeypatch):
    urls = ["https://www.amazon.ca/1", "https://www.amazon.com/2", "https://www.amazon.ca/3", "https://www.amazon.de/4"]
    drive, entry, browser = _locale_run(monkeypatch, urls, {u: "#1 Some Book" for u in urls})
    asyncio.run(sr.cmd_run(entry))
    kws = [kw for kw, _ in browser.made]
    assert len(kws) == 2  # ca once (rows 1 and 3 share it); com and de share the default
    ca = [kw for kw in kws if kw.get("locale") == "en-CA"]
    other = [kw for kw in kws if "locale" not in kw]
    assert len(ca) == 1 and len(other) == 1
    for kw in kws:
        assert kw["viewport"] == sr.VIEWPORT
    assert ca[0]["extra_http_headers"] == {"Accept-Language": "en-US,en;q=0.9"}
    # unmapped hosts: exactly what main creates (viewport only)
    assert other[0] == {"viewport": sr.VIEWPORT}
    assert [c.pages for _, c in browser.made] == [1, 1]
    assert len(_pngs(drive, "main")) == 4


def test_run_different_hosts_get_different_contexts(monkeypatch):
    urls = ["https://www.amazon.ca/1", "https://www.amazon.co.uk/2", "https://www.amazon.com/3"]
    drive, entry, browser = _locale_run(monkeypatch, urls, {u: "#1 Some Book" for u in urls})
    asyncio.run(sr.cmd_run(entry))
    assert sorted(kw.get("locale", "") for kw, _ in browser.made) == ["", "en-CA", "en-GB"]
    assert len({id(c) for _, c in browser.made}) == 3


def test_run_closes_every_context_even_when_rows_fail(monkeypatch):
    urls = ["https://www.amazon.ca/1", "https://www.amazon.co.uk/2", "https://www.amazon.co.uk/3", "https://www.amazon.in/4"]
    texts = {u: "#1 Some Book" for u in urls}
    texts[urls[1]] = "RAISE"
    texts[urls[2]] = "RAISE"
    texts[urls[3]] = "RAISE"
    drive, entry, browser = _locale_run(monkeypatch, urls, texts)
    with pytest.raises(SystemExit):
        asyncio.run(sr.cmd_run(entry))
    assert len(browser.made) == 3
    assert all(c.closed for _, c in browser.made)
    # a failed row gets a fresh page in the same context, not a new context
    assert [c.pages for _, c in browser.made] == [1, 3, 2]


def test_run_closes_contexts_when_the_run_itself_crashes(monkeypatch):
    urls = ["https://www.amazon.ca/1", "https://www.amazon.in/2"]
    texts = {urls[0]: "#1 Some Book", urls[1]: "RAISE"}
    drive, entry, browser = _locale_run(monkeypatch, urls, texts)
    orig = browser.new_context

    async def crashing_context(**kw):
        ctx = await orig(**kw)
        if kw.get("locale") == "en-IN":
            async def boom():
                if ctx.pages >= 1:
                    raise RuntimeError("browser died")
                ctx.pages += 1
                return _RunPage(texts)
            ctx.new_page = boom
        return ctx

    browser.new_context = crashing_context
    with pytest.raises(RuntimeError):
        asyncio.run(sr.cmd_run(entry))
    assert len(browser.made) == 2 and all(c.closed for _, c in browser.made)


def test_unmapped_hosts_get_main_context_kwargs(monkeypatch):
    urls = ["https://www.amazon.com/1", "https://www.amazon.de/2", "https://www.amazon.fr/3", "https://www.amazon.co.jp/4"]
    drive, entry, browser = _locale_run(monkeypatch, urls, {u: "#1 Some Book" for u in urls})
    asyncio.run(sr.cmd_run(entry))
    assert [kw for kw, _ in browser.made] == [{"viewport": sr.VIEWPORT}]


def test_mapped_hosts_get_locale_and_header(monkeypatch):
    hosts = {"ca": "en-CA", "co.uk": "en-GB", "com.au": "en-AU", "in": "en-IN", "sg": "en-SG"}
    urls = [f"https://www.amazon.{h}/1" for h in hosts]
    drive, entry, browser = _locale_run(monkeypatch, urls, {u: "#1 Some Book" for u in urls})
    asyncio.run(sr.cmd_run(entry))
    assert len(browser.made) == 5
    for kw, _ in browser.made:
        assert kw["viewport"] == sr.VIEWPORT
        assert kw["extra_http_headers"] == {"Accept-Language": "en-US,en;q=0.9"}
    assert sorted(kw["locale"] for kw, _ in browser.made) == sorted(hosts.values())


def test_context_closed_when_new_page_fails(monkeypatch):
    urls = ["https://www.amazon.ca/1", "https://www.amazon.in/2"]
    drive, entry, browser = _locale_run(monkeypatch, urls, {u: "#1 Some Book" for u in urls})
    orig = browser.new_context

    async def ctx_bad_page(**kw):
        ctx = await orig(**kw)
        if kw.get("locale") == "en-CA":
            async def boom():
                raise RuntimeError("no page")
            ctx.new_page = boom
        return ctx

    browser.new_context = ctx_bad_page
    try:
        asyncio.run(sr.cmd_run(entry))
    except SystemExit:
        pass  # a row failed; whether the run exits is not what this test checks
    bad = [c for kw, c in browser.made if kw.get("locale") == "en-CA"]
    assert len(bad) == 1 and bad[0].closed
    assert all(c.closed for _, c in browser.made)


# ---------- blocked rows retried in a fresh browser context ----------

BLOCKED = "Request was throttled"
CA_URL = "https://www.amazon.ca/bestsellers"


class _CtxWithState(_FakeContext):
    def __init__(self, texts, label, raise_on_new_page=False):
        super().__init__(texts)
        self.label = label
        self.closed = False
        self._raise = raise_on_new_page

    async def new_page(self):
        if self._raise:
            raise RuntimeError("boom")
        return await super().new_page()


class _SeqBrowser:
    """First context uses the first text map; each later new_context() gets the next one."""

    def __init__(self, maps):
        self.maps = list(maps)
        self.contexts = []
        self.kwargs = []

    async def new_context(self, **kw):
        idx = len(self.contexts)
        texts = self.maps[min(idx, len(self.maps) - 1)]
        raise_np = isinstance(texts, tuple)
        ctx = _CtxWithState(texts[0] if raise_np else texts, idx, raise_np)
        self.contexts.append(ctx)
        self.kwargs.append(kw)
        return ctx

    async def close(self):
        pass


def _setup_blocked(monkeypatch, url, maps):
    rows = [["Fiction", url, "CA", "Some Book"]]
    drive, entry, hkey = _setup_run(monkeypatch, rows, {})
    browser = _SeqBrowser(maps)

    class Chromium:
        async def launch(self, headless=True):
            return browser

    class P:
        chromium = Chromium()

    class CM:
        async def __aenter__(self):
            return P()

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(sr, "async_playwright", lambda: CM())
    monkeypatch.setattr(sr, "BLOCKED_RETRY_WAIT_S", 0)
    return drive, entry, hkey, browser


def test_blocked_row_recovered_in_fresh_context(monkeypatch, capsys):
    drive, entry, hkey, browser = _setup_blocked(
        monkeypatch, CA_URL, [{CA_URL: BLOCKED}, {CA_URL: BLOCKED}, {CA_URL: "#2 Some Book"}])
    asyncio.run(sr.cmd_run(entry))
    assert len(_pngs(drive, "main")) == 1
    assert len(browser.contexts) == 3  # cached one + two fresh (first fresh still blocked)
    assert all(c.closed for c in browser.contexts[1:])
    assert sr.chunk_done(drive, "mk", "S", hkey, 0, 1)
    assert "recovered 1 blocked row(s) in a fresh context" in capsys.readouterr().out


def test_blocked_row_recovered_on_first_fresh_attempt_uses_new_context(monkeypatch):
    drive, entry, hkey, browser = _setup_blocked(
        monkeypatch, CA_URL, [{CA_URL: BLOCKED}, {CA_URL: "#2 Some Book"}])
    asyncio.run(sr.cmd_run(entry))
    assert len(browser.contexts) == 2
    assert browser.contexts[1] is not browser.contexts[0]
    assert browser.kwargs[1]["locale"] == "en-CA"
    assert browser.kwargs[1]["extra_http_headers"] == sr.ACCEPT_LANGUAGE
    assert len(_pngs(drive, "main")) == 1


def test_blocked_on_all_attempts_is_error_as_before_and_no_marker(monkeypatch, capsys):
    rows_map = [{CA_URL: BLOCKED}]
    drive, entry, hkey, browser = _setup_blocked(monkeypatch, CA_URL, rows_map)
    with pytest.raises(SystemExit):  # one row, one error: found == 0
        asyncio.run(sr.cmd_run(entry))
    out = capsys.readouterr().out
    assert "\n  - row 1: blocked/throttled by the site\n" in out
    assert "recovered" not in out
    assert len(browser.contexts) == 1 + sr.BLOCKED_RETRY_ATTEMPTS
    assert _pngs(drive, "main") == []
    assert not sr.chunk_done(drive, "mk", "S", hkey, 0, 1)


def test_unmapped_host_fresh_context_is_viewport_only(monkeypatch):
    url = "https://www.amazon.com/bestsellers"
    drive, entry, hkey, browser = _setup_blocked(
        monkeypatch, url, [{url: BLOCKED}, {url: "#2 Some Book"}])
    asyncio.run(sr.cmd_run(entry))
    assert browser.kwargs[1] == {"viewport": sr.VIEWPORT}
    assert len(_pngs(drive, "main")) == 1


def test_fresh_context_closed_even_when_attempt_raises(monkeypatch):
    drive, entry, hkey, browser = _setup_blocked(
        monkeypatch, CA_URL, [{CA_URL: BLOCKED}, ({CA_URL: BLOCKED},), {CA_URL: "#2 Some Book"}])
    asyncio.run(sr.cmd_run(entry))
    assert browser.contexts[1].closed  # its new_page raised
    assert browser.contexts[2].closed
    assert len(_pngs(drive, "main")) == 1


def _scripted_rows(monkeypatch, outcomes):
    """process_row_limited raises/returns the next scripted outcome on each call."""
    calls = []

    async def scripted(page, *a):
        calls.append(page)
        out = outcomes[min(len(calls) - 1, len(outcomes) - 1)]
        if isinstance(out, Exception):
            raise out
        return out

    monkeypatch.setattr(sr, "process_row_limited", scripted)
    return calls


def test_non_blocked_load_error_in_fresh_context_is_not_retried(monkeypatch, capsys):
    drive, entry, hkey, browser = _setup_blocked(monkeypatch, CA_URL, [{CA_URL: BLOCKED}])
    calls = _scripted_rows(monkeypatch, [sr.BlockedError("blocked"), RuntimeError("load failed"), True])
    with pytest.raises(SystemExit):
        asyncio.run(sr.cmd_run(entry))
    assert len(browser.contexts) == 2  # cached one + a single fresh attempt
    assert len(calls) == 2
    assert all(c.closed for c in browser.contexts[1:])
    out = capsys.readouterr().out
    assert "\n  - row 1: load failed\n" in out
    assert "recovered" not in out


def test_upload_error_in_fresh_context_is_not_retried(monkeypatch, capsys):
    drive, entry, hkey, browser = _setup_blocked(monkeypatch, CA_URL, [{CA_URL: BLOCKED}])
    calls = _scripted_rows(monkeypatch, [sr.BlockedError("blocked"), OSError("upload failed"), True])
    with pytest.raises(SystemExit):
        asyncio.run(sr.cmd_run(entry))
    assert len(calls) == 2  # no second fresh attempt, so no duplicate upload
    assert len(browser.contexts) == 2
    assert "\n  - row 1: upload failed\n" in capsys.readouterr().out


def test_context_creation_failure_moves_to_next_fresh_attempt(monkeypatch):
    drive, entry, hkey, browser = _setup_blocked(
        monkeypatch, CA_URL, [{CA_URL: BLOCKED}, {CA_URL: "#2 Some Book"}])
    real_new_context = browser.new_context
    state = {"n": 0}

    async def flaky(**kw):
        state["n"] += 1
        if state["n"] == 2:  # first fresh context cannot be created
            raise RuntimeError("no context")
        return await real_new_context(**kw)

    browser.new_context = flaky
    asyncio.run(sr.cmd_run(entry))
    assert state["n"] == 3
    assert len(_pngs(drive, "main")) == 1


def test_blocked_retry_wait_uses_constant(monkeypatch):
    drive, entry, hkey, browser = _setup_blocked(
        monkeypatch, CA_URL, [{CA_URL: BLOCKED}, {CA_URL: "#2 Some Book"}])
    monkeypatch.setattr(sr, "BLOCKED_RETRY_WAIT_S", 7)
    waits = []
    real_sleep = asyncio.sleep

    async def fake_sleep(s, *a):
        waits.append(s)
        await real_sleep(0)

    monkeypatch.setattr(sr.asyncio, "sleep", fake_sleep)
    asyncio.run(sr.cmd_run(entry))
    assert waits == [7]


def test_blocked_retries_skipped_when_budget_spent(monkeypatch, capsys):
    drive, entry, hkey, browser = _setup_blocked(
        monkeypatch, CA_URL, [{CA_URL: BLOCKED}, {CA_URL: "#2 Some Book"}])
    clock = {"t": 0.0}
    monkeypatch.setattr(sr.time, "monotonic", lambda: clock["t"])
    real_process = sr.process_row_limited

    async def slow(page, *a):
        try:
            return await real_process(page, *a)
        finally:
            clock["t"] += sr.RUN_BUDGET_S + 1  # the first load used up the budget

    monkeypatch.setattr(sr, "process_row_limited", slow)
    with pytest.raises(SystemExit):
        asyncio.run(sr.cmd_run(entry))
    assert len(browser.contexts) == 1
    assert "\n  - row 1: blocked/throttled by the site\n" in capsys.readouterr().out


def test_non_blocked_error_is_not_retried_in_fresh_context(monkeypatch):
    drive, entry, hkey, browser = _setup_blocked(
        monkeypatch, CA_URL, [{CA_URL: "RAISE"}, {CA_URL: "#2 Some Book"}])
    with pytest.raises(SystemExit):
        asyncio.run(sr.cmd_run(entry))
    assert len(browser.contexts) == 1
    assert _pngs(drive, "main") == []
