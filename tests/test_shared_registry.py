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


def test_an_unpublish_in_flight_is_left_alone_by_another_host(hosts):
    """A second unpublish that took over the first's reservation would
    free the version while the first was still removing: a publish
    could claim it again, and lose its new branch to the first remover."""
    a, b = hosts
    with _draft(a, "one", "from a") as ws:
        a.publish(ws, "demo")
    entered, release = _pause(a, "_remove_version_state")
    done = {}

    def unpublish():
        try:
            a.unpublish("demo", "v1", min_age=0)
        except Exception as e:  # noqa: BLE001 - reported below
            done["error"] = e

    removing = threading.Thread(target=unpublish)
    removing.start()
    try:
        assert entered.wait(timeout=10)
        assert b.publication("demo") is None
        with pytest.raises(WorkspaceError, match="being unpublished"):
            b.unpublish("demo", "v1")
        with _draft(b, "two", "from b") as other:
            with pytest.raises(WorkspaceError, match="being unpublished"):
                b.publish(other, "demo", version="v1")
    finally:
        release.set()
        removing.join(timeout=10)
    assert "error" not in done
    assert "@store/pub/demo/v1" not in b._branches()
    with _draft(b, "three", "again") as other:
        pub = b.publish(other, "demo", version="v1")
    assert _read(a, pub.current_version.ref) == b"again"


def test_a_failed_unpublish_puts_the_record_back(hosts, monkeypatch):
    """The record goes before the tag and the branch, so a removal
    that fails must put it back: the version stays published, rather
    than vanishing from every listing with its state still there."""
    a, b = hosts
    with _draft(a, "one", "first") as ws:
        a.publish(ws, "demo")
        ws.files.write(PAGE, "second")
        ws.commit()
        a.publish(ws, "demo")

    def fails(*args, **kwargs):
        raise RuntimeError("the backend went away mid-removal")

    for version in ("v1", "v2"):
        if version == "v2":
            a.unpublish("demo", "v1", min_age=0)  # v2 is now the last
        before = b.publication("demo")
        monkeypatch.setattr(a, "_remove_version_state", fails)
        with pytest.raises(RuntimeError):
            a.unpublish("demo", version, min_age=0)
        monkeypatch.undo()
        after = b.publication("demo")
        assert after.versions == before.versions
        assert after.current == before.current == "v2"
        assert _read(b, after.version(version).ref) in (b"first", b"second")
        assert a._registry_read()["demo"].get("pending") is None

    a.unpublish("demo", "v2", min_age=0)  # and calling it again finishes it
    assert b.publication("demo") is None
    assert b.publications() == {}


def test_a_publication_open_racing_an_unpublish_says_so(hosts, monkeypatch):
    """The backend may be the first to notice the branch is gone, while
    the open walks it: the registry is asked again, and the answer is
    the unpublish, not the backend's error."""
    from kvgit import UnknownBranchError

    a, b = hosts
    with _draft(a, "one", "page") as ws:
        pub = a.publish(ws, "demo")

    def walked_into_it(*args, **kwargs):
        b.unpublish("demo", "v1", min_age=0)
        raise UnknownBranchError("@store/pub/demo/v1")

    monkeypatch.setattr(a, "_provider_at_commit", walked_into_it)
    with pytest.raises(WorkspaceError, match="No longer published"):
        pub.open()


def _two_roots(store):
    """One commit read under two workspace roots, each with an app
    tree of its own under it: relative paths name a different tree at
    each root."""
    with store.open("roots", autocommit=False, root="/") as ws:
        ws.files.write("/a/app/index.html", "under a")
        ws.files.write("/b/app/index.html", "under b")
        ws.commit()
        ref = ws.ref
    return store.resolve(ref, root="/a"), store.resolve(ref, root="/b")


