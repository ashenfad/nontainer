import pytest

from nontainer import NotSupportedError, SessionIdError
from nontainer.providers import DirProvider


def test_creates_root(tmp_path):
    p = DirProvider(tmp_path / "ws", session="s1")
    assert p.root.is_dir()
    assert p.session == "s1"


def test_caps_unversioned(tmp_path):
    p = DirProvider(tmp_path / "ws", session="s1")
    assert not p.caps.versioned
    assert not p.caps.staging
    assert not p.caps.cheap_fork


def test_session_id_validated(tmp_path):
    with pytest.raises(SessionIdError):
        DirProvider(tmp_path / "ws", session="../escape")


def test_versioning_verbs_raise(tmp_path):
    p = DirProvider(tmp_path / "ws", session="s1")
    with pytest.raises(NotSupportedError):
        p.checkpoint()
    with pytest.raises(NotSupportedError):
        p.restore("x")
    with pytest.raises(NotSupportedError):
        p.history()
    with pytest.raises(NotSupportedError):
        p.fork("other")
    with pytest.raises(NotSupportedError):
        p.discard()
    with pytest.raises(NotSupportedError):
        p.mount()


def test_kv_persists_across_instances(tmp_path):
    p1 = DirProvider(tmp_path / "ws", session="s1")
    p1.kv["k"] = {"nested": [1, 2, 3]}
    p1.close()

    p2 = DirProvider(tmp_path / "ws", session="s1")
    assert p2.kv["k"] == {"nested": [1, 2, 3]}
    del p2.kv["k"]
    assert "k" not in p2.kv


def test_fs_satisfies_termish_protocol(tmp_path):
    p = DirProvider(tmp_path / "ws", session="s1")
    fs = p.fs
    for method in (
        "getcwd",
        "chdir",
        "read",
        "write",
        "exists",
        "isfile",
        "isdir",
        "stat",
        "mkdir",
        "makedirs",
        "remove",
        "rmdir",
        "rename",
        "list",
        "list_detailed",
        "glob",
    ):
        assert callable(getattr(fs, method, None)), f"fs missing {method}()"
