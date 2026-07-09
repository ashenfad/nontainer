"""MCP server adapter: tool listing, calls, exposure modes."""

import pytest

from nontainer import Workspace
from nontainer.providers import KvgitProvider

pytest.importorskip("mcp")

from nontainer.adapters.mcp import build_server  # noqa: E402


def make_ws(**kwargs) -> Workspace:
    return Workspace(KvgitProvider.open(None, session="s1"), **kwargs)


@pytest.mark.asyncio
async def test_mcp_server_tools_and_call():
    ws = make_ws()
    server = build_server(ws)
    tools = {t.name for t in await server.list_tools()}
    assert tools == {"terminal", "run_python", "file_write", "file_edit"}

    result = await server.call_tool("terminal", {"command": "echo mcp-works"})
    text = result[0][0].text if isinstance(result, tuple) else result[0].text
    assert "mcp-works" in text
    ws.close()


@pytest.mark.asyncio
async def test_mcp_terminal_only_mode():
    ws = make_ws(cache=False)
    server = build_server(ws)
    tools = {t.name for t in await server.list_tools()}
    assert tools == {"terminal", "file_write", "file_edit"}
    ws.close()


@pytest.mark.asyncio
async def test_mcp_file_tools():
    ws = make_ws()
    server = build_server(ws)
    tools = {t.name for t in await server.list_tools()}
    assert {"file_write", "file_edit"} <= tools
    await server.call_tool("file_write", {"path": "f.txt", "content": "abc"})
    await server.call_tool(
        "file_edit", {"path": "f.txt", "old_string": "abc", "new_string": "xyz"}
    )
    assert ws.fs.read("f.txt") == b"xyz"
    ws.close()


@pytest.mark.asyncio
async def test_mcp_apps_exposure():
    """The apps wiring the CLI's --apps flag enables: a test_app tool
    plus the curl terminal builtin, over one workspace."""
    from nontainer.apps import enable_apps

    ws = make_ws()
    runtime = enable_apps(ws)
    server = build_server(ws, apps=runtime)
    tools = {t.name for t in await server.list_tools()}
    assert "test_app" in tools

    ws.fs.makedirs("/app/api", exist_ok=True)
    ws.fs.write("/app/api/ping.py", b"def get(req):\n    return {'pong': True}\n")
    result = await server.call_tool("terminal", {"command": "curl /api/ping"})
    text = result[0][0].text if isinstance(result, tuple) else result[0].text
    assert "pong" in text
    ws.close()


def test_cli_flags_parse():
    """The CLI parser accepts the documented flags (main() itself
    would block serving stdio, so parse-level only)."""
    from nontainer.adapters.mcp import _build_parser

    args = _build_parser().parse_args(
        ["--session", "s", "--apps", "--module", "math", "--tools", "split"]
    )
    assert args.apps and args.module == ["math"] and args.tools == "split"
    assert not _build_parser().parse_args([]).apps  # off by default
