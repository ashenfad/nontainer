"""``AppsConfig.request_deadline``: a hard limit on each handler call,
kept outside the handler's code, answered with a 504 (#344)."""

import json
import math
import threading
import time

import pytest

from nontainer import PythonConfig, Store, ViewSpec, WorkerSetup, Workspace
from nontainer.apps import AppsConfig, enable_apps, request
from nontainer.apps.dispatch import AppRuntime
from nontainer.apps.testapp import build_spec
from nontainer.errors import NotSupportedError
from nontainer.providers import KvgitProvider

# Killing and reaping a worker, on a loaded CI machine.
SLACK = 3.0

SLEEPER = "import time\ndef {verb}(req):\n    time.sleep({seconds})\n    return {{'ok': True}}\n"


class Slow:
    """A host object whose call outlasts the deadline: it runs in the
    host, where killing the worker can't reach it."""

    def wait(self, seconds: float) -> str:
        time.sleep(seconds)
        return "done"


def _ws(isolation="process", **python):
    return Workspace(
        KvgitProvider.open(None, session="deadline"),
        python=PythonConfig(isolation=isolation, modules=[time], **python),
    )


def _write(ws, name, source):
    ws.files.fs.makedirs("/workspace/app/api", exist_ok=True)
    ws.files.fs.write(f"/workspace/app/api/{name}.py", source.encode())
    ws.commit()


def _timed(rt, method, path, **kwargs):
    start = time.monotonic()
    resp = rt.dispatch(request(method, path, **kwargs))
    return resp, time.monotonic() - start


def _log(ws):
    return ws.files.fs.read("/workspace/app/logs/api.log").decode()


def test_a_handler_blocked_past_the_deadline_answers_504():
    """The issue's case: ``request_timeout`` can't stop a handler blocked
    in a call, and it answered 200 late. The deadline answers 504."""
    ws = _ws()
    try:
        rt = enable_apps(ws, AppsConfig(request_timeout=2.0, request_deadline=1.0))
        _write(ws, "slow", SLEEPER.format(verb="get", seconds=30))
        resp, took = _timed(rt, "GET", "/api/slow")
        assert resp.status == 504
        body = json.loads(resp.content)
        assert body["error"] == "the handler did not answer within 1s"
        assert body["log"] == "/workspace/app/logs/api.log"
        assert took < 1.0 + SLACK
        log = _log(ws)
        assert "[slow:get] DEADLINE: stopped at the 1s request deadline" in log
        assert "give that call a timeout of its own" in log
        assert "GET /api/slow -> 504" in log
    finally:
        ws.close()


def test_the_request_after_a_deadline_starts_a_fresh_worker():
    ws = _ws()
    try:
        rt = enable_apps(ws, AppsConfig(request_deadline=1.0))
        _write(ws, "slow", SLEEPER.format(verb="get", seconds=30))
        _write(ws, "fast", "def get(req):\n    return {'ok': True}\n")
        assert rt.dispatch(request("GET", "/api/slow")).status == 504
        resp = rt.dispatch(request("GET", "/api/fast"))
        assert resp.status == 200, resp.content
        assert json.loads(resp.content) == {"ok": True}
    finally:
        ws.close()


def test_a_fast_handler_answers_normally_under_a_deadline():
    ws = _ws()
    try:
        rt = enable_apps(ws, AppsConfig(request_deadline=5.0))
        _write(ws, "nap", SLEEPER.format(verb="get", seconds=0.1))
        for _ in range(3):
            resp = rt.dispatch(request("GET", "/api/nap"))
            assert resp.status == 200, resp.content
    finally:
        ws.close()


def test_a_handler_that_computes_past_its_timeout_still_gets_the_500():
    """Code that runs is stopped by ``request_timeout`` first, as before:
    a 500 with its traceback, not a 504."""
    ws = _ws()
    try:
        rt = enable_apps(ws, AppsConfig(request_timeout=0.5, request_deadline=10.0))
        _write(ws, "spin", "def get(req):\n    while True:\n        pass\n")
        resp = rt.dispatch(request("GET", "/api/spin"))
        assert resp.status == 500
        log = _log(ws)
        assert "[spin:get] ERROR" in log
        assert "DEADLINE" not in log
    finally:
        ws.close()


def test_a_write_staged_by_a_request_past_its_deadline_is_discarded():
    ws = _ws()
    try:
        rt = enable_apps(ws, AppsConfig(request_deadline=1.0))
        _write(
            ws,
            "save",
            "import time\n"
            "def post(req):\n"
            "    with open('/workspace/saved.txt', 'w') as f:\n"
            "        f.write('late')\n"
            "    time.sleep(30)\n"
            "    return {'ok': True}\n",
        )
        assert rt.dispatch(request("POST", "/api/save", body=b"{}")).status == 504
        assert not ws.files.fs.exists("/workspace/saved.txt")
    finally:
        ws.close()


