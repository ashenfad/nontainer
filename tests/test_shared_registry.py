"""The publication registry is shared by every Store on one backend.

Two ``Store`` objects with directories of their own over one kvgit
backend stand in for two hosts in front of one PostgreSQL store: the
same storage boundary, without a server.
"""

import json
import os
import threading
import uuid

import pytest
from kvgit.kv.memory import Memory

from nontainer import Store
from nontainer.errors import WorkspaceError

PAGE = "app/index.html"


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


@pytest.fixture(params=["memory", "postgres"])
def hosts(request, tmp_path):
    """Two stores on one backend, each with a directory of its own: one
    in-memory backend, or one PostgreSQL table, reached through a
    connection pool of each store's own, where a server is set up
    (NONTAINER_TEST_PG_URL)."""
    if request.param == "memory":
        shared = Memory()
        backends = [shared, shared]
    else:
        url = _postgres_url()
        if url is None:
            pytest.skip("no PostgreSQL server (set NONTAINER_TEST_PG_URL)")
        from kvgit.kv.postgres import Postgres

        table = f"t_{uuid.uuid4().hex[:16]}"
        backends = [Postgres(url, table=table), Postgres(url, table=table)]
    a = Store(tmp_path / "a", kv=backends[0])
    b = Store(tmp_path / "b", kv=backends[1])
    yield a, b
    a.close()
    b.close()
    if request.param == "postgres":
        backends[0].drop()
        for backend in backends:
            backend.close()


def _draft(store, session, text):
    """A session holding one page, committed; returns it open."""
    ws = store.open(session, autocommit=False)
    ws.files.write(PAGE, text)
    ws.commit()
    return ws


def _read(store, ref):
    with store.resolve(ref) as version:
        return version.files.read(PAGE)


def _pause(store, method):
    """Stop ``store``'s calls to ``method`` until released; returns
    (entered, release) events."""
    real = getattr(store, method)
    entered, release = threading.Event(), threading.Event()

    def paused(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=10)
        return real(*args, **kwargs)

    setattr(store, method, paused)
    return entered, release


def test_another_host_resolves_a_published_ref(hosts):
    """The reproduction from #155: a reader holding the exact
    Version.ref could see the commit but not the record."""
    publisher, reader = hosts
    with _draft(publisher, "draft", "version one") as ws:
        draft_ref = ws.ref
        published_ref = publisher.publish(ws, "demo").current_version.ref

    assert _read(reader, draft_ref) == b"version one"
    assert reader.repo.get_commit(published_ref.commit) is not None
    assert _read(publisher, published_ref) == b"version one"
    assert _read(reader, published_ref) == b"version one"


def test_another_host_lists_and_opens_what_was_published(hosts):
    publisher, reader = hosts
    with _draft(publisher, "draft", "version one") as ws:
        publisher.publish(ws, "demo", info={"title": "Demo"})

    assert list(reader.publications()) == ["demo"]
    pub = reader.publication("demo")
    assert pub.current == "v1"
    assert pub.current_version.info == {"title": "Demo"}
    with pub.open() as served:
        assert served.files.read(PAGE) == b"version one"


def test_another_host_extends_the_lineage_instead_of_clearing_it(hosts):
    """Before the shared registry, the second host saw the first's live
    version as a dead publish's leftover, and its error told the user to
    unpublish it, which deleted the version the first host was serving."""
    a, b = hosts
    with _draft(a, "one", "from a") as ws:
        first = a.publish(ws, "demo").current_version
    with _draft(b, "two", "from b") as ws:
        pub = b.publish(ws, "demo")

    assert [v.version for v in pub.versions] == ["v1", "v2"]
    assert [v.version for v in a.publication("demo").versions] == ["v1", "v2"]
    assert _read(a, first.ref) == b"from a"
    assert _read(a, pub.current_version.ref) == b"from b"


