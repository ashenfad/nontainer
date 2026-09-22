"""agno Toolkit adapter: exposure modes, locking, commit modes, schemas."""

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
        list(
            pool.map(
                lambda i: fw(path=f"src/mod_{i}.py", content=f"X = {i}\n"),
                range(6),
            )
        )

    for i in range(6):
        assert ws.files.fs.read(f"src/mod_{i}.py").decode() == f"X = {i}\n"
    assert len(list(ws.log())) >= 7  # each write committed
    ws.close()


# -- commit granularity ------------------------------------------------------


def test_turn_commit_mode():
    """agex-style granularity: one commit per turn via end_turn."""
    ws = make_ws()
    tk = WorkspaceTools(ws, commit="turn")
    before = len(list(ws.log()))

    # a "turn": several mutations, zero commits until end_turn
    tk.functions["file_write"].entrypoint(path="a.py", content="A = 1\n")
    tk.functions["file_write"].entrypoint(path="b.py", content="B = 2\n")
    tk.functions["terminal"].entrypoint("echo hi > c.txt")
    assert len(list(ws.log())) == before

    tk.end_turn()  # the post_hooks boundary
    entries = list(ws.log())
    assert len(entries) == before + 1
    assert entries[0].info == {"tool": "turn"}

    tk.end_turn()  # idle turn → no empty commit
    assert len(list(ws.log())) == before + 1

    # checking out the commit before it rewinds the WHOLE turn
    ws.checkout(list(ws.log())[1].id)
    assert not ws.files.fs.exists("a.py") and not ws.files.fs.exists("c.txt")
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
    ws.files.fs.write("/plot.png", png)
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


def test_the_agno_surface_this_adapter_depends_on():
    """The whole of nontainer's agno contract, named in one place.

    It is four imports, which is why `agno>=2` carries no upper bound:
    agno 3.0's breaking changes are all in surfaces this adapter never
    touches (the runs table, renamed Agent params, Workflow/HITL,
    MultiMCPTools). Verified on 2.0.0, 2.4.0, 2.7.2 and 3.0.1.

    The FLOOR is the part worth pinning down. `agno>=1.0` stood for two
    months while being false — ToolResult does not exist in agno 1.x, so
    `pip install 'nontainer[agno]'` resolving to 1.0.0 failed at import.
    Nothing noticed because CI installs unpinned and therefore only ever
    tested the newest release; the agno-floor job now covers the other
    end.
    """
    from agno.media import Image
    from agno.tools import Toolkit
    from agno.tools.function import ToolResult

    assert issubclass(WorkspaceTools, Toolkit)
    # Constructed by the adapter, so a move or rename must fail loudly
    # here rather than inside a tool call at runtime.
    assert ToolResult(content="x").content == "x"
    assert Image(filepath="x.png").filepath == "x.png"


def test_the_documented_integration_shapes_construct():
    """The adapter clearing a floor is not the same as the SHIPPED
    INTEGRATIONS clearing it.

    `agno>=2` looked right — `WorkspaceTools` builds fine on 2.0.0 and
    every adapter test passes there. But examples/analyst.py passes
    `post_hooks` and studio passes `pre_hooks`, and both landed in
    2.1.0, so the declared floor shipped an example that could not run
    on it. Nothing caught that because nothing constructed an Agent the
    way the README and examples tell people to.

    `model=None` keeps this a construction check: no key, no network,
    no model call — a renamed or dropped kwarg is a TypeError right
    here.
    """
    from agno.agent import Agent

    ws = make_ws()
    toolkit = WorkspaceTools(ws)

    # README / quick-start
    Agent(model=None, tools=[toolkit])
    # examples/analyst.py
    Agent(
        model=None,
        tools=[toolkit],
        tool_call_limit=12,
        markdown=False,
        post_hooks=[toolkit.end_turn],
    )
    # examples/webapp.py
    Agent(model=None, tools=[toolkit], tool_call_limit=30)
    # nontainer-studio's shape, so a floor bump there is caught here too
    Agent(model=None, tools=[toolkit], pre_hooks=[lambda **kw: None])


