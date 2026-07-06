"""Shared browser: reuse, concurrent sessions on one browser, shutdown."""

import asyncio

import pytest

from nontainer import Workspace
from nontainer.apps import arun_test_app, enable_apps, shutdown_browser
from nontainer.providers import KvgitProvider

pytest.importorskip("playwright")


@pytest.fixture(scope="module")
def chromium_available():
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as p:
            p.chromium.launch().close()
    except Exception as e:  # pragma: no cover
        pytest.skip(f"chromium unavailable: {e}")


_APP = b"""<!doctype html><html><body>
<h1 id="t">hi</h1><script>console.log('booted')</script>
</body></html>"""


def _app_ws(session: str) -> tuple[Workspace, object]:
    ws = Workspace(KvgitProvider.open(None, session=session))
    rt = enable_apps(ws)
    ws.fs.makedirs("/app", exist_ok=True)
    ws.fs.write("/app/index.html", _APP)
    ws.checkpoint()
    return ws, rt


def _launches() -> int:
    from nontainer.apps.browser import _worker

    return _worker.launches


def test_browser_is_reused_across_calls(chromium_available):
    ws, rt = _app_ws("reuse")
    rt.test_app([{"read": "#t"}])  # warm up: browser now running
    before = _launches()
    rt.test_app([{"read": "#t"}])
    rt.test_app([{"read": "#t"}])
    assert _launches() == before  # no relaunch — one shared browser
    ws.close()


async def test_concurrent_sessions_share_one_browser(chromium_available):
    """The payoff: N sessions verify at once, on ONE browser (contexts),
    not N browsers."""
    workspaces = [_app_ws(f"c{i}") for i in range(6)]
    # warm up so the launch isn't attributed to the concurrent batch
    workspaces[0][1].test_app([{"read": "#t"}])
    before = _launches()

    results = await asyncio.gather(
        *(arun_test_app(rt, [{"read": "#t"}, {"assert": "true"}]) for _, rt in workspaces)
    )
    assert all(r.ok for r in results), [r.load_error for r in results]
    assert _launches() == before  # all six ran on the one warm browser
    for ws, _ in workspaces:
        ws.close()


def test_shutdown_then_relaunch(chromium_available):
    ws, rt = _app_ws("shut")
    rt.test_app([{"read": "#t"}])  # browser up
    before = _launches()
    shutdown_browser()  # tears down browser + loop-thread
    rt.test_app([{"read": "#t"}])  # must relaunch transparently
    assert _launches() == before + 1
    ws.close()
