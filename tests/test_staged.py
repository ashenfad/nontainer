"""The agent's git over kvgit: index, agent commits, the graph.

Host spelling (``ws.index``) of the fiction in ``nontainer/agentgit.py``.
What these pin, beyond the verbs themselves, is the two properties the
model exists for: a framework commit never disturbs what the agent is
composing, and an agent commit's tree holds exactly what the agent
committed — not the work in progress it left in the tree.
"""

import json
import threading

import pytest

from nontainer import (
    NotSupportedError,
    Workspace,
    WorkspaceError,
    workspace,
)
from nontainer.agentgit import BLOB_KEY
from nontainer.providers import KvgitProvider


@pytest.fixture
def kv_ws():
    """Memory-backed kvgit workspace (autocommit on by default)."""
    provider = KvgitProvider.open(None, session="stage-session")
    ws = Workspace(provider)
    yield ws
    ws.close()


def _provider(ws):
    return ws._provider


def _history(ws):
    return list(ws.log())


def _at(ws, commit):
    """The files of one commit, by workspace path."""
    return _provider(ws).files_at(commit)


def test_stage_status_split(kv_ws):
    kv_ws.files.fs.write("/workspace/a.txt", b"one")
    kv_ws.files.fs.write("/workspace/b.txt", b"two")

    assert kv_ws.index.stage(["/workspace/a.txt"]) == ("/workspace/a.txt",)

    st = kv_ws.index.status()
    assert st.branch == "stage-session"
    assert st.staged == ("/workspace/a.txt",)
    assert st.unstaged == ("/workspace/b.txt",)
    assert st.merge_source is None
    assert st.merge_unresolved == ()
    # Staging commits nothing and suspends nothing: the index is
    # bookkeeping, and the workspace goes on committing as it always did.
    assert kv_ws.index.head is None


def test_second_stage_reports_only_what_was_new(kv_ws):
    kv_ws.files.fs.write("/workspace/a.txt", b"one")
    kv_ws.files.fs.write("/workspace/b.txt", b"two")

    assert kv_ws.index.stage(["/workspace/a.txt"]) == ("/workspace/a.txt",)
    assert kv_ws.index.stage(["/workspace/b.txt"]) == ("/workspace/b.txt",)
    assert kv_ws.index.stage(["/workspace/a.txt"]) == ()


def test_stage_unknown_and_directories_refused(kv_ws):
    kv_ws.files.fs.write("/workspace/a.txt", b"one")
    kv_ws.commit()

    with pytest.raises(ValueError, match="unknown path"):
        kv_ws.index.stage(["/workspace/nope.txt"])
    kv_ws.terminal("mkdir sub")
    with pytest.raises(ValueError, match="not directories"):
        kv_ws.index.stage(["/workspace/sub"])
    # Valid-but-unstaged paths are silently ignored by unstage.
    assert kv_ws.index.unstage(["/workspace/a.txt"]) == ()
    with pytest.raises(ValueError, match="unknown path"):
        kv_ws.index.unstage(["/workspace/nope.txt"])


def test_partial_commit_materializes_the_exact_tree(kv_ws):
    """The correction to the reference implementation.

    An agent stages one file, edits two, and the framework commits in
    between (a turn hook would). The agent's commit must hold its own
    file and the OTHER file as it was at the agent's last commit — not
    the edit in flight. Parenting the keyed commit on the store's head
    without that revert is what silently absorbs the edit into the
    baseline: status would go clean and a later checkout would bring
    the absorbed edit back as if it had been committed.
    """
    kv_ws.files.fs.write("/workspace/a.py", b"A = 1\n")
    kv_ws.files.fs.write("/workspace/b.py", b"B = 1\n")
    base = kv_ws.index.commit("base")

    kv_ws.index.stage(["/workspace/a.py"])
    kv_ws.files.fs.write("/workspace/a.py", b"A = 2\n")
    kv_ws.files.fs.write("/workspace/b.py", b"B = 2\n")
    kv_ws.commit(info={"tool": "turn"})  # the framework, mid-composition

    commit = kv_ws.index.commit("just a")

    # b.py is still work in progress, and still says so
    assert kv_ws.index.status().staged == ()
    assert kv_ws.index.status().unstaged == ("/workspace/b.py",)
    # the agent's commit holds a.py's edit and b.py's BASE content
    tree = _at(kv_ws, commit)
    assert tree["/workspace/a.py"] == b"A = 2\n"
    assert tree["/workspace/b.py"] == b"B = 1\n"
    assert _at(kv_ws, base)["/workspace/b.py"] == b"B = 1\n"
    # the working tree kept the edit, and the restore commit landed it
    assert kv_ws.files.read("/workspace/b.py") == b"B = 2\n"
    assert _at(kv_ws, kv_ws.head)["/workspace/b.py"] == b"B = 2\n"
    # ... and the agent sees one commit, not the framework's two
    assert [e.info["message"] for e in kv_ws.index.log()] == ["just a", "base"]
    assert kv_ws.index.head == commit


def test_framework_commits_never_change_status(kv_ws):
    """The property the whole model exists for."""
    kv_ws.files.fs.write("/workspace/a.txt", b"one")
    kv_ws.files.fs.write("/workspace/b.txt", b"two")
    kv_ws.index.stage(["/workspace/a.txt"])
    before = kv_ws.index.status()

    for _ in range(3):
        kv_ws.commit(info={"tool": "turn"})
        assert kv_ws.index.status() == before
    assert not kv_ws.dirty  # everything IS in the store; nothing is withheld
    assert before.staged == ("/workspace/a.txt",)
    assert before.unstaged == ("/workspace/b.txt",)


