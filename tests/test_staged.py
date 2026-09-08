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
