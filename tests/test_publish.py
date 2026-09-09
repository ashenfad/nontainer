"""Publications: the app subtree, derived, tagged, and served.

What a publish must NOT carry is as much of the contract as what it
does: the session's cache, its working directory, its ws-git
bookkeeping and its conversation record all stay behind, and the link
back to the session is a soft reference in the commit's info rather
than a parent pointer.
"""

import json
import threading

import pytest
from monkeyfs import VirtualFS

from nontainer import Publication, Ref, Store, Version, Workspace
from nontainer.errors import NotSupportedError, WorkspaceError
from nontainer.providers.kvgit import KvgitProvider


def seeded(store, session="author", *, conversation=True):
    """A session with an app, a private note, cache, cwd and ws-git."""
    ws = store.open(session)
    ws.files.write("app/index.html", "<h1>scores</h1>")
    ws.files.write("app/api/scores.py", "def get(request):\n    return {'n': 1}\n")
    ws.files.write("notes/private.txt", "the transcript lives here")
    ws.cache["expensive"] = {"answer": 42}
    ws.terminal("cd app")
    ws.index.stage(["/workspace/notes/private.txt"])
    ws.index.commit("wip")
    if conversation:
        ws._provider.kv["__agno__/session"] = b'{"messages": []}'
    ws.commit(info={"tool": "seed"})
    return ws


def commit_keys(store, ref):
    """Every store key the commit a ref names actually holds."""
    provider = store._provider_at_commit(ref.session, ref.commit)
    try:
        return sorted(provider.staged.keys())
    finally:
        provider.close()


def to_legacy_table(ws):
    """Rewrite this session's metadata as the single pre-row table.

    What a state written by an older monkeyfs looks like: no rows, one
    ``__vfs_metadata__`` blob describing every path. Publishing from one
    is the migration case, and the fixture has to be built by hand
    because nothing writes the table any more.
    """
    kv = ws._provider.kv
    table = {}
    for key in list(kv.keys()):
        if VirtualFS.is_metadata_key(key):
            table[VirtualFS.path_for_metadata_key(key)] = json.loads(kv[key])
            del kv[key]
    kv[VirtualFS.METADATA_KEY] = json.dumps(table).encode()
    ws.commit(info={"tool": "legacy"})


# -- what a publish produces -------------------------------------------------


def test_publish_makes_a_branch_a_tag_and_a_record(tmp_path):
    store = Store(tmp_path)
    ws = seeded(store)
    pub = store.publish(ws, "scoreboard")

    assert isinstance(pub, Publication)
    assert pub.name == "scoreboard"
    assert pub.current == "v1"
    (version,) = pub.versions
    assert isinstance(version, Version)
    assert version.tag == "scoreboard/v1"

    # the tag names the derived commit
    assert store.tags.list()["scoreboard/v1"] == version.ref.commit
    # the branch is the anchor that keeps it openable
    assert "@store/pub/scoreboard/v1" in store._branches()
    assert version.ref.session == "@store/pub/scoreboard/v1"
    # the record is on disk
    registry = json.loads((tmp_path / "publications.json").read_text())
    assert registry == {
        "scoreboard": {
            "current": "v1",
            "versions": {
                "v1": {
                    "tag": "scoreboard/v1",
                    "ref": str(version.ref),
                    "published_from": str(ws.ref),
                    "created": version.created,
                    "root": "/workspace",
                }
            },
        }
    }
    ws.close()