def test_commit_without_an_index_takes_everything_modified(kv_ws):
    kv_ws.files.fs.write("/workspace/a.txt", b"one")
    kv_ws.files.fs.write("/workspace/b.txt", b"two")

    commit = kv_ws.index.commit("all of it")

    assert set(_at(kv_ws, commit)) == {"/workspace/a.txt", "/workspace/b.txt"}
    assert kv_ws.index.status().unstaged == ()


def test_commit_with_nothing_to_commit_refused(kv_ws):
    with pytest.raises(WorkspaceError, match="nothing to commit"):
        kv_ws.index.commit()
    kv_ws.terminal("echo one > a.txt")
    kv_ws.index.commit("one")
    # An index over unchanged paths is nothing to commit either, even
    # with other work modified around it.
    kv_ws.terminal("echo two > b.txt")
    kv_ws.index.stage(["/workspace/a.txt"])
    with pytest.raises(WorkspaceError, match="staged paths match"):
        kv_ws.index.commit()


def test_unstage_and_discard_leave_the_tree_alone(kv_ws):
    kv_ws.files.fs.write("/workspace/a.txt", b"one")
    kv_ws.files.fs.write("/workspace/b.txt", b"two")
    kv_ws.index.stage(["/workspace/a.txt", "/workspace/b.txt"])

    assert kv_ws.index.unstage(["/workspace/a.txt"]) == ("/workspace/a.txt",)
    assert kv_ws.index.status().staged == ("/workspace/b.txt",)

    kv_ws.index.discard()
    st = kv_ws.index.status()
    assert st.staged == ()
    assert st.unstaged == ("/workspace/a.txt", "/workspace/b.txt")
    assert kv_ws.terminal("cat a.txt").stdout.strip() == "one"


def test_stage_deleted_file_commits_deletion(kv_ws):
    kv_ws.terminal("echo one > a.txt")
    kv_ws.terminal("echo two > b.txt")
    kv_ws.index.commit("both")
    kv_ws.files.fs.remove("/workspace/a.txt")

    kv_ws.index.stage(["/workspace/a.txt"])
    assert kv_ws.index.status().staged == ("/workspace/a.txt",)
    commit = kv_ws.index.commit("drop a")
    assert set(_at(kv_ws, commit)) == {"/workspace/b.txt"}
    assert "a.txt" not in kv_ws.files.fs.list("/workspace")
    assert kv_ws.index.status().unstaged == ()


def test_fork_copies_the_index_then_diverges(kv_ws):
    kv_ws.files.fs.write("/workspace/a.txt", b"one")
    kv_ws.index.commit("base")
    kv_ws.files.fs.write("/workspace/a.txt", b"one-main")
    kv_ws.index.stage(["/workspace/a.txt"])

    fork = kv_ws.fork("worker")
    try:
        assert fork.index.status().staged == ("/workspace/a.txt",)
        kv_ws.index.commit("main")
        assert kv_ws.index.status().staged == ()
        assert fork.index.status().staged == ("/workspace/a.txt",)
    finally:
        fork.close()


def test_log_hides_framework_commits_and_walks_across_a_fork(kv_ws):
    kv_ws.terminal("echo one > a.txt")
    first = kv_ws.index.commit("first")
    kv_ws.terminal("echo two > b.txt")  # a framework commit, unmessaged
    second = kv_ws.index.commit("second")

    entries = kv_ws.index.log()
    assert [e.id for e in entries] == [second, first]
    assert [e.info["message"] for e in entries] == ["second", "first"]
    assert len(_history(kv_ws)) > len(entries) + 1  # the framework's are there
    assert [e.id for e in kv_ws.index.log(limit=1)] == [second]

    fork = kv_ws.fork("worker")
    try:
        fork.terminal("echo three > c.txt")
        third = fork.index.commit("third")
        assert [e.info["message"] for e in fork.index.log()] == [
            "third",
            "second",
            "first",
        ]
        assert fork.index.head == third
    finally:
        fork.close()


def test_checkout_restores_the_tree_and_appends(kv_ws):
    kv_ws.files.fs.write("/workspace/a.py", b"A = 1\n")
    kv_ws.files.fs.write("/workspace/b.py", b"B = 1\n")
    first = kv_ws.index.commit("first")
    kv_ws.files.fs.write("/workspace/b.py", b"B = 2\n")
    kv_ws.files.fs.write("/workspace/c.py", b"C = 1\n")
    kv_ws.index.commit("second")
    before = len(_history(kv_ws))

    assert kv_ws.index.checkout(first) == first

    assert kv_ws.files.read("/workspace/b.py") == b"B = 1\n"
    assert not kv_ws.files.exists("/workspace/c.py")
    assert kv_ws.index.head == first
    assert kv_ws.index.status().staged == ()
    assert kv_ws.index.status().unstaged == ()
    # The fiction rewound; the store appended. Nothing left history.
    assert len(_history(kv_ws)) == before + 1
    assert kv_ws.head != first


