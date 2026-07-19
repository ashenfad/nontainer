"""DudExecutor delta suite: the dud-backed workspace.

Two kinds of assertion live here: (1) the executor seam holds — the
same Workspace contract (results, checkpoints, history, restore,
fork) over a real machine; (2) the INTENDED divergences from
LocalExecutor are pinned as facts, not left as surprises (merged
stderr, codec-narrowed namespace, opaque-bytes cache host-side,
real-bash constructs termish rejects).

Requires the ``dud`` extra; skipped when it isn't installed.
"""

import pytest

from nontainer import Workspace
from nontainer.providers import KvgitProvider

pytest.importorskip("dud")

from nontainer.executor_dud import DudExecutor  # noqa: E402


@pytest.fixture
def ws():
    w = Workspace(
        KvgitProvider.open(None, session="dud-test"), executor=DudExecutor()
    )
    try:
        yield w
    finally:
        w.close()


# -- terminal basics ---------------------------------------------------------


def test_terminal_basics(ws):
    r = ws.terminal("echo hello > greet.txt; cat greet.txt")
    assert r, r.stdout
    assert r.stdout.strip() == "hello"
    assert r.exit_code == 0
    # the guest write landed in the PROVIDER (diff absorbed), not just
    # in the guest scratch dir
    assert ws.fs.read("/workspace/greet.txt").strip() == b"hello"
    # ...and was committed by the normal autocheckpoint flow
    assert r.checkpoint is not None


def test_terminal_failure_contract(ws):
    """Match LocalExecutor's failure contract: command failure is a
    result, never an exception — exit codes carry through (127 for
    not-found, bash and termish agree). Delta: the message rides the
    merged transcript (stdout), not stderr."""
    r = ws.terminal("definitely_not_a_command_xyz")
    assert not r
    assert r.exit_code == 127
    assert "not found" in (r.stdout + r.stderr)


def test_real_bash_upgrade_command_substitution(ws):
    """The fidelity dividend: $(...) is real bash, which termish's
    parser rejects — the exact class of construct dud exists for."""
    r = ws.terminal("echo $(printf up)grade")
    assert r, r.stdout
    assert r.stdout.strip() == "upgrade"


def test_cwd_persists_across_calls(ws):
    ws.terminal("mkdir -p sub && cd sub && echo x > here.txt")
    r = ws.terminal("pwd")
    assert r.stdout.strip().endswith("/sub")
    # host-side mirror caught up once the diff landed files under sub/
    assert ws.fs.read("/workspace/sub/here.txt").strip() == b"x"


# -- python ------------------------------------------------------------------


def test_run_python_namespace_roundtrip(ws):
    r = ws.run_python("x = {'a': 1}\ny = [1, 2, 3]\nz = 'hi'\nprint(z)")
    assert r, r.error
    assert "hi" in r.stdout
    assert r.namespace["x"] == {"a": 1}
    assert r.namespace["y"] == [1, 2, 3]
    assert r.namespace["z"] == "hi"


def test_python_error_is_a_result(ws):
    r = ws.run_python("boom = 1\nraise ValueError('nope')")
    assert not r
    assert r.error is not None and "ValueError" in r.error and "nope" in r.error


def test_files_shared_between_shell_and_python(ws):
    ws.terminal("echo data > in.txt")
    r = ws.run_python("content = open('in.txt').read().strip()")
    assert r, r.error
    assert r.namespace["content"] == "data"
    r2 = ws.run_python("open('out.txt', 'w').write('py')")
    assert r2, r2.error
    assert r2.checkpoint is not None  # the write dirtied the provider
    assert ws.terminal("cat out.txt").stdout.strip() == "py"
    assert ws.fs.read("/workspace/out.txt") == b"py"


def test_host_write_visible_in_guest(ws):
    """write_file goes behind the executor's back; sync() re-materializes."""
    ws.write_file("seeded.txt", "from host")
    r = ws.terminal("cat seeded.txt")
    assert r.stdout.strip() == "from host"


