"""The workspace() factory."""

import pytest

from nontainer import SessionIdError, workspace


def test_dir_backend(tmp_path):
    with workspace("user-42", store=tmp_path, backend="dir") as ws:
        assert ws.session == "user-42"
        r = ws.terminal("echo hi > f.txt; cat f.txt")
        assert r.stdout.strip() == "hi"
    assert (tmp_path / "user-42" / "f.txt").exists()


def test_session_validated(tmp_path):
    with pytest.raises(SessionIdError):
        workspace("../etc", store=tmp_path, backend="dir")


def test_kvgit_backend_not_yet(tmp_path):
    with pytest.raises(NotImplementedError):
        workspace("s1", store=tmp_path, backend="kvgit")


def test_provider_override(tmp_path):
    from nontainer.providers import DirProvider

    p = DirProvider(tmp_path / "custom", session="s1")
    with workspace("s1", provider=p) as ws:
        assert ws.session == "s1"
        assert ws.terminal("pwd")
