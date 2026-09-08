"""Tags: naming a commit, the two scopes, frozen snapshots, diff."""

import pytest

from nontainer import (
    CommitNotFoundError,
    NotSupportedError,
    Store,
    Workspace,
    WorkspaceError,
    workspace,
)
from nontainer.providers import KvgitProvider


@pytest.fixture
def kv_ws():
    """Memory-backed kvgit workspace (autocommit on by default)."""
    ws = Workspace(KvgitProvider.open(None, session="test-session"))
    yield ws
    ws.close()


def _store_tags(tmp_path):
    """Every tag in the store, as kvgit stores it (prefixes included)."""
    import kvgit

    handle = kvgit.store(kind="disk", path=str(tmp_path / "kvgit"), branch="probe")
    try:
        return handle.tags()
    finally:
        close = getattr(handle.versioned.store, "close", None)
        if callable(close):
            close()


# -- round trip, both scopes -------------------------------------------------


def test_session_tag_round_trip(kv_ws):
    kv_ws.terminal("echo one > a.txt")
    head = kv_ws.head

    assert kv_ws.tags.add("v1", info={"by": "ann"}) == head
    assert kv_ws.tags.list() == {"v1": head}

    info = kv_ws.tags.info("v1")
    assert info.name == "v1" and info.scope == "session"
    assert info.id == head
    assert info.tree == next(iter(kv_ws.log(limit=1))).tree and info.tree is not None
    assert info.time > 0
    assert info.info == {"by": "ann"}
    assert not info.dangling

    kv_ws.tags.delete("v1")
    assert kv_ws.tags.list() == {}
    assert kv_ws.tags.info("v1") is None


def test_store_tag_round_trip(tmp_path):
    store = Store(tmp_path)
    with store.open("author") as ws:
        ws.terminal("echo one > a.txt")
        head = ws.head

        assert store.tags.add(ws, "v1", info={"by": "ann"}) == head
        assert store.tags.list() == {"v1": head}

        info = store.tags.info("v1")
        assert info.name == "v1" and info.scope == "store"
        assert info.id == head
        assert info.tree == next(iter(ws.log(limit=1))).tree and info.tree is not None
        assert info.time > 0
        assert info.info == {"by": "ann"}
        assert not info.dangling

    store.tags.delete("v1")
    assert store.tags.list() == {}
    assert store.tags.info("v1") is None


def test_tags_are_immutable(kv_ws):
    kv_ws.terminal("echo one > a.txt")
    kv_ws.tags.add("v1")
    with pytest.raises(WorkspaceError):
        kv_ws.tags.add("v1")


@pytest.mark.parametrize("name", ["v1", "@store/x", "test-session/x", ""])
def test_a_refused_tag_commits_nothing(name):
    """Everything checkable is checked before the commit a dirty
    workspace needs: a refused name must not leave the history — a
    whole turn's worth of it, in turn-granularity mode — advanced."""
    ws = Workspace(KvgitProvider.open(None, session="test-session"))
    try:
        ws.terminal("echo one > a.txt")
        ws.tags.add("v1")
        ws.autocommit = False
        ws.terminal("echo two > a.txt")
        head, dirty = ws.head, ws.uncommitted
        assert dirty

        with pytest.raises((WorkspaceError, ValueError)):
            ws.tags.add(name)

        assert ws.head == head
        assert ws.uncommitted
    finally:
        ws.close()


def test_delete_unknown_tag_raises(kv_ws):
    with pytest.raises(CommitNotFoundError):
        kv_ws.tags.delete("never")


def test_tag_commits_pending_changes(kv_ws):
    kv_ws.files.fs.write("staged.txt", b"staged")  # host-side: staged, uncommitted
    assert kv_ws.uncommitted
    named = kv_ws.tags.add("v1")
    assert not kv_ws.uncommitted
    assert named == kv_ws.head
    assert list(kv_ws.log())[0].info == {"tool": "tag", "name": "v1"}


