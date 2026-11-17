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
    assert tools == {"terminal", "run_python", "file_write", "file_edit", "view_image"}

    result = await server.call_tool("terminal", {"command": "echo mcp-works"})
    text = result.content[0].text
    assert "mcp-works" in text
    ws.close()


@pytest.mark.asyncio
async def test_mcp_terminal_only_mode():
    ws = make_ws(cache=False)
    server = build_server(ws)
    tools = {t.name for t in await server.list_tools()}
    assert tools == {"terminal", "file_write", "file_edit", "view_image"}
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
    assert ws.files.fs.read("f.txt") == b"xyz"
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

    ws.files.fs.makedirs("/workspace/app/api", exist_ok=True)
    ws.files.fs.write(
        "/workspace/app/api/ping.py", b"def get(req):\n    return {'pong': True}\n"
    )
    result = await server.call_tool(
        "terminal", {"command": "ws-curl $APP_ORIGIN/api/ping"}
    )
    text = result.content[0].text
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


# -- artifact channels: view_image, resources, mounts ------------------------

# 1x1 red PNG
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080200000090"
    "7753de0000000c49444154089963f8cfc000000301010018dd8db0000000"
    "0049454e44ae426082"
)


@pytest.mark.asyncio
async def test_mcp_view_image():
    ws = make_ws()
    ws.files.fs.write("/plot.png", PNG)
    server = build_server(ws)
    result = await server.call_tool("view_image", {"path": "/plot.png"})
    blocks = result.content
    kinds = {b.type for b in blocks}
    assert "image" in kinds
    img = next(b for b in blocks if b.type == "image")
    assert img.mimeType == "image/png"
    ws.close()


@pytest.mark.asyncio
async def test_mcp_view_image_errors_are_actionable():
    ws = make_ws()
    server = build_server(ws)
    result = await server.call_tool("view_image", {"path": "/nope.png"})
    blocks = result.content
    assert "cannot read" in blocks[0].text
    result = await server.call_tool("view_image", {"path": "/data.csv"})
    blocks = result.content
    assert "not a viewable image" in blocks[0].text
    ws.close()


# -- a failed call is an error result -----------------------------------------


async def _over_the_wire(server, name, args):
    """A tool call as an MCP client receives it: through a client
    session, not FastMCP's in-process call."""
    from mcp.shared.memory import create_connected_server_and_client_session

    async with create_connected_server_and_client_session(server._mcp_server) as client:
        return await client.call_tool(name, args)


@pytest.mark.asyncio
async def test_a_call_that_did_not_come_out_as_asked_is_an_error_result():
    """A non-zero exit or a failed edit came back with ``isError`` false,
    so a client counted every call a success."""
    ws = make_ws()
    server = build_server(ws)
    failed = await _over_the_wire(server, "terminal", {"command": "cat /missing.txt"})
    ok = await _over_the_wire(server, "terminal", {"command": "echo fine"})
    edit = await _over_the_wire(
        server, "file_edit", {"path": "/nope.txt", "old_string": "a", "new_string": "b"}
    )
    image = await _over_the_wire(server, "view_image", {"path": "/nope.png"})
    assert [r.isError for r in (failed, ok, edit, image)] == [True, False, True, True]
    assert "fine" in ok.content[0].text
    # a text tool keeps the output schema it advertised, failed or not
    assert failed.structuredContent == {"result": failed.content[0].text}
    schema = {t.name: t.outputSchema for t in await server.list_tools()}["terminal"]
    assert schema["properties"] == {"result": {"title": "Result", "type": "string"}}
    ws.close()


@pytest.mark.asyncio
async def test_a_failed_test_app_run_keeps_its_screenshots(monkeypatch):
    """The screenshot of a failed run is what shows why it failed."""
    from nontainer.apps import enable_apps
    from nontainer.apps.testapp import TestAppResult

    ws = make_ws()
    ws.files.fs.write("/shot.png", PNG)
    apps = enable_apps(ws)
    server = build_server(ws, apps=apps)
    for passed in (False, True):
        monkeypatch.setattr(
            apps,
            "test_app",
            lambda actions, passed=passed, **kwargs: TestAppResult(
                ok=passed, screenshots=("/shot.png",)
            ),
        )
        result = await _over_the_wire(
            server, "test_app", {"actions": [{"screenshot": True}]}
        )
        assert result.isError is not passed
        assert [b.type for b in result.content] == ["text", "image"]
        assert result.content[1].mimeType == "image/png"
    ws.close()