def test_unpublish_set_current_and_set_meta_reach_every_host(hosts):
    a, b = hosts
    with _draft(a, "one", "first") as ws:
        a.publish(ws, "demo")
        ws.files.write(PAGE, "second")
        ws.commit()
        a.publish(ws, "demo")

    b.set_current("demo", "v1")
    assert a.publication("demo").current == "v1"
    b.set_meta("demo", {"title": "Demo"})
    assert a.publication("demo").meta == {"title": "Demo"}

    # v1 is current and v2 remains: refused on either host, so nothing
    # a second host does reaches a version that is being served
    with pytest.raises(WorkspaceError, match="current version"):
        b.unpublish("demo", "v1", min_age=0)
    b.unpublish("demo", "v2", min_age=0)
    assert [v.version for v in a.publication("demo").versions] == ["v1"]
    assert "@store/pub/demo/v2" not in a._branches()
    assert "demo/v2" not in a.tags.list()


def test_a_publish_in_flight_is_left_alone_by_another_host(hosts):
    """Between writing its branch and recording the version, a publish
    holds a reservation: no other host may clear what it has written,
    claim its version, or see it published yet."""
    a, b = hosts
    entered, release = _pause(a, "_record_version")
    ws = _draft(a, "one", "from a")
    done = {}
    publishing = threading.Thread(
        target=lambda: done.setdefault("pub", a.publish(ws, "demo", version="v1"))
    )
    publishing.start()
    try:
        assert entered.wait(timeout=10)
        assert "@store/pub/demo/v1" in b._branches()
        assert b.publication("demo") is None
        assert b.publications() == {}

        with pytest.raises(WorkspaceError, match="being published"):
            b.unpublish("demo", "v1")
        with _draft(b, "two", "from b") as other:
            with pytest.raises(WorkspaceError, match="being published"):
                b.publish(other, "demo", version="v1")
            # numbering moves past the reservation
            assert b.publish(other, "demo").current_version.version == "v2"
    finally:
        release.set()
        publishing.join(timeout=10)
    ws.close()

    pub = b.publication("demo")
    assert [v.version for v in pub.versions] == ["v1", "v2"]
    assert done["pub"].version("v1").ref == pub.version("v1").ref
    assert _read(b, pub.version("v1").ref) == b"from a"


def _crash_before_the_record(store, monkeypatch):
    """A publish whose process dies between its branch and its record:
    nothing runs after, so its reservation is not given back."""

    def died(*args, **kwargs):
        raise RuntimeError("the process died before the record")

    monkeypatch.setattr(store, "_record_version", died)
    monkeypatch.setattr(store, "_release_quietly", lambda *a, **k: None)


def test_a_dead_publish_is_resumed_by_the_same_publish(hosts, monkeypatch):
    a, b = hosts
    with _draft(a, "one", "from a") as ws:
        _crash_before_the_record(a, monkeypatch)
        with pytest.raises(RuntimeError):
            a.publish(ws, "demo")
        monkeypatch.undo()
        stranded = a.tags.list()["demo/v1"]

        pub = a.publish(ws, "demo")
    assert [v.version for v in pub.versions] == ["v1"]
    assert pub.current_version.ref.commit == stranded
    assert b.publication("demo").current_version.ref.commit == stranded


def test_a_dead_publish_is_cleared_once_older_than_min_age(hosts, monkeypatch):
    a, b = hosts
    with _draft(a, "one", "from a") as ws:
        _crash_before_the_record(a, monkeypatch)
        with pytest.raises(RuntimeError):
            a.publish(ws, "demo", version="v1")
        monkeypatch.undo()

    with _draft(b, "two", "from b") as other:
        with pytest.raises(WorkspaceError, match="being published"):
            b.publish(other, "demo", version="v1")
        with pytest.raises(WorkspaceError, match="being published"):
            b.unpublish("demo", "v1")  # an hour's grace by default
        b.unpublish("demo", "v1", min_age=0)
        assert "@store/pub/demo/v1" not in a._branches()
        assert "demo/v1" not in a.tags.list()

        pub = b.publish(other, "demo", version="v1")
    assert _read(a, pub.current_version.ref) == b"from b"


def test_two_attempts_of_one_publish_record_one_version(hosts):
    """A retry while the first attempt is still going, from another
    host: the same source, paths and exclusions write the same tree,
    so they share the reservation, the commit and the record."""
    a, b = hosts
    ws = _draft(a, "one", "same")
    entered, release = _pause(a, "_record_version")
    done = {}
    first = threading.Thread(target=lambda: done.setdefault("a", a.publish(ws, "demo")))
    first.start()
    try:
        assert entered.wait(timeout=10)
        with b.open("one") as same:
            retried = b.publish(same, "demo")
    finally:
        release.set()
        first.join(timeout=10)
    ws.close()

    assert [v.version for v in retried.versions] == ["v1"]
    assert [v.version for v in done["a"].versions] == ["v1"]
    assert done["a"].current_version.ref == retried.current_version.ref
    assert [v.version for v in a.publication("demo").versions] == ["v1"]
    branches = [x for x in a._branches() if x.startswith("@store/pub/")]
    assert branches == ["@store/pub/demo/v1"]


