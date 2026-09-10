"""Publications: the app subtree, derived, tagged, and served.

What a publish must NOT carry is as much of the contract as what it
does: the session's cache, its working directory, its ws-git
bookkeeping and its conversation record all stay behind, and the link
back to the session is a soft reference in the commit's info rather
than a parent pointer.
"""

import dataclasses
import json
import threading
from types import MappingProxyType

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
                    "paths": ["app/"],
                    "info": {},
                }
            },
            "meta": {},
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
    # the live cache is session state, not a file: a publication does
    # not carry it, and a frozen open starts with an empty one
    assert dict(snapshot.cache) == {}
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


def test_the_version_row_carries_the_callers_info(tmp_path):
    """Listing published apps with their titles is a registry read: the
    caller's keys ride on the row, so no tag has to be opened for them."""
    store = Store(tmp_path)
    ws = seeded(store)
    store.publish(ws, "scoreboard", info={"title": "Scores", "owner": "ann"})
    ws.close()

    version = Store(tmp_path).publications()["scoreboard"].current_version
    assert version.info == {"title": "Scores", "owner": "ann"}
    assert version.paths == ("app/",)

    row = json.loads((tmp_path / "publications.json").read_text())
    row = row["scoreboard"]["versions"]["v1"]
    assert row["info"] == {"title": "Scores", "owner": "ann"}
    # The provenance keys are row fields already; the info mapping is
    # the caller's own and does not repeat them.
    assert set(row["info"]).isdisjoint({"tool", "name", "version", "published_from"})


def test_a_version_row_written_without_info_reads_as_empty(tmp_path):
    store = Store(tmp_path)
    ws = seeded(store)
    store.publish(ws, "scoreboard")
    ws.close()

    path = tmp_path / "publications.json"
    registry = json.loads(path.read_text())
    row = registry["scoreboard"]["versions"]["v1"]
    row.pop("info")
    row.pop("paths")
    path.write_text(json.dumps(registry))

    version = Store(tmp_path).publication("scoreboard").current_version
    assert version.info == {}
    assert version.paths == ()
    with pytest.raises(TypeError):
        version.info["title"] = "no"


def test_a_version_and_a_publication_stay_hashable(tmp_path):
    """Public frozen records go in sets and dict keys. The info mapping
    holds whatever JSON the caller passed, so it is out of the hash and
    in the comparison."""
    store = Store(tmp_path)
    ws = seeded(store)
    store.publish(ws, "plain")
    store.publish(ws, "tagged", info={"title": "Scores", "labels": ["a", "b"]})
    ws.close()

    pubs = Store(tmp_path).publications()
    for pub in pubs.values():
        assert hash(pub) == hash(pub)
        assert {v: v.version for v in pub.versions}
    assert len({pub for pub in pubs.values()}) == 2

    version = pubs["tagged"].current_version
    other = dataclasses.replace(version, info=MappingProxyType({"title": "Other"}))
    assert version != other
    assert hash(version) == hash(other)


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
    with pytest.raises(ValueError, match="Nothing to publish"):
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
    with pytest.raises(ValueError, match="Version already published"):
        store.publish(ws, "scoreboard", version="beta")
    # the default sequence ignores names it did not mint
    assert store.publish(ws, "scoreboard").current == "v1"
    ws.close()


def test_the_default_version_is_the_next_in_the_v_series(tmp_path):
    """The default version name is a series of its own. A version the
    caller named stands outside it and is never counted, so a lineage
    holding v1, v2 and release-1 gets v3 next."""
    store = Store(tmp_path)
    ws = seeded(store)
    store.publish(ws, "scoreboard")
    store.publish(ws, "scoreboard")
    store.publish(ws, "scoreboard", version="release-1")
    pub = store.publish(ws, "scoreboard")
    assert pub.current == "v3"
    assert [v.version for v in pub.versions] == ["v1", "v2", "v3", "release-1"]
    ws.close()


def test_publish_can_record_a_version_without_taking_the_pointer(tmp_path):
    """current=False records the version and leaves what is served
    alone, so a caller can land the tree first and switch after."""
    store = Store(tmp_path)
    ws = seeded(store)
    store.publish(ws, "scoreboard")
    ws.files.write("/workspace/app/index.html", "<h1>scores v2</h1>")
    ws.commit()

    pub = store.publish(ws, "scoreboard", current=False)
    assert [v.version for v in pub.versions] == ["v1", "v2"]
    assert pub.current == "v1"
    assert pub.open().files.read("app/index.html") == b"<h1>scores</h1>"
    assert pub.open("v2").files.read("app/index.html") == b"<h1>scores v2</h1>"

    promoted = store.set_current("scoreboard", "v2")
    assert promoted.current == "v2"
    ws.close()


