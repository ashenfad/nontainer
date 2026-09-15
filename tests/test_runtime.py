"""Runtime: the executor half, and what still reads through Workspace."""

import pytest

from nontainer import PythonConfig, Runtime, Workspace
from nontainer.executor import LocalExecutor
from nontainer.protocol import ViewSpec
from nontainer.providers import KvgitProvider


@pytest.fixture
def ws():
    w = Workspace(KvgitProvider.open(None, session="rt"))
    yield w
    w.close()


# -- the seam ----------------------------------------------------------------


def test_workspace_builds_its_own_runtime(ws):
    assert isinstance(ws.runtime, Runtime)
    assert ws.runtime.workspace is ws
    assert isinstance(ws.runtime.executor, LocalExecutor)


def test_the_execution_surface_lives_on_the_runtime(ws):
    """Execution verbs are reached through ws.runtime and nowhere else:
    the workspace grew delegates for them once and the namespaces
    replaced those, so the seam is visible in the call site."""
    for gone in (
        "exec_python",
        "register_command",
        "set_shell_env",
        "shell_env",
        "env",
        "python_config",
        "supports_commands",
        "supports_ws_verbs",
        "cache_enabled",
    ):
        assert not hasattr(ws, gone), gone

    ws.runtime.register_command("shout", lambda ctx: ctx.stdout.write("HI\n"))
    assert "shout" in ws.runtime.commands
    assert ws.terminal("shout").stdout.strip() == "HI"

    ws.runtime.env["GREETING"] = "hello"
    assert ws.runtime.env["GREETING"] == "hello"
    assert dict(ws.runtime.env) == {"GREETING": "hello"}
    assert ws.terminal("echo $GREETING").stdout.strip() == "hello"
    del ws.runtime.env["GREETING"]
    assert "GREETING" not in ws.runtime.env
    assert ws.terminal("echo $GREETING").stdout.strip() == ""
    ws.runtime.env["GREETING"] = "hello"

    assert ws.runtime.exec_python("x = 6 * 7").namespace["x"] == 42
    assert ws.runtime.python_config is ws.runtime.executor._ctx.python_config
    assert ws.runtime.cache_enabled is True


def test_the_headline_verbs_commit_and_the_raw_ones_do_not(ws):
    """Executors never commit: the runtime returns a result and the
    workspace decides what becomes a commit."""
    before = ws.head
    r = ws.runtime.exec_shell("echo raw > /workspace/raw.txt")
    assert r.exit_code == 0
    assert r.commit is None
    assert ws.head == before
    assert ws.uncommitted

    r = ws.terminal("echo committed > /workspace/done.txt")
    assert r.commit is not None
    assert ws.head == r.commit


def test_shell_env_names_are_validated(ws):
    with pytest.raises(ValueError, match="Invalid shell variable name"):
        ws.runtime.env["not a name"] = "x"


def test_reserved_command_names_are_refused(ws):
    with pytest.raises(ValueError, match="Reserved terminal command name"):
        ws.runtime.register_command("python", lambda ctx: None)
    with pytest.raises(ValueError, match="Reserved terminal command prefix"):
        ws.runtime.register_command("ws-mine", lambda ctx: None)


# -- a runtime of one's own --------------------------------------------------


def test_runtime_over_a_live_workspace_is_a_second_environment(ws):
    """Its own executor, over the same state."""
    rt = Runtime(ws)
    try:
        assert rt.executor is not ws.runtime.executor
        ws.files.write("/workspace/shared.txt", "seen")
        assert (
            rt.exec_python("text = open('/workspace/shared.txt').read()").namespace[
                "text"
            ]
            == "seen"
        )
    finally:
        rt.close()
    # closing the second runtime leaves the workspace's own working
    assert ws.terminal("cat /workspace/shared.txt").stdout.strip() == "seen"


def test_runtime_inherits_the_workspace_python_config_by_default():
    """Serving a snapshot should run under the session's own policy,
    so a runtime built without one takes the workspace's."""
    cfg = PythonConfig(timeout=7.5)
    live = Workspace(KvgitProvider.open(None, session="cfg"), python=cfg)
    try:
        rt = Runtime(live)
        try:
            assert rt.python_config is cfg
        finally:
            rt.close()
    finally:
        live.close()


