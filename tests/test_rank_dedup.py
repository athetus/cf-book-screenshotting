import asyncio
import hashlib

from fakes import FakeDrive
from screenshot_runner import (
    delete_from_drive,
    extract_rank,
    find_filtered_entries,
    update_filtered_best,
)


class _FakeLocator:
    def __init__(self, result=None, raises=None):
        self.first = self
        self._result = result
        self._raises = raises

    async def evaluate(self, js):
        if self._raises:
            raise self._raises
        return self._result


class _FakePage:
    def __init__(self, locator):
        self._locator = locator

    def get_by_text(self, keyword, exact=False):
        return self._locator


def test_extract_rank_parses_matched_badge_text():
    page = _FakePage(_FakeLocator(result="#7"))
    assert asyncio.run(extract_rank(page, "Some Book")) == 7


def test_extract_rank_returns_none_when_no_single_match():
    page = _FakePage(_FakeLocator(result=None))
    assert asyncio.run(extract_rank(page, "Some Book")) is None


def test_extract_rank_returns_none_on_locator_error():
    page = _FakePage(_FakeLocator(raises=RuntimeError("boom")))
    assert asyncio.run(extract_rank(page, "Some Book")) is None


def test_find_filtered_entries_parses_rank_and_ignores_non_matching_names():
    drive = FakeDrive()
    f = drive.files()
    a = f.add("rank0007_abc123_Fiction_US_Some_Book_20260717-090000.png", "folder123")
    b = f.add("rank0003_abc123_Fiction_US_Some_Book_20260717-100000.png", "folder123")
    f.add("abc123_not_a_rank_file.png", "folder123")
    f.add("rank0001_zzz999_Other.png", "folder123")
    entries = find_filtered_entries(drive, "folder123", "abc123")
    assert sorted(entries) == sorted([(a, 7), (b, 3)])


def test_find_filtered_entries_empty_when_no_matches():
    assert find_filtered_entries(FakeDrive(), "folder123", "abc123") == []


def test_find_filtered_entries_reads_every_page():
    drive = FakeDrive()
    f = drive.files()
    f.page_size_cap = 2
    for r in range(5):
        f.add(f"rank{r + 1:04d}_abc123_x.png", "folder123")
    assert len(find_filtered_entries(drive, "folder123", "abc123")) == 5


def test_delete_from_drive_deletes():
    drive = FakeDrive()
    fid = drive.files().add("x.png", "p")
    delete_from_drive(drive, fid)
    assert fid not in drive.files().files


def test_delete_from_drive_ignores_already_deleted_file():
    delete_from_drive(FakeDrive(), "gone")  # must not raise


ROW = {"category": "Fiction", "country": "US", "keyword": "Some Book"}
URL = "https://www.amazon.com/gp/bestsellers/x"
KEY = hashlib.sha1(URL.encode()).hexdigest()[:10]


def test_update_filtered_best_replaces_worse_entry_upload_first():
    drive = FakeDrive()
    f = drive.files()
    old = f.add(f"rank0009_{KEY}_old.png", "filt")
    status = update_filtered_best(drive, "filt", URL, ROW, 3, "20261102-100000", b"png")
    names = [x["name"] for x in f.files.values()]
    assert old not in f.files
    assert names == [f"rank0003_{KEY}_Fiction_US_Some_Book_20261102-100000.png"]
    kinds = [c[0] for c in f.calls if c[0] in ("create", "delete")]
    assert kinds == ["create", "delete"]
    assert "rank #3" in status


def test_update_filtered_best_keeps_better_existing():
    drive = FakeDrive()
    f = drive.files()
    f.add(f"rank0002_{KEY}_old.png", "filt")
    status = update_filtered_best(drive, "filt", URL, ROW, 5, "ts", b"png")
    assert len(f.files) == 1
    assert "not better" in status


def test_update_filtered_best_keeps_old_when_upload_fails(monkeypatch):
    import screenshot_runner
    monkeypatch.setattr(screenshot_runner.time, "sleep", lambda s: None)
    drive = FakeDrive()
    f = drive.files()
    old = f.add(f"rank0009_{KEY}_old.png", "filt")
    f.create_failures = [TimeoutError("t")] * 3
    try:
        update_filtered_best(drive, "filt", URL, ROW, 3, "ts", b"png")
    except TimeoutError:
        pass
    assert old in f.files