# -- versioning over dud diffs (the point of the whole design) ---------------


def test_checkpoint_restore_history(ws):
    r1 = ws.terminal("echo one > f.txt")
    cp1 = r1.checkpoint
    assert cp1 is not None
    r2 = ws.terminal("echo two > f.txt")
    assert r2.checkpoint is not None and r2.checkpoint != cp1
    entries = list(ws.history())
    assert entries[0].id == r2.checkpoint
    assert any(e.info.get("tool") == "terminal" for e in entries)

    ws.restore(cp1)
    # provider is back...
    assert ws.fs.read("/workspace/f.txt").strip() == b"one"
    # ...and so is the GUEST's view (sync re-materialized it)
    assert ws.terminal("cat f.txt").stdout.strip() == "one"


def test_read_only_calls_do_not_checkpoint(ws):
    ws.terminal("echo x > f.txt")
    head = ws.head
    r = ws.terminal("ls")
    assert r.checkpoint is None
    assert ws.head == head


def test_fork():
    """Fork = provider branch + a fresh dud session pointed at it
    (workspaces own their executor, so the fork gets its own guest)."""
    provider = KvgitProvider.open(None, session="dud-fork")
    ws = Workspace(provider, executor=DudExecutor())
    try:
        ws.terminal("echo base > shared.txt")
        child_provider = provider.fork("dud-fork-child")
        child = Workspace(child_provider, executor=DudExecutor())
        try:
            assert child.terminal("cat shared.txt").stdout.strip() == "base"
            child.terminal("echo kid > kid.txt")
            assert child.fs.exists("/workspace/kid.txt")
            assert not ws.fs.exists("/workspace/kid.txt")  # branches independent
        finally:
            child.close()
    finally:
        ws.close()


def test_fork_inherits_executor_factory(tmp_path):
    """With ``executor_factory``, ``ws.fork()`` builds the fork on the
    SAME executor kind (a fresh dud guest), so a whole session lineage
    runs on dud — what studio's fork-a-session needs. Contrast with
    ``test_fork``, which wires each side by hand. ``store=tmp_path``
    keeps this off the default on-disk store."""
    from nontainer import workspace

    ws = workspace(
        "dud-factory-parent",
        store=tmp_path,
        executor_factory=lambda: DudExecutor(),
    )
    try:
        ws.terminal("echo base > shared.txt")
        # real-bash construct termish rejects — proves the fork is dud,
        # not a silent LocalExecutor fallback
        child = ws.fork("dud-factory-child")
        try:
            assert child.terminal("cat shared.txt").stdout.strip() == "base"
            r = child.terminal("echo $(echo nested)")  # command substitution
            assert r.stdout.strip() == "nested"
            child.terminal("echo kid > kid.txt")
            assert not ws.fs.exists("/workspace/kid.txt")
        finally:
            child.close()
    finally:
        ws.close()


# -- cache: same keyspace, opaque bytes host-side ----------------------------


def test_cache_guest_roundtrip_and_host_opacity(ws):
    r = ws.run_python("cache['k'] = {'a': 1}")
    assert r, r.error
    assert r.checkpoint is not None  # cache write-back is staged + committed
    # guest round-trips its own pickle
    r2 = ws.run_python("w = cache['k']['a']")
    assert r2, r2.error
    assert r2.namespace["w"] == 1
    # INTENDED delta: host-side reads of guest-written keys are opaque
    # pickle bytes — the host never unpickles guest bytes (dud DESIGN,
    # "no pickle ever crosses this boundary" — host-bound direction)
    assert isinstance(ws.cache["k"], bytes)


def test_cache_host_seeded_value_reaches_guest(ws):
    ws.cache["seed"] = {"n": 5}  # host-side rich value
    r = ws.run_python("v = cache['seed']['n']")
    assert r, r.error
    assert r.namespace["v"] == 5


