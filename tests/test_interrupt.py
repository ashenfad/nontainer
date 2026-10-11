"""``ws.runtime.interrupt()``: the code running now ends early, as a
timeout would end it, and what it wrote before commits with it."""

import threading
import time

import pytest

from nontainer import PythonConfig, Workspace
from nontainer.providers import KvgitProvider

LOOP = "open('started.txt', 'w').write('x')\ni = 0\nwhile True:\n    i += 1\n"


def _interrupted(ws, call):
    """Run ``call`` on a thread and interrupt it until it ends, as a
    host stopping a turn does; its result, and whether an interrupt
    found it running."""
    box = {}
    thread = threading.Thread(target=lambda: box.update(result=call()))
    thread.start()
    found = False
    deadline = time.monotonic() + 30
    while thread.is_alive():
        assert time.monotonic() < deadline, "the interrupted call never ended"
        found = ws.runtime.interrupt() or found
        time.sleep(0.05)
    thread.join()
    return box["result"], found


@pytest.fixture(params=["none", "process"])
def ws(request):
    w = Workspace(
        KvgitProvider.open(None, session="interrupt"),
        python=PythonConfig(isolation=request.param, timeout=60),
    )
    try:
        yield w
    finally:
        w.close()


def test_an_interrupted_run_python_ends_and_keeps_its_writes(ws):
    result, found = _interrupted(ws, lambda: ws.run_python(LOOP))
    assert found
    assert "cancelled" in result.error.lower()
    # the call's writes commit with it, as any failed call's do
    assert result.commit is not None
    assert ws.files.read("started.txt") == b"x"
    assert ws.run_python("x = 1").namespace["x"] == 1  # and it carries on


def test_an_interrupted_terminal_python_ends(ws):
    result, found = _interrupted(
        ws, lambda: ws.terminal("python -c 'while True: pass'")
    )
    assert found
    assert result.exit_code == 1 and "cancelled" in result.stderr.lower()


def test_nothing_running_is_nothing_to_interrupt(ws):
    assert ws.runtime.interrupt() is False
    ws.runtime.close()
    assert ws.runtime.interrupt() is False


def _has_dud():
    import importlib.util

    return importlib.util.find_spec("dud") is not None


@pytest.mark.skipif(not _has_dud(), reason="needs the dud extra")
class TestOnDud:
    @pytest.fixture
    def ws(self):
        from nontainer.executor_dud import DudExecutor

        w = Workspace(
            KvgitProvider.open(None, session="interrupt-dud"),
            executor=DudExecutor(backend="subprocess"),
            python=PythonConfig(timeout=60),
        )
        try:
            yield w
        finally:
            w.close()

    def test_a_blocking_call_is_killed(self, ws):
        """The guest kills the process, so even a call that never
        reaches a checkpoint ends."""
        started = time.monotonic()
        result, found = _interrupted(
            ws, lambda: ws.run_python("import time\ntime.sleep(30)")
        )
        assert found and "Cancelled" in result.error
        assert time.monotonic() - started < 20

    def test_an_interrupted_script_says_so(self, ws):
        result, found = _interrupted(ws, lambda: ws.terminal("sleep 30"))
        assert found and result.exit_code == 130
        assert "interrupted" in result.stderr


class _Sandbox:
    """A sandbox that records whose call it served when cancelled."""

    def __init__(self):
        self.call = "the call running"
        self.cancelled_during: list[str] = []

    def cancel(self):
        self.cancelled_during.append(self.call)


def test_an_interrupt_never_cancels_a_sandbox_handed_on_meanwhile():
    """A call can end while an interrupt is cancelling, and its sandbox
    go back to the pool for another call: that call is not the one
    stopped, so the sandbox is cancelled for its own call or not at all."""
    from nontainer.executor import LocalExecutor

    executor = LocalExecutor()
    first, second = _Sandbox(), _Sandbox()
    first_call, second_call = object(), object()

    def second_call_ends_and_its_sandbox_is_reused():
        with executor._running_lock:  # as exec_python's own ending does
            del executor._running[second_call]
        second.call = "a call that picked it up since"

    def cancel_first():
        first.cancelled_during.append(first.call)
        ending = threading.Thread(target=second_call_ends_and_its_sandbox_is_reused)
        ending.start()
        ending.join(timeout=0.5)  # it may end now, if nothing holds it off

    first.cancel = cancel_first
    executor._running.update({first_call: first, second_call: second})
    assert executor.interrupt() is True
    assert second.cancelled_during in ([], ["the call running"])
