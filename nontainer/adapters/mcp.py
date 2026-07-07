"""MCP adapter: a FastMCP server exposing one Workspace session.

Run over stdio (the common local-agent shape)::

    python -m nontainer.adapters.mcp --session my-project
    python -m nontainer.adapters.mcp --backend dir --store ./scratch \\
        --module math --module json --tools split

Or embed: :func:`build_server` returns the ``FastMCP`` instance for a
Workspace you constructed yourself (custom PythonConfig, mounts,
host objects — CLI flags only cover the config-file-able subset).

Concurrency: FastMCP may run sync tools on worker threads; every tool
call holds a per-workspace ``threading.Lock`` (same rationale as the
agno adapter — see protocol.py's concurrency note).
"""

from __future__ import annotations

import threading
from typing import Any

from mcp.server.fastmcp import FastMCP

from ..workspace import Workspace
from .render import (
    FILE_EDIT_DESCRIPTION,
    FILE_WRITE_DESCRIPTION,
    ToolsMode,
    python_description,
    render_python,
    render_terminal,
    resolve_tools_mode,
    terminal_description,
)


def build_server(
    workspace: Workspace,
    *,
    tools: ToolsMode = "auto",
    apps: Any = None,
    name: str = "nontainer",
    terminal_primer: str | None = None,
    python_primer: str | None = None,
) -> FastMCP:
    """Build a FastMCP server over an existing Workspace.

    ``apps``: an ``AppRuntime`` — when given, a ``test_app`` tool is
    registered; screenshots return as MCP ImageContent AND persist
    under /app/screenshots/. ``terminal_primer``/``python_primer``
    append host guidance to the respective tool descriptions."""
    server = FastMCP(name)
    lock = threading.Lock()
    mode = resolve_tools_mode(workspace, tools)
    split = mode == "split"
    if python_primer and not split:
        import warnings

        warnings.warn(
            "python_primer set but tools resolved to terminal-only "
            "(no run_python tool); it appears in the terminal tool's "
            "python section instead.",
            stacklevel=2,
        )

    @server.tool(
        name="terminal",
        description=terminal_description(
            workspace,
            split=split,
            apps=apps is not None,
            primer=terminal_primer,
            python_primer=None if split else python_primer,
        ),
    )
    def terminal(command: str) -> str:
        with lock:
            return render_terminal(workspace.terminal(command))

    @server.tool(name="file_write", description=FILE_WRITE_DESCRIPTION)
    def file_write(path: str, content: str) -> str:
        with lock:
            out = workspace.write_file(path, content)
            return f"wrote {out.path} ({out.size} bytes)"

    @server.tool(name="file_edit", description=FILE_EDIT_DESCRIPTION)
    def file_edit(
        path: str, old_string: str, new_string: str, replace_all: bool = False
    ) -> str:
        from ..errors import WorkspaceError

        with lock:
            try:
                out = workspace.edit_file(
                    path, old_string, new_string, replace_all=replace_all
                )
            except WorkspaceError as e:
                return f"edit failed: {e}"
            if out.mode == "already_applied":
                return f"no-op: replacement already present in {path}"
            note = "" if out.mode == "exact" else f" (matched via {out.mode})"
            return f"replaced {out.count} occurrence(s) in {path}{note}"

    if split:

        @server.tool(
            name="run_python",
            description=python_description(workspace, primer=python_primer),
        )
        def run_python(code: str) -> str:
            with lock:
                return render_python(workspace.run_python(code))

    if apps is not None:
        from mcp.server.fastmcp import Image

        from ..apps import render_test_app
        from .render import TEST_APP_DESCRIPTION

        @server.tool(name="test_app", description=TEST_APP_DESCRIPTION)
        async def test_app(actions: list[dict], viewport: str = "desktop") -> list:
            from ..apps.testapp import coerce_actions

            try:
                actions = coerce_actions(actions)
            except ValueError as e:
                return [f"test_app failed: {e}"]
            # async + to_thread: Playwright's sync API refuses to run on
            # a live asyncio loop thread (FastMCP executes sync tools
            # in-loop), so the browser work must be off-loop.
            import anyio

            def work() -> tuple[Any, list]:
                with lock:
                    result = apps.test_app(actions, viewport=viewport)
                    shots = [
                        Image(data=workspace.fs.read(p), format="png")
                        for p in result.screenshots
                    ]
                    return result, shots

            result, shots = await anyio.to_thread.run_sync(work)
            return [render_test_app(result), *shots]

    return server


def main(argv: list[str] | None = None) -> None:
    import argparse
    import importlib

    from ..workspace import PythonConfig, workspace as make_workspace

    parser = argparse.ArgumentParser(
        prog="python -m nontainer.adapters.mcp",
        description="Serve a nontainer workspace session over MCP (stdio).",
    )
    parser.add_argument("--session", default="default")
    parser.add_argument("--store", default=None, help="store directory")
    parser.add_argument("--backend", default="kvgit", choices=["kvgit", "dir"])
    parser.add_argument(
        "--tools", default="auto", choices=["auto", "terminal", "split"]
    )
    parser.add_argument(
        "--no-cache", action="store_true", help="disable the persistent cache"
    )
    parser.add_argument(
        "--module",
        action="append",
        default=[],
        metavar="NAME",
        help="whitelist an importable module (repeatable), e.g. --module math",
    )
    args = parser.parse_args(argv)

    modules = [importlib.import_module(m) for m in args.module]
    ws = make_workspace(
        args.session,
        store=args.store,
        backend=args.backend,
        python=PythonConfig(modules=modules),
        cache=not args.no_cache,
    )
    try:
        build_server(ws, tools=args.tools).run()
    finally:
        ws.close()


if __name__ == "__main__":
    main()
