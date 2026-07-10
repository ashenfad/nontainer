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
    text = result[0][0].text if isinstance(result, tuple) else result[0].text
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
    ws.fs.write("/plot.png", PNG)
    server = build_server(ws)
    result = await server.call_tool("view_image", {"path": "/plot.png"})
    blocks = result[0] if isinstance(result, tuple) else result
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
    blocks = result[0] if isinstance(result, tuple) else result
    assert "cannot read" in blocks[0].text
    result = await server.call_tool("view_image", {"path": "/data.csv"})
    blocks = result[0] if isinstance(result, tuple) else result
    assert "not a viewable image" in blocks[0].text
    ws.close()


@pytest.mark.asyncio
async def test_mcp_resources():
    """Workspace files are readable as workspace://{path} — text as
    str, binary as bytes — and workspace://-/tree lists them. Pins the
    multi-segment template registration against mcp internals."""
    ws = make_ws()
    ws.fs.makedirs("/data/deep", exist_ok=True)
    ws.fs.write("/data/deep/x.csv", b"a,b\n1,2\n")
    ws.fs.write("/blob.bin", bytes([0, 255, 128]))
    server = build_server(ws)

    tree = list(await server.read_resource("workspace://-/tree"))[0]
    assert tree.content == "/blob.bin\n/data/deep/x.csv"
    csv = list(await server.read_resource("workspace://data/deep/x.csv"))[0]
    assert csv.content == "a,b\n1,2\n"
    blob = list(await server.read_resource("workspace://blob.bin"))[0]
    assert blob.content == bytes([0, 255, 128])
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
    blocks = result[0] if isinstance(result, tuple) else result
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
