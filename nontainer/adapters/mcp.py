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

from mcp.server.fastmcp import FastMCP

from ..workspace import Workspace
from .render import (
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
    name: str = "nontainer",
) -> FastMCP:
    """Build a FastMCP server over an existing Workspace."""
    server = FastMCP(name)
    lock = threading.Lock()
    mode = resolve_tools_mode(workspace, tools)
    split = mode == "split"

    @server.tool(
        name="terminal",
        description=terminal_description(workspace, split=split),
    )
    def terminal(command: str) -> str:
        with lock:
            return render_terminal(workspace.terminal(command))

    if split:

        @server.tool(
            name="run_python",
            description=python_description(workspace),
        )
        def run_python(code: str) -> str:
            with lock:
                return render_python(workspace.run_python(code))

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