# -- the sessions tool ---------------------------------------------------------


def test_agno_no_sessions_tool_without_a_runner():
    """No runner, no tool: an agent that cannot delegate is never told
    about delegation."""
    ws = make_ws()
    tk = WorkspaceTools(ws)
    assert "sessions" not in tk.functions
    assert tk.sessions is None
    ws.close()


def test_agno_sessions_tool_round_trips_a_delegate(tmp_path):
    from nontainer import Store

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

    tk = WorkspaceTools(ws, sessions=Scripted())
    try:
        assert "sessions" in tk.functions
        call = tk.functions["sessions"].entrypoint

        out = call(action="ask", task="polish the report", wait=True)
        assert "polished the report" in out
        assert "/workspace/report.md" in out
        # the next step is spelled for the terminal, not for host python
        assert "ws-git merge analyst." in out
        assert "ws-git checkout analyst." in out
        assert "ws-git diff analyst." in out

        listed = call(action="list")
        assert "answered" in listed and "polish the report" in listed

        name = [j.name for j in tk.sessions.list()][0]
        assert call(action="result", name=name).startswith("polished the report")
        assert "kept" in call(action="keep", name=name)
        assert "unknown action" in call(action="nope")
        assert "needs a task" in call(action="ask")
        assert "no job named" in call(action="result", name="nobody")
    finally:
        tk.sessions.close()
        ws.close()
        store.close()


def test_agno_sessions_tool_takes_a_prebuilt_helper(tmp_path):
    from nontainer import Store
    from nontainer.sessions import Sessions

    store = Store(tmp_path / "store")
    ws = store.open("analyst")

    class Scripted:
        def run(self, session, task, *, budget=None):
            return "did it"

    helper = Sessions(ws, Scripted())
    tk = WorkspaceTools(ws, sessions=helper)
    try:
        assert tk.sessions is helper
        assert "delegated to analyst." in tk.functions["sessions"].entrypoint(
            action="ask", task="go"
        )
    finally:
        helper.close()
        ws.close()
        store.close()


def test_agno_sessions_tool_asks_from_a_fork_point_and_resumes(tmp_path):
    """The delegate can start from state that is not this session's,
    and a delegate you already have can be given another task."""
    from nontainer import Store

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

    tk = WorkspaceTools(ws, sessions=Scripted())
    try:
        call = tk.functions["sessions"].entrypoint
        assert "fork_from=" in tk.functions["sessions"].entrypoint.__doc__
        assert "resume=" in tk.functions["sessions"].entrypoint.__doc__

        out = call(
            action="ask", task="what is north?", fork_from="rates-2026", wait=True
        )
        child = tk.sessions.list()[0].name
        assert "answering: what is north?" in out
        # the next step is the same for every ask
        assert f"ws-git merge {child}" in out
        assert f"ws-git checkout {child} -- <paths>" in out

        listed = call(action="list")
        assert "rates-2026" in listed and child in listed

        again = call(action="ask", task="and south?", resume=child, wait=True)
        assert "answering: and south?" in again
        assert [j.name for j in tk.sessions.list()] == [child]  # one child, one row

        assert "nothing named" in call(action="ask", task="q", fork_from="nope")
        assert "no job named" in call(action="ask", task="q", resume="analyst.nope")

        # the fork point is read, never written
        sage = store.open("sage")
        try:
            assert sage.head == head
        finally:
            sage.close()
    finally:
        tk.sessions.close()
        ws.close()
        store.close()


# -- the inbox: notes delivered with the next tool result ----------------------


def call_through_agno(function, arguments, hook):
    """Run a tool the way agno runs one: a Function whose tool_hooks
    hold the delivery hook, executed through FunctionCall.

    Driving agno's own chain rather than calling the hook directly is
    the point of these tests — the hook's argument names are bound by
    agno's signature inspection, and a rename there is exactly the
    breakage worth catching.
    """
    from agno.tools.function import Function, FunctionCall

    fn = Function(name=function.__name__, entrypoint=function, tool_hooks=[hook])
    return FunctionCall(function=fn, arguments=arguments).execute()


