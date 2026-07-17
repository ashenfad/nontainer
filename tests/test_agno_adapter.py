"""agno Toolkit adapter: exposure modes, locking, checkpoint modes, schemas."""

import pytest

from nontainer import Workspace
from nontainer.providers import KvgitProvider

pytest.importorskip("agno")

from nontainer.adapters.agno import WorkspaceTools  # noqa: E402


def make_ws(**kwargs) -> Workspace:
    return Workspace(KvgitProvider.open(None, session="s1"), **kwargs)


# -- tool exposure -------------------------------------------------------------


def test_agno_toolkit_split_mode():
    ws = make_ws()
    tk = WorkspaceTools(ws)
    names = set(tk.functions)
    assert names == {"terminal", "run_python", "file_write", "file_edit", "view_image"}
    assert "ONE" in (tk.instructions or "")

    out = tk.functions["terminal"].entrypoint("echo hello | tr a-z A-Z")
    assert out.strip() == "HELLO"
    out = tk.functions["run_python"].entrypoint("cache['n'] = 1\nprint('ok')")
    assert "ok" in out
    assert ws.cache["n"] == 1
    ws.close()


def test_agno_toolkit_terminal_only():
    ws = make_ws(cache=False)
    tk = WorkspaceTools(ws)
    assert set(tk.functions) == {"terminal", "file_write", "file_edit", "view_image"}
    # python still reachable as a terminal builtin
    out = tk.functions["terminal"].entrypoint("python -c 'print(2+2)'")
    assert out.strip() == "4"
    ws.close()


def test_agno_toolkit_with_apps_mentions_curl_in_terminal():
    from nontainer.apps import enable_apps

    ws = make_ws()
    rt = enable_apps(ws)
    tk = WorkspaceTools(ws, apps=rt)
    # agno parses the docstring into .description lazily at schema-build
    term_desc = tk.functions["terminal"].entrypoint.__doc__ or ""
    assert "curl" in term_desc and "def get(req)" in term_desc
    assert "test_app" in tk.functions  # the verify tool rides along
    ws.close()


def test_agno_file_tools_registered_both_modes():
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


# -- concurrency: the per-workspace lock -----------------------------------------


def test_agno_parallel_calls_serialize():
    """Simulate agno arun's thread-concurrent tool execution."""
    from concurrent.futures import ThreadPoolExecutor

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


def test_parallel_file_writes_are_safe():
    """The Claude-Code idiom: several file_write calls in one turn.
    Under agno arun they run on threads; the lock keeps them safe."""
    from concurrent.futures import ThreadPoolExecutor

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


# -- checkpoint granularity ------------------------------------------------------


def test_turn_checkpoint_mode():
    """agex-style granularity: one commit per turn via end_turn."""
    ws = make_ws()
    tk = WorkspaceTools(ws, checkpoint="turn")
    before = len(list(ws.history()))

    # a "turn": several mutations, zero commits until end_turn
    tk.functions["file_write"].entrypoint(path="a.py", content="A = 1\n")
    tk.functions["file_write"].entrypoint(path="b.py", content="B = 2\n")
    tk.functions["terminal"].entrypoint("echo hi > c.txt")
    assert len(list(ws.history())) == before

    tk.end_turn()   # the post_hooks boundary
    entries = list(ws.history())
    assert len(entries) == before + 1
    assert entries[0].info == {"tool": "turn"}

    tk.end_turn()   # idle turn → no empty commit
    assert len(list(ws.history())) == before + 1

    # rollback rewinds the WHOLE turn
    ws.rollback(1)
    assert not ws.fs.exists("a.py") and not ws.fs.exists("c.txt")
    ws.close()


# -- schema regressions -----------------------------------------------------------


def test_agno_test_app_schema_parses():
    """Regression: local ToolResult import + string annotations broke
    agno's signature parsing → degraded schema → model chaos. The
    schema must actually parse."""
    from nontainer.apps import enable_apps

    ws = make_ws()
    tk = WorkspaceTools(ws, apps=enable_apps(ws))
    fn = tk.functions["test_app"]
    fn.process_entrypoint()  # what agno does at agent-prep time
    params = fn.parameters or {}
    props = params.get("properties", {})
    assert "actions" in props, f"schema failed to parse: {params}"
    ws.close()


def test_agno_test_app_accepts_stringified_actions():
    """Models routinely send nested lists as JSON STRINGS; the pydantic
    validation agno wraps entrypoints in must not reject them before
    coerce_actions gets its chance. (Invalid JSON exercises the path
    without launching a browser: coercion fails first, as a ToolResult,
    not a validation explosion.)"""
    from pydantic import validate_call

    from nontainer.apps import enable_apps

    ws = make_ws()
    tk = WorkspaceTools(ws, apps=enable_apps(ws))
    entry = validate_call(tk.functions["test_app"].entrypoint)  # agno's wrapper
    result = entry(actions='[{"screenshot": true')  # torn JSON string
    assert "test_app failed" in result.content
    ws.close()


def test_agno_view_image():
    png = bytes.fromhex(  # 1x1 red PNG
        "89504e470d0a1a0a0000000d494844520000000100000001080200000090"
        "7753de0000000c49444154089963f8cfc000000301010018dd8db0000000"
        "0049454e44ae426082"
    )

    ws = make_ws()
    ws.fs.write("/plot.png", png)
    tk = WorkspaceTools(ws)
    result = tk.functions["view_image"].entrypoint(path="/plot.png")
    assert result.images and result.images[0].format == "png"
    assert "/plot.png" in result.content
    # agno re-delivers tool images as a synthetic USER message, which
    # humble models read as the human sharing a picture — the result
    # text must claim provenance before that message lands
    assert "this tool call's result" in result.content
    assert "the human did not send it" in result.content

    miss = tk.functions["view_image"].entrypoint(path="/nope.png")
    assert not miss.images and "cannot read" in miss.content
    assert "next message" not in miss.content  # no image, no note
    ws.close()
