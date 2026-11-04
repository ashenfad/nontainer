"""ws.land: a framework-level apply of files, cache and cwd between two
commits, for work run in a fork of the session (#302).

The host forks the session at the commit the work starts from; the work
runs in the fork (often another process, with its own Store handle on
the same directory) and commits its result there; the host lands the
change from the fork point to that result onto the session.
"""

import pytest

from nontainer import (
    CommitNotFoundError,
    HeadMoved,
    LandConflict,
    Mount,
    NotSupportedError,
    Ref,
    Store,
    WorkspaceError,
)
from nontainer.agentgit import BLOB_KEY
from nontainer.planes import COMPACTION_PREFIX, CONVERSATION_INDEX_KEY
from nontainer.wsgit import register_wsgit


def _dispatch(ws, info=None):
    """Land staged work and return the commit a task starts from."""
    with ws.lock:
        return ws.commit(info or {"tool": "dispatch"})


def _run_task(path, session, at, work, name="_task.1"):
    """Fork ``session`` at ``at`` through a Store handle of its own, as
    a worker process would, run ``work`` there and commit; returns the
    result's ref."""
    worker = Store(path)
    child = worker.fork(session, name, at=at)
    try:
        work(child)
        result = child.commit({"tool": "task"})
    finally:
        child.close()
        worker.close()
    return f"{name}@{result}"


@pytest.fixture
def store(tmp_path):
    st = Store(tmp_path)
    yield st
    st.close()


@pytest.fixture
def ws(store):
    ws = store.open("demo", autocommit=False)
    register_wsgit(ws)
    yield ws
    ws.close()


def test_land_carries_files_cache_and_cwd_from_a_fork(tmp_path, store, ws):
    """The issue's scenario: what a take leaves behind, a land brings."""
    ws.files.write("a.txt", "base")
    ws.files.write("gone.txt", "the task deletes this")
    ws.cache["n"] = 1
    ws.cache["obsolete"] = "remove me"
    base = _dispatch(ws)
    ws.files.write("a.txt", "edited")  # the agent's own work, committed below
    base = _dispatch(ws)
    before = ws.index.status()
    stamps = {}

    def work(child):
        child.files.write("out.txt", "result")
        stamps["out"] = child.files.fs.stat("/workspace/out.txt").modified_at
        child.files.fs.remove("/workspace/gone.txt")
        child.cache["n"] = 2
        child.cache["kept"] = None  # a stored None, not a deletion
        child.cache["shape"] = {"rows": (1, 2), "tags": {"a"}}
        del child.cache["obsolete"]
        assert child.terminal("mkdir -p w/empty && cd w")

    theirs = _run_task(tmp_path, "demo", base, work)
    landed = ws.land(base, theirs, expect_head=base)

    assert landed is not None and ws.head == landed
    assert ws.files.read("/workspace/out.txt") == b"result"
    assert ws.files.fs.stat("/workspace/out.txt").modified_at == stamps["out"]
    assert not ws.files.exists("/workspace/gone.txt")
    assert ws.files.fs.isdir("/workspace/w/empty")
    assert ws.files.fs.getcwd() == "/workspace/w"
    assert ws.cache["n"] == 2
    assert "obsolete" not in ws.cache
    assert "kept" in ws.cache and ws.cache["kept"] is None
    assert ws.cache["shape"] == {"rows": (1, 2), "tags": {"a"}}
    # The agent sees the task's work as working-tree changes, as if it
    # had run here: gone.txt was never the agent's, so with it deleted
    # it is no change at all.
    status = ws.index.status()
    assert set(status.unstaged) == (set(before.unstaged) - {"/workspace/gone.txt"}) | {
        "/workspace/out.txt"
    }
    assert not ws._provider.dirty