def test_the_published_commit_holds_the_subtree_and_nothing_else(tmp_path):
    """No cache, no cwd, no ws-git blob, no conversation, no file from
    outside the published paths — the privacy and GC argument for a
    derived commit is only true if the keyset says so."""
    store = Store(tmp_path)
    ws = seeded(store)
    session_keys = commit_keys(store, ws.ref)
    assert any(k.startswith("__cache__/") for k in session_keys)
    assert VirtualFS.CWD_KEY in session_keys
    assert "__agno__/session" in session_keys

    pub = store.publish(ws, "scoreboard")
    keys = commit_keys(store, pub.current_version.ref)

    # A row per published file (plus the directories above them), and
    # never the legacy table, which describes files this tree lacks.
    assert VirtualFS.METADATA_KEY not in keys
    rows = [k for k in keys if VirtualFS.is_metadata_key(k)]
    files = [k for k in keys if k not in rows]
    # exactly the two app files, and they are file keys
    assert len(files) == 2
    assert all(k.startswith(VirtualFS.PREFIX) for k in files)
    assert {VirtualFS.path_for_metadata_key(k) for k in rows} >= {
        VirtualFS.path_for_metadata_key(
            VirtualFS.META_PREFIX + f[len(VirtualFS.PREFIX) :]
        )
        for f in files
    }
    assert not [k for k in keys if k.startswith("__cache__/")]
    assert VirtualFS.CWD_KEY not in keys
    assert "__agno__/session" not in keys
    assert not [k for k in keys if k.startswith("__ws_git")]

    snapshot = pub.open()
    assert sorted(snapshot.files.list("app", recursive=True)) == [
        "app/api",
        "app/api/scores.py",
        "app/index.html",
    ]
    assert not snapshot.files.exists("notes/private.txt")
    snapshot.close()
    ws.close()


def test_provenance_is_a_soft_ref_not_a_parent(tmp_path):
    """The publication's history is one commit deep: the session it came
    from is named in the info, and pinned by nothing."""
    store = Store(tmp_path)
    ws = seeded(store)
    session_ref = ws.ref
    pub = store.publish(ws, "scoreboard")
    version = pub.current_version

    assert version.published_from == session_ref
    assert isinstance(version.published_from, Ref)

    snapshot = pub.open()
    log = list(snapshot.log())
    # The publish commit, and beneath it nothing: the branch was started
    # from the store's empty root, so no commit of the session's is
    # reachable from here and the tag pins none of them alive.
    assert len(log) == 2
    assert log[0].id == version.ref.commit
    assert log[0].info["tool"] == "publish"
    assert log[0].info["name"] == "scoreboard"
    assert log[0].info["version"] == "v1"
    assert log[0].info["published_from"] == str(session_ref)

    session_commits = [e.id for e in ws.log()]
    assert log[0].id not in session_commits
    # The one commit the two share is that empty root, which the session
    # started from too — it holds nothing.
    assert log[1].id == session_commits[-1]
    assert commit_keys(store, Ref(session=version.ref.session, commit=log[1].id)) == []
    snapshot.close()
    ws.close()


def test_info_rides_along_on_the_commit(tmp_path):
    store = Store(tmp_path)
    ws = seeded(store)
    pub = store.publish(ws, "scoreboard", info={"verified": "green"})
    snapshot = pub.open()
    entry = next(iter(snapshot.log()))
    assert entry.info["verified"] == "green"
    assert entry.info["tool"] == "publish"
    snapshot.close()
    ws.close()


def test_paths_select_what_lands(tmp_path):
    store = Store(tmp_path)
    ws = seeded(store)
    ws.files.write("/workspace/data/seed.csv", "a,b\n1,2\n")
    ws.commit()
    pub = store.publish(ws, "scoreboard", paths=("app/", "data/seed.csv"))
    snapshot = pub.open()
    assert snapshot.files.exists("data/seed.csv")
    assert snapshot.files.exists("app/index.html")
    assert not snapshot.files.exists("notes/private.txt")
    snapshot.close()
    ws.close()


def test_publishing_nothing_is_refused(tmp_path):
    store = Store(tmp_path)
    ws = seeded(store)
    with pytest.raises(WorkspaceError, match="Nothing to publish"):
        store.publish(ws, "scoreboard", paths=("nowhere/",))
    ws.close()


# -- versions and the current pointer ---------------------------------------


def test_publishing_twice_increments_and_moves_current(tmp_path):
    store = Store(tmp_path)
    ws = seeded(store)
    store.publish(ws, "scoreboard")
    ws.files.write("/workspace/app/index.html", "<h1>scores v2</h1>")
    ws.commit()
    pub = store.publish(ws, "scoreboard")

    assert [v.version for v in pub.versions] == ["v1", "v2"]
    assert pub.current == "v2"
    assert pub.open().files.read("app/index.html") == b"<h1>scores v2</h1>"
    assert pub.open("v1").files.read("app/index.html") == b"<h1>scores</h1>"

    moved = store.set_current("scoreboard", "v1")
    assert moved.current == "v1"
    assert store.publication("scoreboard").current == "v1"
    assert moved.open().files.read("app/index.html") == b"<h1>scores</h1>"
    ws.close()