@pytest.mark.asyncio
async def test_mcp_resources():
    """Workspace files are readable as workspace://{path} — text as
    str, binary as bytes — and workspace://-/tree lists them. Pins the
    multi-segment template registration against mcp internals."""
    ws = make_ws()
    ws.files.fs.makedirs("/data/deep", exist_ok=True)
    ws.files.fs.write("/data/deep/x.csv", b"a,b\n1,2\n")
    ws.files.fs.write("/blob.bin", bytes([0, 255, 128]))
    server = build_server(ws)

    tree = list(await server.read_resource("workspace://-/tree"))[0]
    assert tree.content == "/blob.bin\n/data/deep/x.csv"
    csv = list(await server.read_resource("workspace://data/deep/x.csv"))[0]
    assert csv.content == "a,b\n1,2\n"
    blob = list(await server.read_resource("workspace://blob.bin"))[0]
    assert blob.content == bytes([0, 255, 128])
    # RFC 3986: query/fragment aren't part of the path (PR #8 review)
    q = list(await server.read_resource("workspace://data/deep/x.csv?reload=1"))[0]
    assert q.content == "a,b\n1,2\n"
    ws.close()


@pytest.mark.asyncio
async def test_mcp_tree_elides_beyond_depth_cap():
    ws = make_ws()
    deep = "/" + "/".join(f"d{i}" for i in range(40))
    ws.files.fs.makedirs(deep, exist_ok=True)
    ws.files.fs.write(f"{deep}/leaf.txt", b"x")
    ws.files.fs.write("/top.txt", b"y")
    server = build_server(ws)
    tree = list(await server.read_resource("workspace://-/tree"))[0].content
    assert "/top.txt" in tree
    assert "leaf.txt" not in tree  # beyond the cap...
    assert "elided" in tree  # ...and the cap announces itself
    ws.close()


def test_parse_mounts():
    from nontainer.adapters.mcp import _parse_mounts

    mounts = _parse_mounts(["/data=~/datasets", "/out=/tmp/outbox:rw"])
    assert mounts["/data"].path == "~/datasets" and mounts["/data"].readonly
    assert mounts["/out"].path == "/tmp/outbox" and not mounts["/out"].readonly
    with pytest.raises(SystemExit):
        _parse_mounts(["data=~/x"])  # point must be absolute
    with pytest.raises(SystemExit):
        _parse_mounts(["/data"])  # missing =DIR
    with pytest.raises(SystemExit):
        _parse_mounts(["/data="])  # empty DIR would mount the cwd
    with pytest.raises(SystemExit):
        _parse_mounts(["/data=:rw"])  # ditto after :rw strip


def test_cli_mount_flag_parses():
    from nontainer.adapters.mcp import _build_parser

    args = _build_parser().parse_args(["--mount", "/data=/tmp/d"])
    assert args.mount == ["/data=/tmp/d"]


@pytest.mark.asyncio
async def test_mcp_file_write_returns_resource_link():
    """file_write's result carries a ground-truth ResourceLink to the
    artifact — the link exists because the write succeeded."""
    ws = make_ws()
    server = build_server(ws)
    result = await server.call_tool(
        "file_write", {"path": "/out/report.md", "content": "# done\n"}
    )
    blocks = result.content
    link = next(b for b in blocks if b.type == "resource_link")
    assert str(link.uri) == "workspace://out/report.md"
    assert link.name == "report.md"
    # and the link resolves through the resource template
    content = list(await server.read_resource(str(link.uri)))[0]
    assert content.content == "# done\n"
    ws.close()


@pytest.mark.asyncio
async def test_mcp_descriptions_coach_resource_uris():
    ws = make_ws()
    server = build_server(ws)
    descs = {t.name: t.description for t in await server.list_tools()}
    for name in ("terminal", "run_python", "file_write"):
        assert "workspace://" in descs[name], name
    ws.close()


# -- the sessions tool ---------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_no_sessions_tool_without_a_runner():
    ws = make_ws()
    server = build_server(ws)
    assert "sessions" not in {t.name for t in await server.list_tools()}
    ws.close()


@pytest.mark.asyncio
async def test_mcp_sessions_tool_round_trips_a_delegate(tmp_path):
    from nontainer import Store
    from nontainer.sessions import Sessions

    store = Store(tmp_path / "store")
    ws = store.open("analyst")
    ws.files.write("/workspace/report.md", "draft\n")
    ws.index.commit("seed")

    class Scripted:
        def run(self, session, task, *, budget=None):
            child = store.open(session)
            try:
                child.files.write("/workspace/report.md", "polished\n")
            finally:
                child.close()
            return "polished the report"

    helper = Sessions(ws, Scripted())
    server = build_server(ws, sessions=helper)
    try:
        assert "sessions" in {t.name for t in await server.list_tools()}

        async def call(**args) -> str:
            out = await server.call_tool("sessions", args)
            blocks = out.content
            return blocks[0].text

        text = await call(action="ask", task="polish the report", wait=True)
        assert "polished the report" in text
        assert "ws-git merge analyst." in text

        assert "answered" in await call(action="list")
        name = helper.list()[0].name
        assert (await call(action="result", name=name)).startswith("polished")
        assert "cancelled" not in await call(action="cancel", name=name)
    finally:
        helper.close()
        ws.close()
        store.close()