def test_publishes_from_two_roots_are_two_attempts(hosts):
    """Same commit, same paths, another root: another tree. It never
    shares the first's reservation."""
    a, b = hosts
    at_a, at_b = _two_roots(a)
    entered, release = _pause(a, "_record_version")
    done = {}
    first = threading.Thread(
        target=lambda: done.setdefault("pub", a.publish(at_a, "demo", version="v1"))
    )
    first.start()
    try:
        assert entered.wait(timeout=10)
        with pytest.raises(WorkspaceError, match="being published"):
            a.publish(at_b, "demo", version="v1")
    finally:
        release.set()
        first.join(timeout=10)
    with a.publication("demo").open() as served:
        assert served.root == "/a"
        assert served.files.read("/a/app/index.html") == b"under a"
    at_a.close()
    at_b.close()


def test_a_leftover_from_another_root_is_not_adopted(hosts, monkeypatch):
    """A publish that failed after writing its branch gives back its
    reservation, and the branch it left records no root. A retry from
    another root is not the same publish, and is refused rather than
    recorded over a tree it did not select."""
    a, b = hosts
    at_a, at_b = _two_roots(a)

    def died(*args, **kwargs):
        raise RuntimeError("the record's write failed")

    monkeypatch.setattr(a, "_record_version", died)
    with pytest.raises(RuntimeError):
        a.publish(at_a, "demo", version="v1")
    monkeypatch.undo()

    with pytest.raises(WorkspaceError, match="@store/pub/demo/v1"):
        a.publish(at_b, "demo", version="v1")
    pub = a.publish(at_a, "demo", version="v1")  # the same publish adopts it
    with pub.open() as served:
        assert served.files.read("/a/app/index.html") == b"under a"
    at_a.close()
    at_b.close()


def test_a_shared_publish_still_moves_the_pointer_it_was_asked_to(hosts):
    """Two attempts of one publish share its reservation. The one asked
    to leave the pointer alone may record first; the one asked to move
    it still moves it."""
    a, b = hosts
    with _draft(a, "one", "first") as ws:
        a.publish(ws, "demo")
        ws.files.write(PAGE, "second")
        ws.commit()
    entered, release = _pause(a, "_record_version")
    done = {}
    with a.open("one") as ws:
        promoting = threading.Thread(
            target=lambda: done.setdefault("pub", a.publish(ws, "demo"))
        )
        promoting.start()
        try:
            assert entered.wait(timeout=10)
            with b.open("one") as same:
                quiet = b.publish(same, "demo", current=False)
            assert quiet.current == "v1"
        finally:
            release.set()
            promoting.join(timeout=10)
    assert done["pub"].current == "v2"
    assert b.publication("demo").current == "v2"
    assert [v.version for v in b.publication("demo").versions] == ["v1", "v2"]


def test_a_named_version_is_not_resumed_by_a_publish_naming_none(hosts):
    """A publish that names no version takes the next free one, past
    every version held or reserved, even beside an attempt of the same
    tree that named its own: it never takes that attempt's over."""
    a, b = hosts
    entered, release = _pause(a, "_record_version")
    done = {}
    ws = _draft(a, "one", "page")
    named = threading.Thread(
        target=lambda: done.setdefault("pub", a.publish(ws, "demo", version="v99"))
    )
    named.start()
    try:
        assert entered.wait(timeout=10)
        with b.open("one") as same:
            assert b.publish(same, "demo").current_version.version == "v100"
    finally:
        release.set()
        named.join(timeout=10)
    ws.close()
    assert [v.version for v in b.publication("demo").versions] == ["v99", "v100"]


def test_a_failed_removal_that_took_the_branch_is_not_put_back(hosts, monkeypatch):
    """Once the branch is gone the version cannot be opened, so a
    failure after it (the sweep, say) leaves it unpublished rather than
    listed and broken."""
    a, b = hosts
    with _draft(a, "one", "page") as ws:
        a.publish(ws, "demo")
    real = a._remove_version_state

    def then_fails(*args, **kwargs):
        real(*args, **kwargs)
        raise RuntimeError("the sweep after the branch failed")

    monkeypatch.setattr(a, "_remove_version_state", then_fails)
    with pytest.raises(RuntimeError):
        a.unpublish("demo", "v1", min_age=0)
    assert b.publication("demo") is None
    assert a._registry_read() == {}
    assert "demo/v1" not in b.tags.list()


