"""``isolation="process"``: agent code runs in a forked worker while
the workspace's world — VirtualFS, cache, commits — stays in the
parent, bridged over sandtrap's RPC channel. A worker crash costs the
crashing call, never the host or the workspace."""

import os
import signal
import socket

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


def test_writes_land_in_workspace_and_commit(ws):
    r = ws.run_python("open('/x.txt', 'w').write('hi from worker')")
    assert r.error is None
    assert ws.files.fs.read("/x.txt") == b"hi from worker"
    assert r.commit  # the write dirtied the PARENT's fs -> committed


def test_reads_see_parent_state(ws):
    ws.files.write("/seed.txt", "from parent")
    r = ws.run_python("content = open('/seed.txt').read()")
    assert r.error is None
    assert r.namespace["content"] == "from parent"


def test_cache_round_trips_via_rpc(ws):
    assert ws.run_python("cache['n'] = 41").error is None
    r = ws.run_python("m = cache['n'] + 1\nhas = 'n' in cache\nkeys = list(cache)")
    assert r.error is None
    assert r.namespace["m"] == 42
    assert r.namespace["has"] is True
    assert r.namespace["keys"] == ["n"]
    assert ws.cache["n"] == 41  # the PARENT's cache is the store


def test_stdin_and_argv_cross_the_boundary(ws):
    r = ws.runtime.exec_python("line = input()", stdin="hello worker")
    assert r.error is None
    assert r.namespace["line"] == "hello worker"


def test_runtime_errors_arrive_with_frames(ws):
    """Traceback objects don't survive the pickle home from the worker,
    so runtime errors used to arrive as a bare message — no line
    numbers for the repair loop to aim at. Sandtrap renders worker-side
    now; the rendered error must carry the frames."""
    r = ws.run_python("a = 1\nb = 2\nc = missing_name\n")
    assert not r
    assert "Traceback (most recent call last)" in r.error
    assert "line 3" in r.error
    assert "NameError" in r.error


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
    w.run_python("1")  # the worker starts with the first execution
    proc = w._sandbox._process
    assert proc.is_alive()
    w.close()
    proc.join(timeout=5.0)
    assert not proc.is_alive()


def test_worker_does_not_keep_unrelated_host_socket_alive():
    """LocalExecutor opts into sandtrap's ambient descriptor cleanup."""
    reader, writer = socket.socketpair()
    try:
        reader.settimeout(1.0)
        w = Workspace(
            KvgitProvider.open(None, session="iso-fds"),
            python=PythonConfig(isolation="process"),
        )
        try:
            assert w._sandbox._close_fds is True
            writer.close()
            assert reader.recv(1) == b""
        finally:
            w.close()
    finally:
        reader.close()
        writer.close()


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
    w.files.fs.makedirs("/workspace/app/api", exist_ok=True)
    w.files.fs.write("/workspace/app/index.html", b"<html><body>hi</body></html>")
    w.files.fs.write("/workspace/app/api/count.py", _COUNTER)
    w.commit()


def test_authoring_dispatch_runs_in_workers(ws):
    """Preview dispatch inherits the workspace's isolation: handlers
    execute in a worker (a per-call view sandbox forks one under
    process isolation, like the executor's default), cache crossing the
    bridge both read-write (POST) and read-only (GET)."""
    import json

    from nontainer.apps import enable_apps, request

    _seed_app(ws)
    runtime = enable_apps(ws)
    try:
        # process isolation → the executor's sandbox is a real worker;
        # handler view executions fork one the same way (no longer a
        # long-lived runtime-held worker — one per call)
        assert hasattr(ws.runtime.executor._sandbox, "_process")

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
    ws.commit()
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


def test_agent_python_forms_work_under_a_non_forked_worker(ws):
    """The terminal's ``python`` builtin runs inside the existing worker, so
    none of these start a process — which is why they are unaffected by
    sandtrap's requirement that a worker-creating process have an importable
    ``__main__``.

    That requirement is real, but it applies to the EMBEDDER's entry point:
    constructing a ``Workspace`` from ``python -c`` or a heredoc is what
    breaks, not an agent writing one. Pinned because the two are easy to
    conflate, and conflating them reads as a regression that isn't there.
    """
    heredoc = ws.terminal("python <<'PY'\nprint('heredoc says hi')\nPY")
    assert heredoc.exit_code == 0
    assert heredoc.stdout.strip() == "heredoc says hi"

    dash_c = ws.terminal("python -c 'print(6*7)'")
    assert dash_c.exit_code == 0
    assert dash_c.stdout.strip() == "42"

    ws.files.write("/s.py", 'print("from a file")')
    from_file = ws.terminal("python /s.py")
    assert from_file.exit_code == 0
    assert from_file.stdout.strip() == "from a file"

    piped = ws.terminal("echo 'print(\"piped\")' | python")
    assert piped.exit_code == 0
    assert piped.stdout.strip() == "piped"


# -- preload_grants ----------------------------------------------------------
#
# The lever on worker cost. A forkserver worker re-imports every granted
# module; preloading puts them in the broker once and lets workers inherit
# them copy-on-write. nontainer's job is only to carry the flag down to
# sandtrap — the behaviour itself, including the process-global first-use-wins
# rule, is sandtrap's and tested there.


def test_preload_grants_is_off_by_default():
    """Off because preloading runs a grant's import-time code in the BROKER,
    and a grant that starts a thread on import puts every worker forked from
    it back on the deadlock path. Only the embedder can vouch for their
    grants, so nontainer must not decide this for them."""
    assert PythonConfig().preload_grants is False


