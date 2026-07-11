"""``isolation="process"``: agent code runs in a forked worker while
the workspace's world — VirtualFS, cache, checkpoints — stays in the
parent, bridged over sandtrap's RPC channel. A worker crash costs the
crashing call, never the host or the workspace."""

import os
import signal

import pytest

from nontainer import PythonConfig, Workspace
from nontainer.providers.kvgit import KvgitProvider

pytest.importorskip("sandtrap.fs.remote", reason="needs sandtrap with RemoteFS")


@pytest.fixture
def ws():
    w = Workspace(
        KvgitProvider.open(None, session="iso"),
        python=PythonConfig(isolation="process"),
    )
    yield w
    w.close()


def test_writes_land_in_workspace_and_checkpoint(ws):
    r = ws.run_python("open('/x.txt', 'w').write('hi from worker')")
    assert r.error is None
    assert ws.fs.read("/x.txt") == b"hi from worker"
    assert r.checkpoint  # the write dirtied the PARENT's fs -> committed


def test_reads_see_parent_state(ws):
    ws.write_file("/seed.txt", "from parent")
    r = ws.run_python("content = open('/seed.txt').read()")
    assert r.error is None
    assert r.namespace["content"] == "from parent"


def test_cache_round_trips_via_rpc(ws):
    assert ws.run_python("cache['n'] = 41").error is None
    r = ws.run_python(
        "m = cache['n'] + 1\nhas = 'n' in cache\nkeys = list(cache)"
    )
    assert r.error is None
    assert r.namespace["m"] == 42
    assert r.namespace["has"] is True
    assert r.namespace["keys"] == ["n"]
    assert ws.cache["n"] == 41  # the PARENT's cache is the store


def test_stdin_and_argv_cross_the_boundary(ws):
    r = ws.exec_python("line = input()", stdin="hello worker")
    assert r.error is None
    assert r.namespace["line"] == "hello worker"


def test_worker_crash_is_contained(ws):
    assert ws.run_python("open('/kept.txt', 'w').write('before')").error is None

    os.kill(ws._sandbox._process.pid, signal.SIGKILL)
    ws._sandbox._process.join(timeout=5.0)

    # next call respawns transparently; nothing already written is lost
    r = ws.run_python("content = open('/kept.txt').read()")
    assert r.error is None
    assert r.namespace["content"] == "before"


def test_close_shuts_down_the_worker():
    w = Workspace(
        KvgitProvider.open(None, session="iso-close"),
        python=PythonConfig(isolation="process"),
    )
    proc = w._sandbox._process
    assert proc.is_alive()
    w.close()
    proc.join(timeout=5.0)
    assert not proc.is_alive()


def test_live_host_objects_bridge_as_proxies():
    """The docstring promise: host_objects cross process isolation as
    RPC proxies — method calls hit the PARENT's live object."""

    class Counter:
        def __init__(self):
            self.n = 0

        def bump(self, by=1):
            self.n += by
            return self.n

    counter = Counter()
    w = Workspace(
        KvgitProvider.open(None, session="iso-host"),
        python=PythonConfig(isolation="process", host_objects={"counter": counter}),
    )
    try:
        r = w.run_python("a = counter.bump()\nb = counter.bump(10)")
        assert r.error is None, r.error
        assert r.namespace["a"] == 1
        assert r.namespace["b"] == 11
        assert counter.n == 11  # the PARENT's instance moved
    finally:
        w.close()


# -- apps under isolation ----------------------------------------------------------

_COUNTER = b"""
def get(req):
    return {"n": cache.get("n", 0)}

def post(req):
    cache["n"] = cache.get("n", 0) + 1
    return {"n": cache["n"]}
"""


def _seed_app(w):
    w.fs.makedirs("/app/api", exist_ok=True)
    w.fs.write("/app/index.html", b"<html><body>hi</body></html>")
    w.fs.write("/app/api/count.py", _COUNTER)
    w.checkpoint()


def test_authoring_dispatch_runs_in_workers(ws):
    """Preview dispatch inherits the workspace's isolation: handlers
    execute in the runtime's long-lived workers, cache crossing the
    bridge both read-write and read-only."""
    import json

    from nontainer.apps import enable_apps, request

    _seed_app(ws)
    runtime = enable_apps(ws)
    try:
        assert hasattr(runtime._rw_sandbox, "_process")  # really a worker

        r = runtime.dispatch(request("POST", "/api/count"))
        assert r.status == 200 and json.loads(r.content) == {"n": 1}
        r = runtime.dispatch(request("GET", "/api/count"))
        assert r.status == 200 and json.loads(r.content) == {"n": 1}
        assert ws.cache["n"] == 1  # landed in the PARENT's cache
    finally:
        runtime.close()


def test_frozen_serving_forks_per_request(ws):
    """Frozen serving under isolation: each request gets its own
    worker (full concurrency, ~2ms fork), reads work, mutation is
    still rejected through the bridged read-only cache."""
    import json

    from nontainer.apps import AppRuntime, request

    _seed_app(ws)
    ws.cache["n"] = 41
    ws.checkpoint()
    snapshot = ws.fork("frozen-iso")
    try:
        runtime = AppRuntime(snapshot, frozen=True, log_sink=lambda msg: None)
        r = runtime.dispatch(request("GET", "/api/count"))
        assert r.status == 200 and json.loads(r.content) == {"n": 41}
        # a second request forks its own worker and agrees
        r = runtime.dispatch(request("GET", "/api/count"))
        assert r.status == 200 and json.loads(r.content) == {"n": 41}
        # mutation dies at the read-only cache, across the bridge
        r = runtime.dispatch(request("POST", "/api/count"))
        assert r.status == 500
        assert snapshot.cache["n"] == 41
    finally:
        snapshot.close()