def test_scopes_do_not_see_each_other(tmp_path):
    store = Store(tmp_path)
    with store.open("test-session") as ws:
        ws.terminal("echo one > a.txt")
        ws.tags.add("mine")
        store.tags.add(ws, "ours")

        assert set(ws.tags.list()) == {"mine"}
        assert set(store.tags.list()) == {"ours"}
        assert ws.tags.info("ours") is None
        assert store.tags.info("mine") is None


def test_session_tags_do_not_collide_across_sessions(tmp_path):
    with workspace("alice", store=tmp_path) as alice:
        alice.terminal("echo alice > who.txt")
        alice_commit = alice.tags.add("v1")
    with workspace("bob", store=tmp_path) as bob:
        bob.terminal("echo bob > who.txt")
        bob_commit = bob.tags.add("v1")
        assert bob_commit != alice_commit
        assert bob.tags.list() == {"v1": bob_commit}  # only its own
    with workspace("alice", store=tmp_path) as alice2:
        assert alice2.tags.list() == {"v1": alice_commit}


def test_store_tags_are_visible_from_another_workspace(tmp_path):
    store = Store(tmp_path)
    with store.open("author") as author:
        author.terminal("echo published > report.txt")
        published = store.tags.add(author, "report")
    with store.open("reader") as reader:
        assert store.tags.list() == {"report": published}
        assert reader.tags.list() == {}  # its own session has none


def test_scope_prefixes_cannot_be_spoofed(tmp_path):
    store = Store(tmp_path)
    with store.open("test-session") as ws:
        ws.terminal("echo one > a.txt")
        for name in ("@store/x", "test-session/x"):
            with pytest.raises(ValueError):
                ws.tags.add(name)
            with pytest.raises(ValueError):
                store.tags.add(ws, name)


def test_no_session_can_be_named_into_the_store_scope(tmp_path):
    """The store prefix starts with ``@``, which a session id cannot,
    so the scopes stay separate however sessions are named."""
    store = Store(tmp_path)
    with store.open("store") as ws:
        ws.terminal("echo x > x.txt")
        ws.tags.add("mine")
        assert store.tags.list() == {}

    with store.open("reader") as reader:
        reader.terminal("echo y > y.txt")
        store.tags.add(reader, "ours")

    store.delete("store")
    remaining = set(_store_tags(tmp_path))
    assert "store/mine" not in remaining  # the session's own tag went
    assert "@store/ours" in remaining  # the publication stayed


# -- teardown ----------------------------------------------------------------


def test_store_delete_takes_session_tags_and_leaves_store_tags(tmp_path):
    store = Store(tmp_path)
    with store.open("doomed") as ws:
        ws.terminal("echo x > x.txt")
        ws.tags.add("mine")
        store.tags.add(ws, "ours")
    with store.open("bystander") as other:
        other.terminal("echo y > y.txt")
        other.tags.add("kept")

    store.delete("doomed")

    remaining = set(_store_tags(tmp_path))
    assert "doomed/mine" not in remaining
    assert "@store/ours" in remaining  # a publication is not session state
    assert "bystander/kept" in remaining  # someone else's session untouched


def test_store_tag_outlives_the_session_that_made_it(tmp_path):
    """The publication property: a store-scoped tag keeps its commit
    readable after the session that made it is deleted — branch, history
    and session tags all gone."""
    store = Store(tmp_path)
    with store.open("author") as author:
        author.terminal("echo published > report.txt")
        store.tags.add(author, "report")

    store.delete("author")

    # A store tag belongs to no session, but kvgit has no handle
    # without a branch: the read anchors on whatever session is open.
    with store.open("reader"), store.tags.at("report") as snapshot:
        assert snapshot.terminal("cat report.txt").stdout.strip() == "published"


# -- frozen workspaces -------------------------------------------------------


