import pytest

from nontainer import Workspace
from nontainer.providers import DirProvider


@pytest.fixture(scope="module")
def chromium_available():
    """Skip cleanly where the [apps] browser isn't installed. Shared:
    both test_app suites (behavior and page-error frames) need it."""
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as p:
            b = p.chromium.launch()
            b.close()
    except Exception as e:  # pragma: no cover
        pytest.skip(f"chromium unavailable: {e}")


@pytest.fixture
def dir_ws(tmp_path):
    """A dir-backed workspace with default (stdlib-only) python config."""
    provider = DirProvider(tmp_path / "ws", session="test-session")
    ws = Workspace(provider)
    yield ws
    ws.close()
