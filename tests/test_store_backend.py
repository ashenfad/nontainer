"""Where a kvgit store keeps its data — ``kv=``, ``NONTAINER_KV`` — and
the one repository a Store shares across its sessions."""

import os
import uuid

import pytest
from kvgit.kv.memory import Memory

from nontainer import Store, workspace
from nontainer.errors import NotSupportedError
from nontainer.store import KV_ENV, KV_TABLE_ENV


class CountingMemory(Memory):
    """An in-memory backend that counts how often it is closed."""

    def __init__(self):
        super().__init__()
        self.closes = 0

    def close(self):
        self.closes += 1


def test_a_store_keeps_its_data_in_the_backend_it_is_handed(tmp_path):
    kv = Memory()
    st = Store(tmp_path, kv=kv)
    with st.open("alice") as ws:
        ws.files.write("app/index.html", "<h1>hi</h1>")
        ws.commit()
        st.tags.add(ws, "release")
        st.publish(ws, "site", paths=["app/"])
    assert st.sessions() == ["alice"]
    assert not (tmp_path / "kvgit").exists()
    assert any(key.startswith("__branch_head__") for key in kv.keys())

    with st.open("alice") as again:
        assert again.files.read("app/index.html") == b"<h1>hi</h1>"
    with st.tags.at("release") as snap:
        assert snap.files.read("app/index.html") == b"<h1>hi</h1>"
    with st.publication("site").open() as served:
        assert served.files.read("app/index.html") == b"<h1>hi</h1>"
    st.delete("alice", min_age=0)
    assert st.sessions() == []


def test_every_session_shares_one_repository(tmp_path):
    kv = CountingMemory()
    st = Store(tmp_path, kv=kv)
    a = st.open("a")
    b = st.open("b")
    assert a._provider.repo is b._provider.repo is st.repo
    child = a.fork("c")
    assert child._provider.repo is st.repo

    child.close()
    a.close()
    b.close()
    st.close()
    # A backend handed in stays the caller's: nothing here closes it, so
    # the store can reopen its repository over the same object.
    assert kv.closes == 0
    with st.open("a") as again:
        assert again.head is not None
    st.close()
    assert kv.closes == 0


@pytest.mark.disk_only
def test_close_closes_a_backend_the_store_built(tmp_path, monkeypatch):
    import kvgit.kv.disk

    closes = []
    real_close = kvgit.kv.disk.Disk.close

    def counting(self):
        closes.append(self)
        return real_close(self)

    monkeypatch.setattr(kvgit.kv.disk.Disk, "close", counting)
    st = Store(tmp_path)
    with st.open("a") as ws:
        ws.files.write("x.txt", "1")
        ws.commit()
    assert closes == []  # the workspace borrowed it
    st.close()
    assert len(closes) == 1
    with st.open("a") as again:  # reopens on a fresh backend
        assert again.files.read("x.txt") == b"1"
    st.close()
    assert len(closes) == 2


@pytest.mark.disk_only
def test_the_workspace_helper_hands_its_store_backend_to_the_workspace(
    tmp_path, monkeypatch
):
    """``workspace()`` builds a store for one workspace, so closing that
    workspace closes the backend under it."""
    import kvgit.kv.disk

    closes = []
    real_close = kvgit.kv.disk.Disk.close

    def counting(self):
        closes.append(self)
        return real_close(self)

    monkeypatch.setattr(kvgit.kv.disk.Disk, "close", counting)
    ws = workspace("a", store=tmp_path)
    ws.files.write("x.txt", "1")
    ws.commit()
    assert closes == []
    ws.close()
    assert len(closes) == 1


def test_kv_takes_only_what_it_can_open(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="PostgreSQL URL"):
        Store(tmp_path, kv="sqlite:///store.db").sessions()
    monkeypatch.setenv(KV_ENV, "redis://cache")
    with pytest.raises(ValueError, match="PostgreSQL URL"):
        Store(tmp_path).sessions()


def test_an_explicit_backend_wins_over_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv(KV_ENV, "redis://cache")
    assert Store(tmp_path, kv=Memory()).sessions() == []


def test_repo_is_a_kvgit_store_verb(tmp_path):
    with pytest.raises(NotSupportedError):
        Store(tmp_path, backend="agentfs").repo


def _postgres_url() -> str | None:
    """A PostgreSQL URL to test against, or None where there is none."""
    url = os.environ.get("NONTAINER_TEST_PG_URL")
    if not url:
        return None
    try:
        import psycopg

        psycopg.connect(url, connect_timeout=3).close()
    except Exception:  # noqa: BLE001 - no server is a skip, not a failure
        return None
    return url


def test_a_postgres_url_names_the_store(tmp_path, monkeypatch):
    url = _postgres_url()
    if url is None:
        pytest.skip("no PostgreSQL server (set NONTAINER_TEST_PG_URL)")
    table = f"t_{uuid.uuid4().hex[:16]}"
    monkeypatch.setenv(KV_TABLE_ENV, table)
    st = Store(tmp_path, kv=url)
    try:
        with st.open("alice") as ws:
            ws.files.write("notes.txt", "on postgres\n")
            ws.commit()
        # A second store object on the same URL and table is the same store.
        other = Store(tmp_path / "elsewhere", kv=url)
        try:
            assert other.sessions() == ["alice"]
            with other.open("alice") as again:
                assert again.files.read("notes.txt") == b"on postgres\n"
        finally:
            other.close()
        assert not (tmp_path / "kvgit").exists()
    finally:
        st.repo.store.drop()
        st.close()
