"""Daily Slack digest: how many screenshots each active book got in 24 h."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import screenshot_runner as sr
from fakes import FakeDrive, FakeSheets

UTC = timezone.utc
IST = ZoneInfo("Asia/Kolkata")
FOLDER = "root"
SHEET = "SHEET_A"
# 09:30 IST on 25 Nov 2026
NOW = datetime(2026, 11, 25, 4, 0, tzinfo=UTC)
HEADER = ["Category", "URL", "Country", "Keyword"]


def make_job(start="2026-11-01 10:00", stop="2026-12-30 20:00", sheet_id=SHEET):
    tz = IST
    return {
        "row": 2,
        "sheet_id": sheet_id,
        "start": datetime.strptime(start, "%Y-%m-%d %H:%M").replace(tzinfo=tz),
        "stop": datetime.strptime(stop, "%Y-%m-%d %H:%M").replace(tzinfo=tz),
        "tz": tz,
        "tz_str": "Asia/Kolkata",
    }


def make_world(n_rows=3, title="Fake Book"):
    drive = FakeDrive()
    f = drive.files()
    folder = "application/vnd.google-apps.folder"
    book = f.add(title, FOLDER, folder)
    f.add("Fake Book filtered", FOLDER, folder)
    markers = f.add(sr.MARKERS_FOLDER, FOLDER, folder)
    rows = [HEADER] + [["Fiction", f"https://x/{i}", "US", f"kw{i}"] for i in range(n_rows)]
    sheets = FakeSheets({(SHEET, "A:D"): rows}, {SHEET: title})
    return drive, sheets, book, markers


@pytest.fixture
def slack(monkeypatch):
    sent = []
    monkeypatch.setattr(sr, "notify_slack", lambda text: sent.append(text) or True)
    return sent


def add_shots(drive, folder, n, age_h):
    for i in range(n):
        drive.files().add(f"20261125-0{i}0000_a_us_kw{i}_abc123.png", folder,
                          created=NOW - timedelta(hours=age_h))


def run(drive, sheets, jobs, now=NOW, run_hours=(9, 19)):
    return sr.post_daily_digest(drive, sheets, FOLDER, jobs, list(run_hours), now)


def test_digest_reports_count_and_expected_per_book(slack):
    drive, sheets, book, _ = make_world(n_rows=3)
    add_shots(drive, book, 4, age_h=2)    # inside 24 h; same names may repeat
    add_shots(drive, book, 3, age_h=26)   # older: not counted
    assert run(drive, sheets, [make_job()]) is True
    assert len(slack) == 1
    # only the 19:00 run yesterday is finished; the 09:00 run of the current hour has not happened yet
    assert "Fake Book: 4 screenshots saved in the last 24 h (expected about 3)" in slack[0]
    assert "NO screenshots" not in slack[0]


def test_digest_counts_repeated_captures_of_the_same_row(slack):
    drive, sheets, book, _ = make_world(n_rows=1)
    f = drive.files()
    f.add("20261124-190000_a_us_kw0_abc123.png", book, created=NOW - timedelta(hours=9))
    f.add("20261125-090000_a_us_kw0_abc123.png", book, created=NOW - timedelta(hours=1))
    run(drive, sheets, [make_job()])
    assert "2 screenshots saved" in slack[0]


def test_zero_screenshots_after_three_hours_is_flagged(slack):
    drive, sheets, _, _ = make_world()
    run(drive, sheets, [make_job()])
    assert ":warning:" in slack[0] and "NO screenshots yet" in slack[0]


def test_zero_screenshots_in_first_three_hours_is_not_flagged(slack):
    drive, sheets, _, _ = make_world()
    start = (NOW.astimezone(IST) - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")
    run(drive, sheets, [make_job(start=start)])
    assert "0 screenshots saved" in slack[0]
    assert "NO screenshots" not in slack[0] and ":warning:" not in slack[0]


def test_only_books_in_their_window_are_listed(slack):
    drive, sheets, _, _ = make_world()
    run(drive, sheets, [make_job(), make_job(start="2026-01-01 10:00", stop="2026-02-01 10:00", sheet_id="OLD")])
    assert len(slack) == 1 and slack[0].count("screenshots saved") == 1


def test_nothing_posted_when_no_book_is_in_window(slack):
    drive, sheets, _, markers = make_world()
    assert run(drive, sheets, [make_job(start="2026-01-01 10:00", stop="2026-02-01 10:00")]) is False
    assert slack == []
    assert drive.files().calls == []  # no Drive traffic at all


def test_nothing_posted_before_nine_ist(slack):
    drive, sheets, _, _ = make_world()
    early = datetime(2026, 11, 25, 3, 20, tzinfo=UTC)  # 08:50 IST
    assert run(drive, sheets, [make_job()], now=early) is False
    assert slack == []


def test_posted_once_per_day_with_marker(slack):
    drive, sheets, _, markers = make_world()
    assert run(drive, sheets, [make_job()]) is True
    names = [f["name"] for f in drive.files().files.values() if markers in f["parents"]]
    assert names == ["digest_20261125"]
    assert run(drive, sheets, [make_job()], now=NOW + timedelta(hours=3)) is False
    assert len(slack) == 1
    assert run(drive, sheets, [make_job()], now=NOW + timedelta(days=1)) is True
    assert len(slack) == 2


def test_marker_write_failure_only_warns(slack, capsys):
    drive, sheets, _, _ = make_world()
    drive.files().create_failures = [RuntimeError("drive down")]
    assert run(drive, sheets, [make_job()]) is True
    assert len(slack) == 1
    assert "::warning::" in capsys.readouterr().out


def test_failed_slack_post_leaves_no_marker_so_next_trigger_retries(monkeypatch):
    drive, sheets, _, markers = make_world()
    monkeypatch.setattr(sr, "notify_slack", lambda text: False)
    assert run(drive, sheets, [make_job()]) is False
    assert not [f for f in drive.files().files.values() if f["name"].startswith("digest_")]


def test_expected_count_ignores_run_hours_outside_window(slack):
    drive, sheets, _, _ = make_world(n_rows=3)
    # window opened at 09:15 IST today: the 09:00 run did not belong to it, nor did yesterday's
    run(drive, sheets, [make_job(start="2026-11-25 09:15")])
    assert "expected about 0)" in slack[0]


def test_current_hour_run_is_not_expected_and_zero_saved_warns(slack):
    drive, sheets, _, _ = make_world(n_rows=3)
    now = datetime(2026, 11, 25, 4, 5, tzinfo=UTC)  # 09:35 IST
    run(drive, sheets, [make_job(start="2026-11-23 20:00")], now=now)
    assert "expected about 3)" in slack[0]  # only yesterday's 19:00 run
    assert "NO screenshots yet" in slack[0]


def test_healthy_new_book_with_no_finished_run_hour_is_not_flagged(slack):
    drive, sheets, _, _ = make_world(n_rows=3)
    now = datetime(2026, 11, 25, 4, 5, tzinfo=UTC)  # 09:35 IST
    run(drive, sheets, [make_job(start="2026-11-25 05:00")], now=now, run_hours=(10, 16, 22))
    assert "0 screenshots saved in the last 24 h (expected about 0)" in slack[0]
    assert "NO screenshots" not in slack[0] and ":warning:" not in slack[0]


def test_expected_runs_only_counts_hours_before_the_current_one():
    job = make_job(start="2026-11-01 10:00")
    now = datetime(2026, 11, 25, 4, 5, tzinfo=UTC)  # 09:35 IST
    assert sr.expected_runs(job, [9], now) == 0
    assert sr.expected_runs(job, [9, 19], now) == 1


def test_one_unreadable_book_does_not_stop_the_others(slack, monkeypatch):
    drive, sheets, book, markers = make_world(n_rows=2, title="Good Book")
    bad = make_job(sheet_id="BAD")
    real = sr.get_sheet_title

    def title(svc, sid):
        if sid == "BAD":
            raise RuntimeError("sheet gone")
        return real(svc, sid)

    monkeypatch.setattr(sr, "get_sheet_title", title)
    add_shots(drive, book, 2, age_h=2)
    assert run(drive, sheets, [bad, make_job()]) is True
    assert len(slack) == 1
    assert "could not check" in slack[0]
    assert "Good Book: 2 screenshots saved" in slack[0]
    assert [f for f in drive.files().files.values() if f["name"] == "digest_20261125"]


def test_book_failing_after_its_title_is_listed_as_could_not_check(slack, monkeypatch):
    drive, sheets, book, _ = make_world(n_rows=2, title="Good Book")
    sheets._s._values._ranges[("BAD", "A:D")] = [HEADER]
    sheets._s._titles["BAD"] = "Broken Book"
    real = sr.files_since

    def flaky(svc, folder_id, since):
        if folder_id is None or svc.files().files.get(folder_id, {}).get("name") == "Broken Book":
            raise RuntimeError("drive hiccup")
        return real(svc, folder_id, since)

    drive.files().add("Broken Book", FOLDER, "application/vnd.google-apps.folder")
    monkeypatch.setattr(sr, "files_since", flaky)
    assert run(drive, sheets, [make_job(sheet_id="BAD"), make_job()]) is True
    assert "Broken Book: could not check" in slack[0]
    assert "Good Book:" in slack[0] and "drive hiccup" not in slack[0]


def test_never_dispatched_book_creates_no_drive_folder(slack):
    drive, sheets, _, _ = make_world(title="Fresh Book")
    drive.files().files = {k: v for k, v in drive.files().files.items() if v["name"] != "Fresh Book"}
    run(drive, sheets, [make_job()])
    assert "Fresh Book" not in {f["name"] for f in drive.files().files.values()}
    assert not [c for c in drive.files().calls if c[0] == "create" and c[1]["name"] == "Fresh Book"]
    assert "Fresh Book: 0 screenshots saved" in slack[0]
    assert "NO screenshots yet" in slack[0]  # same warning rules as any book with nothing saved


def test_digest_stops_checking_books_after_its_deadline(slack):
    drive, sheets, book, _ = make_world(n_rows=1, title="Fake Book")
    add_shots(drive, book, 1, age_h=2)
    sheets._s._values._ranges[("B2", "A:D")] = [HEADER, ["Fiction", "https://x/1", "US", "kw1"]]
    sheets._s._titles["B2"] = "Second Book"
    ticks = iter([0, 10, 100, 100, 100, 100])
    assert sr.post_daily_digest(drive, sheets, FOLDER,
                                [make_job(), make_job(sheet_id="B2")], [9, 19], NOW,
                                max_seconds=90, clock=lambda: next(ticks)) is True
    assert "Fake Book: 1 screenshots saved" in slack[0]
    assert "sheet B2: skipped" in slack[0] and "Second Book" not in slack[0]


# ---------- wiring into dispatch ----------

def dispatch_world(monkeypatch, tmp_path):
    out = tmp_path / "gh_out"
    all_hours = ",".join(str(h) for h in range(24))
    sheets = FakeSheets(
        {
            ("ctl", "Jobs!A:D"): [["s", "a", "b", "c"],
                                  [SHEET, "2000-01-01 00:00", "2999-01-01 00:00", "UTC"]],
            ("ctl", "Settings!A:B"): [["k", "v"], ["run_hours", all_hours]],
            (SHEET, "A:D"): [HEADER, ["Fiction", "https://x/1", "US", "kw1"]],
        },
        {SHEET: "Fake Book"},
    )
    drive = FakeDrive()
    monkeypatch.setattr(sr, "get_credentials", lambda: object())
    monkeypatch.setattr(sr, "build", lambda name, *a, **k: sheets if name == "sheets" else drive)
    monkeypatch.setenv("CONTROL_SHEET_ID", "ctl")
    monkeypatch.setenv("DRIVE_FOLDER_ID", FOLDER)
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    return out


def test_outputs_are_complete_before_the_digest_runs(monkeypatch, tmp_path):
    out = dispatch_world(monkeypatch, tmp_path)
    seen = []
    monkeypatch.setattr(sr, "post_daily_digest",
                        lambda *a, **k: seen.append(out.read_text() if out.exists() else ""))
    sr.cmd_dispatch()
    assert len(seen) == 1
    assert "has_jobs=true" in seen[0] and "problems=false" in seen[0] and SHEET in seen[0]
    final = out.read_text()
    assert final.count("matrix=") == 1 and final.count("has_jobs=") == 1  # written once


def test_digest_failure_does_not_change_the_outputs(monkeypatch, tmp_path):
    out = dispatch_world(monkeypatch, tmp_path)
    monkeypatch.setattr(sr, "post_daily_digest", lambda *a, **k: None)
    sr.cmd_dispatch()
    baseline = out.read_text()
    out.unlink()
    monkeypatch.setattr(sr, "post_daily_digest", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    sr.cmd_dispatch()
    assert out.read_text() == baseline


def test_dispatch_survives_a_digest_failure_and_keeps_the_matrix(monkeypatch, tmp_path, capsys):
    out = tmp_path / "gh_out"
    all_hours = ",".join(str(h) for h in range(24))
    sheets = FakeSheets(
        {
            ("ctl", "Jobs!A:D"): [["s", "a", "b", "c"],
                                  [SHEET, "2000-01-01 00:00", "2999-01-01 00:00", "UTC"]],
            ("ctl", "Settings!A:B"): [["k", "v"], ["run_hours", all_hours]],
            (SHEET, "A:D"): [HEADER, ["Fiction", "https://x/1", "US", "kw1"]],
        },
        {SHEET: "Fake Book"},
    )
    drive = FakeDrive()
    monkeypatch.setattr(sr, "get_credentials", lambda: object())
    monkeypatch.setattr(sr, "build", lambda name, *a, **k: sheets if name == "sheets" else drive)
    monkeypatch.setenv("CONTROL_SHEET_ID", "ctl")
    monkeypatch.setenv("DRIVE_FOLDER_ID", FOLDER)
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise RuntimeError("digest exploded")

    monkeypatch.setattr(sr, "post_daily_digest", boom)
    sr.cmd_dispatch()
    assert calls == [1]
    text = out.read_text()
    assert "has_jobs=true" in text and SHEET in text
    assert "::warning::" in capsys.readouterr().out