@pytest.mark.asyncio
async def test_mcp_sessions_tool_asks_from_a_fork_point_and_resumes(tmp_path):
    from nontainer import Store
    from nontainer.sessions import Sessions

    store = Store(tmp_path / "store")
    ws = store.open("analyst")
    ws.files.write("/workspace/report.md", "draft\n")
    ws.index.commit("seed")

    sage = store.open("sage")
    sage.files.write("/workspace/rates.md", "north 4, south 7\n")
    sage.index.commit("curated")
    store.tags.add(sage, "rates-2026")
    head = sage.head
    sage.close()

    class Scripted:
        def run(self, session, task, *, budget=None):
            child = store.open(session)
            try:
                child.files.write("/workspace/answer.md", "north is 4\n")
            finally:
                child.close()
            return f"answering: {task}"

    helper = Sessions(ws, Scripted())
    server = build_server(ws, sessions=helper)
    try:
        descs = {t.name: t.description for t in await server.list_tools()}
        assert "fork_from=" in descs["sessions"] and "resume=" in descs["sessions"]
        schema = {t.name: t.inputSchema for t in await server.list_tools()}["sessions"]
        assert {"fork_from", "resume"} <= set(schema["properties"])

        async def call(**args) -> str:
            out = await server.call_tool("sessions", args)
            blocks = out.content
            return blocks[0].text

        text = await call(
            action="ask", task="what is north?", fork_from="rates-2026", wait=True
        )
        child = helper.list()[0].name
        assert "answering: what is north?" in text
        assert f"ws-git merge {child}" in text
        assert "rates-2026" in await call(action="list")

        again = await call(action="ask", task="and south?", resume=child, wait=True)
        assert "answering: and south?" in again
        assert [j.name for j in helper.list()] == [child]

        sage = store.open("sage")
        try:
            assert sage.head == head
        finally:
            sage.close()
    finally:
        helper.close()
        ws.close()
        store.close()


@pytest.mark.asyncio
async def test_a_slow_tool_leaves_the_event_loop_free():
    """FastMCP runs a sync tool inline on its loop, so a long run_python
    (or a ws-pytest, or a sessions ask with wait=true) held every other
    request, ping and notification. The tools run their work on a
    thread; the loop keeps serving meanwhile."""
    import asyncio

    ws = make_ws()
    server = build_server(ws)
    ticks = 0

    async def heartbeat():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.05)
            ticks += 1

    beat = asyncio.create_task(heartbeat())
    try:
        result = await server.call_tool(
            "run_python", {"code": "import time\ntime.sleep(1)\nprint('slept')"}
        )
    finally:
        beat.cancel()
    text = result.content[0].text
    assert "slept" in text
    assert ticks >= 10  # the loop ran throughout, not once at the end
    ws.close()


@pytest.mark.asyncio
async def test_a_resource_read_during_a_slow_tool_leaves_the_loop_free():
    """The resource readers take the tools' lock. Read on the loop while
    a slow tool held it, a resource request froze the server just as a
    sync tool did; they wait off the loop too."""
    import asyncio

    ws = make_ws()
    server = build_server(ws)
    ticks = 0

    async def heartbeat():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.05)
            ticks += 1

    beat = asyncio.create_task(heartbeat())
    try:
        slow = asyncio.create_task(
            server.call_tool("run_python", {"code": "import time\ntime.sleep(1)"})
        )
        await asyncio.sleep(0.1)  # the tool holds the lock now
        tree = await server.read_resource("workspace://-/tree")
        await slow
    finally:
        beat.cancel()
    assert tree is not None
    assert ticks >= 10
    ws.close()


def test_a_cancelled_request_stops_its_body_and_waits_for_it():
    """The client's cancel tells the threaded body to stop, and the
    request ends only after the body has returned."""
    import time

    import anyio

    from nontainer.adapters.mcp import _off_loop_stoppable

    seen: dict[str, bool] = {}

    def work(stopped):
        while not stopped():
            time.sleep(0.01)
        time.sleep(0.3)  # the body's own cleanup
        seen["cleaned"] = True

    async def main() -> None:
        with anyio.move_on_after(0.3):
            await _off_loop_stoppable(work)
        seen["when_the_request_ended"] = seen.get("cleaned", False)

    anyio.run(main)
    assert seen["when_the_request_ended"] is True


def test_a_request_cancelled_before_its_body_starts_ends_at_once():
    """Every worker thread is taken, so the body is still waiting for one
    when the client cancels: the request ends without waiting for it,
    and the body never runs."""
    import threading
    import time

    import anyio

    from nontainer.adapters.mcp import _off_loop_stoppable

    ran: list[bool] = []
    release = threading.Event()

    async def main() -> float:
        anyio.to_thread.current_default_thread_limiter().total_tokens = 1
        async with anyio.create_task_group() as tg:
            tg.start_soon(anyio.to_thread.run_sync, release.wait)  # the only thread
            await anyio.sleep(0.1)
            started = time.monotonic()
            with anyio.move_on_after(0.3):
                await _off_loop_stoppable(lambda stopped: ran.append(True))
            took = time.monotonic() - started
            release.set()
        return took

    assert anyio.run(main) < 2
    assert ran == []