def test_cache_survives_restore(ws):
    r1 = ws.run_python("cache['stage'] = 'first'")
    r2 = ws.run_python("cache['stage'] = 'second'")
    assert r1.checkpoint and r2.checkpoint
    ws.restore(r1.checkpoint)
    r = ws.run_python("s = cache['stage']")
    assert r.namespace["s"] == "first"


# -- surfaces that stay local-only --------------------------------------------


# -- apps under dud (the webapp loop): full dispatch over a real machine -----
#
# What crosses the executor boundary: Request rides IN as a host→guest
# pickle (the safe direction); a Response return crosses OUT reconstructed
# from the view's contract classes; cache read-through works; read-only
# GET is enforced (cache write raises; a fs write is rejected via the
# diff); a mutating handler's writes are absorbed into the provider. The
# apps.md-recommended pattern — state in cache/an external store, not the
# VFS — works fully. (Absolute VFS paths like ``/app/x`` in a handler's
# own fs writes are a rung-1 limitation: no chroot, so ``/app`` hits the
# real root; the VM rungs mount the workspace at ``/``. Relative writes
# resolve against the guest cwd and work — see below.)

_APP_HANDLER = b"""
def get(req):
    return {"names": cache.get("names", []), "path": req.path, "q": req.params.get("q")}

def post(req):
    names = cache.get("names", [])
    names.append(req.require("name"))
    cache["names"] = names
    return Response(status=201, body={"n": len(names)})
"""


def _seed_app(ws, name="names.py", src=_APP_HANDLER):
    ws.fs.makedirs("/workspace/app/api", exist_ok=True)
    ws.fs.write(f"/workspace/app/api/{name}", src)
    ws.checkpoint()


def test_apps_dispatch_under_dud(ws):
    """The whole cache-based apps loop over a real machine: Request in,
    Response out, read-only GET, mutating POST — all across the boundary."""
    import json

    from nontainer.apps import enable_apps, request

    _seed_app(ws)
    runtime = enable_apps(ws)
    try:
        r = runtime.dispatch(request("POST", "/api/names", body=b'{"name": "amy"}'))
        assert r.status == 201, r.content  # Response(status=201) crossed back
        assert json.loads(r.content) == {"n": 1}

        r = runtime.dispatch(request("GET", "/api/names?q=hi"))
        assert r.status == 200
        body = json.loads(r.content)
        assert body["names"] == ["amy"]  # POST's cache write persisted
        assert body["path"] == "/api/names" and body["q"] == "hi"  # Request fields
    finally:
        runtime.close()


def test_apps_readonly_get_rejects_cache_write(ws):
    """A GET that writes the cache hits the read-only view → 500."""
    from nontainer.apps import enable_apps, request

    _seed_app(ws, "w.py", b"def get(req):\n    cache['x'] = 1\n    return {}\n")
    runtime = enable_apps(ws)
    try:
        assert runtime.dispatch(request("GET", "/api/w")).status == 500
        assert "x" not in ws.cache  # nothing leaked
    finally:
        runtime.close()


def test_apps_readonly_get_rejects_fs_write(ws):
    """A GET that writes the fs (relative path): the write lands in the
    guest, dispatch rejects the non-empty diff → 500, nothing absorbed."""
    from nontainer.apps import enable_apps, request

    _seed_app(ws, "w.py", b"def get(req):\n    open('sneak.txt','w').write('x')\n    return {}\n")
    runtime = enable_apps(ws)
    try:
        assert runtime.dispatch(request("GET", "/api/w")).status == 500
        assert not ws.fs.exists("/sneak.txt")  # discarded, not absorbed
    finally:
        runtime.close()