def test_land_leaves_this_sessions_bookkeeping_alone(tmp_path, store, ws):
    """The fork's ws-git blob, conversation and compaction records are
    its own; none of them lands, and the view seed stays this one's."""
    ws.files.write("a.txt", "a")
    ws.index.commit("seed")  # this session's agent git
    base = _dispatch(ws)
    kv = ws._provider.kv
    held = {key: kv.get(key) for key in (BLOB_KEY, CONVERSATION_INDEX_KEY)}

    def work(child):
        child.files.write("b.txt", "b")
        child.index.commit("the task's own agent commit")
        child._provider.kv[COMPACTION_PREFIX + "fold/x"] = {"by": "the task"}
        child._provider.kv[CONVERSATION_INDEX_KEY] = {"session": "_task.1"}

    theirs = _run_task(tmp_path, "demo", base, lambda c: (register_wsgit(c), work(c)))
    assert ws.land(base, theirs) is not None
    assert ws.files.read("/workspace/b.txt") == b"b"
    assert {key: kv.get(key) for key in held} == held
    assert COMPACTION_PREFIX + "fold/x" not in kv
    assert [e.info.get("message") for e in ws.index.log()][:1] == ["seed"]


def test_land_records_info_as_given(tmp_path, store, ws):
    ws.files.write("a.txt", "a")
    base = _dispatch(ws)
    theirs = _run_task(tmp_path, "demo", base, lambda c: c.files.write("b.txt", "b"))
    landed = ws.land(base, theirs, info={"attempt": 2, "digest": "abc"})
    entry = ws.log()[0]
    assert entry.id == landed
    assert entry.info == {
        "tool": "land",
        "attempt": 2,
        "digest": "abc",
        "landed": [f"demo@{base}", str(Ref.parse(theirs))],
    }

    again = _run_task(
        tmp_path, "demo", landed, lambda c: c.files.write("c.txt", "c"), "_task.2"
    )
    ws.land(landed, again, info={"tool": "task-result"})
    assert ws.log()[0].info["tool"] == "task-result"  # a caller's tool stands

    with pytest.raises(ValueError, match="'landed' is land's own"):
        ws.land(landed, again, info={"landed": "mine"})


def test_a_change_holding_nothing_commits_nothing(tmp_path, store, ws):
    ws.files.write("a.txt", "a")
    base = _dispatch(ws)
    assert ws.land(base, base) is None
    theirs = _run_task(tmp_path, "demo", base, lambda c: c.files.write("b.txt", "b"))
    landed = ws.land(base, theirs)
    assert ws.land(base, theirs) is None  # already here
    assert ws.head == landed


def test_expect_head_refuses_a_moved_session_and_writes_nothing(tmp_path, store, ws):
    ws.files.write("a.txt", "a")
    base = _dispatch(ws)
    theirs = _run_task(tmp_path, "demo", base, lambda c: c.files.write("b.txt", "b"))
    ws.files.write("a.txt", "moved on")
    moved = _dispatch(ws)

    with pytest.raises(HeadMoved, match="nothing was landed") as caught:
        ws.land(base, theirs, expect_head=base)
    assert (caught.value.expected, caught.value.head) == (base, moved)
    assert ws.head == moved
    assert not ws.files.exists("/workspace/b.txt")
    assert not ws._provider.dirty


def test_expect_head_is_the_commits_own_check_across_processes(tmp_path, store, ws):
    """Another process commits to the session after this handle last
    saw it: the handle still holds the expected head, and the commit's
    compare-and-swap is what refuses."""
    ws.files.write("a.txt", "a")
    base = _dispatch(ws)
    theirs = _run_task(tmp_path, "demo", base, lambda c: c.files.write("b.txt", "b"))
    other = Store(tmp_path)
    with other.open("demo", autocommit=False) as elsewhere:
        elsewhere.files.write("z.txt", "elsewhere")
        tip = elsewhere.commit({"tool": "elsewhere"})
    other.close()

    assert ws.head == base  # this handle has not seen it
    with pytest.raises(HeadMoved) as caught:
        ws.land(base, theirs, expect_head=base)
    assert caught.value.head == tip
    assert not ws._provider.dirty