@pytest.mark.filterwarnings("ignore:the forkserver broker is already running")
def test_preload_grants_reaches_the_sandbox():
    # Expect that warning here: earlier tests in this module already started
    # the broker without a preload, and sandtrap reads the preload list once,
    # at broker start. This suite is therefore a live demonstration of the
    # caveat on PythonConfig.preload_grants — in a host that builds many
    # workspaces, the first one to start a worker decides for the process.
    # What nontainer owns is carrying the flag down, which is what's asserted.
    ws = Workspace(
        KvgitProvider.open(None, session="preload-on"),
        python=PythonConfig(isolation="process", preload_grants=True),
    )
    try:
        assert ws.runtime.executor._sandbox._preload_grants is True
    finally:
        ws.close()


def test_preload_grants_is_harmless_in_process():
    """``isolation="none"`` has no worker to preload for. sandtrap ignores the
    flag rather than raising, so one PythonConfig can drive every rung."""
    ws = Workspace(
        KvgitProvider.open(None, session="preload-none"),
        python=PythonConfig(isolation="none", preload_grants=True),
    )
    try:
        assert ws.run_python("x = 1 + 1").namespace["x"] == 2
    finally:
        ws.close()


@pytest.mark.filterwarnings("ignore:the forkserver broker is already running")
def test_a_preloaded_worker_still_runs_granted_code():
    """The point of the flag is speed, not semantics: a worker that inherited
    its grants from the broker must behave exactly like one that imported
    them itself."""
    import math

    from nontainer import ModuleGrant

    ws = Workspace(
        KvgitProvider.open(None, session="preload-works"),
        python=PythonConfig(
            isolation="process",
            modules=[ModuleGrant(math)],
            preload_grants=True,
        ),
    )
    try:
        r = ws.run_python("import math\nv = math.sqrt(81)")
        assert r.error is None, r.error
        assert r.namespace["v"] == 9.0
    finally:
        ws.close()


# -- the session worker starts on first use (#159) ---------------------------


def _iso_ws(session):
    return Workspace(
        KvgitProvider.open(None, session=session),
        python=PythonConfig(isolation="process"),
    )


def _worker(w):
    return w._sandbox._process


def test_opening_a_workspace_starts_no_worker():
    w = _iso_ws("lazy-open")
    try:
        assert _worker(w) is None
        w.files.write("a.txt", b"a")
        w.commit()
        assert w.files.read("a.txt") == b"a"
        assert _worker(w) is None
    finally:
        w.close()


def test_the_first_execution_starts_the_worker_and_later_ones_reuse_it():
    w = _iso_ws("lazy-first")
    try:
        assert w.run_python("x = 1 + 1\nx").error is None
        first = _worker(w)
        assert first is not None and first.is_alive()
        w.run_python("2")
        assert _worker(w) is first
    finally:
        w.close()


def test_shell_work_alone_starts_no_worker():
    w = _iso_ws("lazy-shell")
    try:
        assert w.terminal("echo hi > greet.txt; cat greet.txt").stdout.strip() == "hi"
        assert _worker(w) is None
    finally:
        w.close()


def test_a_frozen_snapshot_starts_no_worker_until_it_runs_code(tmp_path):
    from nontainer import Store

    st = Store(tmp_path)
    cfg = PythonConfig(isolation="process")
    ws = st.open("a", python=cfg)
    ws.files.write("x.txt", b"x")
    ws.commit()
    st.tags.add(ws, "t1")
    frozen = st.tags.at("t1", python=cfg)
    try:
        assert frozen.frozen
        assert frozen.files.read("x.txt") == b"x"
        assert _worker(frozen) is None
        assert _worker(ws) is None
        assert frozen.run_python("open('x.txt').read()").error is None
        assert _worker(frozen) is not None
    finally:
        frozen.close()
        ws.close()
        st.close()


def test_warm_starts_the_worker_once():
    w = _iso_ws("lazy-warm")
    try:
        w.runtime.warm()
        started = _worker(w)
        assert started is not None and started.is_alive()
        w.runtime.warm()
        w.run_python("1")
        assert _worker(w) is started
    finally:
        w.close()


def test_closing_without_executing_is_clean_and_warm_after_close_does_nothing():
    w = _iso_ws("lazy-close")
    w.close()
    w.runtime.warm()
    assert _worker(w) is None


def test_racing_starts_make_one_worker():
    """Several threads reaching the not-yet-started worker at once —
    the start is what races, so that is what they call. (Executions on
    the session worker are serialized by the workspace lock; only view
    calls run concurrently, each on a sandbox of its own.)"""
    import threading

    w = _iso_ws("lazy-race")
    started: list = []
    errors: list = []
    gate = threading.Barrier(6)

    def first_call():
        try:
            gate.wait()
            w.runtime.warm()
            started.append(_worker(w))
        except Exception as e:  # noqa: BLE001 - reported below
            errors.append(e)

    threads = [threading.Thread(target=first_call) for _ in range(6)]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert errors == []
        assert len({id(p) for p in started}) == 1
        assert w.run_python("1").error is None
        assert _worker(w) is started[0]
    finally:
        w.close()


def test_in_process_isolation_has_nothing_to_warm():
    w = Workspace(KvgitProvider.open(None, session="lazy-none"))
    try:
        w.runtime.warm()
        assert w.run_python("1 + 1").error is None
    finally:
        w.close()