def test_apps_mutating_handler_absorbs_fs_write(ws):
    """A POST writing a relative path lands in the provider, like
    LocalExecutor's write-through (visible in ws.fs afterward)."""
    from nontainer.apps import enable_apps, request

    _seed_app(ws, "mk.py", b"def post(req):\n    open('made.txt','w').write('hi')\n    return {'ok': True}\n")
    runtime = enable_apps(ws)
    try:
        assert runtime.dispatch(request("POST", "/api/mk")).status == 200
        assert ws.fs.read("/workspace/made.txt") == b"hi"
    finally:
        runtime.close()


def test_exec_python_stdin_argv_fail_loud(ws):
    from nontainer.errors import NotSupportedError

    with pytest.raises(NotSupportedError):
        ws.exec_python("pass", stdin="data")


def test_bad_inputs_raise_typeerror(ws):
    with pytest.raises(TypeError):
        ws.run_python("pass", inputs={"obj": object()})


# -- backend selection (subprocess vs vfkit VM rung) -------------------------


def test_make_session_vfkit_uses_shared_pool(monkeypatch):
    """backend='vfkit' acquires from dud's shared pool by default (VMs are
    fungible across same-spec sessions), passing VM config + the common
    host_objects/cache kwargs. No VM is booted (acquire is faked)."""
    captured = {}

    def fake_acquire(**kw):
        captured.update(kw)
        return "pooled-session"

    monkeypatch.setattr("dud.backends.pool.acquire_vfkit", fake_acquire)
    ex = DudExecutor(backend="vfkit", vm={"image": "python:3.12-slim", "cpus": 4})
    s = ex._make_session({"obj": object()}, {"c": b"x"})
    assert s == "pooled-session"
    assert captured["image"] == "python:3.12-slim" and captured["cpus"] == 4
    assert "pooled" not in captured  # the knob is consumed, not forwarded
    assert "host_objects" in captured and "cache" in captured


def test_make_session_vfkit_unpooled_constructs_directly(monkeypatch):
    captured = {}

    class FakeVfkit:
        def __init__(self, **kw):
            captured.update(kw)

    monkeypatch.setattr("dud.backends.vfkit.VfkitSession", FakeVfkit)
    ex = DudExecutor(backend="vfkit", vm={"pooled": False, "cpus": 2})
    s = ex._make_session({}, {})
    assert isinstance(s, FakeVfkit)
    assert captured["cpus"] == 2 and "pooled" not in captured


def test_make_session_selects_subprocess(monkeypatch):
    captured = {}

    class FakeSub:
        def __init__(self, **kw):
            captured.update(kw)

    monkeypatch.setattr("dud.backends.subprocess.Session", FakeSub)
    ex = DudExecutor(backend="subprocess", root="/tmp/scratch")
    s = ex._make_session({}, {})
    assert isinstance(s, FakeSub) and captured["root"] == "/tmp/scratch"


def test_make_session_unknown_backend():
    with pytest.raises(ValueError):
        DudExecutor(backend="nope")._make_session({}, {})


def test_view_contract_crosses_without_guest_install(ws, tmp_path):
    """The VM-rung scenario: extra_classes whose module the GUEST cannot
    import. The contract must cross by source (bootstrap synthesizes the
    module before the unpickle) — instance in, methods callable, instance
    back out. Pinned on the subprocess rung by loading the module from a
    tmp file the guest's sys.path can't see."""
    import importlib.util
    import sys

    from nontainer.executor import ViewSpec

    src = (
        "from dataclasses import dataclass\n"
        "@dataclass\n"
        "class Ping:\n"
        "    tag: str\n"
        "    def loud(self):\n"
        "        return self.tag.upper()\n"
    )
    p = tmp_path / "ghost_contract.py"
    p.write_text(src)
    spec = importlib.util.spec_from_file_location("ghost_contract", p)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ghost_contract"] = mod
    try:
        spec.loader.exec_module(mod)
        Ping = mod.Ping
        r = ws.exec_python(
            "out = {'loud': ping.loud()}\nresp = Ping(tag='pong')",
            inputs={"ping": Ping(tag="hi")},
            view=ViewSpec(extra_classes=(Ping,)),
        )
        assert r.error is None, r.error
        assert r.namespace["out"] == {"loud": "HI"}
        resp = r.namespace["resp"]
        assert isinstance(resp, Ping) and resp.tag == "pong"
    finally:
        del sys.modules["ghost_contract"]