def test_a_host_checkout_rewinds_the_agents_git_with_the_tree(kv_ws):
    """The fiction's head and graph live in a key, and a host checkout
    restores the whole keyset — so ``ws.checkout`` puts the agent's
    git back where it was at the target, and the store keeps both."""
    kv_ws.files.fs.write("/workspace/a.py", b"A = 1\n")
    first = kv_ws.index.commit("first")
    at_first = kv_ws.head  # the store commit made while head was `first`
    kv_ws.files.fs.write("/workspace/b.py", b"B = 1\n")
    second = kv_ws.index.commit("second")
    assert kv_ws.index.head == second

    landed = kv_ws.checkout(at_first)

    assert kv_ws.index.head == first
    assert [e.info["message"] for e in kv_ws.index.log()] == ["first"]
    assert kv_ws.index.status().staged == ()
    assert kv_ws.index.status().unstaged == ()
    assert not kv_ws.files.exists("/workspace/b.py")
    # The fiction rewound; the store did not. Both agent commits are
    # still in the session's history, so the second is reachable again.
    assert {first, second, at_first, landed} <= {e.id for e in kv_ws.log()}


def test_old_layout_blob_migrates(kv_ws):
    """A branch written before the fiction carried a key-level index
    and a suspension flag. Neither means anything now, so it reads as a
    fresh state rather than being reinterpreted."""
    kv = _provider(kv_ws).kv
    kv[BLOB_KEY] = json.dumps(
        {"version": 1, "index": ["f:/workspace/a.txt"], "suspended": True}
    ).encode()
    kv_ws.files.fs.write("/workspace/a.txt", b"one")
    kv_ws.commit()

    st = kv_ws.index.status()
    assert kv_ws.index.head is None
    assert st.staged == ()
    assert st.unstaged == ("/workspace/a.txt",)

    # and the next write records the new layout
    kv_ws.index.stage(["/workspace/a.txt"])
    blob = json.loads(_provider(kv_ws).kv.get(BLOB_KEY))
    assert blob["version"] == 2
    assert blob["staged"] == ["/workspace/a.txt"]
    assert blob["head"] is None


def test_frozen_verbs_refused_status_open(kv_ws):
    kv_ws.terminal("echo one > a.txt")
    kv_ws.index.commit("one")
    kv_ws.tags.add("v1")
    snap = kv_ws.tags.at("v1")
    try:
        with pytest.raises(NotSupportedError, match="frozen"):
            snap.index.stage(["/workspace/a.txt"])
        with pytest.raises(NotSupportedError, match="frozen"):
            snap.index.unstage(["/workspace/a.txt"])
        with pytest.raises(NotSupportedError, match="frozen"):
            snap.commit()
        with pytest.raises(NotSupportedError, match="frozen"):
            snap.index.discard()
        # Reads are the point of snapshots: status stays open.
        assert snap.index.status().unstaged == ()
    finally:
        snap.close()


def test_dir_provider_has_no_index(tmp_path):
    from nontainer.providers.dir import DirProvider

    ws = Workspace(DirProvider(tmp_path / "ws", session="dir"))
    try:
        for call in (
            lambda: ws.index.stage(["/workspace/a.txt"]),
            lambda: ws.index.unstage(["/workspace/a.txt"]),
            lambda: ws.index.commit(),
            lambda: ws.index.discard(),
            lambda: ws.index.status(),
            lambda: ws.index.log(),
        ):
            with pytest.raises(NotSupportedError, match="no index"):
                call()
    finally:
        ws.close()