def test_the_first_version_is_current_whatever_current_says(tmp_path):
    """A publication must point somewhere, so the version that opens a
    lineage takes the pointer even when the caller declined it."""
    store = Store(tmp_path)
    ws = seeded(store)
    pub = store.publish(ws, "scoreboard", current=False)
    assert pub.current == "v1"
    ws.close()


def test_set_current_refuses_a_version_that_is_not_there(tmp_path):
    store = Store(tmp_path)
    ws = seeded(store)
    store.publish(ws, "scoreboard")
    with pytest.raises(ValueError, match="No such version"):
        store.set_current("scoreboard", "v9")
    with pytest.raises(ValueError, match="No such publication"):
        store.set_current("nope", "v1")
    with pytest.raises(ValueError, match="No such version"):
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


def _registry_write_dies(monkeypatch):
    """Make the registry write raise, so a publish dies exactly where
    its branch and tag are already in the store and its record is
    not — the one window publish cannot make atomic."""

    def boom(self, data):
        raise RuntimeError("the process died writing the registry")

    monkeypatch.setattr(Store, "_registry_write", boom)


def test_a_publish_that_died_before_its_record_is_resumed(tmp_path, monkeypatch):
    store = Store(tmp_path)
    ws = seeded(store)
    _registry_write_dies(monkeypatch)
    with pytest.raises(RuntimeError):
        store.publish(ws, "scoreboard")
    monkeypatch.undo()

    stranded = store.tags.list()["scoreboard/v1"]
    assert store.publication("scoreboard") is None
    assert "@store/pub/scoreboard/v1" in store._branches()

    # the same publish again: the branch is that attempt's, so it is
    # adopted rather than rebuilt or refused
    pub = store.publish(ws, "scoreboard")
    assert [v.version for v in pub.versions] == ["v1"]
    assert pub.current_version.ref.commit == stranded
    assert pub.open().files.read("app/index.html") == b"<h1>scores</h1>"
    assert [b for b in store._branches() if b.startswith("@store/pub/")] == [
        "@store/pub/scoreboard/v1"
    ]
    ws.close()


def test_a_publish_that_died_leaves_a_tag_the_retry_adopts(tmp_path, monkeypatch):
    """The tag is written with the branch, so the retry finds it there
    and keeps it rather than refusing its own name."""
    store = Store(tmp_path)
    ws = seeded(store)
    _registry_write_dies(monkeypatch)
    with pytest.raises(RuntimeError):
        store.publish(ws, "scoreboard", version="beta")
    monkeypatch.undo()

    assert "scoreboard/beta" in store.tags.list()
    pub = store.publish(ws, "scoreboard", version="beta")
    assert pub.current == "beta"
    assert store.tags.list()["scoreboard/beta"] == pub.current_version.ref.commit
    ws.close()


def test_a_resumed_row_records_the_commits_info_not_the_retrys(tmp_path, monkeypatch):
    """The adopted commit is immutable, so the retry's info never landed
    anywhere. Recording it on the row would make Version.info disagree
    with what Publication.open() serves."""
    store = Store(tmp_path)
    ws = seeded(store)
    _registry_write_dies(monkeypatch)
    with pytest.raises(RuntimeError):
        store.publish(ws, "scoreboard", info={"title": "a", "dropped": "yes"})
    monkeypatch.undo()

    pub = store.publish(ws, "scoreboard", info={"title": "b"})
    version = pub.current_version
    assert version.info == {"title": "a", "dropped": "yes"}

    row = json.loads((tmp_path / "publications.json").read_text())
    assert row["scoreboard"]["versions"]["v1"]["info"] == dict(version.info)

    snapshot = pub.open()
    entry = next(iter(snapshot.log()))
    assert entry.info["title"] == "a"
    assert entry.info["dropped"] == "yes"
    snapshot.close()
    ws.close()


def test_a_resumed_tag_carries_the_commits_info_not_the_retrys(tmp_path, monkeypatch):
    """The tag is minted late when the crash came between the commit and
    the tag, and it describes the commit it names."""
    store = Store(tmp_path)
    ws = seeded(store)
    _registry_write_dies(monkeypatch)
    with pytest.raises(RuntimeError):
        store.publish(ws, "scoreboard", info={"title": "a"})
    monkeypatch.undo()
    store.tags.delete("scoreboard/v1")

    store.publish(ws, "scoreboard", info={"title": "b"})
    assert store.tags.info("scoreboard/v1").info["title"] == "a"
    ws.close()


