"""Amazon's soft block ("click the button below to continue shopping") is cleared
by clicking the button and reloading the original URL (probe 25 Nov 2026)."""
import asyncio

import pytest

import screenshot_runner as sr
from fakes import FakeDrive
from test_reliability import FakePage, ROW

SOFT = "Click the button below to continue shopping"


class ClickablePage(FakePage):
    """FakePage whose 'continue shopping' button can be present or missing."""

    def __init__(self, texts, has_button=True, click_fails=False):
        super().__init__(texts)
        self.has_button = has_button
        self.click_fails = click_fails
        self.clicks = 0

    def locator(self, selector):
        page = self

        class Loc:
            def locator(self, _):
                return self

            @property
            def first(self):
                return self

            async def click(self, timeout=None):
                if not page.has_button or page.click_fails:
                    raise RuntimeError("no visible button")
                page.clicks += 1

        return Loc()


def run(page, row=ROW):
    return asyncio.run(sr.process_row(page, row, FakeDrive(), "main", "", "L"))


def test_soft_block_is_cleared_by_click_and_reload():
    page = ClickablePage([SOFT, "#4 Some Book by Jane"])
    assert run(page) is True
    assert page.clicks == 1 and page.gotos == 2


def test_click_cycles_are_capped_then_old_retry_and_blocked_error():
    page = ClickablePage([SOFT])
    with pytest.raises(sr.BlockedError):
        run(page)
    # first load + CLICK_CYCLES reloads, then the 20 s retry does the same again
    assert page.clicks == 2 * sr.CLICK_CYCLES
    assert page.gotos == 2 * (1 + sr.CLICK_CYCLES)


def test_no_button_means_no_click_and_old_behaviour():
    page = ClickablePage(["Request was throttled", "#4 Some Book"], has_button=False)
    assert run(page) is True
    assert page.clicks == 0 and page.gotos == 2


def test_failing_click_does_not_crash_the_row():
    page = ClickablePage([SOFT], click_fails=True)
    with pytest.raises(sr.BlockedError):
        run(page)
    assert page.clicks == 0


def test_unblocked_page_is_never_clicked():
    page = ClickablePage(["#4 Some Book"])
    assert run(page) is True
    assert page.clicks == 0 and page.gotos == 1


def test_keyword_absent_after_click_is_not_found_not_blocked():
    page = ClickablePage([SOFT, "#1 Other Book #2 Another Book"])
    assert run(page) is False
    assert page.clicks == 1
