"""View-call worker pooling.

``exec_python(view=...)`` (apps' handler dispatch) used to mint and reap
a sandbox per call, which under process/kernel isolation meant one
``fork()`` per app request — taken from whatever the host looked like at
that moment. For a live ASGI server that host is multi-threaded, and a
fork that inherits a lock held by a thread the child doesn't have hangs
instead of crashing (sandtrap#38).

``_ViewWorkerPool`` keeps view workers resident: a handful of forks for
the executor's life instead of one per request. The unit tests pin the
pool's bookkeeping against fake sandboxes (no forks — the cap,
liveness, and close paths are where the bugs would be); the integration
tests pin the observable property under real ``isolation="process"``.
"""

import os

import pytest

from nontainer import ModuleGrant, PythonConfig, Workspace
from nontainer.executor import ViewSpec, _view_key, _ViewWorkerPool
from nontainer.providers.kvgit import KvgitProvider

VIEW = ViewSpec(readonly_fs=True, readonly_cache=True, timeout=10.0)


# -- unit: pool bookkeeping ---------------------------------------------------


class FakeSandbox:
    """A sandbox shaped enough for the pool: a worker that can be
    entered, reaped, and pronounced dead."""

    def __init__(self) -> None:
        self.entered = 0
        self.shutdowns = 0
        self.alive = True
        self._process = self

    def is_alive(self) -> bool:  # stands in for multiprocessing.Process
        return self.alive

    def __enter__(self):
        self.entered += 1
        return self

    def shutdown(self) -> None:
        self.shutdowns += 1
        self.alive = False


class InProcessSandbox:
    """``isolation="none"``: no worker, so nothing to keep resident."""


def _pool(size: int = 2, *, enabled: bool = True) -> _ViewWorkerPool:
    return _ViewWorkerPool(size, enabled=enabled)


def _builder(made: list) -> callable:
    def build():
        sb = FakeSandbox()
        made.append(sb)
        return sb

    return build


def test_released_worker_is_reused():
    made: list = []
    pool = _pool()

    first, pooled = pool.acquire(VIEW, _builder(made))
    assert pooled and first.entered == 1
    pool.release(VIEW, first)

    second, pooled = pool.acquire(VIEW, _builder(made))
    assert pooled
    assert second is first  # the same worker, not a second fork
    assert len(made) == 1
    assert first.entered == 1  # entered once, for its whole life


def test_distinct_views_get_distinct_workers():
    made: list = []
    pool = _pool()
    other = ViewSpec(readonly_fs=False, readonly_cache=True, timeout=10.0)

    a, _ = pool.acquire(VIEW, _builder(made))
    b, _ = pool.acquire(other, _builder(made))
    assert a is not b

    pool.release(VIEW, a)
    pool.release(other, b)
    again, _ = pool.acquire(VIEW, _builder(made))
    assert again is a  # keyed by view, not shared across them


def test_saturation_falls_back_to_a_transient_sandbox():
    """The cap bounds resident workers; concurrency past it gets the old
    per-call sandbox rather than queueing behind a checkout."""
    made: list = []
    pool = _pool(size=1)

    held, pooled = pool.acquire(VIEW, _builder(made))
    assert pooled

    extra, pooled = pool.acquire(VIEW, _builder(made))
    assert not pooled  # caller enters/reaps this one itself
    assert extra is not held
    assert extra.entered == 0  # untouched by the pool

    pool.release(VIEW, held)
    back, pooled = pool.acquire(VIEW, _builder(made))
    assert pooled and back is held  # the slot is free again


def test_checked_out_workers_are_never_handed_out_twice():
    """The property the whole pool rests on: one exec at a time per
    sandbox. ``ProcessSandbox.exec`` writes and reads one pipe with no
    lock of its own, so two threads sharing a worker would interleave
    messages on it."""
    made: list = []
    pool = _pool(size=4)

    held = [pool.acquire(VIEW, _builder(made))[0] for _ in range(4)]
    assert len({id(sb) for sb in held}) == 4


def test_dead_worker_is_reaped_not_recycled():
    made: list = []
    pool = _pool(size=1)

    worker, _ = pool.acquire(VIEW, _builder(made))
    worker.alive = False  # crashed, OOM-killed, or killed as unresponsive
    pool.release(VIEW, worker)

    replacement, pooled = pool.acquire(VIEW, _builder(made))
    assert pooled
    assert replacement is not worker
    assert len(made) == 2
    # ...and the dead one's slot was freed, not leaked against the cap.
    assert pool._live[_view_key(VIEW)] == 1


def test_worker_that_dies_while_idle_is_dropped_on_checkout():
    made: list = []
    pool = _pool(size=1)

    worker, _ = pool.acquire(VIEW, _builder(made))
    pool.release(VIEW, worker)
    worker.alive = False  # died between requests

    replacement, pooled = pool.acquire(VIEW, _builder(made))
    assert pooled and replacement is not worker
    assert worker.shutdowns == 1  # reaped rather than handed out


def test_close_reaps_idle_and_checked_out_workers():
    made: list = []
    pool = _pool()

    # Two at once, so releasing one leaves the other checked out —
    # releasing first would just hand the same worker back.
    in_flight, _ = pool.acquire(VIEW, _builder(made))
    idle, _ = pool.acquire(VIEW, _builder(made))
    pool.release(VIEW, idle)

    pool.close()
    assert idle.shutdowns == 1
    assert in_flight.shutdowns == 0  # still running its call

    pool.release(VIEW, in_flight)
    assert in_flight.shutdowns == 1  # ...reaped when it comes back

    after, pooled = pool.acquire(VIEW, _builder(made))
    assert not pooled  # a closed pool builds transients, never recycles
    assert after.entered == 0