def test_an_explicit_version_must_be_unused(tmp_path):
    store = Store(tmp_path)
    ws = seeded(store)
    pub = store.publish(ws, "scoreboard", version="beta")
    assert pub.current == "beta"
    with pytest.raises(WorkspaceError, match="Version already published"):
        store.publish(ws, "scoreboard", version="beta")
    # the default sequence ignores names it did not mint
    assert store.publish(ws, "scoreboard").current == "v1"
    ws.close()


def test_set_current_refuses_a_version_that_is_not_there(tmp_path):
    store = Store(tmp_path)
    ws = seeded(store)
    store.publish(ws, "scoreboard")
    with pytest.raises(WorkspaceError, match="No such version"):
        store.set_current("scoreboard", "v9")
    with pytest.raises(WorkspaceError, match="No such publication"):
        store.set_current("nope", "v1")
    with pytest.raises(WorkspaceError, match="No such version"):
        store.publication("scoreboard").open("v9")
    ws.close()


def test_publications_lists_every_lineage(tmp_path):
    store = Store(tmp_path)
    ws = seeded(store)
    store.publish(ws, "scoreboard")
    store.publish(ws, "dashboard")
    found = store.publications()
    assert sorted(found) == ["dashboard", "scoreboard"]
    assert all(isinstance(p, Publication) for p in found.values())
    assert store.publication("nothing") is None
    assert Store(tmp_path / "empty").publications() == {}
    ws.close()


# -- opening and resolving --------------------------------------------------


def test_a_publication_opens_frozen(tmp_path):
    store = Store(tmp_path)
    ws = seeded(store)
    pub = store.publish(ws, "scoreboard")
    snapshot = pub.open()
    assert isinstance(snapshot, Workspace)
    assert snapshot.frozen
    assert snapshot.session == "@store/pub/scoreboard/v1"
    assert snapshot.files.read("app/index.html") == b"<h1>scores</h1>"
    with pytest.raises(NotSupportedError):
        snapshot.files.write("app/index.html", "nope")
    snapshot.close()
    ws.close()


def test_a_publication_is_served_with_the_embedders_settings(tmp_path):
    """A publication carries the tree; the live objects a handler calls
    are the embedder's, and it hands them over at the open."""
    from nontainer import PythonConfig

    class Db:
        def read(self):
            return "live"

    store = Store(tmp_path)
    ws = seeded(store)
    pub = store.publish(ws, "scoreboard")
    ws.close()

    db = Db()
    snapshot = pub.open(python=PythonConfig(host_objects={"db": db}))
    assert snapshot.runtime.python_config.host_objects["db"] is db
    assert snapshot.run_python("out = db.read()").namespace["out"] == "live"
    snapshot.close()

    bare = pub.open()
    assert bare.runtime.python_config.host_objects == {}
    bare.close()


def test_a_publication_open_takes_no_root(tmp_path):
    """The root the files were published under is recorded with the
    version, so a second spelling could only contradict it."""
    store = Store(tmp_path)
    ws = seeded(store)
    pub = store.publish(ws, "scoreboard")
    ws.close()
    with pytest.raises(TypeError, match="takes no 'root'"):
        pub.open(root="/elsewhere")
    with pytest.raises(TypeError, match="a frozen open takes python, mounts"):
        pub.open(autocommit=False)


def test_store_resolve_takes_a_version_ref(tmp_path):
    store = Store(tmp_path)
    ws = seeded(store)
    version = store.publish(ws, "scoreboard").current_version
    resolved = store.resolve(str(version.ref))
    assert resolved.frozen
    assert resolved.files.read("app/index.html") == b"<h1>scores</h1>"
    resolved.close()
    with pytest.raises(WorkspaceError, match="No longer published"):
        store.resolve("@store/pub/scoreboard/v9@" + version.ref.commit)
    ws.close()


def test_publication_branches_are_not_sessions(tmp_path):
    store = Store(tmp_path)
    ws = seeded(store)
    store.publish(ws, "scoreboard")
    assert store.sessions() == ["author"]
    assert not store.exists("@store/pub/scoreboard/v1")
    assert "@store/pub/scoreboard/v1" in store._branches()
    ws.close()