def test_inbox_note_rides_out_with_the_next_tool_result():
    from nontainer.inbox import split

    ws = make_ws()
    tk = WorkspaceTools(ws)
    tk.inbox.put("switch the chart to a log scale")

    out = call_through_agno(
        tk.functions["terminal"].entrypoint, {"command": "echo hi"}, tk.deliver
    )
    assert out.status == "success"
    bare, notes = split(out.result)
    assert bare.strip().endswith("hi")
    assert "switch the chart to a log scale" in notes
    assert "NOT part of the tool's output" in notes
    # spent: delivered once, and not pending for the next call
    assert tk.inbox.pending() == []
    assert len(tk.inbox.delivered()) == 1
    ws.close()


def test_an_empty_inbox_leaves_the_result_exactly_as_it_was():
    ws = make_ws()
    tk = WorkspaceTools(ws)
    out = call_through_agno(
        tk.functions["terminal"].entrypoint, {"command": "echo hi"}, tk.deliver
    )
    assert "inbox" not in out.result
    assert out.result == tk.functions["terminal"].entrypoint("echo hi")
    ws.close()


def test_delivery_keeps_a_tool_results_images():
    """A screenshot's images are what the model called the tool for;
    appending a sentence must not cost them."""
    from agno.tools.function import ToolResult

    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d494844520000000100000001080200000090"
        "7753de0000000c49444154089963f8cfc000000301010018dd8db0000000"
        "0049454e44ae426082"
    )
    ws = make_ws()
    ws.files.fs.write("/plot.png", png)
    tk = WorkspaceTools(ws)
    tk.inbox.put("stop after this one")

    out = call_through_agno(
        tk.functions["view_image"].entrypoint, {"path": "/plot.png"}, tk.deliver
    )
    result = out.result
    assert isinstance(result, ToolResult)
    assert result.images and result.images[0].format == "png"
    assert result.content.endswith("stop after this one")
    ws.close()


def test_a_result_that_is_neither_str_nor_tool_result_is_stringified():
    ws = make_ws()
    tk = WorkspaceTools(ws)
    tk.inbox.put("noted")

    def counter() -> int:
        return 42

    out = call_through_agno(counter, {}, tk.deliver)
    assert out.result.startswith("42")
    assert "noted" in out.result
    ws.close()


def test_a_raising_tool_leaves_the_notes_pending():
    """Notes wait for a result that lands rather than being spent on
    a message the model may never read."""
    ws = make_ws()
    tk = WorkspaceTools(ws)
    tk.inbox.put("still waiting")

    def boom() -> str:
        raise RuntimeError("nope")

    out = call_through_agno(boom, {}, tk.deliver)
    assert out.status == "failure"
    assert [n.text for n in tk.inbox.pending()] == ["still waiting"]
    assert tk.inbox.delivered() == []
    ws.close()


def test_on_delivered_is_called_with_the_notes():
    seen = []
    ws = make_ws()
    tk = WorkspaceTools(ws)
    tk.inbox.on_delivered = seen.append
    tk.inbox.put("recorded")

    call_through_agno(
        tk.functions["terminal"].entrypoint, {"command": "echo hi"}, tk.deliver
    )
    assert [n.text for n in seen[0]] == ["recorded"]
    ws.close()


def test_an_on_delivered_that_raises_does_not_cost_the_tool_result():
    from nontainer.inbox import Inbox

    def angry(notes):
        raise RuntimeError("bookkeeping is down")

    ws = make_ws()
    tk = WorkspaceTools(ws, inbox=Inbox(on_delivered=angry))
    tk.inbox.put("delivered anyway")
    out = call_through_agno(
        tk.functions["terminal"].entrypoint, {"command": "echo hi"}, tk.deliver
    )
    assert "delivered anyway" in out.result
    ws.close()