def test_state_identity_guard():
    """head names the commit the fs EQUALS: None while staging is dirty,
    None without commit identity — a reusable-substrate executor must
    never tag a tree with a state it doesn't hold."""
    from nontainer.workspace import _state_identity

    class Clean:
        dirty = False

        def head(self):
            return "c1"

    class Dirty:
        dirty = True

        def head(self):  # pragma: no cover — must not be consulted
            return "c1"

    assert _state_identity(Clean())() == "c1"
    assert _state_identity(Dirty())() is None
    assert _state_identity(object()) is None


# -- death recovery ----------------------------------------------------------


def test_dead_guest_recovers_with_state_and_retries():
    """Kill the guest under a live workspace: the next call raises
    SessionLost inside the executor, which reopens a session, re-pushes
    the provider tree, and retries — the caller sees a normal result
    with committed state intact (the disposable thesis as resilience)."""
    ex = DudExecutor()
    ws = Workspace(
        KvgitProvider.open(None, session="dud-recover"), executor=ex
    )
    try:
        ws.terminal("echo sturdy > f.txt && mkdir -p sub && cd sub && echo x > here.txt")
        # A writing call from within sub/: the cwd mirror lands (sub is
        # in the provider now) and the checkpoint persists it.
        ws.terminal("echo y > also.txt")
        assert ws.fs.getcwd() == "/workspace/sub"
        ex._session._proc.kill()  # VM crash / pool reclaim, guest's view
        r = ws.terminal("cat ../f.txt")
        assert r, r.stdout + (r.stderr or "")
        assert r.stdout.strip() == "sturdy"
        # recovery re-asserted the persisted cwd, not just the tree
        assert ws.terminal("pwd").stdout.strip().endswith("/sub")
    finally:
        ws.close()


def test_dead_guest_recovers_python_namespace():
    ex = DudExecutor()
    ws = Workspace(
        KvgitProvider.open(None, session="dud-recover-py"), executor=ex
    )
    try:
        ws.run_python("open('n.txt', 'w').write('42')")
        ex._session._proc.kill()
        r = ws.run_python("n = int(open('n.txt').read())")
        assert r, r.error
        assert r.namespace["n"] == 42
    finally:
        ws.close()


def test_harvest_loss_is_an_error_not_a_silent_success():
    """The torn-call window: dud applies cache write-backs inside a
    successful exec, fs writes cross only via the follow-up diff(). A
    guest lost in between (pool reclaim of a quiet VM) must surface as
    an errored call with pre-call state restored — NOT as an empty
    diff committed alongside the cache half."""
    from dud.backends.base import SessionLost

    ex = DudExecutor()
    ws = Workspace(
        KvgitProvider.open(None, session="dud-harvest-loss"), executor=ex
    )
    try:
        ws.run_python("cache['pre'] = 'committed'")  # clean baseline
        head = ws.head
        sess = ex._session

        def dying_diff(**kw):
            sess._proc.kill()  # the realism
            raise SessionLost("reclaimed mid-harvest")  # the determinism

        sess.diff = dying_diff
        r = ws.run_python("cache['torn'] = 1\nopen('lost.txt', 'w').write('x')")
        assert not r  # errored result, never silent success
        assert r.error is not None and "harvest" in r.error
        assert "rolled back" in r.error  # entry-clean staging unwound
        assert r.checkpoint is None
        # zero-times semantics: neither plane of the torn call survives
        assert not ws.fs.exists("/workspace/lost.txt")
        assert "torn" not in ws.cache
        assert ws.head == head
        # and the recovered guest carries on normally
        r2 = ws.run_python("ok = 1")
        assert r2, r2.error
        assert r2.namespace["ok"] == 1
    finally:
        ws.close()