HANDLER = """
readback = open('/workspace/note.txt').read().strip()
"""


def test_runtime_over_a_frozen_workspace_serves_views(ws):
    """What serving a published snapshot needs: a Runtime built
    directly over a frozen Workspace, running the restricted,
    per-call ``view`` executions apps dispatch through."""
    ws.files.write("/workspace/note.txt", "published\n")
    ws.commit()
    ws.tags.add("v1")

    snap = ws.tags.at("v1")
    try:
        assert snap.frozen
        rt = Runtime(snap)
        try:
            view = ViewSpec(readonly_fs=True, readonly_cache=True)
            r = rt.exec_python(HANDLER, view=view)
            assert r.error is None, r.error
            assert r.namespace["readback"] == "published"

            # the view is read-only, and the snapshot is frozen: a
            # write fails where it happens rather than staging
            denied = rt.exec_python(
                "open('/workspace/note.txt', 'w').write('nope')", view=view
            )
            assert denied.error is not None
        finally:
            rt.close()

        # the snapshot's own runtime serves the same way
        r = snap.runtime.exec_python(HANDLER, view=ViewSpec(readonly_fs=True))
        assert r.namespace["readback"] == "published"
    finally:
        snap.close()


def test_runtime_close_is_idempotent(ws):
    rt = Runtime(ws)
    rt.close()
    rt.close()


def test_runtime_takes_mounts_of_its_own(ws, tmp_path):
    """Mount composition is the workspace's — both ws.files.fs and execution
    see its mounts — so a runtime-only mount is exactly that: visible
    to this runtime's executions and to nothing else."""
    from nontainer import Mount

    (tmp_path / "data.txt").write_text("mounted\n")
    rt = Runtime(ws, mounts={"/extra": Mount(tmp_path)})
    try:
        r = rt.exec_python("text = open('/extra/data.txt').read().strip()")
        assert r.error is None, r.error
        assert r.namespace["text"] == "mounted"
    finally:
        rt.close()
    assert not ws.files.fs.exists("/extra/data.txt")


def test_each_runtime_sees_only_its_own_shell_environment(ws):
    """Shell variables belong to the runtime that published them, not
    to the workspace. Reading them off ``ws.runtime`` gave a second
    runtime the primary's variables and dropped its own."""
    ws.runtime.env["WHO"] = "primary"
    second = Runtime(ws)
    try:
        second.env["WHO"] = "second"
        second.env["ONLY_MINE"] = "yes"

        assert ws.runtime.exec_shell("echo $WHO").stdout.strip() == "primary"
        assert second.exec_shell("echo $WHO").stdout.strip() == "second"
        # the primary never sees the second's variables
        assert "yes" not in ws.runtime.exec_shell("echo $ONLY_MINE").stdout
        assert second.exec_shell("echo $ONLY_MINE").stdout.strip() == "yes"
    finally:
        second.close()


def test_each_runtime_sees_only_its_own_commands(ws):
    """Same rule for the command registry: a command registered on one
    runtime is not a command on another over the same workspace."""
    second = Runtime(ws)
    try:
        ws.runtime.register_command("only-primary", lambda ctx: ctx.stdout.write("p\n"))
        second.register_command("only-second", lambda ctx: ctx.stdout.write("s\n"))

        assert ws.runtime.exec_shell("only-primary").stdout.strip() == "p"
        assert second.exec_shell("only-second").stdout.strip() == "s"
        assert second.exec_shell("only-primary").exit_code == 127
        assert ws.runtime.exec_shell("only-second").exit_code == 127
    finally:
        second.close()