@pytest.fixture
def snapshot(kv_ws):
    """A frozen workspace at ``v1``, one commit behind its parent."""
    kv_ws.terminal("echo one > a.txt")
    kv_ws.tags.add("v1")
    kv_ws.terminal("echo two > a.txt; echo new > b.txt")
    snap = kv_ws.tags.at("v1")
    yield snap
    snap.close()


def test_frozen_workspace_reads_the_tagged_state(snapshot):
    assert snapshot.frozen
    assert not snapshot.autocommit
    assert snapshot.terminal("cat a.txt").stdout.strip() == "one"
    assert not snapshot.terminal("cat b.txt")  # written after the tag
    assert (
        snapshot.run_python(
            "print(open('/workspace/a.txt').read().strip())"
        ).stdout.strip()
        == "one"
    )


def test_frozen_workspace_refuses_tool_writes(snapshot):
    shell = snapshot.terminal("echo nope > c.txt")
    assert shell.exit_code != 0
    assert "frozen snapshot at tag 'v1'" in shell.stderr

    py = snapshot.run_python("open('/workspace/c.txt', 'w').write('nope')")
    assert "frozen snapshot at tag 'v1'" in py.error

    with pytest.raises(NotSupportedError, match="frozen"):
        snapshot.files.write("c.txt", "nope")
    with pytest.raises(NotSupportedError, match="frozen"):
        snapshot.files.edit("a.txt", "one", "two")

    assert not snapshot.files.fs.exists("/workspace/c.txt")


def test_frozen_workspace_refuses_history_writes(snapshot):
    for call in (
        lambda: snapshot.commit(),
        lambda: snapshot.fork("child"),
        lambda: snapshot.tags.add("v2"),
        lambda: snapshot.tags.delete("v1"),
        lambda: snapshot.rollback(1),
    ):
        with pytest.raises(NotSupportedError, match="frozen"):
            call()


def test_frozen_workspace_leaves_its_parent_alone(kv_ws):
    kv_ws.terminal("echo one > a.txt")
    kv_ws.tags.add("v1")
    kv_ws.terminal("echo two > a.txt")
    head = kv_ws.head

    with kv_ws.tags.at("v1") as snapshot:
        snapshot.terminal("echo nope > c.txt")  # refused
        snapshot.discard()  # allowed: dropping what could not land

    assert kv_ws.head == head
    assert kv_ws.terminal("cat a.txt").stdout.strip() == "two"
    assert not kv_ws.files.fs.exists("/workspace/c.txt")


def test_frozen_workspace_serves_a_get_handler(kv_ws):
    """Apps dispatch through a snapshot: the read path an embedder
    publishes an app on."""
    from nontainer.apps import enable_apps, request

    kv_ws.files.fs.makedirs("/workspace/app/api", exist_ok=True)
    kv_ws.files.fs.write(
        "/workspace/app/api/note.py",
        b"def get(req):\n    return {'note': open('/workspace/a.txt').read().strip()}\n",
    )
    kv_ws.terminal("echo one > a.txt")
    kv_ws.tags.add("v1")

    with kv_ws.tags.at("v1") as snapshot:
        runtime = enable_apps(snapshot)
        response = runtime.dispatch(request("GET", "/api/note"))
        assert response.status == 200
        assert "one" in response.text


def test_frozen_workspace_refuses_a_cache_write(snapshot):
    result = snapshot.run_python("cache['x'] = 1")
    assert "frozen snapshot at tag 'v1'" in result.error
    assert not snapshot.uncommitted

    with pytest.raises(PermissionError, match="frozen snapshot"):
        snapshot.cache["x"] = 1
    assert "x" not in snapshot.cache