def test_open_failure_closes_the_session(monkeypatch):
    """A failure after session construction but inside open() (boot
    race, bad tree) must close the session it just made — otherwise the
    guest is orphaned with no workspace to close it (and a pooled one
    would linger in the pool's bound set past teardown)."""
    closed = []

    class DoomedSession:
        def ping(self):
            raise RuntimeError("boot race")

        def close(self):
            closed.append(True)

    ex = DudExecutor()
    monkeypatch.setattr(ex, "_make_session", lambda live, cache: DoomedSession())
    provider = KvgitProvider.open(None, session="dud-open-fail")
    try:
        with pytest.raises(RuntimeError, match="boot race"):
            Workspace(provider, executor=ex)
        assert closed == [True]
        assert ex._session is None
    finally:
        provider.close()


def test_sync_recovery_pushes_the_tree_once():
    """sync() on a dead guest: the failed push routes into _recover(),
    whose rebuild pushes the tree itself — no retry on top (the
    wholesale push is the expensive path; it must not run twice)."""
    ex = DudExecutor()
    ws = Workspace(
        KvgitProvider.open(None, session="dud-sync-once"), executor=ex
    )
    try:
        ws.terminal("echo x > f.txt")
        ex._session._proc.kill()
        calls = []
        orig = ex._push_tree

        def counting():
            calls.append(1)
            return orig()

        ex._push_tree = counting
        ws.write_file("g.txt", "host")  # host write -> executor.sync()
        # 1: sync's own push (dies on the dead guest); 2: recovery's.
        # The old shape retried on top for a third, double-pushing.
        assert len(calls) == 2
        assert ws.terminal("cat g.txt").stdout.strip() == "host"
    finally:
        ws.close()


# -- concurrency: the frozen-serving path --------------------------------------


def test_concurrent_view_calls_are_safe(ws):
    """Frozen app serving dispatches exec_python(view=) WITHOUT the
    workspace lock (the seam's one sanctioned concurrent path). dud
    multiplexes one session channel, so the executor must serialize
    internally — without its lock, concurrent calls interleave frames
    on the socket and corrupt the protocol."""
    import threading

    from nontainer.executor import ViewSpec

    view = ViewSpec(readonly_fs=True, readonly_cache=True)
    errs: list[BaseException] = []

    def worker(i: int) -> None:
        try:
            for j in range(4):
                r = ws.exec_python(f"v = {i} * 100 + {j}", view=view)
                assert r.error is None, r.error
                assert r.namespace["v"] == i * 100 + j
        except BaseException as e:  # noqa: BLE001 — collected for the assert
            errs.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errs, errs


def test_view_inputs_do_not_ride_back(ws):
    """Parity with LocalExecutor, which drops injected names from the
    outgoing namespace: a view call's inputs (the pickled-in Request in
    apps dispatch) must not be marshaled back across the wire."""
    from nontainer.executor import ViewSpec

    r = ws.exec_python("out = ping * 2", inputs={"ping": 21}, view=ViewSpec())
    assert r.error is None, r.error
    assert r.namespace["out"] == 42
    assert "ping" not in r.namespace


def test_host_files_outside_the_root_never_reach_the_guest(ws):
    """The push covers the <root> subtree only: state an embedder parks
    beside the root (manifests, secrets) is host-only by contract."""
    with ws.lock:
        ws.fs.write("/host-only.txt", b"secret")
        ws.fs.write("/workspace/inside.txt", b"visible")
    ws._executor.sync()  # raw fs writes bypass the tool-call sync
    r = ws.terminal("ls")
    assert "inside.txt" in r.stdout
    assert "host-only.txt" not in r.stdout
