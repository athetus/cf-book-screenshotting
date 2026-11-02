import io
from datetime import datetime, timezone

from PIL import Image

from screenshot_runner import (
    META_BAR_HEIGHT,
    URL_BAR_HEIGHT,
    add_meta_banner,
    add_url_banner,
)


def _blank_png(width=400, height=300):
    img = Image.new("RGB", (width, height), (10, 20, 30))
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def test_add_url_banner_adds_height_and_preserves_width():
    raw = _blank_png(400, 300)
    stamped = add_url_banner(raw, "https://www.amazon.com/some/very/long/path/that/should/get/truncated")
    img = Image.open(io.BytesIO(stamped))
    assert img.size == (400, 300 + URL_BAR_HEIGHT)


def test_add_meta_banner_adds_height_and_preserves_width():
    raw = _blank_png(400, 300)
    captured_at = datetime(2026, 7, 17, 12, 0, 0, tzinfo=timezone.utc)
    stamped = add_meta_banner(raw, "Fiction", captured_at)
    img = Image.open(io.BytesIO(stamped))
    assert img.size == (400, 300 + META_BAR_HEIGHT)


def test_add_meta_banner_handles_missing_category():
    raw = _blank_png(400, 300)
    captured_at = datetime(2026, 7, 17, 12, 0, 0, tzinfo=timezone.utc)
    stamped = add_meta_banner(raw, "", captured_at)
    img = Image.open(io.BytesIO(stamped))
    assert img.size == (400, 300 + META_BAR_HEIGHT)


def test_banners_stack_on_top_of_each_other():
    raw = _blank_png(400, 300)
    captured_at = datetime(2026, 7, 17, 12, 0, 0, tzinfo=timezone.utc)
    stamped = add_meta_banner(add_url_banner(raw, "https://example.com"), "Fiction", captured_at)
    img = Image.open(io.BytesIO(stamped))
    assert img.size == (400, 300 + URL_BAR_HEIGHT + META_BAR_HEIGHT)
