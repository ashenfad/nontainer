"""Chromium's transient "Unable to capture screenshot" is retried.

Headless Chromium sometimes refuses a capture on a loaded machine; the
frame is readable a moment later. Unretried, it failed agents'
screenshots and grid tilings, and CI runs at random.
"""

import asyncio

import pytest

from nontainer.apps import driver_playwright
from nontainer.apps.driver_playwright import _screenshot

REFUSED = "Page.screenshot: Protocol error (Page.captureScreenshot): Unable to capture screenshot"


class Page:
    def __init__(self, errors):
        self.errors = list(errors)
        self.calls = []

    async def screenshot(self, **kwargs):
        self.calls.append(kwargs)
        if self.errors:
            raise RuntimeError(self.errors.pop(0))
        return b"png"


@pytest.fixture(autouse=True)
def no_waits(monkeypatch):
    monkeypatch.setattr(driver_playwright, "_SCREENSHOT_RETRY_WAITS", (0, 0))


def test_a_refused_capture_is_retried():
    page = Page([REFUSED, REFUSED])
    assert asyncio.run(_screenshot(page, full_page=True)) == b"png"
    assert page.calls == [{"full_page": True}] * 3


def test_a_capture_refused_every_time_raises_the_refusal():
    page = Page([REFUSED] * 3)
    with pytest.raises(RuntimeError, match="Unable to capture screenshot"):
        asyncio.run(_screenshot(page))
    assert len(page.calls) == 3


def test_any_other_error_is_not_retried():
    page = Page(["Target page, context or browser has been closed"])
    with pytest.raises(RuntimeError, match="has been closed"):
        asyncio.run(_screenshot(page))
    assert len(page.calls) == 1