def test_a_failed_removal_that_took_only_the_tag_puts_it_back(hosts, monkeypatch):
    """The tag goes before the branch. A removal that fails between
    them leaves the branch, so the version goes back whole, its tag
    minted again."""
    a, b = hosts
    with _draft(a, "one", "page") as ws:
        version = a.publish(ws, "demo").current_version

    def tag_only(tag, branch, *, min_age):
        a._delete_store_tag(tag)
        raise RuntimeError("the branch deletion failed")

    monkeypatch.setattr(a, "_remove_version_state", tag_only)
    with pytest.raises(RuntimeError):
        a.unpublish("demo", "v1", min_age=0)
    assert b.tags.list()["demo/v1"] == version.ref.commit
    assert b.publication("demo").current_version.ref == version.ref
    assert _read(b, version.ref) == b"page"


def test_a_short_ref_racing_an_unpublish_says_so(hosts, monkeypatch):
    """Expanding a short id walks the version's branch, which an
    unpublish may take mid-walk: that reads as the unpublish. A version
    still published keeps the expansion's own error."""
    from kvgit import UnknownBranchError

    from nontainer.errors import CommitNotFoundError
    from nontainer.store import Ref

    a, b = hosts
    with _draft(a, "one", "page") as ws:
        version = a.publish(ws, "demo").current_version
    short = Ref(version.ref.session, version.ref.commit[:7])
    assert _read(a, short) == b"page"

    def not_found(ref):
        raise CommitNotFoundError("no such commit on that line")

    monkeypatch.setattr(a, "_expanded_ref", not_found)
    with pytest.raises(CommitNotFoundError, match="no such commit"):
        a.resolve(short)

    def walked_into_it(ref):
        b.unpublish("demo", "v1", min_age=0)
        raise UnknownBranchError(version.ref.session)

    monkeypatch.setattr(a, "_expanded_ref", walked_into_it)
    with pytest.raises(WorkspaceError, match="No longer published"):
        a.resolve(short)


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


def _legacy_store(tmp_path):
    """A store as 0.13 left it: one published version, its registry in
    publications.json. Returns the store directory."""
    path = tmp_path / "store"
    store = Store(path)
    with _draft(store, "one", "page") as ws:
        store.publish(ws, "demo")
    _as_legacy_file(store, path / "publications.json")
    store.close()
    return path


def test_a_host_that_cannot_write_its_directory_still_reads(tmp_path, caplog):
    """A host that only serves may not own its store directory. The
    import still lands in the backend; the file can't be renamed, which
    is logged, and reads go on."""
    import os
    import stat

    path = _legacy_store(tmp_path)
    os.chmod(path, stat.S_IRUSR | stat.S_IXUSR)
    try:
        store = Store(path)
        with caplog.at_level("WARNING", logger="nontainer.store"):
            assert list(store.publications()) == ["demo"]
            assert list(store.publications()) == ["demo"]
        assert "could not rename" in caplog.text
        assert (path / "publications.json").is_file()
        store.close()
    finally:
        os.chmod(path, stat.S_IRWXU)


def test_a_host_whose_writes_are_refused_still_reads(tmp_path, monkeypatch, caplog):
    """A read-only database role refuses the import's write: logged, the
    read goes on with the shared registry, and the file stays for a
    process that can write. The explicit import says why."""
    path = _legacy_store(tmp_path)
    store = Store(path)

    def refused(*args, **kwargs):
        raise PermissionError("permission denied for table kvgit")

    monkeypatch.setattr(store, "_registry_put", refused)
    with caplog.at_level("WARNING", logger="nontainer.store"):
        assert store.publications() == {}
    assert "import_registry" in caplog.text
    assert (path / "publications.json").is_file()
    with pytest.raises(PermissionError):
        store.import_registry()
    monkeypatch.undo()

    report = store.import_registry()
    assert report.imported == ("demo/v1",)
    assert report.renamed
    assert list(store.publications()) == ["demo"]
    store.close()