def test_a_host_object_call_delays_the_504_but_never_answers_200():
    """A host object's call runs in this process, so the deadline lands
    as soon as it returns, and what the handler would have answered is
    not sent."""
    ws = _ws(host_objects={"slow": Slow()})
    try:
        rt = enable_apps(ws, AppsConfig(request_deadline=0.5))
        _write(ws, "host", "def get(req):\n    return {'said': slow.wait(1.5)}\n")
        resp, took = _timed(rt, "GET", "/api/host")
        assert resp.status == 504
        assert took >= 1.5
    finally:
        ws.close()


def test_one_request_past_its_deadline_leaves_a_concurrent_one_to_finish(tmp_path):
    """Each call has its own deadline: on a published app, a request that
    runs past it doesn't take a concurrent one with it."""
    store = Store(tmp_path)
    python = PythonConfig(isolation="process", modules=[time])
    try:
        ws = store.open("author", python=python)
        _write(ws, "slow", SLEEPER.format(verb="get", seconds=30))
        _write(ws, "steady", SLEEPER.format(verb="get", seconds=1.0))
        pub = store.publish(ws, "demo")
        ws.close()
        snap = pub.open(python=python)
        try:
            rt = AppRuntime(
                snap,
                AppsConfig(request_deadline=3.0),
                frozen=True,
                log_sink=print,
            )
            answers: dict[str, int] = {}

            def call(name):
                answers[name] = rt.dispatch(request("GET", f"/api/{name}")).status

            slow = threading.Thread(target=call, args=("slow",))
            slow.start()
            time.sleep(2.0)  # the slow handler is a second from its deadline
            # The steady one starts a worker of its own (the pool keeps
            # one) and is still running when the slow one is killed.
            call("steady")
            slow.join(30)
            assert answers == {"slow": 504, "steady": 200}
        finally:
            snap.close()
    finally:
        store.close()


def test_starting_a_worker_counts_toward_the_deadline():
    """The deadline counts from when the executor takes the call. A
    worker whose start takes all of it isn't cut short, but the code
    doesn't run, and the worker serves the next call."""
    ws = _ws(worker_setup=WorkerSetup("import time\ntime.sleep(1.5)"))
    try:
        rt = enable_apps(ws, AppsConfig(request_deadline=1.0))
        _write(
            ws,
            "count",
            "def post(req):\n"
            "    with open('/workspace/ran.txt', 'a') as f:\n"
            "        f.write('x')\n"
            "    return {'ok': True}\n",
        )
        resp, took = _timed(rt, "POST", "/api/count", body=b"{}")
        assert resp.status == 504
        assert took >= 1.5
        assert not ws.files.fs.exists("/workspace/ran.txt")
        resp, took = _timed(rt, "POST", "/api/count", body=b"{}")
        assert resp.status == 200, resp.content
        assert took < 1.0
        assert ws.files.read("ran.txt") == b"x"
    finally:
        ws.close()


def test_the_executor_reports_a_call_ended_at_its_deadline():
    ws = _ws()
    try:
        assert ws.runtime.view_deadline
        result = ws.runtime.exec_python(
            "import time\ntime.sleep(30)\nok = True",
            view=ViewSpec(readonly_fs=True, readonly_cache=True, deadline=0.5),
        )
        assert result.deadline_exceeded
        assert "0.5s deadline" in result.error
        assert "ok" not in result.namespace
        done = ws.runtime.exec_python("ok = True", view=ViewSpec(deadline=5.0))
        assert done.error is None, done.error
        assert not done.deadline_exceeded
        assert done.namespace["ok"] is True
    finally:
        ws.close()


def test_a_deadline_is_refused_in_process():
    """In process there is no worker to kill, and a deadline nothing can
    keep is refused rather than ignored."""
    ws = _ws(isolation="none")
    try:
        assert not ws.runtime.view_deadline
        with pytest.raises(NotSupportedError, match="request_deadline"):
            enable_apps(ws, AppsConfig(request_deadline=1.0))
        with pytest.raises(NotSupportedError, match="deadline"):
            ws.runtime.exec_python("pass", view=ViewSpec(deadline=1.0))
        enable_apps(ws, AppsConfig())  # without one, as before
    finally:
        ws.close()


@pytest.mark.parametrize(
    "bad, error",
    [
        (0, ValueError),
        (-1.0, ValueError),
        (math.nan, ValueError),
        (math.inf, ValueError),
        (True, TypeError),
        ("1", TypeError),
    ],
)
def test_a_request_deadline_that_is_not_a_positive_finite_number_is_refused(bad, error):
    with pytest.raises(error, match="request_deadline"):
        AppsConfig(request_deadline=bad)


@pytest.mark.parametrize(
    "config, drain_ms",
    [
        (AppsConfig(request_timeout=4.0), 9_000),
        (AppsConfig(request_timeout=4.0, request_deadline=2.0), 7_000),
        (AppsConfig(request_timeout=4.0, request_deadline=8.0), 13_000),
    ],
)
def test_test_app_waits_for_requests_by_the_handler_limit(config, drain_ms):
    """A run that has ended waits for its page's requests by the
    deadline when there is one, since nothing pauses it."""
    ws = _ws()
    try:
        rt = enable_apps(ws, config)
        assert build_spec(rt, None).drain_timeout_ms == drain_ms
    finally:
        ws.close()
