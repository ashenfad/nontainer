"""Adapters: rendering, exposure heuristic, agno Toolkit, MCP server."""

import pytest

from nontainer import PythonConfig, Workspace
from nontainer.adapters.render import (
    render_python,
    render_terminal,
    resolve_tools_mode,
    terminal_description,
)
from nontainer.providers import KvgitProvider
from nontainer.workspace import PythonResult, TerminalResult


def make_ws(**kwargs) -> Workspace:
    return Workspace(KvgitProvider.open(None, session="s1"), **kwargs)


# -- rendering ---------------------------------------------------------------


def test_render_terminal_success():
    assert render_terminal(TerminalResult(stdout="hi\n", exit_code=0)) == "hi"


def test_render_terminal_failure_and_truncation():
    out = render_terminal(
        TerminalResult(stdout="part", exit_code=1, stderr="boom", truncated=True)
    )
    assert "[exit code 1]" in out
    assert "[stderr]\nboom" in out
    assert "[output truncated]" in out


def test_render_python_never_inlines_namespace():
    r = PythonResult(stdout="", namespace={"ui": {"secret": list(range(1000))}})
    out = render_python(r)
    assert "secret" not in out
    assert "[namespace kept for host: ui]" in out


def test_render_python_error():
    out = render_python(PythonResult(stdout="x", error="Traceback...ZeroDivision"))
    assert "[error]" in out and "ZeroDivision" in out


# -- exposure heuristic --------------------------------------------------------


def test_auto_mode_split_when_cache_enabled():
    ws = make_ws()  # cache on by default
    assert resolve_tools_mode(ws, "auto") == "split"
    ws.close()


def test_auto_mode_terminal_when_plain():
    ws = make_ws(cache=False)
    assert resolve_tools_mode(ws, "auto") == "terminal"
    ws.close()


def test_auto_mode_split_when_host_objects():
    ws = make_ws(cache=False, python=PythonConfig(host_objects={"db": {"x": 1}}))
    assert resolve_tools_mode(ws, "auto") == "split"
    ws.close()


def test_explicit_mode_wins():
    ws = make_ws()
    assert resolve_tools_mode(ws, "terminal") == "terminal"
    ws.close()


def test_terminal_description_mentions_cache_only_when_terminal_only():
    ws = make_ws()
    assert "cache" not in terminal_description(ws, split=True)
    assert "cache" in terminal_description(ws, split=False)
    ws.close()


# -- agno --------------------------------------------------------------------

agno = pytest.importorskip("agno")


def test_agno_toolkit_split_mode():
    from nontainer.adapters.agno import WorkspaceTools

    ws = make_ws()
    tk = WorkspaceTools(ws)
    names = set(tk.functions)
    assert names == {"terminal", "run_python"}
    assert "ONE" in (tk.instructions or "")

    out = tk.functions["terminal"].entrypoint("echo hello | tr a-z A-Z")
    assert out.strip() == "HELLO"
    out = tk.functions["run_python"].entrypoint("cache['n'] = 1\nprint('ok')")
    assert "ok" in out
    assert ws.cache["n"] == 1
    ws.close()


def test_agno_toolkit_terminal_only():
    from nontainer.adapters.agno import WorkspaceTools

    ws = make_ws(cache=False)
    tk = WorkspaceTools(ws)
    assert set(tk.functions) == {"terminal"}
    # python still reachable as a terminal builtin
    out = tk.functions["terminal"].entrypoint("python -c 'print(2+2)'")
    assert out.strip() == "4"
    ws.close()


def test_agno_parallel_calls_serialize():
    """Simulate agno arun's thread-concurrent tool execution."""
    from concurrent.futures import ThreadPoolExecutor

    from nontainer.adapters.agno import WorkspaceTools

    ws = make_ws()
    tk = WorkspaceTools(ws)
    term = tk.functions["terminal"].entrypoint

    def call(i: int) -> str:
        return term(f"echo line{i} >> log.txt; cat log.txt | wc -l")

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(call, range(8)))

    final = term("cat log.txt | wc -l")
    assert final.strip() == "8"  # no lost writes, no corruption
    ws.close()


# -- mcp -----------------------------------------------------------------------

mcp_mod = pytest.importorskip("mcp")


@pytest.mark.asyncio
async def test_mcp_server_tools_and_call():
    from nontainer.adapters.mcp import build_server

    ws = make_ws()
    server = build_server(ws)
    tools = {t.name for t in await server.list_tools()}
    assert tools == {"terminal", "run_python"}

    result = await server.call_tool("terminal", {"command": "echo mcp-works"})
    text = result[0][0].text if isinstance(result, tuple) else result[0].text
    assert "mcp-works" in text
    ws.close()


@pytest.mark.asyncio
async def test_mcp_terminal_only_mode():
    from nontainer.adapters.mcp import build_server

    ws = make_ws(cache=False)
    server = build_server(ws)
    tools = {t.name for t in await server.list_tools()}
    assert tools == {"terminal"}
    ws.close()
