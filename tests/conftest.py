import pytest

from nontainer import Workspace
from nontainer.providers import DirProvider


@pytest.fixture
def dir_ws(tmp_path):
    """A dir-backed workspace with default (stdlib-only) python config."""
    provider = DirProvider(tmp_path / "ws", session="test-session")
    ws = Workspace(provider)
    yield ws
    ws.close()