def test_frozen_refusal_survives_a_remote_executor(tmp_path):
    """An executor with its own substrate writes before anything here
    can stop it, so the workspace refuses the harvest instead: the
    frozen state is untouched, the call says so, and the next call
    reads the frozen bytes again."""
    pytest.importorskip("dud")
    from nontainer.executor_dud import DudExecutor

    def dud():
        return DudExecutor(backend="subprocess")

    parent = Workspace(
        KvgitProvider.open(tmp_path / "kvgit", session="remote"),
        executor_factory=dud,
    )
    try:
        parent.terminal("echo one > a.txt")
        parent.tags.add("v1")
        tagged = parent.head
    finally:
        parent.close()

    reader = Workspace(
        KvgitProvider.open(tmp_path / "kvgit", session="remote"),
        executor_factory=dud,  # a factory, so the snapshot gets one too
    )
    try:
        with reader.tags.at("v1") as snapshot:
            assert type(snapshot.runtime.executor) is DudExecutor
            write = snapshot.terminal("echo changed > a.txt; echo new > b.txt")
            assert write.exit_code != 0
            assert "frozen snapshot at tag 'v1'" in write.stderr
            assert not snapshot.uncommitted

            assert snapshot.terminal("cat a.txt").stdout.strip() == "one"
            assert not snapshot.files.fs.exists("/workspace/b.txt")
            assert snapshot.head == tagged

            cache_write = snapshot.run_python("cache['x'] = 1")
            assert "frozen snapshot at tag 'v1'" in cache_write.error
    finally:
        reader.close()


def test_at_tag_unknown_name(kv_ws):
    kv_ws.terminal("echo one > a.txt")
    with pytest.raises(CommitNotFoundError):
        kv_ws.tags.at("never")


# -- diff / changed_since ----------------------------------------------------


def test_changed_since_lists_files_and_no_framework_keys(kv_ws):
    kv_ws.terminal("echo one > a.txt; echo keep > keep.txt")
    first = kv_ws.head
    kv_ws.tags.add("v1")

    kv_ws.terminal("echo two > a.txt; echo new > b.txt; rm keep.txt")
    kv_ws.run_python("cache['n'] = 1")  # cache + cwd are not files

    expected = {
        "added": frozenset({"/workspace/b.txt"}),
        "removed": frozenset({"/workspace/keep.txt"}),
        "modified": frozenset({"/workspace/a.txt"}),
    }
    for diff in (kv_ws.changed_since(first), kv_ws.changed_since("v1")):
        assert diff.added == expected["added"]
        assert diff.removed == expected["removed"]
        assert diff.modified == expected["modified"]

    assert kv_ws.diff(first, kv_ws.head) == kv_ws.changed_since("v1")


def test_tree_names_the_content_a_commit_holds(kv_ws):
    """``tree`` travels with the content, not with the commit: the tag,
    the history entry and the head all report one hash for one state,
    and it moves when the files do. Equal trees mean identical content;
    the converse does not hold, because kvgit stamps every write with
    when it happened."""
    kv_ws.terminal("echo one > a.txt")
    tagged = next(iter(kv_ws.log(limit=1))).tree
    kv_ws.tags.add("v1")
    assert kv_ws.tags.info("v1").tree == tagged
    assert list(kv_ws.log(limit=1))[0].tree == tagged

    kv_ws.terminal("echo two > a.txt")
    assert next(iter(kv_ws.log(limit=1))).tree != tagged  # the files moved
    assert kv_ws.tags.info("v1").tree == tagged  # the tag did not


def test_a_same_bytes_rewrite_is_not_a_change(kv_ws):
    """``changed_since`` answers the content question: re-saving a file
    with the bytes it already had changed nothing, however many commits
    it took. ``tree`` still moves — it identifies the write, not the
    content."""
    kv_ws.terminal("echo one > a.txt")
    published = kv_ws.head
    tree = next(iter(kv_ws.log(limit=1))).tree

    kv_ws.terminal("echo one > a.txt")  # identical content, new commit

    assert kv_ws.head != published
    assert next(iter(kv_ws.log(limit=1))).tree != tree
    diff = kv_ws.changed_since(published)
    assert not (diff.added or diff.removed or diff.modified)


