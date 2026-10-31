"""``PythonConfig.worker_setup``: every worker is prepared before an
execution reaches it — the session worker, pooled and transient view
workers, and each replacement — and ``Runtime.warm(view)`` prepares the
pool ahead of traffic."""

import json
import os
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest
import setup_state

from nontainer import (
    ModuleGrant,
    NotSupportedError,
    PythonConfig,
    ViewSpec,
    WorkerSetup,
    WorkerSetupError,
    Workspace,
)
from nontainer.providers.kvgit import KvgitProvider

pytest.importorskip("sandtrap.fs.remote", reason="needs sandtrap with RemoteFS")

VIEW = ViewSpec(readonly_fs=True, readonly_cache=True, timeout=10.0)
PREPARE = "import setup_state\nsetup_state.prepare()"
STATE = "import json, setup_state\nprint(json.dumps(setup_state.state()))"


class Gate:
    """A host object the setup calls: it refuses while ``closed``."""

    def __init__(self) -> None:
        self.closed = False

    def check(self) -> None:
        if self.closed:
            raise RuntimeError("gate closed")


class Admissions:
    """An ``admit`` gate that records each setup and whether it exited."""

    def __init__(self) -> None:
        self.events = []
        self.open = 0
        self.lock = threading.Lock()

    @contextmanager
    def __call__(self, event):
        with self.lock:
            self.events.append(event)
            self.open += 1
        try:
            yield
        finally:
            with self.lock:
                self.open -= 1


def _ws(session, source=PREPARE, *, admit=None, timeout=None, **cfg):
    setup = WorkerSetup(source, timeout=timeout, admit=admit)
    return Workspace(
        KvgitProvider.open(None, session=session),
        python=PythonConfig(
            isolation="process",
            modules=[ModuleGrant(setup_state, host_fs=True)],
            worker_setup=setup,
            **cfg,
        ),
    )


def _state(ws, view=None):
    result = ws.runtime.exec_python(STATE, view=view, echo="none")
    assert result.error is None, result.error
    return tuple(json.loads(result.stdout))


def test_the_session_worker_is_prepared_before_its_first_execution():
    admit = Admissions()
    ws = _ws("setup-session", admit=admit)
    try:
        pid, prepared_in = _state(ws)
        assert prepared_in == pid
        assert ws.run_python(STATE).error is None
    finally:
        ws.close()
    (event,) = admit.events
    assert (event.worker, event.view, event.pooled, event.generation) == (
        "session",
        None,
        True,
        1,
    )
    assert admit.open == 0


def test_pooled_transient_and_reaped_view_workers_are_all_prepared(tmp_path):
    """The issue's own reproduction: a call while the pooled worker is
    busy gets a transient worker, and a call after reap_idle a new one;
    each is prepared before the call runs."""
    admit = Admissions()
    ws = _ws("setup-views", admit=admit, warm_view_workers=1)
    entered, release = tmp_path / "entered", tmp_path / "release"
    try:
        pooled = _state(ws, VIEW)
        assert pooled[1] == pooled[0]
        with ThreadPoolExecutor(max_workers=1) as pool:
            held = pool.submit(
                ws.runtime.exec_python,
                "import setup_state\n"
                f"print(setup_state.hold({str(entered)!r}, {str(release)!r}))",
                view=VIEW,
                echo="none",
            )
            try:
                deadline = time.monotonic() + 10
                while not entered.exists():
                    assert time.monotonic() < deadline, "barrier not entered"
                    time.sleep(0.01)
                transient = _state(ws, VIEW)
            finally:
                release.touch()
            assert held.result(timeout=10).error is None
        assert transient[0] != pooled[0] and transient[1] == transient[0]
        assert ws.runtime.reap_idle(0) == 1
        replaced = _state(ws, VIEW)
        assert replaced[0] != pooled[0] and replaced[1] == replaced[0]
    finally:
        ws.close()
    views = [e for e in admit.events if e.worker == "view"]
    assert [e.pooled for e in views] == [True, False, True]
    assert all(e.view == VIEW for e in views)


def test_warm_prepares_the_views_pool_ahead_of_its_calls():
    admit = Admissions()
    ws = _ws("setup-warm", admit=admit, warm_view_workers=2)
    try:
        assert ws.runtime.warm(VIEW) == 2
        assert [e.worker for e in admit.events] == ["view", "view"]
        assert ws.runtime.warm(VIEW) == 0  # already full
        pid, prepared_in = _state(ws, VIEW)
        assert prepared_in == pid
        assert len(admit.events) == 2  # the call found a prepared worker
    finally:
        ws.close()