def test_a_publication_outlives_its_session(tmp_path):
    """The point of the derived commit: deleting the session takes the
    transcript, the cache and the history with it, and the published
    version reads exactly as before."""
    store = Store(tmp_path)
    ws = seeded(store)
    store.publish(ws, "scoreboard")
    ws.close()
    store.delete("author", min_age=0)

    assert store.sessions() == []
    snapshot = store.publication("scoreboard").open()
    assert snapshot.files.read("app/index.html") == b"<h1>scores</h1>"
    snapshot.close()
    # ...and a store tag can still be read with no session to anchor on,
    # because the publication brings its own branch — which the read
    # borrows rather than minting the store's own anchor.
    anchored = store.tags.at("scoreboard/v1")
    assert anchored.files.read("app/index.html") == b"<h1>scores</h1>"
    anchored.close()
    assert "@store/anchor" not in store._branches()


# -- refusals ---------------------------------------------------------------


def test_a_dirty_workspace_is_refused(tmp_path):
    store = Store(tmp_path)
    ws = seeded(store)
    ws.autocommit = False
    ws.files.write("/workspace/app/index.html", "<h1>uncommitted</h1>")
    assert ws.uncommitted
    with pytest.raises(WorkspaceError, match="ws.commit\\(\\) or drop"):
        store.publish(ws, "scoreboard")
    ws.discard()
    assert store.publish(ws, "scoreboard").current == "v1"
    ws.close()


def test_a_workspace_from_another_store_is_refused(tmp_path):
    mine = Store(tmp_path / "mine")
    theirs = Store(tmp_path / "theirs")
    ws = seeded(theirs)
    with pytest.raises(WorkspaceError, match="Cannot publish"):
        mine.publish(ws, "scoreboard")
    loose = Workspace(KvgitProvider.open(None, session="loose"))
    with pytest.raises(WorkspaceError, match="no store owns it"):
        mine.publish(loose, "scoreboard")
    loose.close()
    ws.close()


def test_unversioned_and_factory_stores_refuse(tmp_path):
    plain = Store(tmp_path / "dir", backend="dir")
    ws = plain.open("author")
    ws.files.write("app/index.html", "<h1>hi</h1>")
    with pytest.raises(NotSupportedError, match="kvgit"):
        plain.publish(ws, "scoreboard")
    ws.close()

    factory = Store(provider_factory=lambda s: KvgitProvider.open(None, session=s))
    other = factory.open("author")
    with pytest.raises(NotSupportedError, match="provider_factory"):
        factory.publish(other, "scoreboard")
    other.close()


def test_a_bad_publication_name_is_refused(tmp_path):
    store = Store(tmp_path)
    ws = seeded(store)
    for bad in ("with/slash", "", ".dotted", "per%cent"):
        with pytest.raises(ValueError, match="publication name"):
            store.publish(ws, bad)
    with pytest.raises(ValueError, match="version name"):
        store.publish(ws, "scoreboard", version="a/b")
    ws.close()


# -- unpublish --------------------------------------------------------------


def test_unpublish_removes_the_tag_the_branch_and_the_record(tmp_path):
    store = Store(tmp_path)
    ws = seeded(store)
    store.publish(ws, "scoreboard")
    store.unpublish("scoreboard", "v1", min_age=0)

    assert store.publication("scoreboard") is None
    assert store.tags.list() == {}
    assert "@store/pub/scoreboard/v1" not in store._branches()
    assert json.loads((tmp_path / "publications.json").read_text()) == {}
    ws.close()


def test_unpublish_refuses_the_current_version_while_others_remain(tmp_path):
    store = Store(tmp_path)
    ws = seeded(store)
    store.publish(ws, "scoreboard")
    store.publish(ws, "scoreboard")
    with pytest.raises(WorkspaceError, match="current version"):
        store.unpublish("scoreboard", "v2", min_age=0)
    # the other one goes without argument, and current stays put
    store.unpublish("scoreboard", "v1", min_age=0)
    pub = store.publication("scoreboard")
    assert [v.version for v in pub.versions] == ["v2"]
    assert pub.current == "v2"
    # ...and now the last one may go, current or not
    store.unpublish("scoreboard", "v2", min_age=0)
    assert store.publication("scoreboard") is None
    ws.close()