def test_disabled_pool_always_builds_transients():
    for pool in (_pool(size=0), _pool(size=4, enabled=False)):
        made: list = []
        sb, pooled = pool.acquire(VIEW, _builder(made))
        assert not pooled
        assert sb.entered == 0  # the caller's context manager enters it
        assert not pool.enabled


def test_workerless_sandbox_is_not_pooled_and_frees_its_reservation():
    """``isolation="none"`` view sandboxes have no worker to keep. The
    pool must hand them straight back without burning a slot."""
    pool = _pool(size=1)

    sb, pooled = pool.acquire(VIEW, InProcessSandbox)
    assert not pooled and isinstance(sb, InProcessSandbox)
    assert pool._live.get(_view_key(VIEW), 0) == 0

    made: list = []
    worker, pooled = pool.acquire(VIEW, _builder(made))
    assert pooled  # the cap was never consumed


def test_failed_worker_start_frees_its_reservation():
    pool = _pool(size=1)

    def explode():
        raise RuntimeError("fork failed")

    with pytest.raises(RuntimeError, match="fork failed"):
        pool.acquire(VIEW, explode)
    assert pool._live.get(_view_key(VIEW), 0) == 0

    made: list = []
    _, pooled = pool.acquire(VIEW, _builder(made))
    assert pooled  # a failed start doesn't permanently shrink the pool


def test_view_key_tolerates_a_list_of_extra_classes():
    """``ViewSpec`` is frozen but stores what it is handed, so
    ``extra_classes`` may be an unhashable list — the same coercion the
    policy memo makes."""

    class Marker:
        pass

    as_list = ViewSpec(extra_classes=[Marker])
    as_tuple = ViewSpec(extra_classes=(Marker,))
    assert _view_key(as_list) == _view_key(as_tuple)
    hash(_view_key(as_list))  # usable as a dict key at all


# -- integration: real workers under process isolation ------------------------

pytest.importorskip("sandtrap.fs.remote", reason="needs sandtrap with RemoteFS")

# os.getpid is the cheapest way to ask "which worker ran this?" from
# inside the sandbox; the stdlib preset's os grant doesn't include it.
PIDS = [ModuleGrant(os, include=["getpid"])]
WHICH_WORKER = "import os\npid = os.getpid()"


def _ws(session: str, **cfg):
    return Workspace(
        KvgitProvider.open(None, session=session),
        python=PythonConfig(isolation="process", modules=PIDS, **cfg),
    )


def test_view_calls_reuse_one_worker():
    ws = _ws("pool-reuse")
    try:
        pids = {
            ws.exec_python(WHICH_WORKER, view=VIEW).namespace["pid"] for _ in range(4)
        }
        assert len(pids) == 1  # four requests, one fork
        assert pids != {os.getpid()}  # ...and it isn't the host
    finally:
        ws.close()


def test_view_workers_zero_restores_per_call_workers():
    ws = _ws("pool-off", view_workers=0)
    try:
        pids = [
            ws.exec_python(WHICH_WORKER, view=VIEW).namespace["pid"] for _ in range(3)
        ]
        assert len(set(pids)) == 3
    finally:
        ws.close()


def test_pooled_worker_is_reaped_on_close():
    ws = _ws("pool-close")
    ws.exec_python(WHICH_WORKER, view=VIEW)
    pool = ws._executor._pool
    resident = [sb for group in pool._idle.values() for sb in group]
    assert len(resident) == 1
    # Grab the handle now: shutdown() clears the sandbox's own.
    process = resident[0]._process

    ws.close()
    process.join(timeout=5.0)
    assert not process.is_alive()


def test_a_worker_that_dies_while_idle_doesnt_poison_the_pool():
    """A resident worker killed between requests costs no request: the
    next checkout finds it dead, drops it, and forks a replacement."""
    ws = _ws("pool-crash")
    try:
        first = ws.exec_python(WHICH_WORKER, view=VIEW).namespace["pid"]

        resident = next(
            sb for group in ws._executor._pool._idle.values() for sb in group
        )
        # kill+join, not os.kill: SIGKILL is asynchronous, and a checkout
        # racing the death would hand out a worker that is still alive.
        resident._process.kill()
        resident._process.join(timeout=5.0)

        result = ws.exec_python(WHICH_WORKER, view=VIEW)
        assert result.error is None
        assert result.namespace["pid"] != first
    finally:
        ws.close()


def test_concurrent_view_calls_stay_correct():
    """Lock-free frozen dispatch threads requests onto one workspace.
    Each must get its own worker; a shared one would interleave two
    request/response pairs on a single pipe."""
    import threading

    ws = _ws("pool-threads")
    results: dict[int, tuple] = {}

    def work(n: int) -> None:
        r = ws.exec_python(
            f"import os\ntotal = sum(range(200_000)) + {n}\npid = os.getpid()",
            view=VIEW,
        )
        results[n] = (r.error, r.namespace.get("total"), r.namespace.get("pid"))

    try:
        threads = [threading.Thread(target=work, args=(n,)) for n in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        assert len(results) == 6
        for n, (error, total, pid) in results.items():
            assert error is None, error
            assert total == sum(range(200_000)) + n  # no crossed replies
            assert pid != os.getpid()
    finally:
        ws.close()


def test_run_python_still_uses_the_session_worker():
    """The pool is for view calls only — the default sandbox entered at
    open() still serves ``run_python``, and its state persists."""
    ws = _ws("pool-default")
    try:
        session_pid = ws.run_python(WHICH_WORKER).namespace["pid"]
        again = ws.run_python(WHICH_WORKER).namespace["pid"]
        assert session_pid == again

        view_pid = ws.exec_python(WHICH_WORKER, view=VIEW).namespace["pid"]
        assert view_pid != session_pid  # a view is its own worker
    finally:
        ws.close()