def test_hosts_publishing_at_once_get_distinct_versions(hosts):
    a, b = hosts
    sessions = [
        _draft(store, f"s{i}", f"page {i}")
        for i, store in enumerate((a, b, a, b, a, b))
    ]
    barrier = threading.Barrier(len(sessions))
    errors = []

    def publish(ws):
        barrier.wait(timeout=10)
        try:
            ws._store.publish(ws, "demo")
        except Exception as e:  # noqa: BLE001 - reported below
            errors.append(e)

    threads = [threading.Thread(target=publish, args=(ws,)) for ws in sessions]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    for ws in sessions:
        ws.close()

    assert errors == []
    pub = a.publication("demo")
    assert [v.version for v in pub.versions] == ["v1", "v2", "v3", "v4", "v5", "v6"]
    pages = sorted(_read(b, v.ref) for v in pub.versions)
    assert pages == sorted(f"page {i}".encode() for i in range(6))
    assert a._registry_read()["demo"].get("pending") is None


# -- the registry file earlier releases kept ---------------------------------


def _as_legacy_file(store, path):
    """Move the store's registry out of the backend into the file an
    earlier release kept, as a store written by one would hold it."""
    registry = store._registry_read()
    store.repo.store.remove("__nontainer_publications__")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(registry, indent=2))
    return registry


def test_a_registry_file_is_imported_once_and_kept(tmp_path):
    store = Store(tmp_path)
    with _draft(store, "one", "page") as ws:
        store.publish(ws, "demo", info={"title": "Demo"})
    store.set_meta("demo", {"owner": "ann"})
    before = _as_legacy_file(store, tmp_path / "publications.json")
    store.close()

    reopened = Store(tmp_path)
    pub = reopened.publication("demo")
    assert pub.current_version.info == {"title": "Demo"}
    assert pub.meta == {"owner": "ann"}
    assert reopened._registry_read() == before
    assert not (tmp_path / "publications.json").exists()
    assert (tmp_path / "publications.json.imported").is_file()
    reopened.close()


def test_each_hosts_registry_file_is_merged_in(tmp_path):
    """Each host sharing a backend kept a file of its own. Importing
    one must not hide the other's versions, or they would read as dead
    publishes' leftovers."""
    kv = Memory()
    a = Store(tmp_path / "a", kv=kv)
    with _draft(a, "one", "from a") as ws:
        a.publish(ws, "alpha")
    from_a = _as_legacy_file(a, tmp_path / "a" / "publications.json")
    # with the key gone, the next publish starts a registry holding beta
    # alone, as host b's own file did
    with _draft(a, "two", "from b") as ws:
        a.publish(ws, "beta")
    _as_legacy_file(a, tmp_path / "b" / "publications.json")
    a.close()

    a, b = Store(tmp_path / "a", kv=kv), Store(tmp_path / "b", kv=kv)
    assert list(a.publications()) == ["alpha"]
    assert list(b.publications()) == ["alpha", "beta"]
    assert list(a.publications()) == ["alpha", "beta"]
    assert a._registry_read()["alpha"] == from_a["alpha"]
    a.close()
    b.close()


def test_an_imported_record_the_store_no_longer_backs_is_skipped(tmp_path, caplog):
    """A record whose tag is gone, or names another commit, was
    unpublished on another host or written over since: importing it
    would publish a version nothing serves."""
    store = Store(tmp_path)
    with _draft(store, "one", "page") as ws:
        store.publish(ws, "kept")
        store.publish(ws, "gone")
    _as_legacy_file(store, tmp_path / "publications.json")
    store.tags.delete("gone/v1")
    store.close()

    reopened = Store(tmp_path)
    with caplog.at_level("WARNING", logger="nontainer.store"):
        assert list(reopened.publications()) == ["kept"]
    assert "gone/v1" in caplog.text
    reopened.close()