def test_a_stranded_branch_from_another_attempt_is_refused_and_cleared(
    tmp_path, monkeypatch
):
    """A recordless branch whose commit is not this publish's is not
    adopted: the message names the branch and unpublish clears it."""
    store = Store(tmp_path)
    ws = seeded(store)
    _registry_write_dies(monkeypatch)
    with pytest.raises(RuntimeError):
        store.publish(ws, "scoreboard", version="v1")
    monkeypatch.undo()

    ws.files.write("/workspace/app/index.html", "<h1>different</h1>")
    ws.commit()
    with pytest.raises(WorkspaceError, match="@store/pub/scoreboard/v1"):
        store.publish(ws, "scoreboard", version="v1")

    # no record names it, and unpublish takes it out anyway
    assert store.publication("scoreboard") is None
    store.unpublish("scoreboard", "v1", min_age=0)
    assert "@store/pub/scoreboard/v1" not in store._branches()
    assert store.tags.list() == {}

    fresh = store.publish(ws, "scoreboard", version="v1")
    assert fresh.open().files.read("app/index.html") == b"<h1>different</h1>"
    ws.close()


def test_publishing_other_paths_is_a_different_attempt(tmp_path, monkeypatch):
    """Adoption matches on what was published as well as where from, so
    a retry that widened paths= is refused rather than served the
    narrower tree."""
    store = Store(tmp_path)
    ws = seeded(store)
    _registry_write_dies(monkeypatch)
    with pytest.raises(RuntimeError):
        store.publish(ws, "scoreboard", version="v1", paths=("app/",))
    monkeypatch.undo()

    with pytest.raises(WorkspaceError, match="@store/pub/scoreboard/v1"):
        store.publish(ws, "scoreboard", version="v1", paths=("app/", "notes/"))
    ws.close()


def test_unpublish_leaves_a_store_tag_it_did_not_publish_alone(tmp_path):
    """A store tag may hold a slash, so 'release/prod' is a version of
    'release' by name alone. Only what carries publish's own provenance
    is cleared; anything else is left where it is."""
    store = Store(tmp_path)
    ws = seeded(store)
    tagged = store.tags.add(ws, "release/prod")

    with pytest.raises(ValueError, match="release/prod"):
        store.unpublish("release", "prod", min_age=0)

    assert store.tags.list() == {"release/prod": tagged}
    at = store.tags.at("release/prod")
    assert at.files.read("/workspace/app/index.html") == b"<h1>scores</h1>"
    at.close()
    ws.close()


def test_unpublish_clears_a_stranded_branch_whose_tag_never_landed(
    tmp_path, monkeypatch
):
    """The commit lands before the tag, so an attempt can die with a
    branch and no tag at all. The branch's own info is what proves it
    came from a publish."""
    store = Store(tmp_path)
    ws = seeded(store)
    _registry_write_dies(monkeypatch)
    with pytest.raises(RuntimeError):
        store.publish(ws, "scoreboard")
    monkeypatch.undo()
    store.tags.delete("scoreboard/v1")

    assert store.tags.list() == {}
    assert "@store/pub/scoreboard/v1" in store._branches()
    store.unpublish("scoreboard", "v1", min_age=0)
    assert "@store/pub/scoreboard/v1" not in store._branches()
    ws.close()


def test_unpublish_refuses_what_is_not_there(tmp_path):
    store = Store(tmp_path)
    ws = seeded(store)
    store.publish(ws, "scoreboard")
    with pytest.raises(ValueError, match="No such version"):
        store.unpublish("scoreboard", "v9")
    with pytest.raises(ValueError, match="No such publication"):
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


# -- create_only -------------------------------------------------------------


def test_create_only_opens_a_lineage(tmp_path):
    store = Store(tmp_path)
    ws = seeded(store)
    pub = store.publish(ws, "scoreboard", create_only=True)
    assert [v.version for v in pub.versions] == ["v1"]
    assert pub.current == "v1"
    ws.close()


