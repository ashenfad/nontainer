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


def test_workspace_delegates_the_execution_surface(ws):
    """The four verbs the spec moves to ws.runtime keep working from
    the workspace in this release, so nothing breaks mid-migration."""
    assert ws.python_config is ws.runtime.python_config
    assert ws.supports_commands is ws.runtime.supports_commands
    assert ws.supports_ws_verbs is ws.runtime.supports_ws_verbs

    ws.register_command("shout", lambda ctx: ctx.stdout.write("HI\n"))
    assert "shout" in ws.runtime.commands
    assert ws.terminal("shout").stdout.strip() == "HI"

    ws.set_shell_env("GREETING", "hello")
    assert ws.runtime.shell_env["GREETING"] == "hello"
    assert ws.terminal("echo $GREETING").stdout.strip() == "hello"

    assert ws.exec_python("x = 6 * 7").namespace["x"] == 42


def test_the_headline_verbs_commit_and_the_raw_ones_do_not(ws):
    """Executors never commit: the runtime returns a result and the
    workspace decides what becomes a checkpoint."""
    before = ws.head
    r = ws.runtime.exec_shell("echo raw > /workspace/raw.txt")
    assert r.exit_code == 0
    assert r.checkpoint is None
    assert ws.head == before
    assert ws.dirty

    r = ws.terminal("echo committed > /workspace/done.txt")
    assert r.checkpoint is not None
    assert ws.head == r.checkpoint


def test_shell_env_names_are_validated(ws):
    with pytest.raises(ValueError, match="Invalid shell variable name"):
        ws.runtime.set_shell_env("not a name", "x")


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
        ws.write_file("/workspace/shared.txt", "seen")
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
    ws.write_file("/workspace/note.txt", "published\n")
    ws.checkpoint()
    ws.tag("v1")

    snap = ws.at_tag("v1")
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
    """Mount composition is the workspace's — both ws.fs and execution
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
    assert not ws.fs.exists("/extra/data.txt")


def test_the_guest_ferry_reads_the_registry_it_was_given(ws):
    """The ws-* verbs ferried into a guest dispatch through the
    registry of the runtime whose executor ferried them, not through
    the workspace. Reading the workspace's own mapping is what broke
    both ferries when the registry moved to Runtime."""
    from nontainer.wscurl import WsCurlHostHandler
    from nontainer.wsgit import DudHostHandler, register_wsgit

    register_wsgit(ws)  # lands on the workspace's primary runtime
    assert "ws-git" in ws.runtime.commands

    handled = DudHostHandler(ws, ws.runtime.commands).run("/workspace", "status")
    assert handled["exit_code"] == 0, handled["stderr"]

    second = Runtime(ws)
    try:
        # the verb is registered on the primary runtime, not this one
        refused = DudHostHandler(ws, second.commands).run("/workspace", "status")
        assert refused["exit_code"] == 1
        assert "not registered on this workspace" in refused["stderr"]
    finally:
        second.close()

    curl = WsCurlHostHandler(ws, {}).run("/workspace", "http://app.local/api/x")
    assert curl["exit_code"] == 1
    assert "not registered on this workspace" in curl["stderr"]