def test_unpublish_refuses_what_is_not_there(tmp_path):
    store = Store(tmp_path)
    ws = seeded(store)
    store.publish(ws, "scoreboard")
    with pytest.raises(WorkspaceError, match="No such version"):
        store.unpublish("scoreboard", "v9")
    with pytest.raises(WorkspaceError, match="No such publication"):
        store.unpublish("nothing", "v1")
    ws.close()


# -- the registry -----------------------------------------------------------


def test_the_registry_survives_reopening_the_store(tmp_path):
    store = Store(tmp_path)
    ws = seeded(store)
    store.publish(ws, "scoreboard")
    ws.close()
    store.close()

    reopened = Store(tmp_path)
    pub = reopened.publication("scoreboard")
    assert pub is not None and pub.current == "v1"
    assert pub.open().files.read("app/index.html") == b"<h1>scores</h1>"


def test_a_store_with_no_layout_keeps_the_registry_in_memory(tmp_path):
    """A provider_factory owns where state lives, so there is nowhere to
    put the file: the registry lives as long as the Store object."""
    store = Store(
        tmp_path, provider_factory=lambda s: KvgitProvider.open(None, session=s)
    )
    assert store._registry_path() is None
    assert store.publications() == {}
    store._registry_write({"scoreboard": {"versions": {}, "current": None}})
    assert list(store.publications()) == ["scoreboard"]
    assert not (tmp_path / "publications.json").exists()
    assert Store(tmp_path).publications() == {}


def test_an_unreadable_registry_says_so(tmp_path):
    (tmp_path).mkdir(parents=True, exist_ok=True)
    (tmp_path / "publications.json").write_text("{not json")
    with pytest.raises(WorkspaceError, match="unreadable"):
        Store(tmp_path).publications()


# -- concurrent publishing ---------------------------------------------------