def test_staged_work_is_refused(tmp_path, store, ws):
    ws.files.write("a.txt", "a")
    base = _dispatch(ws)
    theirs = _run_task(tmp_path, "demo", base, lambda c: c.files.write("b.txt", "b"))
    ws.files.write("a.txt", "staged")
    with pytest.raises(WorkspaceError, match="uncommitted changes"):
        ws.land(base, theirs, expect_head=base)
    assert ws.files.read("/workspace/a.txt") == b"staged"


def test_without_the_guard_a_disjoint_change_lands_on_a_moved_head(tmp_path, store, ws):
    """Two tasks from one fork point: the second lands after the first
    when they touched different things, a directory included."""
    ws.files.write("a.txt", "a")
    ws.cache["n"] = 1
    base = _dispatch(ws)

    def first(child):
        child.files.write("out/one.txt", "1")
        child.cache["first"] = True

    def second(child):
        child.files.write("out/two.txt", "2")
        child.cache["second"] = True

    one = _run_task(tmp_path, "demo", base, first, "_task.1")
    two = _run_task(tmp_path, "demo", base, second, "_task.2")
    ws.land(base, one)
    ws.land(base, two)
    assert ws.files.read("/workspace/out/one.txt") == b"1"
    assert ws.files.read("/workspace/out/two.txt") == b"2"
    assert ws.cache["first"] and ws.cache["second"]


def test_without_the_guard_an_overlapping_change_is_refused_whole(tmp_path, store, ws):
    ws.files.write("a.txt", "a")
    ws.cache["n"] = 1
    base = _dispatch(ws)

    def work(child):
        child.files.write("a.txt", "the task's a")
        child.files.write("b.txt", "b")
        child.cache["n"] = 2

    theirs = _run_task(tmp_path, "demo", base, work)
    ws.files.write("a.txt", "this session's a")
    ws.cache["n"] = 3
    head = _dispatch(ws)

    with pytest.raises(LandConflict, match="Nothing was written") as caught:
        ws.land(base, theirs)
    assert caught.value.conflicts == ("/workspace/a.txt", "cache:n")
    assert ws.head == head
    assert not ws.files.exists("/workspace/b.txt")
    assert b"<<<<<<<" not in ws.files.read("/workspace/a.txt")
    assert not ws._provider.dirty


def test_without_the_guard_a_concurrent_writer_is_landed_on(tmp_path, store, ws):
    ws.files.write("a.txt", "a")
    base = _dispatch(ws)
    theirs = _run_task(tmp_path, "demo", base, lambda c: c.files.write("b.txt", "b"))
    other = Store(tmp_path)
    with other.open("demo", autocommit=False) as elsewhere:
        elsewhere.files.write("z.txt", "elsewhere")
        tip = elsewhere.commit({"tool": "elsewhere"})
    other.close()

    landed = ws.land(base, theirs)
    entry = ws.log()[0]
    assert entry.id == landed and entry.parents == (tip,)
    assert ws.files.read("/workspace/z.txt") == b"elsewhere"
    assert ws.files.read("/workspace/b.txt") == b"b"


def test_authoring_output_lands_like_any_file(tmp_path, store, ws):
    ws.files.write("a.txt", "a")
    base = _dispatch(ws)
    theirs = _run_task(
        tmp_path,
        "demo",
        base,
        lambda c: c.files.write("app/screenshots/shot.png", b"\x89PNG"),
    )
    ws.land(base, theirs)
    assert ws.files.read("/workspace/app/screenshots/shot.png") == b"\x89PNG"


def test_a_narrowed_view_holds(tmp_path, store, ws):
    ws.files.write("mine/a.txt", "a")
    ws.files.write("secret.txt", "hidden")
    _dispatch(ws)
    child = ws.fork("narrow", paths=["mine"])
    try:
        base = child.head

        def hidden(task):
            task.files.write("secret.txt", "overwritten")

        theirs = _run_task(tmp_path, "narrow", base, hidden, "_task.h")
        with pytest.raises(PermissionError, match="outside this session's view"):
            child.land(base, theirs)
        assert child.head == base
        assert not child._provider.dirty

        created = _run_task(
            tmp_path, "narrow", base, lambda t: t.files.write("new.txt", "n"), "_task.n"
        )
        child.land(base, created)
        assert child.files.read("/workspace/new.txt") == b"n"  # joins the view
    finally:
        child.close()


