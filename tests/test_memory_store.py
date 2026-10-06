"""A memory store: sessions, history and the publication registry held in
this process, with nothing written to disk."""

import os
from pathlib import Path

import pytest

from nontainer import Store, store, workspace


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """A home and a working directory that must stay empty, and a
    NONTAINER_KV that would fail loudly if anything consulted it."""
    home = tmp_path / "home"
    cwd = tmp_path / "cwd"
    home.mkdir()
    cwd.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("NONTAINER_KV", "postgresql://nowhere.invalid/none")
    return home, cwd


def _empty(*dirs: Path) -> bool:
    return all(not any(os.scandir(d)) for d in dirs)


def test_a_memory_store_writes_nothing_to_disk(isolated):
    st = Store(memory=True)
    ws = st.open("author")
    ws.files.write("app/index.html", "<h1>hi</h1>")
    ws.files.write("notes.md", "a")
    ws.commit(info={"tool": "seed"})
    child = ws.fork("author.child")
    child.files.write("notes.md", "b")
    child.commit(info={"tool": "edit"})
    pub = st.publish(ws, "page")
    st.tags.add(child, "keep")

    assert st.sessions() == ["author", "author.child"]
    assert pub.current == "v1"
    assert st.publication("page") is not None
    assert "keep" in st.tags.list()
    child.close()
    ws.close()
    st.close()
    assert _empty(*isolated)


def test_memory_store_identity():
    st = store(memory=True)
    assert st.memory is True
    assert st.path is None
    assert repr(st) == "Store(memory=True)"
    assert Store().memory is False


def test_data_survives_closing_the_repository_handle():
    st = Store(memory=True)
    ws = st.open("s")
    ws.files.write("a.txt", "kept")
    ws.commit(info={"tool": "seed"})
    head = ws.head
    ws.close()
    st.close()

    reopened = st.open("s")
    assert reopened.head == head
    assert reopened.files.read("a.txt") == b"kept"
    reopened.close()


def test_two_memory_stores_share_nothing():
    a, b = Store(memory=True), Store(memory=True)
    ws = a.open("s")
    ws.files.write("a.txt", "only in a")
    ws.commit(info={"tool": "seed"})
    ws.close()
    assert a.sessions() == ["s"]
    assert b.sessions() == []


@pytest.mark.parametrize(
    "kwargs, named",
    [
        ({"path": "/tmp/somewhere"}, "path"),
        ({"kv": object()}, "kv"),
        ({"provider_factory": lambda s: None}, "provider_factory"),
    ],
)
def test_memory_refuses_what_says_where_state_lives(kwargs, named):
    path = kwargs.pop("path", None)
    with pytest.raises(ValueError, match=named):
        Store(path, memory=True, **kwargs)


def test_memory_needs_the_kvgit_backend():
    with pytest.raises(ValueError, match="kvgit"):
        Store(memory=True, backend="agentfs")


def test_workspace_helper_builds_a_memory_store(isolated):
    ws = workspace("scratch", memory=True)
    ws.files.write("a.txt", "x")
    ws.commit(info={"tool": "seed"})
    assert ws._store is not None and ws._store.memory
    ws.close()
    assert _empty(*isolated)


def test_workspace_helper_refuses_memory_with_a_store_or_provider():
    with pytest.raises(ValueError, match="path"):
        workspace("s", memory=True, store="/tmp/somewhere")
    from nontainer.providers import KvgitProvider

    with pytest.raises(ValueError, match="provider"):
        workspace("s", memory=True, provider=KvgitProvider.open(None, session="s"))