def test_a_sync_hook_discards_an_awaitable_callback():
    async def later(notes):
        return None

    ws = make_ws()
    tk = WorkspaceTools(ws)
    tk.inbox.on_delivered = later
    tk.inbox.put("still lands")
    out = call_through_agno(
        tk.functions["terminal"].entrypoint, {"command": "echo hi"}, tk.deliver
    )
    assert "still lands" in out.result
    ws.close()


async def test_the_async_hook_delivers_and_awaits_the_callback():
    """agno's async chain hands a hook a coroutine next_func, which is
    why there are two hook spellings."""
    from agno.tools.function import Function, FunctionCall

    seen = []

    async def record(notes):
        seen.extend(notes)

    ws = make_ws()
    tk = WorkspaceTools(ws)
    tk.inbox.on_delivered = record
    tk.inbox.put("mid-run word")

    async def slow_echo(text: str) -> str:
        return text.upper()

    fn = Function(name="slow_echo", entrypoint=slow_echo, tool_hooks=[tk.adeliver])
    out = await FunctionCall(function=fn, arguments={"text": "hi"}).aexecute()
    assert out.result.startswith("HI")
    assert "mid-run word" in out.result
    assert [n.text for n in seen] == ["mid-run word"]
    ws.close()


def test_begin_turn_requeues_what_a_dropped_attempt_delivered():
    ws = make_ws()
    tk = WorkspaceTools(ws)
    tk.inbox.put("say it once")
    call_through_agno(
        tk.functions["terminal"].entrypoint, {"command": "echo hi"}, tk.deliver
    )

    # a provider-error retry: the attempt's tool calls are gone, so the
    # model never saw the note — the next attempt delivers it again
    tk.begin_turn()
    assert [n.text for n in tk.inbox.pending()] == ["say it once"]
    out = call_through_agno(
        tk.functions["terminal"].entrypoint, {"command": "echo hi"}, tk.deliver
    )
    assert "say it once" in out.result

    # the turn completes: settled, and a later begin_turn finds nothing
    tk.end_turn()
    assert tk.inbox.delivered() == []
    tk.begin_turn()
    assert tk.inbox.pending() == []
    ws.close()


def test_end_turn_settles_even_when_a_session_db_owns_the_commit():
    class FakeDb:
        def owns(self, workspace):
            return True

    ws = make_ws()
    tk = WorkspaceTools(ws, session_db=FakeDb())
    tk.inbox.put("read and done")
    tk.inbox.drain()
    assert tk.end_turn() is None  # the db commits, not the toolkit
    assert tk.inbox.delivered() == []
    ws.close()


def test_a_delegate_answer_arrives_with_the_next_tool_result(tmp_path):
    """The answer is framed as the mechanism, not as the person the
    agent works for."""
    import time

    from nontainer import Store
    from nontainer.inbox import split

    store = Store(tmp_path / "store")
    ws = store.open("analyst")

    class Scripted:
        def run(self, session, task, *, budget=None):
            return "north is 4"

    tk = WorkspaceTools(ws, sessions=Scripted())
    try:
        job = tk.sessions.ask("what is north?")
        deadline = time.time() + 10
        while tk.sessions.list()[0].status == "running" and time.time() < deadline:
            time.sleep(0.01)

        out = call_through_agno(
            tk.functions["terminal"].entrypoint, {"command": "echo hi"}, tk.deliver
        )
        _, notes = split(out.result)
        assert "north is 4" in notes
        assert "the delegation mechanism speaking" in notes
        assert f"delegate {job.name}" in notes

        # taken once: the next tool call carries nothing
        again = call_through_agno(
            tk.functions["terminal"].entrypoint, {"command": "echo hi"}, tk.deliver
        )
        assert "inbox" not in again.result
        # and it is under requeue like any other delivered note
        assert tk.begin_turn() is None
        assert [n.job for n in tk.inbox.pending()] == [job.name]
    finally:
        tk.sessions.close()
        ws.close()
        store.close()