# -- unversioned providers ---------------------------------------------------


def test_dir_workspace_has_no_tags(dir_ws):
    assert not dir_ws.caps.tags
    for call in (
        lambda: dir_ws.tags.add("v1"),
        lambda: dir_ws.tags.list(),
        lambda: dir_ws.tags.info("v1"),
        lambda: dir_ws.tags.delete("v1"),
        lambda: dir_ws.tags.at("v1"),
        lambda: dir_ws.diff("a", "b"),
        lambda: dir_ws.changed_since("v1"),
    ):
        with pytest.raises(NotSupportedError):
            call()


def test_fork_without_at_works_for_a_provider_that_predates_at(tmp_path):
    """A provider written to the older fork(name) shape keeps working
    for the call that has no ``at``."""
    from nontainer import Workspace, workspace

    class OldShape:
        def __init__(self, inner):
            self._inner = inner
            self.calls = []

        def fork(self, name):
            self.calls.append(name)
            return self._inner.fork(name)

        def __getattr__(self, item):
            return getattr(self._inner, item)

    ws = workspace("parent", store=tmp_path)
    ws.files.fs.write("/workspace/a.txt", b"A")
    ws.commit()
    wrapped = Workspace(OldShape(ws._provider))
    child = wrapped.fork("child")
    try:
        assert wrapped._provider.calls == ["child"]
        assert child.files.fs.read("/workspace/a.txt") == b"A"
    finally:
        child.close()
        wrapped.close()
        ws.close()


def test_a_frozen_snapshot_refuses_a_handler_file_write_under_process_isolation(
    tmp_path,
):
    """Under isolation="process" the worker pushes a written file at
    close; sandtrap refuses it at open, so a frozen snapshot's handler
    that writes a file fails loudly instead of silently succeeding."""
    pytest.importorskip("sandtrap")
    from nontainer import PythonConfig, workspace
    from nontainer.apps import AppRuntime, Request

    ws = workspace("s1", store=tmp_path, python=PythonConfig(isolation="process"))
    ws.files.fs.makedirs("/workspace/app/api", exist_ok=True)
    ws.files.fs.write(
        "/workspace/app/api/note.py",
        b"def post(req):\n"
        b"    open('/workspace/app/scribble.txt', 'w').write('nope')\n"
        b"    return {'ok': True}\n",
    )
    ws.commit()
    ws.tags.add("v1")
    snap = ws.tags.at("v1")
    try:
        resp = AppRuntime(snap, frozen=True).dispatch(
            Request(method="POST", path="/api/note")
        )
        assert resp.status == 500
        assert not snap.files.fs.exists("/workspace/app/scribble.txt")
    finally:
        snap.close()
        ws.close()


def test_the_two_tag_namespaces_are_reached_through_their_owners(tmp_path):
    """Which object holds the verb IS the scope: a session's tags are
    invisible to every other session, a store's are visible from all of
    them, and neither surface takes a scope argument any more."""
    store = Store(tmp_path)
    with store.open("alice") as alice, store.open("bob") as bob:
        alice.terminal("echo alice > who.txt")
        mine = alice.tags.add("v1")
        ours = store.tags.add(alice, "release")

        # the session scope: bob's own listing is empty, and a name
        # alice holds means nothing to him
        assert alice.tags.list() == {"v1": mine}
        assert bob.tags.list() == {}
        assert bob.tags.info("v1") is None
        with pytest.raises(CommitNotFoundError):
            bob.tags.delete("v1")

        # the store scope: one listing, readable from either session
        assert store.tags.list() == {"release": ours}
        assert store.tags.info("release").id == ours

        # bob may hold his own "v1" — different tag, same name
        bob.terminal("echo bob > who.txt")
        assert bob.tags.add("v1") != mine

        # the scope argument is gone: the object is the scope
        with pytest.raises(TypeError):
            alice.tags.add("v2", scope="store")
