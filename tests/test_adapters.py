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
    assert names == {"terminal", "run_python", "file_write", "file_edit"}
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
    assert set(tk.functions) == {"terminal", "file_write", "file_edit"}
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
    assert tools == {"terminal", "run_python", "file_write", "file_edit"}

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
    assert tools == {"terminal", "file_write", "file_edit"}
    ws.close()


def test_terminal_description_includes_apps_contract():
    from nontainer.adapters.render import terminal_description

    ws = make_ws()
    plain = terminal_description(ws, split=True, apps=False)
    with_apps = terminal_description(ws, split=True, apps=True)
    assert "def get(req)" not in plain
    for marker in ("def get(req)", "HttpError", "curl /api/scores",
                   "RELATIVE urls", "/app/logs/api.log", "READ-ONLY"):
        assert marker in with_apps, marker
    ws.close()


def test_agno_toolkit_with_apps_mentions_curl_in_terminal():
    pytest.importorskip("agno")
    from nontainer.adapters.agno import WorkspaceTools
    from nontainer.apps import enable_apps

    ws = make_ws()
    rt = enable_apps(ws)
    tk = WorkspaceTools(ws, apps=rt)
    # agno parses the docstring into .description lazily at schema-build
    term_desc = tk.functions["terminal"].entrypoint.__doc__ or ""
    assert "curl" in term_desc and "def get(req)" in term_desc
    assert "test_app" in tk.functions  # the verify tool rides along
    ws.close()


# -- file_write / file_edit ------------------------------------------------


def test_workspace_write_and_edit_file():
    from nontainer import WorkspaceError

    ws = make_ws()
    ws.write_file("src/app.py", "def main():\n    return 1\n")
    assert ws.fs.read("src/app.py").decode().endswith("return 1\n")

    out = ws.edit_file("src/app.py", "return 1", "return 2")
    assert out.count == 1 and out.mode == "exact"
    assert "return 2" in ws.fs.read("src/app.py").decode()

    with pytest.raises(WorkspaceError, match="not found"):
        ws.edit_file("src/app.py", "no such text", "x")

    ws.write_file("dup.txt", "a a a")
    with pytest.raises(WorkspaceError, match="3 times"):
        ws.edit_file("dup.txt", "a", "b")
    assert ws.edit_file("dup.txt", "a", "b", replace_all=True).count == 3

    infos = [c.info.get("tool") for c in ws.history(limit=6)]
    assert "file_write" in infos and "file_edit" in infos
    ws.close()


def test_agno_file_tools_registered_both_modes():
    pytest.importorskip("agno")
    from nontainer.adapters.agno import WorkspaceTools

    for kwargs in ({}, {"cache": False}):
        ws = make_ws(**kwargs)
        tk = WorkspaceTools(ws)
        assert {"file_write", "file_edit"} <= set(tk.functions)
        out = tk.functions["file_write"].entrypoint(
            path="notes.md", content="# hi\nline two\n"
        )
        assert "wrote" in out
        out = tk.functions["file_edit"].entrypoint(
            path="notes.md", old_string="line two", new_string="line 2"
        )
        assert "replaced 1" in out
        # agent-actionable failure comes back as text, not an exception
        out = tk.functions["file_edit"].entrypoint(
            path="notes.md", old_string="absent", new_string="x"
        )
        assert "edit failed" in out
        ws.close()


@pytest.mark.asyncio
async def test_mcp_file_tools():
    from nontainer.adapters.mcp import build_server

    ws = make_ws()
    server = build_server(ws)
    tools = {t.name for t in await server.list_tools()}
    assert {"file_write", "file_edit"} <= tools
    await server.call_tool("file_write", {"path": "f.txt", "content": "abc"})
    result = await server.call_tool(
        "file_edit", {"path": "f.txt", "old_string": "abc", "new_string": "xyz"}
    )
    assert ws.fs.read("f.txt") == b"xyz"
    ws.close()


def test_edit_file_agent_tolerant_matching():
    """The agex strategy set, ported: trailing-ws, indent-flex, no-op."""
    ws = make_ws()

    # trailing whitespace in the file, clean search from the agent
    ws.write_file("a.py", "def f():   \n    return 1\n")
    out = ws.edit_file("a.py", "def f():\n    return 1", "def f():\n    return 2")
    assert out.mode == "trailing_ws"
    assert "return 2" in ws.fs.read("a.py").decode()

    # agent quotes the block at the wrong baseline (uniformly shifted):
    # match anyway, and shift the replacement to the file's baseline.
    # (Constant-delta re-indent, per agex: internal steps are preserved,
    # not rescaled.)
    ws.write_file("b.py", "class C:\n    def m(self):\n        return 'old'\n")
    out = ws.edit_file(
        "b.py",
        "def m(self):\n    return 'old'",
        "def m(self):\n    return 'new'",
    )
    assert out.mode == "indent_flexible"
    assert "        return 'new'" in ws.fs.read("b.py").decode()  # file's indent

    # idempotent retry: replacement already present → no-op, not an error
    out = ws.edit_file("b.py", "return 'old'", "return 'new'")
    assert out.mode == "already_applied" and out.count == 0

    # actionable failure: near-miss shows "did you mean" with line numbers
    from nontainer import WorkspaceError

    ws.write_file("c.py", "def compute(x):\n    return x * 42\n")
    with pytest.raises(WorkspaceError, match="Did you mean"):
        ws.edit_file("c.py", "def compute(x):\n    return x * 43", "zzz")

    # backslash-safe replacement through the regex (trailing-ws) path
    ws.write_file("d.txt", "value:  \nend\n")
    out = ws.edit_file("d.txt", "value:", r"value: \1 \g<0>")
    assert out.count == 1
    assert r"\1 \g<0>" in ws.fs.read("d.txt").decode()
    ws.close()


def test_parallel_file_writes_are_safe():
    """The Claude-Code idiom: several file_write calls in one turn.
    Under agno arun they run on threads; the lock keeps them safe."""
    pytest.importorskip("agno")
    from concurrent.futures import ThreadPoolExecutor

    from nontainer.adapters.agno import WorkspaceTools

    ws = make_ws()
    tk = WorkspaceTools(ws)
    fw = tk.functions["file_write"].entrypoint

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(
            lambda i: fw(path=f"src/mod_{i}.py", content=f"X = {i}\n"),
            range(6),
        ))

    for i in range(6):
        assert ws.fs.read(f"src/mod_{i}.py").decode() == f"X = {i}\n"
    assert len(list(ws.history())) >= 7  # each write checkpointed
    ws.close()
