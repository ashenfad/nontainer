"""revert and cherry-pick on the facade: one engine, two signs.

Applying one commit's change to the tree as it stands now, with the
base named rather than found: the commit and its parent in one order
undo it, in the other order bring it from somewhere else. History
stays append-only — both verbs APPEND a commit, and the commit they
name is still in the log afterwards.
"""

import pytest

from nontainer import NotSupportedError, Store, WorkspaceError
from nontainer.wsgit import register_wsgit


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "store")
    yield s
    s.close()


@pytest.fixture
def ws(store):
    w = store.open("main")
    register_wsgit(w)
    yield w
    w.close()


def _fork(ws, name: str, **kwargs):
    child = ws.fork(name, **kwargs)
    assert "ws-git" in child.runtime.commands
    return child


def test_revert_of_the_last_commit_appends_and_keeps_it(ws):
    ws.files.write("/workspace/a.txt", "one\n")
    ws.index.commit("first")
    first = ws.index.head
    ws.files.write("/workspace/a.txt", "two\n")
    ws.index.commit("second")
    second = ws.index.head

    out = ws.revert(second)

    assert out.merged and out.conflicts == ()
    assert out.auto_merged == ("/workspace/a.txt",)
    assert ws.files.read("/workspace/a.txt") == b"one\n"
    # A new commit, and the reverted one is still where it was.
    log = [e.id for e in ws.index.log()]
    assert log[0] == out.commit != second
    assert log[1:] == [second, first]
    assert ws.index.head == out.commit
    entry = next(e for e in ws.log(kind="agent") if e.id == out.commit)
    assert entry.info["reverted"] == second
    assert entry.parents != (second,)  # a soft reference, not a parent


def test_revert_of_an_older_commit_leaves_later_work_alone(ws):
    ws.files.write("/workspace/a.txt", "one\ntwo\nthree\n")
    ws.index.commit("first")
    ws.files.write("/workspace/a.txt", "one\nTWO\nthree\n")
    ws.index.commit("second")
    second = ws.index.head
    ws.files.write("/workspace/a.txt", "one\nTWO\nTHREE\n")
    ws.index.commit("third")

    out = ws.revert(second)

    assert out.merged and out.conflicts == ()
    assert ws.files.read("/workspace/a.txt") == b"one\ntwo\nTHREE\n"


def test_a_revert_that_conflicts_leaves_markers_and_the_context(ws):
    ws.files.write("/workspace/a.txt", "one\ntwo\nthree\n")
    ws.index.commit("first")
    ws.files.write("/workspace/a.txt", "one\nTWO\nthree\n")
    ws.index.commit("second")
    second = ws.index.head
    ws.files.write("/workspace/a.txt", "one\nLATER\nthree\n")
    ws.index.commit("third")

    out = ws.revert(second)

    assert out.merged
    assert out.conflicts == ("/workspace/a.txt",)
    assert b"<<<<<<< " in ws.files.read("/workspace/a.txt")
    status = ws.index.status()
    assert status.merge_source is not None
    assert second[:7] in status.merge_source
    assert status.merge_unresolved == ("/workspace/a.txt",)
    assert "UU a.txt" in ws.terminal("ws-git status").stdout

    # It ends the way a merge's does: the markers go, and so does it.
    ws.files.write("/workspace/a.txt", "resolved\n")
    ws.terminal("ws-git commit -m resolved")
    assert ws.index.status().merge_source is None
    assert ws.terminal("ws-git status").stdout == ""


def test_revert_of_a_first_commit_against_an_edited_file(ws):
    """A first commit changed the world from nothing, so the side the
    revert applies is the empty tree — and a file edited since is
    resolved against it like any other, markers and all."""
    ws.files.write("/workspace/a.txt", "one\ntwo\nthree\n")
    ws.index.commit("first")
    first = ws.index.head
    ws.files.write("/workspace/a.txt", "one\nEDITED\nthree\n")
    ws.index.commit("second")

    out = ws.revert(first)

    assert out.merged
    assert out.conflicts == ("/workspace/a.txt",)
    assert b"<<<<<<< " in ws.files.read("/workspace/a.txt")
    assert ws.index.status().merge_unresolved == ("/workspace/a.txt",)
    assert "UU a.txt" in ws.terminal("ws-git status").stdout


