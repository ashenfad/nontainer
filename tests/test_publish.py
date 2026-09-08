"""Publications: the app subtree, derived, tagged, and served.

What a publish must NOT carry is as much of the contract as what it
does: the session's cache, its working directory, its ws-git
bookkeeping and its conversation record all stay behind, and the link
back to the session is a soft reference in the commit's info rather
than a parent pointer.
"""

import json

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

    assert VirtualFS.METADATA_KEY in keys
    files = [k for k in keys if k != VirtualFS.METADATA_KEY]
    # exactly the two app files, and they are file keys
    assert len(files) == 2
    assert all(k.startswith(VirtualFS.PREFIX) for k in files)
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


def test_store_resolve_takes_a_version_ref(tmp_path):
    store = Store(tmp_path)
    ws = seeded(store)
    version = store.publish(ws, "scoreboard").current_version
    resolved = store.resolve(str(version.ref))
    assert resolved.frozen
    assert resolved.files.read("app/index.html") == b"<h1>scores</h1>"
    resolved.close()
    with pytest.raises(WorkspaceError, match="No such session"):
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
    # because the publication brings its own branch.
    anchored = store.tags.at("scoreboard/v1")
    assert anchored.files.read("app/index.html") == b"<h1>scores</h1>"
    anchored.close()


# -- refusals ---------------------------------------------------------------


def test_a_dirty_workspace_is_refused(tmp_path):
    store = Store(tmp_path)
    ws = seeded(store)
    ws.autocommit = False
    ws.files.write("/workspace/app/index.html", "<h1>uncommitted</h1>")
    assert ws.dirty
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