def test_refs_name_exact_states(tmp_path, store, ws):
    ws.files.write("a.txt", "a")
    base = _dispatch(ws)
    theirs = _run_task(tmp_path, "demo", base, lambda c: c.files.write("b.txt", "b"))
    parsed = Ref.parse(theirs)
    store.tags.add(theirs, "result")

    # A Ref, a store tag's ref, and short ids all name the same state.
    assert ws.land(f"demo@{base[:7]}", parsed) is not None
    assert ws.land(base, ws.expand_ref("result")) is None  # already landed
    # A bare word is this session's: the task's commit is not.
    with pytest.raises(CommitNotFoundError):
        ws.land(base, parsed.commit)


def test_the_executor_is_told_its_view_moved(tmp_path, store, ws, monkeypatch):
    ws.files.write("a.txt", "a")
    base = _dispatch(ws)
    theirs = _run_task(tmp_path, "demo", base, lambda c: c.files.write("b.txt", "b"))
    calls = []
    monkeypatch.setattr(ws._runtime, "mark_stale", lambda: calls.append(1))
    ws.land(base, theirs)
    assert calls


def test_a_mounted_session_keeps_its_cwd(tmp_path):
    """A mount composition keeps the cwd itself and never commits it,
    so a land moves the files and leaves the cwd where it is."""
    src = tmp_path / "data"
    src.mkdir()
    st = Store(tmp_path / "store")
    ws = st.open("demo", autocommit=False, mounts={"/data": Mount(src)})
    try:
        ws.files.write("a.txt", "a")
        assert ws.terminal("mkdir -p keep && cd keep")
        base = _dispatch(ws)

        def work(child):
            child.files.write("w/out.txt", "out")
            assert child.terminal("cd w")

        theirs = _run_task(tmp_path / "store", "demo", base, work)
        ws.land(base, theirs)
        assert ws.files.read("/workspace/w/out.txt") == b"out"
        assert ws.files.fs.getcwd() == "/workspace/keep"
        assert ws._provider.fs.getcwd() == "/"  # the composition's floor
    finally:
        ws.close()
        st.close()


def test_a_worker_opened_with_mounts_leaves_the_cwd_where_it_was(tmp_path):
    """Such a worker parks its filesystem at the root and commits that,
    which is no cwd of its own: the session's cwd stays."""
    src = tmp_path / "data"
    src.mkdir()
    st = Store(tmp_path / "store")
    ws = st.open("demo", autocommit=False)
    try:
        ws.files.write("a.txt", "a")
        assert ws.terminal("mkdir -p sub && cd sub")
        base = _dispatch(ws)
        worker = Store(tmp_path / "store")
        worker.fork("demo", "_task", at=base).close()
        with worker.open("_task", autocommit=False, mounts={"/data": Mount(src)}) as w:
            w.files.write("/workspace/out.txt", "out")
            result = w.commit({"tool": "task"})
        worker.close()

        ws.land(base, f"_task@{result}")
        assert ws.files.read("/workspace/out.txt") == b"out"
        assert ws.files.fs.getcwd() == "/workspace/sub"
    finally:
        ws.close()
        st.close()


def test_a_provider_without_land_says_so(tmp_path):
    from plain_provider import PlainProvider

    from nontainer import Workspace

    ws = Workspace(PlainProvider(tmp_path / "ws", session="dir"))
    try:
        assert not ws.caps.land
        with pytest.raises(NotSupportedError, match="caps.land"):
            ws.land("a", "b")
    finally:
        ws.close()


def test_a_frozen_snapshot_lands_nothing(tmp_path, store, ws):
    ws.files.write("a.txt", "a")
    base = _dispatch(ws)
    theirs = _run_task(tmp_path, "demo", base, lambda c: c.files.write("b.txt", "b"))
    with store.resolve(f"demo@{base}") as snap:
        with pytest.raises(NotSupportedError, match="frozen"):
            snap.land(base, theirs)