def test_revert_of_a_merge_goes_back_to_ours(ws):
    ws.files.write("/workspace/base.txt", "base\n")
    ws.index.commit("seed")
    child = _fork(ws, "child")
    try:
        child.files.write("/workspace/theirs.txt", "theirs\n")
        child.index.commit("child work")
        ws.files.write("/workspace/mine.txt", "mine\n")
        ws.index.commit("my work")

        merge = ws.merge("child")
        assert merge.merged
        assert ws.files.exists("/workspace/theirs.txt")

        out = ws.revert(merge.commit)

        assert out.merged and out.conflicts == ()
        assert not ws.files.exists("/workspace/theirs.txt")
        assert ws.files.read("/workspace/mine.txt") == b"mine\n"
        assert ws.files.read("/workspace/base.txt") == b"base\n"
    finally:
        child.close()


def test_a_revert_of_nothing_says_so(ws):
    ws.files.write("/workspace/a.txt", "one\n")
    ws.index.commit("first")
    first = ws.index.head
    ws.revert(first)
    head = ws.index.head

    again = ws.revert(first)

    assert not again.merged
    assert again.commit is None and again.conflicts == ()
    assert ws.index.head == head


def test_cherry_pick_brings_one_commit_of_a_delegate(ws):
    ws.files.write("/workspace/base.txt", "base\n")
    ws.index.commit("seed")
    child = _fork(ws, "child")
    try:
        child.files.write("/workspace/one.txt", "one\n")
        child.index.commit("one")
        child.files.write("/workspace/two.txt", "two\n")
        child.index.commit("two")
        wanted = child.index.head
        child.files.write("/workspace/three.txt", "three\n")
        child.index.commit("three")

        out = ws.cherry_pick(f"child@{wanted}")

        assert out.merged and out.conflicts == ()
        assert out.auto_merged == ("/workspace/two.txt",)
        assert ws.files.read("/workspace/two.txt") == b"two\n"
        assert not ws.files.exists("/workspace/one.txt")
        assert not ws.files.exists("/workspace/three.txt")
        entry = next(e for e in ws.log(kind="agent") if e.id == out.commit)
        assert entry.info["picked_from"] == f"child@{wanted}"
    finally:
        child.close()


def test_cherry_pick_takes_a_short_id(ws):
    ws.files.write("/workspace/base.txt", "base\n")
    ws.index.commit("seed")
    child = _fork(ws, "child")
    try:
        child.files.write("/workspace/one.txt", "one\n")
        child.index.commit("one")
        out = ws.cherry_pick(f"child@{child.index.head[:7]}")
        assert out.merged
        assert ws.files.read("/workspace/one.txt") == b"one\n"
    finally:
        child.close()


def test_cherry_pick_of_a_delegates_first_commit_brings_only_its_work(ws):
    """A fork starts with a fresh ws-git state, so a delegate's first
    commit has no agent commit before it. Its change is what it did
    after the fork, not the tree it inherited."""
    ws.files.write("/workspace/base.txt", "base\n")
    ws.index.commit("seed")
    child = _fork(ws, "child")
    try:
        child.files.write("/workspace/from_child.txt", "child\n")
        child.index.commit("child work")
        wanted = child.index.head

        # this side moves on, in a file the delegate never touched
        ws.files.write("/workspace/base.txt", "base, edited here\n")
        ws.index.commit("my work")

        out = ws.cherry_pick(f"child@{wanted}")

        assert out.merged and out.conflicts == ()
        assert out.auto_merged == ("/workspace/from_child.txt",)
        assert ws.files.read("/workspace/base.txt") == b"base, edited here\n"
    finally:
        child.close()


def test_cherry_pick_of_a_first_commit_from_a_parent_that_never_used_ws_git(ws):
    """A fork always leaves a fork point, whether or not the parent
    ever used ws-git — it is what the change in a delegate's first
    commit is measured against, and without it every file the delegate
    inherited would read as one it added."""
    ws.files.write("/workspace/kept.txt", "kept\n")
    ws.files.write("/workspace/gone.txt", "gone\n")
    child = _fork(ws, "child")
    try:
        child.files.write("/workspace/kept.txt", "kept, by the child\n")
        child.index.commit("child edit")
        wanted = child.index.head

        # this side drops a different inherited file, its own business
        ws.files.fs.remove("/workspace/gone.txt")
        ws.commit()

        out = ws.cherry_pick(f"child@{wanted}")

        assert out.merged and out.conflicts == ()
        assert out.auto_merged == ("/workspace/kept.txt",)
        assert ws.files.read("/workspace/kept.txt") == b"kept, by the child\n"
        assert not ws.files.exists("/workspace/gone.txt")
    finally:
        child.close()