def test_concurrent_stage_no_lost_update(kv_ws):
    kv_ws.files.fs.write("/workspace/a.txt", b"a")
    kv_ws.files.fs.write("/workspace/b.txt", b"b")
    kv_ws.commit()
    kv_ws.files.fs.write("/workspace/a.txt", b"a2")
    kv_ws.files.fs.write("/workspace/b.txt", b"b2")

    errors: list = []

    def stage_one(path):
        try:
            kv_ws.index.stage([path])
        except Exception as e:  # noqa: BLE001 - collected, asserted below
            errors.append(e)

    threads = [
        threading.Thread(target=stage_one, args=(path,))
        for path in ("/workspace/a.txt", "/workspace/b.txt")
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert kv_ws.index.status().staged == ("/workspace/a.txt", "/workspace/b.txt")


def test_merge_takes_our_blob(kv_ws):
    """The blob is this session's bookkeeping: a merge never brings
    another session's index or head into it."""
    kv_ws.files.fs.write("/workspace/a.txt", b"base")
    kv_ws.commit()
    fork = kv_ws.fork("worker")
    try:
        fork.terminal("echo worker > b.txt")
        fork.index.commit("worker work")
        kv_ws.files.fs.write("/workspace/a.txt", b"main")
        ours = kv_ws.index.commit("main work")

        out = kv_ws.merge("worker")
        assert out.merged
        assert kv_ws.index.head == out.commit
        # the merge joined the agent's graph rather than ending it
        assert [e.info.get("message") for e in kv_ws.index.log()] == [
            None,
            "main work",
        ]
        assert _at(kv_ws, ours)["/workspace/a.txt"] == b"main"
    finally:
        fork.close()


def test_partial_commit_preserves_the_live_table(kv_ws):
    from monkeyfs import VirtualFS

    p = _provider(kv_ws)
    kv_ws.files.fs.write("/workspace/a.txt", b"one")
    kv_ws.files.fs.write("/workspace/b.txt", b"two")
    kv_ws.index.stage(["/workspace/a.txt"])
    kv_ws.index.commit("a only")

    # Live reads still see the uncommitted file at its live size.
    live = json.loads(p._staged.get(VirtualFS.METADATA_KEY) or b"{}") or json.loads(
        p._staged.checkout(p.head).get(VirtualFS.METADATA_KEY)
    )
    assert live["workspace/b.txt"]["size"] == 3
    assert kv_ws.files.read("/workspace/b.txt") == b"two"


def test_status_reports_merge_context(kv_ws):
    kv_ws.files.fs.write("/workspace/doc.txt", b"a\nb\n")
    kv_ws.commit()
    fork = kv_ws.fork("worker")
    try:
        fork.files.fs.write("/workspace/doc.txt", b"a\nFORK\n")
        fork.commit()
        kv_ws.files.fs.write("/workspace/doc.txt", b"a\nMAIN\n")
        kv_ws.commit()

        out = kv_ws.merge("worker")
        assert out.conflicts == ("/workspace/doc.txt",)
        st = kv_ws.index.status()
        assert st.merge_source == "worker"
        assert st.merge_unresolved == ("/workspace/doc.txt",)
        # The markers are IN the merge commit, which is now the agent's
        # head: the tree reads clean and the merge is what is unfinished.
        assert st.staged == () and st.unstaged == ()

        # Resolve like an agent would: edit, then commit.
        kv_ws.files.fs.write("/workspace/doc.txt", b"a\nBOTH\n")
        kv_ws.index.commit("resolved")
        st = kv_ws.index.status()
        assert st.merge_source is None
        assert st.merge_unresolved == ()
        assert st.unstaged == ()
    finally:
        fork.close()


def test_merge_context_clears_on_a_framework_commit_too(kv_ws):
    """Resolution is measured by the markers, not by who committed:
    the context ends when the last marked path stops being marked."""
    kv_ws.files.fs.write("/workspace/doc.txt", b"a\nb\n")
    kv_ws.commit()
    fork = kv_ws.fork("worker")
    try:
        fork.files.fs.write("/workspace/doc.txt", b"a\nFORK\n")
        fork.commit()
        kv_ws.files.fs.write("/workspace/doc.txt", b"a\nMAIN\n")
        kv_ws.commit()
        kv_ws.merge("worker")
        assert kv_ws.index.status().merge_source == "worker"

        kv_ws.files.fs.write("/workspace/doc.txt", b"a\nBOTH\n")
        kv_ws.commit(info={"tool": "turn"})
        assert kv_ws.index.status().merge_source is None
    finally:
        fork.close()


def test_workspace_wrappers_smoke(tmp_path):
    ws = workspace("stage-smoke", store=str(tmp_path / "store"))
    try:
        ws.terminal("echo one > a.txt")
        assert ws.index.stage(["/workspace/a.txt"]) == ("/workspace/a.txt",)
        assert ws.index.status().staged == ("/workspace/a.txt",)
        ws.index.discard()
        assert ws.index.status().staged == ()
        assert ws.index.status().unstaged == ("/workspace/a.txt",)
    finally:
        ws.close()


def test_commit_takes_everything_the_index_does_not(kv_ws):
    """``ws.commit`` is the framework's verb and ``ws.index.commit``
    the agent's; the difference is what each one is for."""
    kv_ws.files.fs.write("/workspace/a.txt", b"one")
    kv_ws.files.fs.write("/workspace/b.txt", b"two")
    kv_ws.index.stage(["/workspace/a.txt"])

    head = kv_ws.commit()

    assert not kv_ws.dirty
    assert set(_at(kv_ws, head)) >= {"/workspace/a.txt", "/workspace/b.txt"}
    # ... and the agent's composition is exactly where it was
    st = kv_ws.index.status()
    assert st.staged == ("/workspace/a.txt",)
    assert st.unstaged == ("/workspace/b.txt",)


# -- the fiction's own bookkeeping ---------------------------------------------


def test_agent_commit_persists_without_autocommit(tmp_path):
    """The restore of what the commit left out, and the new head, are
    the agent's operation finishing — not the workspace's autocommit
    policy, which a per-turn host turns off."""
    from nontainer import Store

    store = Store(str(tmp_path / "store"))
    ws = store.open("turnmode", autocommit=False)
    try:
        ws.files.fs.write("/workspace/a.py", b"A = 1\n")
        ws.files.fs.write("/workspace/b.py", b"B = 1\n")
        ws.index.stage(["/workspace/a.py"])
        commit = ws.index.commit("just a")
        assert not ws.dirty  # the bookkeeping committed itself
    finally:
        ws.close()

    reopened = store.open("turnmode", autocommit=False)
    try:
        assert reopened.index.head == commit
        assert [e.info["message"] for e in reopened.index.log()] == ["just a"]
        # the work in progress survived the reopen, and is still work
        # in progress: out of the agent's commit, in the tree
        assert reopened.files.read("/workspace/b.py") == b"B = 1\n"
        assert reopened.index.status().unstaged == ("/workspace/b.py",)
        assert "/workspace/b.py" not in _at(reopened, commit)
        assert not reopened.dirty
    finally:
        reopened.close()
        store.close()


def test_the_restore_commit_is_not_an_agent_commit(kv_ws):
    """Bookkeeping is spelled ``ws-git.<something>``; only ``ws-git``
    itself (and the merge) is the agent's."""
    from nontainer.agentgit import CHECKOUT_TOOL, MERGE_RECORD_TOOL, RESTORE_TOOL

    kv_ws.files.fs.write("/workspace/a.py", b"A = 1\n")
    kv_ws.files.fs.write("/workspace/b.py", b"B = 1\n")
    kv_ws.index.stage(["/workspace/a.py"])
    commit = kv_ws.index.commit("just a")

    restore = _history(kv_ws)[0]
    assert restore.info == {"tool": RESTORE_TOOL, "restore_of": commit}
    assert [e.id for e in kv_ws.index.log()] == [commit]
    for tool in (RESTORE_TOOL, CHECKOUT_TOOL, MERGE_RECORD_TOOL):
        assert tool.startswith("ws-git.")


def test_merge_bookkeeping_commits_itself(kv_ws):
    """A merge that left its own record uncommitted returned a dirty
    workspace, lost the head on reopen, and refused the next merge."""
    kv_ws.terminal("echo base > a.txt")
    kv_ws.index.commit("base")
    first = kv_ws.fork("worker-one")
    second = kv_ws.fork("worker-two")
    try:
        first.terminal("echo one > one.txt")
        first.index.commit("one")
        second.terminal("echo two > two.txt")
        second.index.commit("two")

        out = kv_ws.merge("worker-one")
        assert out.merged
        assert not kv_ws.dirty
        assert kv_ws.index.head == out.commit
        assert out.commit in [e.id for e in kv_ws.index.log()]

        # a clean tree is what the next merge needs, and it is what
        # the first one left behind
        assert kv_ws.merge("worker-two").merged
        assert not kv_ws.dirty
    finally:
        first.close()
        second.close()


def test_merge_bookkeeping_survives_without_autocommit(tmp_path):
    from nontainer import Store

    store = Store(str(tmp_path / "store"))
    ws = store.open("mergehost", autocommit=False)
    try:
        ws.terminal("echo base > a.txt")
        ws.index.commit("base")
        fork = ws.fork("mergehost-kid")
        try:
            fork.terminal("echo kid > kid.txt")
            fork.index.commit("kid")
            out = ws.merge("mergehost-kid")
            assert out.merged
            assert not ws.dirty
        finally:
            fork.close()
    finally:
        ws.close()

    reopened = store.open("mergehost")
    try:
        assert reopened.index.head == out.commit
        assert out.commit in [e.id for e in reopened.index.log()]
    finally:
        reopened.close()
        store.close()


def test_a_failed_agent_commit_leaves_the_tree_untouched(kv_ws, monkeypatch):
    """A CAS conflict must not cost the agent its work: the revert this
    commit made on the way in is undone, and the composition stays
    open for another try."""
    kv_ws.files.fs.write("/workspace/a.py", b"A = 1\n")
    kv_ws.files.fs.write("/workspace/b.py", b"B = 1\n")
    kv_ws.index.commit("base")
    kv_ws.files.fs.write("/workspace/a.py", b"A = 2\n")
    kv_ws.files.fs.write("/workspace/b.py", b"B = 2\n")
    kv_ws.files.fs.write("/workspace/c.py", b"C = 1\n")
    kv_ws.index.stage(["/workspace/a.py"])
    before_status = kv_ws.index.status()
    before_tree = {
        path: kv_ws.files.read(path)
        for path in ("/workspace/a.py", "/workspace/b.py", "/workspace/c.py")
    }
    before_head = kv_ws.index.head

    def boom(*args, **kwargs):
        raise WorkspaceError("commit failed: conflicting concurrent commit (CAS)")

    monkeypatch.setattr(_provider(kv_ws), "commit_keys", boom)
    with pytest.raises(WorkspaceError, match="CAS"):
        kv_ws.index.commit("doomed")
    monkeypatch.undo()

    assert kv_ws.index.status() == before_status
    assert kv_ws.index.head == before_head
    for path, content in before_tree.items():
        assert kv_ws.files.read(path) == content
    assert kv_ws.files.fs.getsize("/workspace/b.py") == len(
        before_tree["/workspace/b.py"]
    )
    # and the composition can still land
    commit = kv_ws.index.commit("second try")
    assert _at(kv_ws, commit)["/workspace/a.py"] == b"A = 2\n"
    assert _at(kv_ws, commit)["/workspace/b.py"] == b"B = 1\n"


def test_a_failed_checkout_leaves_the_tree_untouched(kv_ws, monkeypatch):
    kv_ws.files.fs.write("/workspace/a.py", b"A = 1\n")
    first = kv_ws.index.commit("first")
    kv_ws.files.fs.write("/workspace/a.py", b"A = 2\n")
    kv_ws.files.fs.write("/workspace/b.py", b"B = 1\n")
    kv_ws.index.commit("second")
    before_status = kv_ws.index.status()
    before_head = kv_ws.index.head

    def boom(*args, **kwargs):
        raise WorkspaceError("commit failed: conflicting concurrent commit (CAS)")

    monkeypatch.setattr(_provider(kv_ws), "commit_keys", boom)
    with pytest.raises(WorkspaceError, match="CAS"):
        kv_ws.index.checkout(first)
    monkeypatch.undo()

    assert kv_ws.index.head == before_head
    assert kv_ws.index.status() == before_status
    assert kv_ws.files.read("/workspace/a.py") == b"A = 2\n"
    assert kv_ws.files.read("/workspace/b.py") == b"B = 1\n"


# -- what a merge takes ---------------------------------------------------------


def test_merge_refuses_uncommitted_agent_work(kv_ws):
    """Autocommit keeps the store's buffer clean while an agent
    composes, so "clean" for a merge has to mean the agent's own
    status — or the merge folds work in flight into itself."""
    kv_ws.terminal("echo base > a.txt")
    kv_ws.index.commit("base")
    fork = kv_ws.fork("worker")
    try:
        fork.terminal("echo worker > b.txt")
        fork.index.commit("worker work")

        kv_ws.terminal("echo mine > mine.txt")  # committed by the framework
        assert not kv_ws.dirty  # ... so the buffer says nothing is pending
        with pytest.raises(WorkspaceError) as excinfo:
            kv_ws.merge("worker")
        message = str(excinfo.value)
        assert "ws-git commit" in message
        assert "ws-git checkout" in message

        # either fix unblocks it
        kv_ws.index.commit("mine")
        assert kv_ws.merge("worker").merged
    finally:
        fork.close()


def test_merge_refuses_an_open_index_before_the_first_commit(kv_ws):
    """No agent commit yet means no baseline to differ from, so only
    an index in flight refuses."""
    kv_ws.terminal("echo base > a.txt")
    fork = kv_ws.fork("worker")
    try:
        fork.terminal("echo worker > b.txt")
        assert kv_ws.merge("worker").merged  # nothing staged: allowed

        kv_ws.terminal("echo more > c.txt")
        kv_ws.index.stage(["/workspace/c.txt"])
        second = kv_ws.fork("worker-two")
        try:
            second.terminal("echo two > d.txt")
            with pytest.raises(WorkspaceError, match="uncommitted ws-git work"):
                kv_ws.merge("worker-two")
        finally:
            second.close()
    finally:
        fork.close()


def test_merge_takes_the_sources_last_agent_commit(kv_ws):
    """The source side of the same rule: what its agent committed, not
    what the framework committed for it afterwards."""
    kv_ws.terminal("echo base > a.txt")
    kv_ws.index.commit("base")
    fork = kv_ws.fork("worker")
    try:
        fork.files.fs.write("/workspace/done.txt", b"done\n")
        fork.index.commit("done")
        # work in flight on the fork, durable in the store, not in its
        # agent's commit
        fork.terminal("echo wip > wip.txt")
        assert not fork.dirty

        out = kv_ws.merge("worker")
        assert out.merged
        assert kv_ws.files.exists("/workspace/done.txt")
        assert not kv_ws.files.exists("/workspace/wip.txt")
    finally:
        fork.close()


def test_merge_of_a_session_that_never_used_ws_git(kv_ws):
    """No agent commit on the source: its store head is the honest
    answer, and the merge is what it always was."""
    kv_ws.terminal("echo base > a.txt")  # framework commits only, both sides
    fork = kv_ws.fork("worker")
    try:
        fork.terminal("echo worker > b.txt")
        assert fork.index.head is None
        assert kv_ws.merge("worker").merged
        assert kv_ws.files.read("/workspace/b.txt") == b"worker\n"
    finally:
        fork.close()


# -- the merge context ----------------------------------------------------------


def test_a_file_about_markers_is_not_a_conflict(kv_ws):
    """The context records what the MERGE marked. A scan of the tree
    cannot tell a conflict from a file that is about conflicts."""
    kv_ws.files.fs.write(
        "/workspace/notes.md", b"resolve it:\n<<<<<<< HEAD\nours\n=======\n"
    )
    kv_ws.index.commit("notes")
    fork = kv_ws.fork("worker")
    try:
        fork.terminal("echo worker > b.txt")
        fork.index.commit("worker work")
        out = kv_ws.merge("worker")
        assert out.merged
        assert out.conflicts == ()

        st = kv_ws.index.status()
        assert st.merge_source is None
        assert st.merge_unresolved == ()
    finally:
        fork.close()


def test_the_context_holds_until_the_markers_are_gone(kv_ws):
    """Committing a conflicted file is not resolving it: what clears
    the context is content without markers."""
    kv_ws.files.fs.write("/workspace/one.txt", b"a\nb\n")
    kv_ws.files.fs.write("/workspace/two.txt", b"a\nb\n")
    kv_ws.index.commit("base")
    fork = kv_ws.fork("worker")
    try:
        fork.files.fs.write("/workspace/one.txt", b"a\nFORK\n")
        fork.files.fs.write("/workspace/two.txt", b"a\nFORK\n")
        fork.index.commit("fork work")
        kv_ws.files.fs.write("/workspace/one.txt", b"a\nMAIN\n")
        kv_ws.files.fs.write("/workspace/two.txt", b"a\nMAIN\n")
        kv_ws.index.commit("main work")

        out = kv_ws.merge("worker")
        assert set(out.conflicts) == {"/workspace/one.txt", "/workspace/two.txt"}

        # Resolve ONE and merely EDIT the other, then commit both.
        # Both are in the commit; only one of them is resolved, and
        # being committed is not what resolves a conflict.
        kv_ws.files.fs.write("/workspace/one.txt", b"a\nBOTH\n")
        marked = kv_ws.files.read("/workspace/two.txt")
        assert b"<<<<<<< " in marked
        kv_ws.files.fs.write("/workspace/two.txt", marked + b"still working\n")
        commit = kv_ws.index.commit("half resolved")
        assert set(list(kv_ws.index.log())[0].info["files"]) == {
            "/workspace/one.txt",
            "/workspace/two.txt",
        }
        assert b"<<<<<<< " in _at(kv_ws, commit)["/workspace/two.txt"]
        st = kv_ws.index.status()
        assert st.merge_source == "worker"
        assert st.merge_unresolved == ("/workspace/two.txt",)

        kv_ws.files.fs.write("/workspace/two.txt", b"a\nBOTH\n")
        kv_ws.index.commit("resolved")
        assert kv_ws.index.status().merge_source is None
        assert kv_ws.index.status().merge_unresolved == ()
    finally:
        fork.close()


# -- losing the CAS on the bookkeeping ------------------------------------------


def test_a_concurrent_commit_between_the_two_keyed_commits(tmp_path):
    """The agent's commit lands, another handle moves HEAD, and the
    bookkeeping commits anyway — kvgit three-way merges a commit whose
    keys are disjoint, and the keys two writers always contend on (the
    file table, the cwd, the blob) have a policy registered for it."""
    from nontainer import Store

    store = Store(str(tmp_path / "store"))
    ws = store.open("racy")
    other = store.open("racy")
    try:
        ws.files.fs.write("/workspace/a.py", b"A = 1\n")
        ws.files.fs.write("/workspace/b.py", b"B = 1\n")
        ws.index.stage(["/workspace/a.py"])

        real = ws._provider.commit_keys
        seen = []

        def racing(info=None, *, keys):
            out = real(info, keys=keys)
            if not seen:
                seen.append(out)
                other._provider._staged.refresh()
                other.files.fs.write("/workspace/elsewhere.txt", b"theirs\n")
                other.commit(info={"tool": "turn"})
            return out

        ws._provider.commit_keys = racing
        commit = ws.index.commit("just a")
        del ws._provider.commit_keys

        assert ws.index.head == commit
        assert ws.files.read("/workspace/b.py") == b"B = 1\n"
    finally:
        other.close()
        ws.close()

    reopened = store.open("racy")
    try:
        assert reopened.index.head == commit
        assert reopened.files.read("/workspace/b.py") == b"B = 1\n"
        assert reopened.files.read("/workspace/elsewhere.txt") == b"theirs\n"
        # b.py is the agent's own work in progress; the other handle's
        # file is work the agent has not committed either
        assert "/workspace/b.py" in reopened.index.status().unstaged
    finally:
        reopened.close()
        store.close()


def test_bookkeeping_reconciles_when_its_commit_is_refused(kv_ws):
    """The second line of defence: a bookkeeping commit that DOES
    raise (both handles touched one file key, say) is re-applied
    against the head that won and committed again."""
    kv_ws.files.fs.write("/workspace/a.py", b"A = 1\n")
    kv_ws.files.fs.write("/workspace/b.py", b"B = 1\n")
    kv_ws.index.stage(["/workspace/a.py"])

    provider = _provider(kv_ws)
    real = provider.commit_keys
    refused = []

    def once(info=None, *, keys):
        if (info or {}).get("tool") == "ws-git.restore" and not refused:
            refused.append(True)
            raise WorkspaceError("commit failed: conflicting concurrent commit (CAS)")
        return real(info, keys=keys)

    provider.commit_keys = once
    commit = kv_ws.index.commit("just a")
    del provider.commit_keys

    assert refused  # the first attempt really was refused
    assert kv_ws.index.head == commit
    assert not kv_ws.dirty  # the retry landed the record and the restore
    assert kv_ws.files.read("/workspace/b.py") == b"B = 1\n"
    assert _at(kv_ws, kv_ws.head)["/workspace/b.py"] == b"B = 1\n"
    assert kv_ws.index.status().unstaged == ("/workspace/b.py",)


def test_bookkeeping_that_cannot_land_names_the_commit_that_did(kv_ws):
    from nontainer.errors import BookkeepingLost

    kv_ws.files.fs.write("/workspace/a.py", b"A = 1\n")
    kv_ws.files.fs.write("/workspace/b.py", b"B = 1\n")
    kv_ws.index.stage(["/workspace/a.py"])

    provider = _provider(kv_ws)
    real = provider.commit_keys
    landed = []

    def once(info=None, *, keys):
        if (info or {}).get("tool") == "ws-git":
            out = real(info, keys=keys)
            landed.append(out)
            return out
        raise WorkspaceError("commit failed: conflicting concurrent commit (CAS)")

    provider.commit_keys = once
    with pytest.raises(BookkeepingLost) as excinfo:
        kv_ws.index.commit("just a")
    del provider.commit_keys

    message = str(excinfo.value)
    assert landed and landed[0] in message
    assert "landed but its record did not" in message
    assert "Run the verb again" in message
    # the commit is in the store, and the agent's head still is not it
    assert any(e.id == landed[0] for e in _history(kv_ws))
    assert kv_ws.index.head != landed[0]


# -- what a checkout may target -------------------------------------------------


def test_checkout_refuses_a_commit_that_is_not_the_agents(kv_ws):
    """A framework commit has no place in the agent's graph: taking one
    as the head would strand the agent's whole log behind it."""
    kv_ws.terminal("echo one > a.txt")
    first = kv_ws.index.commit("first")
    kv_ws.terminal("echo two > b.txt")  # a framework commit
    kv_ws.index.commit("second")

    framework = next(e.id for e in _history(kv_ws) if e.info.get("tool") == "terminal")
    with pytest.raises(ValueError, match="not one of yours"):
        kv_ws.index.checkout(framework)
    with pytest.raises(ValueError, match=r"ws\.checkout"):
        kv_ws.index.checkout(framework)
    # the agent's own still checks out, and the log lands with it
    assert kv_ws.index.checkout(first) == first
    assert [e.info["message"] for e in kv_ws.index.log()] == ["first"]
    assert kv_ws.index.head == first


def test_checkout_keeps_the_ancestry_it_lands_on(kv_ws):
    kv_ws.terminal("echo one > a.txt")
    kv_ws.index.commit("first")
    kv_ws.terminal("echo two > b.txt")
    second = kv_ws.index.commit("second")
    kv_ws.terminal("echo three > c.txt")
    kv_ws.index.commit("third")

    kv_ws.index.checkout(second)
    assert [e.info["message"] for e in kv_ws.index.log()] == ["second", "first"]
    # and a commit on top extends that ancestry, not a broken root
    kv_ws.terminal("echo four > d.txt")
    kv_ws.index.commit("fourth")
    assert [e.info["message"] for e in kv_ws.index.log()] == [
        "fourth",
        "second",
        "first",
    ]


def test_the_terminal_refuses_a_framework_commit_too(kv_ws):
    from nontainer.wsgit import register_wsgit

    register_wsgit(kv_ws)
    kv_ws.terminal("echo one > a.txt")
    kv_ws.index.commit("first")
    framework = next(e.id for e in _history(kv_ws) if e.info.get("tool") == "terminal")
    r = kv_ws.terminal(f"ws-git checkout {framework[:10]}")
    assert r.exit_code == 1
    assert "not one of yours" in r.stderr
    assert "ws.checkout" in r.stderr
    # ... and the agent's own resolves from a prefix, as git does
    mine = next(e.id for e in kv_ws.index.log())
    assert kv_ws.terminal(f"ws-git checkout {mine[:7]}").exit_code == 0
    assert kv_ws.index.head == mine


# -- a merge whose record cannot land -------------------------------------------


def test_a_merge_record_that_cannot_land_names_the_merge(kv_ws):
    """The merge is in the store and cannot be redone, so the recovery
    is not "run it again" — it is re-establishing the head."""
    from nontainer.errors import BookkeepingLost

    kv_ws.terminal("echo base > a.txt")
    kv_ws.index.commit("base")
    fork = kv_ws.fork("worker")
    try:
        fork.terminal("echo worker > b.txt")
        fork.index.commit("worker work")

        provider = _provider(kv_ws)
        real = provider.commit_keys

        def refuse(info=None, *, keys):
            if (info or {}).get("tool") == "ws-git.merge-record":
                raise WorkspaceError("commit failed: conflicting concurrent (CAS)")
            return real(info, keys=keys)

        provider.commit_keys = refuse
        with pytest.raises(BookkeepingLost) as excinfo:
            kv_ws.merge("worker")
        del provider.commit_keys

        message = str(excinfo.value)
        merge_commit = next(
            e.id for e in _history(kv_ws) if e.info.get("tool") == "ws-git.merge"
        )
        assert f"merge {merge_commit}" in message
        assert "ws.index.checkout" in message
        # the blob in hand is the one the store holds, so the head did
        # not silently advance to a merge nothing recorded
        from nontainer.agentgit import BLOB_KEY, parse_blob

        assert parse_blob(provider.kv.get(BLOB_KEY)) == parse_blob(
            provider.key_at(provider.head, BLOB_KEY)
        )
        assert kv_ws.index.head != merge_commit
        # and the recovery the message names does work: a merge commit
        # is one of the agent's, so it can be checked out
        assert kv_ws.index.checkout(merge_commit) == merge_commit
        assert kv_ws.index.head == merge_commit
        assert merge_commit in [e.id for e in kv_ws.index.log()]
        assert kv_ws.files.read("/workspace/b.txt") == b"worker\n"
    finally:
        fork.close()


# -- the provider contract ------------------------------------------------------


def test_every_provider_takes_the_documented_merge_signature():
    """``Workspace.merge`` passes ``at`` and ``info`` to whatever
    provider it has, including a third-party one with ``caps.merge``
    and no index. The signature is the contract, so it is checked
    against the protocol rather than against one implementation."""
    import inspect

    from nontainer.protocol import WorkspaceProvider
    from nontainer.providers.agentfs import AgentFSProvider
    from nontainer.providers.dir import DirProvider

    documented = set(inspect.signature(WorkspaceProvider.merge).parameters)
    for cls in (KvgitProvider, DirProvider, AgentFSProvider):
        taken = set(inspect.signature(cls.merge).parameters)
        assert documented <= taken, cls.__name__


def test_merge_passes_both_keywords_to_the_provider(tmp_path):
    """The other half: what the facade actually calls with, on a
    provider that has merge without an index — where a drift in the
    call would land first."""
    from nontainer.protocol import Capabilities, MergeOutcome
    from nontainer.providers.dir import DirProvider

    seen = {}

    class MergingDirProvider(DirProvider):
        """The documented contract and nothing else."""

        @property
        def caps(self):
            return Capabilities(versioned=True, merge=True)

        def commit(self, info=None):
            return "c0ffee0"

        def merge(self, source, **kwargs):
            seen["source"] = source
            seen["kwargs"] = kwargs
            return MergeOutcome(
                merged=True, commit="c0ffee", conflicts=(), auto_merged=()
            )

    ws = Workspace(MergingDirProvider(tmp_path / "ws", session="plain"))
    try:
        out = ws.merge("other")
        assert out.merged
        assert seen["source"] == "other"
        assert set(seen["kwargs"]) == {"at", "info"}
        assert seen["kwargs"]["at"] is None
        assert seen["kwargs"]["info"] == {"virtual_parents": []}
    finally:
        ws.close()
