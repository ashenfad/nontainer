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
