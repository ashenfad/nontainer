"""``PythonConfig.supervise_tree`` and ``Runtime.drain``: a host that runs
one job at a time learns when everything the last job's workers started is
gone, while sandtrap's broker stays warm for the next."""

import multiprocessing.forkserver
import os
import signal
import sys
import time

import pytest
import tree_state

from nontainer import (
    DrainResult,
    ModuleGrant,
    NotSupportedError,
    PythonConfig,
    Runtime,
    ViewSpec,
    Workspace,
)
from nontainer.providers.kvgit import KvgitProvider

linux_only = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="supervise_tree is Linux-only"
)

VIEW = ViewSpec(readonly_fs=True, readonly_cache=True, timeout=10.0)


def _ws(session, **cfg):
    cfg.setdefault("isolation", "process")
    # host_fs: the helper's subprocess opens /dev/null on the real
    # filesystem, not the workspace's.
    cfg.setdefault("modules", [ModuleGrant(tree_state, host_fs=True)])
    return Workspace(
        KvgitProvider.open(None, session=session), python=PythonConfig(**cfg)
    )


def _supervised(session, **cfg):
    return _ws(session, supervise_tree=True, tree_grace=0.2, **cfg)


def _running(pid):
    try:
        with open(f"/proc/{pid}/stat") as f:
            state = f.read().rsplit(")", 1)[1].split()[0]
    except FileNotFoundError:
        return False
    return state != "Z"


def _call(ws, expr, view=None):
    result = ws.runtime.exec_python(
        f"import tree_state\nprint({expr})", view=view, echo="none"
    )
    assert result.error is None, result.error
    return int(result.stdout)


@pytest.fixture
def helpers():
    """Pids of helpers a test started; any still running are killed after it."""
    pids = []
    yield pids
    for pid in pids:
        if _running(pid):
            os.kill(pid, signal.SIGKILL)


@linux_only
def test_drain_confirms_what_the_session_worker_started_is_gone(helpers):
    ws = _supervised("drain-session")
    try:
        helpers.append(_call(ws, "tree_state.spawn(60, ignore_term=True)"))
        result = ws.runtime.drain()
    finally:
        ws.close()
    assert result == DrainResult("confirmed", workers=1, helpers=1, killed=1)
    assert not _running(helpers[0])


@linux_only
def test_two_jobs_share_the_broker_with_fresh_workers(helpers):
    seen = []
    for job in range(2):
        ws = _supervised(f"drain-job-{job}")
        try:
            worker = _call(ws, "tree_state.me()")
            helpers.append(_call(ws, "tree_state.spawn(60)"))
            broker = multiprocessing.forkserver._forkserver._forkserver_pid
            assert ws.runtime.drain().confirmed
        finally:
            ws.close()
        assert not _running(helpers[-1])
        seen.append((broker, worker))
    (broker_1, worker_1), (broker_2, worker_2) = seen
    if broker_1 is None:
        pytest.skip("workers here do not come from a forkserver broker")
    assert broker_1 == broker_2
    assert worker_1 != worker_2


@linux_only
def test_view_workers_are_drained_with_the_session(helpers):
    ws = _supervised("drain-views")
    try:
        helpers.append(_call(ws, "tree_state.spawn(60)", view=VIEW))
        helpers.append(_call(ws, "tree_state.spawn(60)"))
        result = ws.runtime.drain()
    finally:
        ws.close()
    # One pooled view worker and the session worker.
    assert result == DrainResult("confirmed", workers=2, helpers=2, killed=0)
    assert not any(_running(pid) for pid in helpers)


@linux_only
def test_a_replaced_session_worker_is_still_accounted_for(helpers):
    ws = _supervised("drain-replaced")
    try:
        helpers.append(_call(ws, "tree_state.spawn(60)"))
        os.kill(_call(ws, "tree_state.me()"), signal.SIGKILL)
        deadline = time.monotonic() + 5
        while _running(helpers[0]) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not _running(helpers[0])  # ended when its worker died
        assert _call(ws, "1") == 1  # on a fresh worker
        result = ws.runtime.drain()
    finally:
        ws.close()
    assert result == DrainResult("confirmed", workers=2, helpers=1, killed=0)


@linux_only
def test_drain_after_close_still_confirms(helpers):
    ws = _supervised("drain-after-close")
    helpers.append(_call(ws, "tree_state.spawn(60)"))
    ws.close()
    assert ws.runtime.drain().confirmed
    assert not _running(helpers[0])


@linux_only
def test_a_drained_runtime_runs_nothing():
    ws = _supervised("drain-closed")
    try:
        assert ws.runtime.drain().confirmed
        with pytest.raises(RuntimeError, match="closed"):
            ws.runtime.exec_python("x = 1")
    finally:
        ws.close()


def test_drain_is_unsupported_without_supervise_tree():
    ws = _ws("drain-unsupervised")
    try:
        assert _call(ws, "1") == 1
        assert ws.runtime.drain() == DrainResult("unsupported")
    finally:
        ws.close()


def test_drain_is_unsupported_in_process():
    ws = Workspace(KvgitProvider.open(None, session="drain-in-process"))
    try:
        assert ws.runtime.drain() == DrainResult("unsupported")
    finally:
        ws.close()


def test_supervise_tree_in_process_is_refused():
    with pytest.raises(NotSupportedError, match="supervise_tree"):
        _ws("drain-refused", isolation="none", supervise_tree=True)


@pytest.mark.skipif(sys.platform.startswith("linux"), reason="Linux supports it")
def test_supervise_tree_off_linux_fails_at_open():
    from sandtrap import IsolationUnavailable

    with pytest.raises(IsolationUnavailable, match="needs Linux"):
        _ws("drain-off-linux", supervise_tree=True)


def test_a_runtime_that_does_not_execute_confirms_at_once():
    from nontainer.executor import NoExecutor

    ws = Workspace(KvgitProvider.open(None, session="drain-no-exec"))
    rt = Runtime(ws, executor=NoExecutor("files only"))
    try:
        assert rt.drain() == DrainResult("confirmed")
    finally:
        rt.close()
        ws.close()
