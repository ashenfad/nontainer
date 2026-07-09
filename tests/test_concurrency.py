"""Core-owned single-writer locking + per-execution stderr isolation.

A Workspace is single-writer by contract and enforces it internally:
mutating public calls hold an RLock, so a harness that threads parallel
tool calls onto one session (agno ``arun()`` does) serializes safely —
no interleaved commits, no lost writes — with no adapter lock required.
Read-only accessors stay lock-free. stderr capture is per-execution
(sandtrap's ContextVar router), so concurrent workspaces don't
cross-contaminate streams.
"""

import sys
import threading

import pytest

from nontainer import ModuleGrant, PythonConfig, Workspace
from nontainer.providers import KvgitProvider


@pytest.fixture
def kv_ws():
    provider = KvgitProvider.open(None, session="test-session")
    ws = Workspace(provider)
    yield ws
    ws.close()


# -- parallel mutating calls serialize safely -------------------------------


def test_parallel_terminal_calls_no_lost_writes(kv_ws):
    n_threads, per_thread = 8, 5
    errors: list[str] = []

    def work(tid: int) -> None:
        for i in range(per_thread):
            r = kv_ws.terminal(f"echo line-{tid}-{i} > /t{tid}-{i}.txt")
            if not r:
                errors.append(r.stderr)
            if r.checkpoint is None:
                errors.append(f"no checkpoint for t{tid}-{i}")

    threads = [threading.Thread(target=work, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    # every call's effect survived...
    for tid in range(n_threads):
        for i in range(per_thread):
            assert kv_ws.fs.exists(f"/t{tid}-{i}.txt")
    # ...and each call minted exactly one commit (plus the seed commit):
    # interleaved staged writes would have merged calls into one.
    assert len(list(kv_ws.history())) == n_threads * per_thread + 1


def test_parallel_mixed_mutators(kv_ws):
    """terminal / run_python / write_file racing on one workspace."""
    barrier = threading.Barrier(3)
    results: dict[str, object] = {}

    def via_terminal() -> None:
        barrier.wait()
        results["terminal"] = kv_ws.terminal("echo t > /from-terminal.txt")

    def via_python() -> None:
        barrier.wait()
        results["python"] = kv_ws.run_python("open('/from-python.txt', 'w').write('p')")

    def via_write() -> None:
        barrier.wait()
        results["write"] = kv_ws.write_file("/from-write.txt", "w")

    threads = [
        threading.Thread(target=f) for f in (via_terminal, via_python, via_write)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(bool(r) for r in results.values())
    for name in ("from-terminal", "from-python", "from-write"):
        assert kv_ws.fs.exists(f"/{name}.txt")
    assert len(list(kv_ws.history())) == 4  # seed + one per call


def test_fork_races_writer_without_corruption(kv_ws):
    """fork() checkpoints pending staged state (kvgit) — it must hold
    the lock so it never commits a half-written staged buffer."""
    kv_ws.terminal("echo base > /base.txt")
    done = threading.Event()
    forks: list[Workspace] = []

    def forker() -> None:
        for i in range(5):
            forks.append(kv_ws.fork(f"fork-{i}"))
        done.set()

    def writer() -> None:
        i = 0
        while not done.is_set():
            kv_ws.terminal(f"echo w-{i} > /w-{i}.txt")
            i += 1

    threads = [threading.Thread(target=forker), threading.Thread(target=writer)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    for f in forks:
        assert f.fs.exists("/base.txt")
        f.close()


# -- reentrancy --------------------------------------------------------------


def test_terminal_python_builtin_does_not_deadlock(kv_ws):
    """terminal() holds the lock while the `python` builtin runs the
    shared internal exec path — must not self-deadlock."""
    r = kv_ws.terminal(
        "echo 40 | python -c 'import sys; print(int(sys.stdin.read()) + 2)'"
    )
    assert r, r.stderr
    assert r.stdout.strip() == "42"


def test_host_object_may_reenter_public_api():
    """RLock, not Lock: agent code calling a host object that calls
    back into the workspace's public surface serializes, not deadlocks."""
    provider = KvgitProvider.open(None, session="reenter")
    holder: dict[str, Workspace] = {}

    def snapshot() -> str:
        return holder["ws"].checkpoint(info={"tool": "host-snapshot"})

    ws = Workspace(provider, python=PythonConfig(host_objects={"snapshot": snapshot}))
    holder["ws"] = ws
    try:
        ws.write_file("/x.txt", "1")
        r = ws.run_python("cid = snapshot()")
        assert r, r.error
        assert isinstance(r.namespace["cid"], str)
    finally:
        ws.close()


# -- read-only calls stay lock-free ------------------------------------------


def test_reads_do_not_take_the_lock(kv_ws):
    kv_ws.terminal("echo hi > /a.txt")
    finished = threading.Event()

    def reader() -> None:
        assert kv_ws.head is not None
        assert kv_ws.dirty is False
        assert kv_ws.get("/a.txt") == b"hi\n"
        list(kv_ws.history(limit=1))
        finished.set()

    with kv_ws._lock:  # simulate a long-running mutating call
        t = threading.Thread(target=reader)
        t.start()
        t.join(timeout=5)
    assert finished.is_set(), "read-only accessors blocked behind the lock"


def test_close_during_call_is_clean(kv_ws):
    """close() holds the lock, and mutating calls check open-ness
    INSIDE it — a call racing close either completes fully or raises
    WorkspaceError, never a provider-level error mid-execution
    (PR #7 review)."""
    from nontainer import WorkspaceError

    outcomes: list[str] = []
    barrier = threading.Barrier(2)

    def caller() -> None:
        barrier.wait()
        for i in range(20):
            try:
                kv_ws.terminal(f"echo {i} > /r{i}.txt")
                outcomes.append("ok")
            except WorkspaceError:
                outcomes.append("closed")
            except Exception as e:  # pragma: no cover
                outcomes.append(f"BAD: {type(e).__name__}: {e}")

    def closer() -> None:
        barrier.wait()
        kv_ws.close()

    threads = [threading.Thread(target=caller), threading.Thread(target=closer)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(o in ("ok", "closed") for o in outcomes), outcomes


# -- stderr isolation ---------------------------------------------------------


def _stderr_ws(session: str) -> Workspace:
    provider = KvgitProvider.open(None, session=session)
    cfg = PythonConfig(modules=[ModuleGrant(sys, include="stderr")])
    return Workspace(provider, python=cfg)


def test_concurrent_workspaces_do_not_cross_contaminate_stderr():
    """Two workspaces executing concurrently must each capture only
    their own sys.stderr (impossible with a process-global redirect)."""
    results: dict[str, object] = {}
    barrier = threading.Barrier(2)

    def run(tag: str) -> None:
        ws = _stderr_ws(f"stderr-{tag}")
        try:
            barrier.wait()
            results[tag] = ws.run_python(
                f"for _ in range(100): sys.stderr.write('{tag}\\n')"
            )
        finally:
            ws.close()

    threads = [threading.Thread(target=run, args=(t,)) for t in ("aaa", "bbb")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    a, b = results["aaa"], results["bbb"]
    assert a and b
    assert a.stderr == "aaa\n" * 100
    assert b.stderr == "bbb\n" * 100
