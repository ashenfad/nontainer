"""MCP adapter: a FastMCP server exposing one Workspace session.

Run over stdio (the common local-agent shape)::

    python -m nontainer.adapters.mcp --session my-project
    python -m nontainer.adapters.mcp --backend dir --store ./scratch \\
        --module math --module json --tools split
    python -m nontainer.adapters.mcp --session webdev --apps  # + curl/test_app

Or embed: :func:`build_server` returns the ``FastMCP`` instance for a
Workspace you constructed yourself (custom PythonConfig, mounts,
host objects — CLI flags only cover the config-file-able subset).

Concurrency: FastMCP may run sync tools on worker threads. ``Workspace``
enforces its own single-writer invariant (mutating calls hold an
internal lock); the adapter's per-workspace ``threading.Lock`` stays as
a fence for adapter-level work around the call (same rationale as the
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
    VIEW_IMAGE_DESCRIPTION,
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

    # MCP-only coaching: workspace files are addressable as resources,
    # so the agent can hand the user real URIs for its artifacts.
    resource_note = (
        "\n\nWorkspace files are readable by your user as MCP resources "
        "at workspace://{path} — when you produce an artifact for them "
        "(a report, a plot, a dataset), mention its workspace:// URI."
    )
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
        )
        + resource_note,
    )
    def terminal(command: str) -> str:
        with lock:
            return render_terminal(workspace.terminal(command))

    @server.tool(name="file_write", description=FILE_WRITE_DESCRIPTION + resource_note)
    def file_write(path: str, content: str) -> list:
        import mcp.types as types

        with lock:
            out = workspace.write_file(path, content)
        # Ground-truth artifact handle: the link exists because the
        # write succeeded — clients can fetch it without trusting prose.
        return [
            f"wrote {out.path} ({out.size} bytes)",
            types.ResourceLink(
                type="resource_link",
                uri=f"workspace://{out.path.lstrip('/')}",
                name=out.path.rsplit("/", 1)[-1],
            ),
        ]

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

    @server.tool(name="view_image", description=VIEW_IMAGE_DESCRIPTION)
    def view_image(path: str) -> list:
        from mcp.server.fastmcp import Image

        from .render import read_workspace_image

        with lock:
            try:
                data, fmt = read_workspace_image(workspace, path)
            except ValueError as e:
                return [f"view_image failed: {e}"]
        return [f"{path} ({fmt}, {len(data)} bytes)", Image(data=data, format=fmt)]

    # -- workspace files as MCP resources -------------------------------
    # The outbound artifact channel: any workspace file is readable as
    # workspace://{path} (bytes for binary, str for utf-8 text), and
    # workspace://-/tree lists what exists. Tools are the agent's hands;
    # resources are the CLIENT's window into the artifacts they produce.

    @server.resource(
        "workspace://-/tree",
        name="workspace-tree",
        description="Recursive file listing of the workspace (one path "
        "per line) — the index for workspace://{path} resources.",
        mime_type="text/plain",
    )
    def workspace_tree() -> str:
        with lock:
            lines: list[str] = []

            def walk(d: str) -> None:
                for name in sorted(workspace.fs.list(d)):
                    full = f"{d.rstrip('/')}/{name}"
                    if workspace.fs.isdir(full):
                        walk(full)
                    else:
                        lines.append(full)

            walk("/")
            return "\n".join(lines)

    def workspace_file(path: str) -> "str | bytes":
        with lock:
            data = workspace.fs.read("/" + path.lstrip("/"))
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return data

    # FastMCP's template params match single URI segments ([^/]+), but
    # workspace paths have slashes — register a template whose params
    # match greedily instead. from_function constructs via cls(), so
    # the subclass rides the normal path; the _templates insert is the
    # one version-coupled touch (pinned by test_mcp_resources).
    import re as _re

    from mcp.server.fastmcp.resources.templates import ResourceTemplate

    class _MultiSegmentTemplate(ResourceTemplate):
        def matches(self, uri: str) -> "dict[str, Any] | None":
            pattern = self.uri_template.replace("{", "(?P<").replace("}", ">.+)")
            m = _re.match(f"^{pattern}$", uri)
            return m.groupdict() if m else None

    template = _MultiSegmentTemplate.from_function(
        workspace_file,
        uri_template="workspace://{path}",
        name="workspace-file",
        description="Read a workspace file by path (see workspace://-/tree).",
    )
    server._resource_manager._templates[template.uri_template] = template

    if split:

        @server.tool(
            name="run_python",
            description=python_description(workspace, primer=python_primer)
            + resource_note,
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


def _build_parser() -> "Any":
    import argparse

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
    parser.add_argument(
        "--apps",
        action="store_true",
        help="enable the apps loop: the curl terminal builtin plus a "
        "test_app tool (requires the [apps] extra; test_app needs "
        "`playwright install chromium` — checked lazily at first use)",
    )
    parser.add_argument(
        "--mount",
        action="append",
        default=[],
        metavar="POINT=DIR[:rw]",
        help="expose a host directory inside the workspace (repeatable), "
        "e.g. --mount /data=~/datasets. Read-only unless :rw. Mounts "
        "are live views: not versioned, not captured by checkpoints.",
    )
    return parser


def _parse_mounts(specs: list[str]) -> dict:
    """``POINT=DIR[:rw]`` → ``{point: Mount(dir, readonly=...)}``."""
    from ..workspace import Mount

    mounts = {}
    for spec in specs:
        point, sep, rest = spec.partition("=")
        if not sep or not point.startswith("/"):
            raise SystemExit(
                f"--mount expects POINT=DIR[:rw] with an absolute point, got {spec!r}"
            )
        readonly = True
        if rest.endswith(":rw"):
            readonly, rest = False, rest[: -len(":rw")]
        mounts[point] = Mount(rest, readonly=readonly)
    return mounts


def main(argv: list[str] | None = None) -> None:
    import importlib

    from ..workspace import PythonConfig, workspace as make_workspace

    args = _build_parser().parse_args(argv)

    modules = [importlib.import_module(m) for m in args.module]
    ws = make_workspace(
        args.session,
        store=args.store,
        backend=args.backend,
        python=PythonConfig(modules=modules),
        mounts=_parse_mounts(args.mount) or None,
        cache=not args.no_cache,
    )
    runtime = None
    if args.apps:
        from ..apps import enable_apps

        runtime = enable_apps(ws)
    try:
        build_server(ws, tools=args.tools, apps=runtime).run()
    finally:
        ws.close()


if __name__ == "__main__":
    main()
