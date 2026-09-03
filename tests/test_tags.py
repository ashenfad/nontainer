"""Tags: naming a checkpoint, the two scopes, frozen snapshots, diff."""

import pytest

from nontainer import (
    CheckpointNotFoundError,
    NotSupportedError,
    Workspace,
    WorkspaceError,
    delete_workspace,
    workspace,
)
from nontainer.providers import KvgitProvider


@pytest.fixture
def kv_ws():
    """Memory-backed kvgit workspace (autocheckpoint on by default)."""
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


@pytest.mark.parametrize("scope", ["session", "store"])
def test_tag_round_trip(kv_ws, scope):
    kv_ws.terminal("echo one > a.txt")
    head = kv_ws.head

    assert kv_ws.tag("v1", info={"by": "ann"}, scope=scope) == head
    assert kv_ws.tags(scope=scope) == {"v1": head}

    info = kv_ws.tag_info("v1", scope=scope)
    assert info.name == "v1" and info.scope == scope
    assert info.id == head
    assert info.tree == kv_ws.head_tree and info.tree is not None
    assert info.time > 0
    assert info.info == {"by": "ann"}
    assert not info.dangling

    kv_ws.delete_tag("v1", scope=scope)
    assert kv_ws.tags(scope=scope) == {}
    assert kv_ws.tag_info("v1", scope=scope) is None


def test_tags_are_immutable(kv_ws):
    kv_ws.terminal("echo one > a.txt")
    kv_ws.tag("v1")
    with pytest.raises(WorkspaceError):
        kv_ws.tag("v1")


def test_delete_unknown_tag_raises(kv_ws):
    with pytest.raises(CheckpointNotFoundError):
        kv_ws.delete_tag("never")


def test_tag_checkpoints_pending_changes(kv_ws):
    kv_ws.fs.write("staged.txt", b"staged")  # host-side: staged, uncommitted
    assert kv_ws.dirty
    named = kv_ws.tag("v1")
    assert not kv_ws.dirty
    assert named == kv_ws.head
    assert list(kv_ws.history())[0].info == {"tool": "tag", "name": "v1"}


def test_scopes_do_not_see_each_other(kv_ws):
    kv_ws.terminal("echo one > a.txt")
    kv_ws.tag("mine", scope="session")
    kv_ws.tag("ours", scope="store")

    assert set(kv_ws.tags()) == {"mine"}
    assert set(kv_ws.tags(scope="store")) == {"ours"}
    assert kv_ws.tag_info("ours") is None
    assert kv_ws.tag_info("mine", scope="store") is None


def test_session_tags_do_not_collide_across_sessions(tmp_path):
    with workspace("alice", store=tmp_path) as alice:
        alice.terminal("echo alice > who.txt")
        alice_commit = alice.tag("v1")
    with workspace("bob", store=tmp_path) as bob:
        bob.terminal("echo bob > who.txt")
        bob_commit = bob.tag("v1")
        assert bob_commit != alice_commit
        assert bob.tags() == {"v1": bob_commit}  # only its own
    with workspace("alice", store=tmp_path) as alice2:
        assert alice2.tags() == {"v1": alice_commit}


def test_store_tags_are_visible_from_another_workspace(tmp_path):
    with workspace("author", store=tmp_path) as author:
        author.terminal("echo published > report.txt")
        published = author.tag("report", scope="store")
    with workspace("reader", store=tmp_path) as reader:
        assert reader.tags(scope="store") == {"report": published}
        assert reader.tags() == {}  # its own session has none


def test_scope_prefixes_cannot_be_spoofed(kv_ws):
    kv_ws.terminal("echo one > a.txt")
    for name in ("store/x", "test-session/x"):
        with pytest.raises(ValueError):
            kv_ws.tag(name)
        with pytest.raises(ValueError):
            kv_ws.tag(name, scope="store")


# -- teardown ----------------------------------------------------------------


def test_delete_workspace_takes_session_tags_and_leaves_store_tags(tmp_path):
    with workspace("doomed", store=tmp_path) as ws:
        ws.terminal("echo x > x.txt")
        ws.tag("mine")
        ws.tag("ours", scope="store")
    with workspace("bystander", store=tmp_path) as other:
        other.terminal("echo y > y.txt")
        other.tag("kept")

    delete_workspace("doomed", store=tmp_path)

    remaining = set(_store_tags(tmp_path))
    assert "doomed/mine" not in remaining
    assert "store/ours" in remaining  # a publication is not session state
    assert "bystander/kept" in remaining  # someone else's session untouched