def test_a_retired_hosts_file_is_imported_from_anywhere(tmp_path):
    """A host that never runs 0.14 never imports its own file. Copied
    anywhere, it is imported by name, and left where it is."""
    kv = Memory()
    a = Store(tmp_path / "a", kv=kv)
    with _draft(a, "one", "page") as ws:
        a.publish(ws, "kept")
        a.publish(ws, "retired")
    registry = a._registry_read()
    a.repo.store.set(
        "__nontainer_publications__",
        json.dumps({"kept": registry["kept"]}).encode(),
    )
    copied = tmp_path / "from-the-old-host.json"
    copied.write_text(json.dumps({"retired": registry["retired"]}))

    assert list(a.publications()) == ["kept"]
    report = a.import_registry(copied)
    assert (report.imported, report.present, report.skipped) == (
        ("retired/v1",),
        (),
        (),
    )
    assert not report.renamed and copied.is_file()
    assert list(a.publications()) == ["kept", "retired"]
    again = a.import_registry(copied)
    assert (again.imported, again.present) == ((), ("retired/v1",))
    a.close()


def test_the_report_names_what_the_store_no_longer_backs(tmp_path):
    path = _legacy_store(tmp_path)
    store = Store(path)
    store.tags.delete("demo/v1")
    report = store.import_registry()
    assert report.skipped == ("demo/v1",) and report.imported == ()
    store.close()


def test_importing_the_own_file_again_points_at_the_renamed_one(tmp_path):
    path = _legacy_store(tmp_path)
    store = Store(path)
    assert list(store.publications()) == ["demo"]  # the automatic import
    with pytest.raises(FileNotFoundError, match="imported already"):
        store.import_registry()
    report = store.import_registry(path / "publications.json.imported")
    assert report.present == ("demo/v1",) and report.imported == ()
    store.close()


def test_an_import_keeps_metadata_a_caller_cleared(tmp_path):
    """An empty meta in the shared registry is one somebody set, and an
    import adding a version to that publication leaves it empty."""
    store = Store(tmp_path / "store")
    with _draft(store, "one", "first") as ws:
        store.publish(ws, "demo")
        ws.files.write(PAGE, "second")
        ws.commit()
        store.publish(ws, "demo")
    store.set_meta("demo", {"title": "old"})
    old = store._registry_read()["demo"]
    store.set_meta("demo", {})
    shared = store._registry_read()
    shared["demo"]["versions"].pop("v2")
    store.repo.store.set("__nontainer_publications__", json.dumps(shared).encode())
    legacy = tmp_path / "old.json"
    legacy.write_text(json.dumps({"demo": old}))

    report = store.import_registry(legacy)
    assert report.imported == ("demo/v2",)
    pub = store.publication("demo")
    assert [v.version for v in pub.versions] == ["v1", "v2"]
    assert pub.meta == {}
    store.close()


@pytest.mark.parametrize(
    "data",
    [
        [],
        {"demo": []},
        {"demo": {"versions": []}},
        {"demo": {"versions": {"v1": {"tag": "demo/v1"}}}},
        {"demo": {"versions": {}, "meta": "title"}},
    ],
)
def test_a_file_that_is_not_a_registry_is_refused_and_kept(tmp_path, data, caplog):
    path = _legacy_store(tmp_path)
    (path / "publications.json").write_text(json.dumps(data))
    store = Store(path)
    with pytest.raises(WorkspaceError, match="not a publication registry"):
        store.import_registry()
    with caplog.at_level("WARNING", logger="nontainer.store"):
        assert store.publications() == {}  # the automatic import logs it
    assert "publications.json" in caplog.text
    assert (path / "publications.json").is_file()
    store.close()
