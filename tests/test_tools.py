"""The shared toolset: one definition of the workspace tools that every
harness adapter wraps."""

import asyncio
import base64

import pytest

from nontainer import PythonConfig, Store
from nontainer.adapters.tools import Tool, ToolOutput, Toolset

# a 1x1 transparent png
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA"
    "60e6kgAAAABJRU5ErkJggg=="
)


class _Runner:
    def run(self, session, task, *, budget=None):  # pragma: no cover - never run
        return "unused"


@pytest.fixture
def ws():
    workspace = Store(memory=True).open("s")
    yield workspace
    workspace.close()


# -- the set -------------------------------------------------------------------


def test_the_set_follows_the_session(ws):
    assert [t.name for t in Toolset(ws).tools()] == [
        "terminal",
        "file_write",
        "file_edit",
        "view_image",
        "run_python",
    ]
    assert "view_image" not in [t.name for t in Toolset(ws, vision=False).tools()]
    assert "run_python" not in [t.name for t in Toolset(ws, tools="terminal").tools()]
    toolset = Toolset(ws, sessions=_Runner())
    assert [t.name for t in toolset.tools()][-1] == "sessions"
    toolset.sessions.close()


def test_a_plain_environment_gets_no_run_python():
    workspace = Store(memory=True).open("plain", cache=False)
    toolset = Toolset(workspace)
    assert not toolset.split
    assert "run_python" not in [t.name for t in toolset.tools()]
    workspace.close()


def test_each_tool_carries_its_description_and_schema(ws):
    for tool in Toolset(ws).tools():
        assert isinstance(tool, Tool)
        assert tool.description
        assert tool.parameters["type"] == "object"
        assert set(tool.parameters["required"]) <= set(tool.parameters["properties"])


def test_the_schemas_are_the_ones_agno_derives(ws):
    """The toolset's JSON Schemas are written out by hand, for loops that
    take a schema rather than a signature. agno derives its schemas from
    the adapter's typed wrappers, so the two must agree, tool for tool."""
    pytest.importorskip("agno")
    from nontainer.adapters.agno import WorkspaceTools
    from nontainer.apps import enable_apps

    apps = enable_apps(ws)
    toolset = Toolset(ws, apps=apps, sessions=_Runner())
    tk = WorkspaceTools(ws, apps=apps, sessions=toolset.sessions)
    derived = {}
    for name, fn in tk.functions.items():
        fn.process_entrypoint()
        derived[name] = fn.to_dict()["parameters"]
    ours = {t.name: dict(t.parameters) for t in toolset.tools()}
    assert ours == derived
    toolset.sessions.close()


# -- the calls -----------------------------------------------------------------


def test_terminal_flags_a_failed_command(ws):
    toolset = Toolset(ws)
    ok = toolset.terminal("echo hi")
    assert ok.text == "hi" and not ok.is_error
    bad = toolset.terminal("cat /nope")
    assert bad.is_error
    assert "exit code" in bad.text


def test_file_write_names_what_it_wrote(ws):
    out = Toolset(ws).file_write("notes.md", "abc")
    assert out.text == "wrote notes.md (3 bytes)"
    assert out.written == "notes.md"
    assert not out.is_error


def test_file_edit_reports_failure_as_an_error(ws):
    toolset = Toolset(ws)
    toolset.file_write("a.txt", "hello world")
    done = toolset.file_edit("a.txt", "world", "there")
    assert done.text == "replaced 1 occurrence(s) in a.txt" and not done.is_error
    # a repeat is recognized as already applied: a no-op, not a failure
    repeat = toolset.file_edit("a.txt", "world", "there")
    assert repeat.text.startswith("no-op:") and not repeat.is_error
    missing = toolset.file_edit("a.txt", "absent", "x")
    assert missing.is_error and missing.text.startswith("edit failed:")


def test_view_image_returns_the_image(ws):
    ws.files.write("dot.png", PNG)
    toolset = Toolset(ws)
    out = toolset.view_image("dot.png")
    (image,) = out.images
    assert image.data == PNG and image.format == "png" and image.source == "dot.png"
    missing = toolset.view_image("nope.png")
    assert missing.is_error and not missing.images


def test_run_python_flags_an_error_and_notes_artifacts(ws):
    toolset = Toolset(ws)
    out = toolset.run_python("ui = {'kpis': [{'label': 'n', 'value': 3}]}")
    assert "[ui artifacts: kpis -> /workspace/ui/kpis.cards.json]" in out.text
    assert not out.is_error
    assert toolset.run_python("1 / 0").is_error


def test_acall_runs_the_call_off_the_loop(ws):
    (terminal,) = [t for t in Toolset(ws).tools() if t.name == "terminal"]
    out = asyncio.run(terminal.acall(command="echo async"))
    assert isinstance(out, ToolOutput) and out.text == "async"


def test_sessions_without_a_runner_is_not_a_tool(ws):
    toolset = Toolset(ws)
    assert toolset.sessions is None
    with pytest.raises(RuntimeError):
        toolset.sessions_action("list")


def test_host_objects_make_the_set_split():
    workspace = Store(memory=True).open(
        "h", cache=False, python=PythonConfig(host_objects={"n": 3})
    )
    assert Toolset(workspace).split
    workspace.close()


# -- what the shared layer changed for MCP -------------------------------------


@pytest.mark.asyncio
async def test_mcp_run_python_reports_ui_artifacts(ws):
    """MCP's run_python returned the bare python output, so artifacts the
    call saved went unmentioned; it now reports them as agno's does."""
    pytest.importorskip("mcp")
    from nontainer.adapters.mcp import build_server

    server = build_server(ws)
    result = await server.call_tool(
        "run_python", {"code": "ui = {'kpis': [{'label': 'n', 'value': 3}]}"}
    )
    blocks = result[0] if isinstance(result, tuple) else result
    text = "".join(getattr(b, "text", "") for b in blocks)
    assert "[ui artifacts: kpis -> /workspace/ui/kpis.cards.json]" in text