def test_the_guest_ferry_reads_the_registry_it_was_given(ws):
    """The ws-* verbs ferried into a guest dispatch through the
    registry of the runtime whose executor ferried them, not through
    the workspace. Reading the workspace's own mapping is what broke
    both ferries when the registry moved to Runtime."""
    from nontainer.wsgit import register_wsgit
    from nontainer.wsverb import WsVerbHostHandler

    register_wsgit(ws)  # lands on the workspace's primary runtime
    assert "ws-git" in ws.runtime.commands

    handler = WsVerbHostHandler(ws, ws.runtime.commands)
    handled = handler.run("ws-git", "/workspace", "status")
    assert handled["exit_code"] == 0, handled["stderr"]

    second = Runtime(ws)
    try:
        # the verb is registered on the primary runtime, not this one
        refused = WsVerbHostHandler(ws, second.commands).run(
            "ws-git", "/workspace", "status"
        )
        assert refused["exit_code"] == 1
        assert "not registered on this workspace" in refused["stderr"]
    finally:
        second.close()

    curl = WsVerbHostHandler(ws, {}).run(
        "ws-curl", "/workspace", "http://app.local/api/x"
    )
    assert curl["exit_code"] == 1
    assert "not registered on this workspace" in curl["stderr"]


# -- guest paths -------------------------------------------------------------


def test_guest_to_host_is_none_where_there_is_one_spelling(ws):
    """An in-process executor runs against the workspace fs directly,
    so a path has no second spelling to map back from."""
    assert ws.runtime.supports_ws_verbs is False
    assert ws.runtime.guest_to_host("/workspace/notes.md") is None


def test_guest_to_host_maps_a_path_back_from_a_guest():
    """On a guest rung the paths a traceback or a shell answer carries
    are the guest's, and naming a workspace file means mapping one
    back. The same probe `supports_ws_verbs` reports."""

    class Guest:
        def open(self, ctx):
            pass

        def close(self):
            pass

        def _guest_to_host(self, guest_path: str) -> str | None:
            return f"/host{guest_path}" if guest_path.startswith("/work") else None

    w = Workspace(KvgitProvider.open(None, session="guest"), executor=Guest())
    try:
        assert w.runtime.supports_ws_verbs is True
        assert w.runtime.guest_to_host("/work/app/api.py") == "/host/work/app/api.py"
        # outside the workspace: no host twin, said honestly
        assert w.runtime.guest_to_host("/etc/hosts") is None
    finally:
        w.close()


# -- the guest-verb half of the executor contract -----------------------------


def _bare_runtime(executor):
    """A runtime around an executor and nothing else — the capability
    probes read the executor, never the workspace."""
    rt = Runtime.__new__(Runtime)
    rt._executor = executor
    return rt


def test_the_local_executor_declares_no_guest_verbs():
    """``supports_ws_verbs`` and ``guest_to_host`` are contract members,
    so the in-process executor answers them rather than being detected
    by the absence of a private name."""
    from nontainer.protocol import Executor

    ex = LocalExecutor()
    assert ex.supports_ws_verbs is False
    assert ex.guest_to_host("/workspace/a.py") is None
    assert isinstance(ex, Executor)
    assert _bare_runtime(ex).supports_ws_verbs is False
    assert _bare_runtime(ex).guest_to_host("/workspace/a.py") is None


def test_the_dud_executor_declares_guest_verbs_publicly():
    from nontainer.executor_dud import DudExecutor

    assert DudExecutor.supports_ws_verbs is True
    assert DudExecutor.guest_to_host is DudExecutor._guest_to_host


def test_an_executor_predating_the_members_is_probed_for_the_private_one():
    """A third-party guest executor written before the contract named
    these still ferries: the runtime falls back to the private mapper."""

    class OldGuestExecutor:
        def _guest_to_host(self, guest_path):
            return "/host" + guest_path

    rt = _bare_runtime(OldGuestExecutor())
    assert rt.supports_ws_verbs is True
    assert rt.guest_to_host("/work/a.py") == "/host/work/a.py"


def test_an_executor_with_neither_name_ferries_nothing():
    class OldExecutor:
        pass

    rt = _bare_runtime(OldExecutor())
    assert rt.supports_ws_verbs is False
    assert rt.guest_to_host("/work/a.py") is None