def test_two_stores_publishing_different_names_both_land(tmp_path):
    """Two Store objects over one path are two handles on one registry
    file. Read-modify-write around the file rather than under a lock
    would drop one record while its branch and tag stayed."""
    stores = [Store(tmp_path), Store(tmp_path)]
    sessions = [seeded(stores[0], "a"), seeded(stores[1], "b")]
    done = []

    def publish(store, ws, name):
        done.append(store.publish(ws, name).name)

    threads = [
        threading.Thread(target=publish, args=(store, ws, name))
        for store, ws, name in zip(stores, sessions, ("first", "second"))
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(done) == ["first", "second"]
    registry = json.loads((tmp_path / "publications.json").read_text())
    assert sorted(registry) == ["first", "second"]
    for name in ("first", "second"):
        assert Store(tmp_path).publication(name).open().files.exists("app/index.html")
    for ws in sessions:
        ws.close()


def test_two_stores_publishing_one_name_get_two_versions(tmp_path):
    """The harder race: both readers see the same version list, so both
    would pick v1 — the version number has to be chosen inside the same
    lock the write lands under."""
    stores = [Store(tmp_path), Store(tmp_path)]
    sessions = [seeded(stores[0], "a"), seeded(stores[1], "b")]
    errors = []

    def publish(store, ws):
        try:
            store.publish(ws, "scoreboard")
        except Exception as e:  # noqa: BLE001 - reported below
            errors.append(e)

    threads = [
        threading.Thread(target=publish, args=(store, ws))
        for store, ws in zip(stores, sessions)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    pub = Store(tmp_path).publication("scoreboard")
    assert [v.version for v in pub.versions] == ["v1", "v2"]
    assert {v.published_from.session for v in pub.versions} == {"a", "b"}
    branches = set(Store(tmp_path)._branches())
    for version in pub.versions:
        assert version.ref.session in branches
    for ws in sessions:
        ws.close()


# -- a retained Publication is not a licence ---------------------------------


def test_a_retained_publication_will_not_open_an_unpublished_version(tmp_path):
    """The Publication object is a snapshot of the registry, not a
    capability: unpublish means unpublish, grace period or not."""
    store = Store(tmp_path)
    ws = seeded(store)
    pub = store.publish(ws, "scoreboard")
    store.unpublish("scoreboard", "v1")  # default grace: commits linger

    with pytest.raises(WorkspaceError, match="No longer published"):
        pub.open()
    with pytest.raises(WorkspaceError, match="No longer published"):
        pub.open("v1")
    with pytest.raises(WorkspaceError, match="No longer published"):
        store.resolve(str(pub.current_version.ref))
    ws.close()


def test_opening_an_unpublished_version_creates_no_branch(tmp_path):
    """kvgit creates a branch opened by an unknown name, so a stale ref
    could resurrect the branch unpublish deleted — and the leftover
    would then block republishing that version."""
    store = Store(tmp_path)
    ws = seeded(store)
    pub = store.publish(ws, "scoreboard")
    store.unpublish("scoreboard", "v1", min_age=0)
    before = set(store._branches())
    assert "@store/pub/scoreboard/v1" not in before

    for attempt in (
        lambda: pub.open(),
        lambda: store.resolve(str(pub.current_version.ref)),
    ):
        with pytest.raises(WorkspaceError):
            attempt()
    assert set(store._branches()) == before

    # ...so the name is free again
    republished = store.publish(ws, "scoreboard", version="v1")
    assert republished.current == "v1"
    assert republished.open().files.read("app/index.html") == b"<h1>scores</h1>"
    ws.close()


def test_resolve_refuses_a_stale_commit_on_a_live_publication(tmp_path):
    """Same version name, different commit: the registry's ref is the
    only one that resolves."""
    store = Store(tmp_path)
    ws = seeded(store)
    stale = store.publish(ws, "scoreboard", version="v1").current_version.ref
    store.unpublish("scoreboard", "v1", min_age=0)
    ws.files.write("/workspace/app/index.html", "<h1>different</h1>")
    ws.commit()
    fresh = store.publish(ws, "scoreboard", version="v1").current_version.ref

    assert stale.commit != fresh.commit
    with pytest.raises(WorkspaceError, match="No longer published"):
        store.resolve(str(stale))
    assert store.resolve(str(fresh)).files.read("app/index.html") == (
        b"<h1>different</h1>"
    )
    ws.close()


# -- provenance is not the caller's to write --------------------------------


def test_info_may_not_overwrite_the_provenance_keys(tmp_path):
    """A false published_from in an immutable commit outlives every
    chance to notice it, so it is refused rather than overridden."""
    store = Store(tmp_path)
    ws = seeded(store)
    for bad in (
        {"published_from": "someone-else@deadbeef"},
        {"tool": "not-publish"},
        {"name": "other"},
        {"version": "v99"},
    ):
        with pytest.raises(ValueError, match="info may not set"):
            store.publish(ws, "scoreboard", info=bad)
    with pytest.raises(ValueError, match="'name', 'version'"):
        store.publish(ws, "scoreboard", info={"version": "v9", "name": "x"})

    # nothing was written by any of those
    assert store.publication("scoreboard") is None
    assert store.tags.list() == {}
    assert store._branches() == ["author"]

    # a key of the caller's own still rides along
    pub = store.publish(ws, "scoreboard", info={"published_by": "the studio"})
    entry = next(iter(pub.open().log()))
    assert entry.info["published_by"] == "the studio"
    assert entry.info["published_from"] == str(ws.ref)
    ws.close()


def test_publishing_an_old_format_state_writes_rows_and_leaves_the_table(tmp_path):
    """A session whose metadata is still the one legacy table publishes
    a row per file like any other: the rows are asked of a filesystem
    over the source commit, and the table — which describes files this
    publication does not hold — never travels."""
    store = Store(tmp_path)
    ws = seeded(store)
    to_legacy_table(ws)
    assert VirtualFS.METADATA_KEY in commit_keys(store, ws.ref)
    assert not [k for k in commit_keys(store, ws.ref) if VirtualFS.is_metadata_key(k)]

    pub = store.publish(ws, "scoreboard")
    keys = commit_keys(store, pub.current_version.ref)

    assert VirtualFS.METADATA_KEY not in keys
    rows = {
        VirtualFS.path_for_metadata_key(k) for k in keys if VirtualFS.is_metadata_key(k)
    }
    assert {"workspace/app/index.html", "workspace/app/api/scores.py"} <= rows
    assert "workspace/notes/private.txt" not in rows

    snapshot = pub.open()
    try:
        assert sorted(snapshot.files.list("app", recursive=True)) == [
            "app/api",
            "app/api/scores.py",
            "app/index.html",
        ]
        body = snapshot.files.read("/workspace/app/index.html")
        assert snapshot.files.fs.stat("/workspace/app/index.html").size == len(body)
    finally:
        snapshot.close()
    ws.close()