def test_create_only_refuses_a_name_that_is_already_published(tmp_path):
    """The refusal lands before anything is written: no branch, no tag,
    no commit, no record."""
    store = Store(tmp_path)
    ws = seeded(store)
    store.publish(ws, "scoreboard")

    registry = json.loads((tmp_path / "publications.json").read_text())
    branches = sorted(store._branches())
    tags = dict(store.tags.list())

    with pytest.raises(ValueError, match="already published"):
        store.publish(ws, "scoreboard", create_only=True)

    assert json.loads((tmp_path / "publications.json").read_text()) == registry
    assert sorted(store._branches()) == branches
    assert dict(store.tags.list()) == tags
    ws.close()


def test_create_only_refuses_a_name_whose_pointer_moved_off_v1(tmp_path):
    """Any version of the name counts, not just the current one."""
    store = Store(tmp_path)
    ws = seeded(store)
    store.publish(ws, "scoreboard", current=False, version="draft")
    with pytest.raises(ValueError, match="already published"):
        store.publish(ws, "scoreboard", create_only=True)
    ws.close()


def test_create_only_lets_exactly_one_of_two_racing_publishes_win(tmp_path):
    """Two workers that each checked the name was free: without this the
    loser silently publishes a second version of someone else's app."""
    stores = [Store(tmp_path), Store(tmp_path)]
    sessions = [seeded(stores[0], "a"), seeded(stores[1], "b")]
    gate = threading.Barrier(2)
    landed = []
    errors = []

    def publish(store, ws):
        gate.wait()
        try:
            landed.append(store.publish(ws, "scoreboard", create_only=True))
        except ValueError as e:
            errors.append(e)

    threads = [
        threading.Thread(target=publish, args=(store, ws))
        for store, ws in zip(stores, sessions)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(landed) == 1 and len(errors) == 1
    pub = Store(tmp_path).publication("scoreboard")
    assert [v.version for v in pub.versions] == ["v1"]
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
        {"paths": ["everything/"]},
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


def test_a_publication_ref_is_a_tag_source(tmp_path):
    """A ref nontainer hands out is a ref nontainer takes back: the
    version's own ref names a commit on a reserved branch, and naming
    that commit store-scoped is a read of the branch and a write of the
    tag."""
    store = Store(tmp_path)
    ws = seeded(store)
    version = store.publish(ws, "scoreboard").current_version
    ws.close()

    commit = store.tags.add(version.ref, "snap", info={"by": "ann"})

    assert commit == version.ref.commit
    info = store.tags.info("snap")
    assert info is not None
    assert info.id == version.ref.commit
    assert info.info["by"] == "ann"

    with store.resolve(version.ref) as resolved:
        assert resolved.files.read("app/index.html") == b"<h1>scores</h1>"
    with store.tags.at("snap") as snap:
        assert snap.files.read("app/index.html") == b"<h1>scores</h1>"


def test_a_publication_ref_spelled_as_a_string_is_a_tag_source(tmp_path):
    """The string spelling of that ref names the same commit."""
    store = Store(tmp_path)
    ws = seeded(store)
    version = store.publish(ws, "scoreboard").current_version
    ws.close()

    assert store.tags.add(str(version.ref), "snap") == version.ref.commit


def test_tagging_an_unpublished_reserved_branch_is_refused(tmp_path):
    """A reserved branch the registry does not hold is not a tag
    source, and the refusal says so instead of minting the branch."""
    store = Store(tmp_path)
    ws = seeded(store)
    version = store.publish(ws, "scoreboard").current_version
    ws.close()

    made_up = Ref(session="@store/pub/nothing/v1", commit=version.ref.commit)
    with pytest.raises(WorkspaceError, match="No longer published"):
        store.tags.add(made_up, "snap")
    assert store.tags.list() == {"scoreboard/v1": version.ref.commit}
    assert "@store/pub/nothing/v1" not in store._branches()


def test_publication_meta_is_set_read_and_replaced_whole(tmp_path):
    """The mutable half of a publication that is not a version pointer:
    a mapping on the row, replaced entire, read back from both listing
    verbs."""
    store = Store(tmp_path)
    ws = seeded(store)
    store.publish(ws, "scoreboard")
    ws.close()

    assert store.publication("scoreboard").meta == {}

    store.set_meta("scoreboard", {"title": "Scores", "owner": "ann"})

    assert store.publication("scoreboard").meta == {"title": "Scores", "owner": "ann"}
    assert store.publications()["scoreboard"].meta["title"] == "Scores"
    assert Store(tmp_path).publication("scoreboard").meta["owner"] == "ann"

    store.set_meta("scoreboard", {"title": "Scoreboard"})

    assert store.publication("scoreboard").meta == {"title": "Scoreboard"}


def test_publication_meta_is_a_read_only_mapping(tmp_path):
    """A Publication is a record of the registry, not a handle on it:
    writing through .meta is refused, and a snapshot taken before a
    set_meta keeps what it was fetched with."""
    store = Store(tmp_path)
    ws = seeded(store)
    store.publish(ws, "scoreboard")
    ws.close()
    store.set_meta("scoreboard", {"title": "Scores"})

    before = store.publication("scoreboard")
    store.set_meta("scoreboard", {"title": "Renamed"})

    assert before.meta == {"title": "Scores"}
    assert store.publication("scoreboard").meta == {"title": "Renamed"}
    with pytest.raises(TypeError):
        before.meta["title"] = "nope"


def test_publication_meta_survives_a_later_publish(tmp_path):
    """Publishing a version says nothing about the publication's own
    metadata, so it is left exactly as it was — and a new lineage
    starts empty."""
    store = Store(tmp_path)
    ws = seeded(store)
    store.publish(ws, "scoreboard")
    store.set_meta("scoreboard", {"title": "Scores"})

    pub = store.publish(ws, "scoreboard")

    assert pub.meta == {"title": "Scores"}
    assert [v.version for v in pub.versions] == ["v1", "v2"]
    assert store.publish(ws, "other").meta == {}
    ws.close()


def test_publication_meta_goes_with_the_last_unpublish(tmp_path):
    """Metadata belongs to the row, and the last version takes the row
    — so republishing the name starts empty rather than inheriting what
    the old publication was called."""
    store = Store(tmp_path)
    ws = seeded(store)
    store.publish(ws, "scoreboard")
    store.publish(ws, "scoreboard", current=False)
    store.set_meta("scoreboard", {"title": "Scores"})

    store.unpublish("scoreboard", "v2")
    assert store.publication("scoreboard").meta == {"title": "Scores"}

    store.unpublish("scoreboard", "v1")
    assert store.publication("scoreboard") is None
    assert store.publish(ws, "scoreboard").meta == {}
    ws.close()


def test_publication_meta_of_an_old_row_reads_as_empty(tmp_path):
    """A registry written before the field existed has no meta key, and
    reads as an empty mapping rather than raising."""
    store = Store(tmp_path)
    ws = seeded(store)
    store.publish(ws, "scoreboard")
    ws.close()

    path = tmp_path / "publications.json"
    registry = json.loads(path.read_text())
    registry["scoreboard"].pop("meta", None)
    path.write_text(json.dumps(registry))

    assert Store(tmp_path).publication("scoreboard").meta == {}


def test_set_meta_refuses_a_bad_value_or_an_unknown_name(tmp_path):
    """The caller's mistakes, all ValueError: not a mapping, not JSON,
    no such publication."""
    store = Store(tmp_path)
    ws = seeded(store)
    store.publish(ws, "scoreboard")
    ws.close()

    for bad in ("title", [("title", "Scores")], None, 7):
        with pytest.raises(ValueError, match="mapping"):
            store.set_meta("scoreboard", bad)
    with pytest.raises(ValueError, match="JSON"):
        store.set_meta("scoreboard", {"handler": object()})
    with pytest.raises(ValueError, match="No such publication"):
        store.set_meta("nothing", {"title": "Scores"})

    assert store.publication("scoreboard").meta == {}


def test_concurrent_set_meta_keeps_the_rest_of_the_registry(tmp_path):
    """Two writers replacing two publications' metadata at once: the
    registry is read, changed and written under one lock, so neither
    call drops the other's row, versions or pointer."""
    stores = [Store(tmp_path), Store(tmp_path)]
    ws = seeded(stores[0])
    for name in ("first", "second"):
        stores[0].publish(ws, name)
    ws.close()

    ready = threading.Barrier(2)
    errors = []

    def rename(store, name):
        try:
            ready.wait()
            store.set_meta(name, {"title": name.title()})
        except Exception as e:  # noqa: BLE001 - reported below
            errors.append(e)

    threads = [
        threading.Thread(target=rename, args=(store, name))
        for store, name in zip(stores, ("first", "second"))
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    fresh = Store(tmp_path).publications()
    assert sorted(fresh) == ["first", "second"]
    for name in ("first", "second"):
        assert fresh[name].meta == {"title": name.title()}
        assert [v.version for v in fresh[name].versions] == ["v1"]
        assert fresh[name].current == "v1"