def test_a_cherry_pick_that_conflicts(ws):
    ws.files.write("/workspace/shared.txt", "one\ntwo\nthree\n")
    ws.index.commit("seed")
    child = _fork(ws, "child")
    try:
        child.files.write("/workspace/shared.txt", "one\nCHILD\nthree\n")
        child.index.commit("child edit")
        wanted = child.index.head
        ws.files.write("/workspace/shared.txt", "one\nMINE\nthree\n")
        ws.index.commit("my edit")

        out = ws.cherry_pick(f"child@{wanted}")

        assert out.merged
        assert out.conflicts == ("/workspace/shared.txt",)
        body = ws.files.read("/workspace/shared.txt")
        assert b"<<<<<<< " in body and b"CHILD" in body and b"MINE" in body
        assert ws.index.status().merge_source == f"child@{wanted[:7]}"
        assert "UU shared.txt" in ws.terminal("ws-git status").stdout
    finally:
        child.close()


def test_both_refuse_uncommitted_agent_work(ws):
    ws.files.write("/workspace/a.txt", "one\n")
    ws.index.commit("first")
    first = ws.index.head
    child = _fork(ws, "child")
    try:
        child.files.write("/workspace/b.txt", "two\n")
        child.index.commit("child work")
        ws.terminal("echo wip > /workspace/wip.txt")
        assert not ws.uncommitted  # the store has it; the agent has not

        with pytest.raises(WorkspaceError, match="uncommitted ws-git work"):
            ws.revert(first)
        with pytest.raises(WorkspaceError, match="uncommitted ws-git work"):
            ws.cherry_pick(f"child@{child.index.head}")
    finally:
        child.close()


def test_both_refuse_while_a_merge_is_outstanding(ws):
    ws.files.write("/workspace/shared.txt", "one\ntwo\nthree\n")
    ws.index.commit("seed")
    first = ws.index.head
    child = _fork(ws, "child")
    try:
        child.files.write("/workspace/shared.txt", "one\nCHILD\nthree\n")
        child.index.commit("child edit")
        picked = child.index.head
        ws.files.write("/workspace/shared.txt", "one\nMINE\nthree\n")
        ws.index.commit("my edit")
        assert ws.merge("child").conflicts

        with pytest.raises(WorkspaceError, match="unresolved merge"):
            ws.revert(first)
        with pytest.raises(WorkspaceError, match="unresolved merge"):
            ws.cherry_pick(f"child@{picked}")

        ws.files.write("/workspace/shared.txt", "resolved\n")
        ws.terminal("ws-git commit -m resolved")
        assert ws.revert(first).merged is not None
    finally:
        child.close()


def test_cherry_pick_names_one_commit(ws):
    ws.files.write("/workspace/a.txt", "one\n")
    ws.index.commit("first")
    with pytest.raises(ValueError, match="session@commit"):
        ws.cherry_pick("child")


def test_revert_of_a_commit_that_is_not_there(ws):
    from nontainer import CommitNotFoundError

    ws.files.write("/workspace/a.txt", "one\n")
    ws.index.commit("first")
    with pytest.raises(CommitNotFoundError):
        ws.revert("0" * 40)


def test_a_provider_without_a_merge_engine_refuses_by_name(tmp_path):
    from nontainer import Workspace
    from nontainer.providers.dir import DirProvider

    ws = Workspace(DirProvider(tmp_path / "ws", session="dir"))
    try:
        assert not ws.caps.merge
        with pytest.raises(NotSupportedError, match="cannot revert"):
            ws.revert("abc1234")
        with pytest.raises(NotSupportedError, match="cannot cherry-pick"):
            ws.cherry_pick("other@abc1234")
    finally:
        ws.close()


def test_agentfs_refuses_revert_and_cherry_pick(tmp_path):
    pytest.importorskip("agentfs_sdk")
    from nontainer import Workspace
    from nontainer.providers import AgentFSProvider

    ws = Workspace(AgentFSProvider(tmp_path / "s1.db", session="s1"))
    try:
        with pytest.raises(NotSupportedError, match="cannot revert"):
            ws.revert("abc1234")
        with pytest.raises(NotSupportedError, match="cannot cherry-pick"):
            ws.cherry_pick("other@abc1234")
    finally:
        ws.close()
