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
    assert ws.fs.read("/greet.txt").strip() == b"hello"
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
    assert ws.fs.read("/sub/here.txt").strip() == b"x"


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
    assert ws.fs.read("/out.txt") == b"py"


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
    assert ws.fs.read("/f.txt").strip() == b"one"
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
            assert child.fs.exists("/kid.txt")
            assert not ws.fs.exists("/kid.txt")  # branches independent
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
            assert not ws.fs.exists("/kid.txt")
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


def test_view_execution_not_yet_supported(ws):
    """build_sandbox dissolved into exec_python(view=); apps handler
    dispatch under dud (the view path) is stage 3c and fails loud until
    then, rather than silently running a handler with read-write access."""
    from nontainer.errors import NotSupportedError
    from nontainer.executor import ViewSpec

    with pytest.raises(NotSupportedError):
        ws.exec_python("pass", view=ViewSpec(readonly_fs=True))


def test_exec_python_stdin_argv_fail_loud(ws):
    from nontainer.errors import NotSupportedError

    with pytest.raises(NotSupportedError):
        ws.exec_python("pass", stdin="data")


def test_bad_inputs_raise_typeerror(ws):
    with pytest.raises(TypeError):
        ws.run_python("pass", inputs={"obj": object()})