def test_store_tag_outlives_the_session_that_made_it(tmp_path):
    """The publication property: a store-scoped tag keeps its checkpoint
    readable after the session that made it is deleted — branch, history
    and session tags all gone."""
    with workspace("author", store=tmp_path) as author:
        author.terminal("echo published > report.txt")
        author.tag("report", scope="store")

    delete_workspace("author", store=tmp_path)

    with workspace("reader", store=tmp_path) as reader:
        with reader.at_tag("report", scope="store") as snapshot:
            assert snapshot.terminal("cat report.txt").stdout.strip() == "published"


# -- frozen workspaces -------------------------------------------------------


@pytest.fixture
def snapshot(kv_ws):
    """A frozen workspace at ``v1``, one commit behind its parent."""
    kv_ws.terminal("echo one > a.txt")
    kv_ws.tag("v1")
    kv_ws.terminal("echo two > a.txt; echo new > b.txt")
    snap = kv_ws.at_tag("v1")
    yield snap
    snap.close()


def test_frozen_workspace_reads_the_tagged_state(snapshot):
    assert snapshot.frozen
    assert not snapshot.autocheckpoint
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
        snapshot.write_file("c.txt", "nope")
    with pytest.raises(NotSupportedError, match="frozen"):
        snapshot.edit_file("a.txt", "one", "two")

    assert not snapshot.fs.exists("/workspace/c.txt")


def test_frozen_workspace_refuses_history_writes(snapshot):
    for call in (
        lambda: snapshot.checkpoint(),
        lambda: snapshot.fork("child"),
        lambda: snapshot.tag("v2"),
        lambda: snapshot.delete_tag("v1"),
        lambda: snapshot.rollback(1),
    ):
        with pytest.raises(NotSupportedError, match="frozen"):
            call()


def test_frozen_workspace_leaves_its_parent_alone(kv_ws):
    kv_ws.terminal("echo one > a.txt")
    kv_ws.tag("v1")
    kv_ws.terminal("echo two > a.txt")
    head = kv_ws.head

    with kv_ws.at_tag("v1") as snapshot:
        snapshot.terminal("echo nope > c.txt")  # refused
        snapshot.discard()  # allowed: dropping what could not land

    assert kv_ws.head == head
    assert kv_ws.terminal("cat a.txt").stdout.strip() == "two"
    assert not kv_ws.fs.exists("/workspace/c.txt")


def test_frozen_workspace_serves_a_get_handler(kv_ws):
    """Apps dispatch through a snapshot: the read path an embedder
    publishes an app on."""
    from nontainer.apps import enable_apps, request

    kv_ws.fs.makedirs("/workspace/app/api", exist_ok=True)
    kv_ws.fs.write(
        "/workspace/app/api/note.py",
        b"def get(req):\n    return {'note': open('/workspace/a.txt').read().strip()}\n",
    )
    kv_ws.terminal("echo one > a.txt")
    kv_ws.tag("v1")

    with kv_ws.at_tag("v1") as snapshot:
        runtime = enable_apps(snapshot)
        response = runtime.dispatch(request("GET", "/api/note"))
        assert response.status == 200
        assert "one" in response.text


def test_at_tag_unknown_name(kv_ws):
    kv_ws.terminal("echo one > a.txt")
    with pytest.raises(CheckpointNotFoundError):
        kv_ws.at_tag("never")


# -- diff / changed_since ----------------------------------------------------


def test_changed_since_lists_files_and_no_framework_keys(kv_ws):
    kv_ws.terminal("echo one > a.txt; echo keep > keep.txt")
    first = kv_ws.head
    kv_ws.tag("v1")

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


def test_tree_names_the_content_a_checkpoint_holds(kv_ws):
    """``tree`` travels with the content, not with the commit: the tag,
    the history entry and the head all report one hash for one state,
    and it moves when the files do. Equal trees mean identical content;
    the converse does not hold, because kvgit stamps every write with
    when it happened."""
    kv_ws.terminal("echo one > a.txt")
    tagged = kv_ws.head_tree
    kv_ws.tag("v1")
    assert kv_ws.tag_info("v1").tree == tagged
    assert list(kv_ws.history(limit=1))[0].tree == tagged

    kv_ws.terminal("echo two > a.txt")
    assert kv_ws.head_tree != tagged  # the files moved
    assert kv_ws.tag_info("v1").tree == tagged  # the tag did not


# -- unversioned providers ---------------------------------------------------


def test_dir_workspace_has_no_tags(dir_ws):
    assert not dir_ws.caps.tags
    for call in (
        lambda: dir_ws.tag("v1"),
        lambda: dir_ws.tags(),
        lambda: dir_ws.tag_info("v1"),
        lambda: dir_ws.delete_tag("v1"),
        lambda: dir_ws.at_tag("v1"),
        lambda: dir_ws.diff("a", "b"),
        lambda: dir_ws.changed_since("v1"),
    ):
        with pytest.raises(NotSupportedError):
            call()