def test_warm_without_a_setup_still_starts_the_workers():
    ws = Workspace(
        KvgitProvider.open(None, session="warm-plain"),
        python=PythonConfig(isolation="process", warm_view_workers=1),
    )
    try:
        assert ws.runtime.warm(VIEW) == 1
        assert ws.runtime.reap_idle(0) == 1
    finally:
        ws.close()


def test_warm_is_zero_in_process_and_after_close():
    ws = Workspace(KvgitProvider.open(None, session="warm-none"))
    try:
        assert ws.runtime.warm(VIEW) == 0
    finally:
        ws.close()
    assert ws.runtime.warm(VIEW) == 0


def test_a_failed_setup_raises_and_the_next_call_starts_afresh():
    gate = Gate()
    gate.closed = True
    admit = Admissions()
    ws = _ws(
        "setup-fails",
        f"gate.check()\n{PREPARE}",
        admit=admit,
        host_objects={"gate": gate},
    )
    try:
        with pytest.raises(WorkerSetupError, match="gate closed"):
            ws.runtime.exec_python(STATE, view=VIEW)
        with pytest.raises(WorkerSetupError, match="session worker"):
            ws.runtime.exec_python(STATE)
        assert admit.open == 0
        gate.closed = False
        pid, prepared_in = _state(ws, VIEW)
        assert prepared_in == pid
        pid, prepared_in = _state(ws)
        assert prepared_in == pid
    finally:
        ws.close()


def test_a_failed_warm_raises():
    gate = Gate()
    gate.closed = True
    ws = _ws(
        "setup-warm-fails", f"gate.check()\n{PREPARE}", host_objects={"gate": gate}
    )
    try:
        with pytest.raises(WorkerSetupError):
            ws.runtime.warm(VIEW)
        with pytest.raises(WorkerSetupError):
            ws.runtime.warm()
    finally:
        ws.close()


def test_a_setup_past_its_budget_fails_the_start():
    ws = _ws("setup-timeout", "while True:\n    pass", timeout=0.3)
    try:
        with pytest.raises(WorkerSetupError, match="timeout"):
            ws.runtime.exec_python(STATE, view=VIEW)
    finally:
        ws.close()


def test_a_crashed_session_worker_is_prepared_again():
    ws = _ws("setup-crash")
    try:
        first, _ = _state(ws)
        os.kill(first, signal.SIGKILL)
        deadline = time.monotonic() + 5
        while ws.runtime._executor._sandbox._process.is_alive():
            assert time.monotonic() < deadline
            time.sleep(0.01)
        pid, prepared_in = _state(ws)
        assert pid != first and prepared_in == pid
    finally:
        ws.close()


def test_a_setup_in_process_is_refused():
    with pytest.raises(NotSupportedError, match="no worker"):
        Workspace(
            KvgitProvider.open(None, session="setup-none"),
            python=PythonConfig(worker_setup=WorkerSetup(PREPARE)),
        )


def test_a_setup_that_does_not_compile_is_refused_when_built():
    with pytest.raises(SyntaxError):
        WorkerSetup("def (")


@pytest.mark.parametrize("bad", [0, -1.0, float("nan"), float("inf"), True])
def test_a_setup_timeout_must_be_positive_and_finite(bad):
    with pytest.raises(ValueError, match="positive, finite"):
        WorkerSetup(PREPARE, timeout=bad)


def test_worker_setup_takes_a_worker_setup():
    with pytest.raises(TypeError, match="WorkerSetup"):
        PythonConfig(worker_setup=PREPARE)


def test_interrupt_cancels_a_setup():
    admit = Admissions()
    ws = _ws("setup-interrupt", "while True:\n    pass", admit=admit, timeout=30.0)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            call = pool.submit(ws.runtime.exec_python, STATE, view=VIEW)
            # As a host stopping a turn does: an interrupt that lands
            # before the setup reaches its worker is not kept, so ask
            # again until the call is over.
            deadline = time.monotonic() + 5
            while not call.done():
                assert time.monotonic() < deadline, "setup not cancelled"
                ws.runtime.interrupt()
                time.sleep(0.05)
            with pytest.raises(WorkerSetupError, match="cancelled"):
                call.result(timeout=10)
        assert admit.open == 0
    finally:
        ws.close()
