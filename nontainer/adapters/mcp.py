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

import threading
from collections.abc import Callable
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent

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


async def _off_loop_stoppable(work: "Callable[[Callable[[], bool]], Any]") -> Any:
    """:func:`_off_loop` for a body that can be told to stop: ``work``
    is handed a ``stopped()`` to ask.

    A request the client cancels (``notifications/cancelled``) tells the
    body to stop and then waits for it to return before the cancellation
    goes on. The body's cleanup is done by the time the request ends,
    and no thread is left working on the workspace with nobody waiting
    for it. A request cancelled before its body started (still waiting
    for a worker thread) has nothing to wait for: the body is withdrawn,
    and runs nothing if a thread picks it up later."""
    import anyio

    stop = threading.Event()
    returned = threading.Event()
    gate = threading.Lock()
    phase = ["queued"]  # then "running", or "withdrawn" if cancelled first

    def body() -> Any:
        with gate:
            if phase[0] == "withdrawn":
                return None
            phase[0] = "running"
        try:
            return work(stop.is_set)
        finally:
            returned.set()

    cancelled = anyio.get_cancelled_exc_class()
    try:
        # Abandoning only the wait: the thread goes on, and is waited
        # for below once it has been told to stop.
        return await anyio.to_thread.run_sync(body, abandon_on_cancel=True)
    except cancelled:
        stop.set()
        with gate:
            started = phase[0] == "running"
            if not started:
                phase[0] = "withdrawn"
        if started:
            # Polled rather than waited on in a thread: when every
            # worker thread is taken, a thread-bound wait could not start.
            with anyio.CancelScope(shield=True):
                while not returned.is_set():
                    await anyio.sleep(0.05)
        raise


def _content(out: ToolOutput, *links: Any) -> CallToolResult:
    """A toolset result as an MCP tool result: the text, any links, then
    its images, with ``isError`` set when the call did not come out as
    asked (``ToolOutput.is_error``: a non-zero exit, a failed edit, a
    ``test_app`` check that failed). The text tells the model either
    way; the flag tells the client, which otherwise counts every call a
    success. A failed call keeps its images, so a failed ``test_app``
    still shows what the page looked like."""
    from mcp.server.fastmcp import Image

    images = [
        Image(data=i.data, format=i.format).to_image_content() for i in out.images
    ]
    return CallToolResult(
        content=[TextContent(type="text", text=out.text), *links, *images],
        isError=out.is_error,
    )


# A text tool's CallToolResult is annotated with str, the type of its
# text: FastMCP then advertises a {"result": string} output schema and
# checks each result's structured content against it.
_TextResult = Annotated[CallToolResult, str]


def _text(out: ToolOutput) -> CallToolResult:
    """:func:`_content` for a text tool, carrying the structured result
    its output schema promises."""
    result = _content(out)
    result.structuredContent = {"result": out.text}
    return result


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
    async def terminal(command: str) -> _TextResult:
        return _text(await _off_loop(lambda: toolset.terminal(command)))

    @server.tool(
        name="file_write",
        description=toolset.description("file_write") + resource_note,
    )
    async def file_write(path: str, content: str) -> CallToolResult:
        import mcp.types as types

        out = await _off_loop(lambda: toolset.file_write(path, content))
        written = out.written or path
        # Ground-truth artifact handle: the link exists because the
        # write succeeded — clients can fetch it without trusting prose.
        return _content(
            out,
            types.ResourceLink(
                type="resource_link",
                uri=f"workspace://{written.lstrip('/')}",
                name=written.rsplit("/", 1)[-1],
            ),
        )

    @server.tool(name="file_edit", description=toolset.description("file_edit"))
    async def file_edit(
        path: str, old_string: str, new_string: str, replace_all: bool = False
    ) -> _TextResult:
        out = await _off_loop(
            lambda: toolset.file_edit(
                path, old_string, new_string, replace_all=replace_all
            )
        )
        return _text(out)

    @server.tool(name="view_image", description=toolset.description("view_image"))
    async def view_image(path: str) -> CallToolResult:
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
        async def run_python(code: str) -> _TextResult:
            return _text(await _off_loop(lambda: toolset.run_python(code)))

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
        ) -> _TextResult:
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
            return _text(out)

    if apps is not None:

        @server.tool(name="test_app", description=toolset.description("test_app"))
        async def test_app(
            actions: list[dict],
            viewport: str | dict = "desktop",
            bind: dict | str | None = None,
        ) -> CallToolResult:
            # Off the loop as every tool is, and here it is required:
            # Playwright's sync API refuses to run on a live asyncio
            # loop thread. A cancelled request stops the run.
            return _content(
                await _off_loop_stoppable(
                    lambda stopped: toolset.test_app(
                        actions, viewport, bind, cancelled=stopped
                    )
                )
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
