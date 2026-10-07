"""MCP adapter: a FastMCP server exposing one Workspace session.

Run over stdio (the common local-agent shape)::

    python -m nontainer.adapters.mcp --session my-project
    python -m nontainer.adapters.mcp --store ./scratch \\
        --module math --module json --tools split
    python -m nontainer.adapters.mcp --session webdev --apps  # + curl/test_app

Or embed: :func:`build_server` returns the ``FastMCP`` instance for a
Workspace you constructed yourself (custom PythonConfig, mounts,
host objects — CLI flags only cover the config-file-able subset).

Concurrency: every tool body runs on a worker thread. ``Workspace``
enforces its own single-writer invariant (mutating calls hold an
internal lock); the shared :class:`~nontainer.adapters.tools.Toolset`'s
per-workspace ``threading.Lock`` stays as a fence for work around the
call, and the resources read under it too (same rationale as the agno
adapter — see protocol.py's concurrency note).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from mcp.server.fastmcp import FastMCP

from ..workspace import Workspace
from .render import ToolsMode
from .tools import ToolOutput, Toolset


async def _off_loop(work: "Callable[[], Any]") -> Any:
    """Run a tool's blocking body on a worker thread.

    FastMCP calls a sync tool inline on its event loop, so a long
    ``run_python``, a ``ws-pytest`` in the terminal, or a ``sessions``
    ask with ``wait=true`` held the loop for its whole run, and with it
    every other request, ping and notification. Every tool here is
    ``async`` and does its work through this, as ``test_app`` always
    has. The per-workspace lock the bodies take still serializes calls
    on one workspace."""
    import anyio

    return await anyio.to_thread.run_sync(work)


def _content(out: ToolOutput) -> list:
    """A toolset result as MCP content: the text, then its images."""
    from mcp.server.fastmcp import Image

    return [out.text, *(Image(data=i.data, format=i.format) for i in out.images)]


def build_server(
    workspace: Workspace,
    *,
    tools: ToolsMode = "auto",
    apps: Any = None,
    sessions: Any = None,
    name: str = "nontainer",
    terminal_primer: str | None = None,
    python_primer: str | None = None,
) -> FastMCP:
    """Build a FastMCP server over an existing Workspace.

    ``apps``: an ``AppRuntime`` — when given, a ``test_app`` tool is
    registered; screenshots return as MCP ImageContent AND persist
    under ``<root>/app/screenshots/``.

    ``sessions``: a ``SessionRunner`` or an already-built
    ``nontainer.sessions.Sessions`` — when given, a ``sessions`` tool is
    registered and the agent can delegate to forks of this session. No
    runner, no tool. A runner passed here builds a helper this server
    holds for its lifetime; pass a ``Sessions`` when you need to close
    it yourself.

    ``terminal_primer``/``python_primer`` append host guidance to the
    respective tool descriptions."""
    server = FastMCP(name)
    toolset = Toolset(
        workspace,
        tools=tools,
        apps=apps,
        sessions=sessions,
        terminal_primer=terminal_primer,
        python_primer=python_primer,
    )
    lock = toolset.lock
    split = toolset.split

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

    # Thin typed wrappers: FastMCP derives each tool's schema from the
    # signature, and the toolset does the work, off the event loop.
    @server.tool(
        name="terminal", description=toolset.description("terminal") + resource_note
    )
    async def terminal(command: str) -> str:
        return (await _off_loop(lambda: toolset.terminal(command))).text

    @server.tool(
        name="file_write",
        description=toolset.description("file_write") + resource_note,
    )
    async def file_write(path: str, content: str) -> list:
        import mcp.types as types

        out = await _off_loop(lambda: toolset.file_write(path, content))
        written = out.written or path
        # Ground-truth artifact handle: the link exists because the
        # write succeeded — clients can fetch it without trusting prose.
        return [
            out.text,
            types.ResourceLink(
                type="resource_link",
                uri=f"workspace://{written.lstrip('/')}",
                name=written.rsplit("/", 1)[-1],
            ),
        ]

    @server.tool(name="file_edit", description=toolset.description("file_edit"))
    async def file_edit(
        path: str, old_string: str, new_string: str, replace_all: bool = False
    ) -> str:
        out = await _off_loop(
            lambda: toolset.file_edit(
                path, old_string, new_string, replace_all=replace_all
            )
        )
        return out.text

    @server.tool(name="view_image", description=toolset.description("view_image"))
    async def view_image(path: str) -> list:
        return _content(await _off_loop(lambda: toolset.view_image(path)))

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
    async def workspace_tree() -> str:
        # Off the loop like the tools: it takes the same lock, so on the
        # loop it would wait out a slow tool and freeze the server.
        return await _off_loop(_workspace_tree)

    def _workspace_tree() -> str:
        with lock:
            lines: list[str] = []
            elided = False

            # Depth-capped against symlink cycles (the VFS supports
            # symlinks) — and the cap announces itself rather than
            # silently truncating (PR #8 review).
            def walk(d: str, depth: int = 0) -> None:
                nonlocal elided
                if depth > 32:
                    elided = True
                    return
                for name in sorted(workspace.files.fs.list(d)):
                    full = f"{d.rstrip('/')}/{name}"
                    if workspace.files.fs.isdir(full):
                        walk(full, depth + 1)
                    else:
                        lines.append(full)

            walk("/")
            if elided:
                lines.append("[... directories deeper than 32 levels elided]")
            return "\n".join(lines)

    async def workspace_file(path: str) -> "str | bytes":
        def read() -> bytes:
            with lock:
                return workspace.files.fs.read("/" + path.lstrip("/"))

        data = await _off_loop(read)
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
            # RFC 3986: query/fragment aren't part of the path — strip
            # them so a client appending ?params still resolves. (Files
            # with literal ?/# in the name were never resource-
            # addressable without percent-encoding anyway.)
            clean = uri.split("?", 1)[0].split("#", 1)[0]
            pattern = self.uri_template.replace("{", "(?P<").replace("}", ">.+)")
            m = _re.match(f"^{pattern}$", clean)
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
            description=toolset.description("run_python") + resource_note,
        )
        async def run_python(code: str) -> str:
            return (await _off_loop(lambda: toolset.run_python(code))).text

    if toolset.sessions is not None:
        # One tool with an action argument, the shape test_app has, and
        # the same dispatch the agno adapter calls — so the two surfaces
        # cannot drift into saying different things about one job.
        @server.tool(name="sessions", description=toolset.description("sessions"))
        # ``fork_from`` rather than ``from``: a JSON argument's name is
        # the python parameter's name in both adapters, and ``from`` is
        # a python keyword, so it can name neither the parameter here
        # nor the argument a model sends. The tool takes the word the
        # host API takes, one spelling everywhere.
        async def sessions_tool(
            action: str,
            task: str = "",
            name: str = "",
            paths: "list[str] | str | None" = None,
            inherit: str = "",
            fork_from: str = "",
            resume: str = "",
            wait: bool = False,
        ) -> str:
            # Off the loop: a wait=true ask blocks for the delegate's
            # whole run. The toolset takes no fence for it.
            out = await _off_loop(
                lambda: toolset.sessions_action(
                    action,
                    task=task,
                    name=name,
                    paths=paths,
                    inherit=inherit,
                    fork_from=fork_from,
                    resume=resume,
                    wait=wait,
                )
            )
            return out.text

    if apps is not None:

        @server.tool(name="test_app", description=toolset.description("test_app"))
        async def test_app(
            actions: list[dict],
            viewport: str | dict = "desktop",
            bind: dict | str | None = None,
        ) -> list:
            # Off the loop as every tool is, and here it is required:
            # Playwright's sync API refuses to run on a live asyncio
            # loop thread.
            return _content(
                await _off_loop(lambda: toolset.test_app(actions, viewport, bind))
            )

    return server


def _build_parser() -> "Any":
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m nontainer.adapters.mcp",
        description="Serve a nontainer workspace session over MCP (stdio).",
    )
    parser.add_argument("--session", default="default")
    parser.add_argument("--store", default=None, help="store directory")
    parser.add_argument("--backend", default="kvgit", choices=["kvgit"])
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
        "are live views: not versioned, not captured by commits.",
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
        if not rest:
            # An empty DIR would Path("").resolve() to the server's
            # cwd — silently mounting wherever it was launched from.
            raise SystemExit(
                f"--mount expects POINT=DIR[:rw] with a non-empty "
                f"directory, got {spec!r}"
            )
        mounts[point] = Mount(rest, readonly=readonly)
    return mounts


def main(argv: list[str] | None = None) -> None:
    import importlib

    from ..workspace import PythonConfig
    from ..workspace import workspace as make_workspace

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
