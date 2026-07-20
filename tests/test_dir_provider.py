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


def test_delete_removes_session_dirs(tmp_path):
    DirProvider(tmp_path / "a", session="a").close()
    DirProvider(tmp_path / "b", session="b").close()
    DirProvider.delete(tmp_path, {"a"})
    assert not (tmp_path / "a").exists()
    assert (tmp_path / "b").is_dir()  # sibling untouched


def test_delete_nonexistent_session_is_noop(tmp_path):
    DirProvider(tmp_path / "a", session="a").close()
    DirProvider.delete(tmp_path, {"a", "never"})  # no raise
    assert not (tmp_path / "a").exists()


def test_delete_from_nonexistent_store_is_noop(tmp_path):
    DirProvider.delete(tmp_path / "no-store", {"a"})  # no raise


def test_delete_rejects_path_traversal(tmp_path):
    # a hostile id must never let rmtree climb out of the store root
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "store").mkdir()
    with pytest.raises(SessionIdError):
        DirProvider.delete(tmp_path / "store", {"../outside"})
    assert outside.is_dir()  # still there — validation ran before rmtree


def test_delete_validates_before_deleting_any(tmp_path):
    # a valid id alongside a hostile one: the whole call rejects, the
    # valid one's dir survives (ids checked up front, before any rmtree)
    DirProvider(tmp_path / "good", session="good").close()
    with pytest.raises(SessionIdError):
        DirProvider.delete(tmp_path, {"good", "../evil"})
    assert (tmp_path / "good").is_dir()


def test_delete_workspace_convenience(tmp_path):
    from nontainer import delete_workspace

    DirProvider(tmp_path / "s", session="s").close()
    delete_workspace("s", store=tmp_path, backend="dir")
    assert not (tmp_path / "s").exists()


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
