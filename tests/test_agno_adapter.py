"""agno Toolkit adapter: exposure modes, locking, commit modes, schemas."""

import threading

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

    Four imports, plus one argument agno hands a pre hook:
    ``run_context``, whose ``run_id`` stays the same across a run's
    retry attempts. That argument is what sets the floor at 3.0.0 —
    agno 2.1 hands a pre hook no run context, and the 2.x releases that
    grew one did so unevenly — and the retry tests below exercise it
    through agno's own run loop rather than naming it here. No upper
    bound: none of this has moved across the 3.x releases.

    The FLOOR is the part worth pinning down. `agno>=1.0` stood for two
    months while being false — ToolResult does not exist in agno 1.x, so
    `pip install 'nontainer[agno]'` resolving to 1.0.0 failed at import.
    Nothing noticed because CI installs unpinned and therefore only ever
    tests the newest release; the agno-versions job covers the other
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

    `WorkspaceTools` can build on a release whose ``Agent`` rejects the
    hooks the README and examples pass it, and then the declared floor
    ships an example that cannot run. So the Agent is constructed here
    the way the README and examples tell people to.

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
    # a retrying agent, as docs/api.md wires it
    Agent(
        model=None,
        tools=[toolkit],
        retries=1,
        pre_hooks=[toolkit.begin_turn],
        post_hooks=[toolkit.end_turn],
    )


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

    from nontainer.inbox import split

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
    _, notes = split(result.content)
    assert "stop after this one" in notes
    ws.close()


def test_a_tool_result_whose_content_is_not_text_still_takes_the_notes():
    """``ToolResult.content`` is typed as text, but a result built
    past validation can carry ``None``; the note must ride out on it
    rather than turn a successful call into an exception."""
    from agno.tools.function import ToolResult

    from nontainer.inbox import split

    ws = make_ws()
    tk = WorkspaceTools(ws)
    tk.inbox.put("noted")

    def odd() -> ToolResult:
        return ToolResult.model_construct(content=None)

    out = call_through_agno(odd, {}, tk.deliver)
    assert isinstance(out.result, ToolResult)
    bare, notes = split(out.result.content)
    assert bare == ""
    assert "noted" in notes
    ws.close()


def test_a_delivery_that_cannot_attach_keeps_the_result_and_the_notes():
    """The notes are drained before the text is built, so a frame
    that raises must not spend them — or replace a tool result that
    succeeded with an exception."""
    from nontainer.inbox import Inbox, split

    def broken(note):
        raise RuntimeError("no frame today")

    ws = make_ws()
    tk = WorkspaceTools(ws, inbox=Inbox(frame=broken))
    tk.inbox.put("first")
    tk.inbox.put("second")

    out = call_through_agno(
        tk.functions["terminal"].entrypoint, {"command": "echo hi"}, tk.deliver
    )
    assert out.status == "success"
    assert split(out.result) == (out.result, "")
    assert [n.text for n in tk.inbox.pending()] == ["first", "second"]
    assert tk.inbox.delivered() == []
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


async def test_the_tool_entrypoints_pick_the_hook_not_run_versus_arun():
    """Which spelling to bind is decided by the TOOL entrypoints.

    agno's seam is ``Model.arun_function_call`` (agno/models/base.py):
    a sync entrypoint is handed to ``asyncio.to_thread`` unless one of
    the tool hooks is a coroutine function, in which case the whole
    call runs inline on the event loop. So the async hook over sync
    tools — everything ``WorkspaceTools`` registers — holds the loop
    for the length of every tool call, which is why the sync hook is
    right under ``arun()`` too. ``FunctionCall.aexecute()`` alone
    cannot show this: it is the model, not the function call, that
    chooses the thread.
    """
    from agno.models.base import Model
    from agno.tools.function import Function, FunctionCall

    ws = make_ws()
    tk = WorkspaceTools(ws)
    ran_on: list[int] = []

    def note_thread() -> str:
        ran_on.append(threading.get_ident())
        return "ok"

    async def dispatch(hook):
        fn = Function(name="note_thread", entrypoint=note_thread, tool_hooks=[hook])
        call = FunctionCall(function=fn, arguments={})
        # arun_function_call reads nothing off the model it is defined
        # on, so calling it unbound keeps a provider out of a test
        # about dispatch.
        success, _timer, _call, _result = await Model.arun_function_call(None, call)
        assert success is True
        return ran_on[-1]

    on_the_loop = threading.get_ident()
    assert await dispatch(tk.adeliver) == on_the_loop
    assert await dispatch(tk.deliver) != on_the_loop
    ws.close()


@pytest.mark.asyncio
async def test_mixed_tools_bind_a_hook_per_function_and_share_the_inbox():
    """An embedder with async tools of its own beside the toolkit binds
    a hook on each function and none on the agent: agno assigns an
    agent-level ``tool_hooks`` over every function's own (agent/_tools.py
    sets ``_func.tool_hooks = agent.tool_hooks``), so one agent-wide
    spelling would put the async hook on the sync workspace tools and
    hold the event loop for each of their calls. Per function, the sync
    tool keeps its worker thread and the async tool runs inline, and
    both deliver from the one inbox."""
    from agno.models.base import Model
    from agno.tools.function import Function, FunctionCall

    from nontainer.inbox import split

    ws = make_ws()
    tk = WorkspaceTools(ws)
    ran_on: list[int] = []

    # the toolkit's own (sync) tool, hooked on the function itself
    terminal = tk.functions["terminal"]
    terminal.tool_hooks = [tk.deliver]

    async def fetch(url: str) -> str:
        ran_on.append(threading.get_ident())
        return f"fetched {url}"

    # the embedder's async tool, hooked on the function itself
    fetching = Function(name="fetch", entrypoint=fetch, tool_hooks=[tk.adeliver])

    tk.inbox.put("first")
    tk.inbox.put("second")

    on_the_loop = threading.get_ident()
    ok, _t, _c, sync_result = await Model.arun_function_call(
        None, FunctionCall(function=terminal, arguments={"command": "echo hi"})
    )
    assert ok is True
    _, notes = split(sync_result.result)
    assert "first" in notes and "second" in notes

    tk.inbox.put("third")
    ok, _t, _c, async_result = await Model.arun_function_call(
        None, FunctionCall(function=fetching, arguments={"url": "u"})
    )
    assert ok is True
    assert ran_on == [on_the_loop]
    bare, notes = split(async_result.result)
    assert bare == "fetched u"
    assert "third" in notes and "first" not in notes
    assert tk.inbox.pending() == []
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


# -- a retried run: begin_turn rewinds the workspace ---------------------------


def flaky_model(script):
    """A scripted model driven through agno's own run loop.

    Steps are consumed in order: a string is the assistant's reply
    (ending the attempt), a ``(tool, args)`` pair is one tool call, and
    ``RAISE`` is a provider error — the failure agno's run-level retry
    exists for. ``seen`` holds the messages of every model call, one
    list per call, so a test can read what an attempt was shown.
    """
    import json

    from agno.exceptions import ModelProviderError
    from agno.models.base import Model
    from agno.models.response import ModelResponse

    class Flaky(Model):
        def __init__(self, steps):
            super().__init__(id="flaky", name="Flaky", provider="flaky")
            self.steps = list(steps)
            self.seen = []

        def _next(self, messages):
            self.seen.append(list(messages))
            step = self.steps.pop(0) if self.steps else "done"
            if step == "RAISE":
                raise ModelProviderError("upstream 503", status_code=503)
            response = ModelResponse(role="assistant")
            if isinstance(step, str):
                response.content = step
                return response
            name, args = step
            response.tool_calls = [
                {
                    "id": f"call_{len(self.steps)}",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)},
                }
            ]
            return response

        def invoke(self, messages, **kwargs):
            return self._next(messages)

        async def ainvoke(self, messages, **kwargs):
            return self._next(messages)

        def invoke_stream(self, messages, **kwargs):
            yield self._next(messages)

        async def ainvoke_stream(self, messages, **kwargs):
            yield self._next(messages)

        def _parse_provider_response(self, response, **kwargs):
            return response

        def _parse_provider_response_delta(self, response):
            return response

    return Flaky(script)


def retrying_agent(tk, script, **kwargs):
    from agno.agent import Agent

    kwargs.setdefault("pre_hooks", [tk.begin_turn])
    return Agent(
        model=flaky_model(script),
        tools=[tk],
        retries=1,
        delay_between_retries=0,
        telemetry=False,
        **kwargs,
    )


def seeded_ws(**kwargs) -> Workspace:
    ws = make_ws(**kwargs)
    ws.files.fs.write("a.txt", b"before")
    ws.commit(info={"tool": "seed"})
    return ws


FAILED_ATTEMPT = [
    ("file_write", {"path": "a.txt", "content": "attempt one"}),
    ("file_write", {"path": "b.txt", "content": "attempt one"}),
    "RAISE",
]


def test_a_retry_rewinds_what_the_failed_attempt_committed():
    """``commit="call"``: every write committed, so the head moved and
    the restore is a checkout — appended, so the abandoned work is
    still in the log."""
    ws = seeded_ws()
    anchor = ws.head
    tk = WorkspaceTools(ws)
    agent = retrying_agent(tk, FAILED_ATTEMPT + ["done"])

    out = agent.run("go")

    assert out.status == "COMPLETED", out.content
    assert ws.files.fs.read("a.txt") == b"before"
    assert not ws.files.fs.exists("b.txt")
    assert not ws.uncommitted
    diff = ws.diff(anchor, ws.head)
    assert not (diff.added or diff.removed or diff.modified)
    tools = [e.info.get("tool") for e in ws.log()]
    assert tools.count("file_write") == 2  # still there, stepped off
    ws.close()


def test_a_retry_discards_what_the_failed_attempt_left_uncommitted():
    """``commit="turn"``: the failed attempt committed nothing, so the
    head never moved — a "did the head move" check alone would keep
    its writes. They are dropped, and the head is the anchor itself."""
    ws = seeded_ws()
    anchor = ws.head
    tk = WorkspaceTools(ws, commit="turn")
    agent = retrying_agent(tk, FAILED_ATTEMPT + ["done"], post_hooks=[tk.end_turn])

    out = agent.run("go")

    assert out.status == "COMPLETED", out.content
    assert ws.files.fs.read("a.txt") == b"before"
    assert not ws.files.fs.exists("b.txt")
    assert not ws.uncommitted
    assert ws.head == anchor  # nothing to restore by commit, nothing new
    ws.close()


def test_the_retry_keeps_its_own_work():
    """Only the failed attempt is undone: what the retry writes lands."""
    ws = seeded_ws()
    tk = WorkspaceTools(ws, commit="turn")
    script = FAILED_ATTEMPT + [
        ("file_write", {"path": "a.txt", "content": "attempt two"}),
        "done",
    ]
    agent = retrying_agent(tk, script, post_hooks=[tk.end_turn])

    agent.run("go")

    assert ws.files.fs.read("a.txt") == b"attempt two"
    assert not ws.files.fs.exists("b.txt")
    assert ws.log()[0].info == {"tool": "turn"}
    ws.close()


def test_a_run_that_succeeds_first_time_writes_nothing_extra():
    ws = seeded_ws()
    before = len(ws.log())
    tk = WorkspaceTools(ws)
    script = [("file_write", {"path": "a.txt", "content": "after"}), "done"]
    agent = retrying_agent(tk, script)

    agent.run("go")
    agent.model.steps = ["done"]
    agent.run("again")  # a new run id: a new anchor, not a retry

    assert ws.files.fs.read("a.txt") == b"after"
    assert len(ws.log()) == before + 1  # the write, and no restore
    ws.close()


def test_a_run_that_began_with_uncommitted_writes_is_not_rewound(caplog):
    """No commit describes a dirty start, and the hook neither commits
    on the embedder's behalf nor destroys its writes: it says so, once,
    and leaves the workspace alone."""
    import logging

    ws = seeded_ws()
    tk = WorkspaceTools(ws, commit="turn")
    ws.files.fs.write("upload.txt", b"from the host")  # staged, not committed
    assert ws.uncommitted
    agent = retrying_agent(
        tk, FAILED_ATTEMPT + ["RAISE", "done"], post_hooks=[tk.end_turn]
    )
    agent.retries = 2

    with caplog.at_level(logging.WARNING, logger="nontainer.adapters.agno"):
        out = agent.run("go")

    assert out.status == "COMPLETED", out.content
    assert ws.files.fs.read("upload.txt") == b"from the host"
    assert ws.files.fs.read("a.txt") == b"attempt one"  # not rewound
    warnings = [r for r in caplog.records if "NOT rewound" in r.getMessage()]
    assert len(warnings) == 1  # two retries, one warning
    ws.close()


def test_an_unversioned_workspace_is_left_alone(tmp_path):
    from nontainer.providers import DirProvider

    ws = Workspace(DirProvider(tmp_path / "ws", session="s1"))
    ws.files.fs.write("a.txt", b"before")
    tk = WorkspaceTools(ws)
    agent = retrying_agent(tk, FAILED_ATTEMPT + ["done"])

    out = agent.run("go")

    assert out.status == "COMPLETED", out.content
    # nothing to return to, so the failed attempt's writes stand
    assert ws.files.fs.read("a.txt") == b"attempt one"
    assert ws.files.fs.read("b.txt") == b"attempt one"
    ws.close()


def test_a_frozen_workspace_is_left_alone():
    """A snapshot at a tag takes no writes, a restore included, so the
    hook never anchors on one."""
    ws = seeded_ws()
    ws.tags.add("v1")
    snap = ws.tags.at("v1")
    tk = WorkspaceTools(snap)
    agent = retrying_agent(tk, [("terminal", {"command": "ls"}), "RAISE", "done"])
    try:
        out = agent.run("go")
        assert out.status == "COMPLETED", out.content
        assert tk._attempt is None
        assert snap.files.fs.read("a.txt") == b"before"
    finally:
        snap.close()
        ws.close()


def test_a_retry_still_requeues_what_the_failed_attempt_delivered():
    """The note rode out on a tool result the retry threw away, so the
    retry delivers it again — and the model finally reads it."""
    ws = seeded_ws()
    tk = WorkspaceTools(ws)
    tk.inbox.put("say it twice if you must")
    script = [
        ("terminal", {"command": "echo one"}),
        "RAISE",
        ("terminal", {"command": "echo two"}),
        "done",
    ]
    agent = retrying_agent(
        tk, script, post_hooks=[tk.end_turn], tool_hooks=[tk.deliver]
    )

    out = agent.run("go")

    assert out.status == "COMPLETED", out.content
    last_call = agent.model.seen[-1]
    tool_texts = [str(m.content) for m in last_call if m.role == "tool"]
    assert len(tool_texts) == 1  # attempt one's tool call is gone
    assert "say it twice if you must" in tool_texts[0]
    assert tk.inbox.pending() == [] and tk.inbox.delivered() == []
    ws.close()


def test_a_retry_rewinds_an_outstanding_merge_with_the_tree(tmp_path):
    """The restore is whole-session, so a merge that was outstanding
    when the run began is outstanding again — markers and context both
    — even though the failed attempt resolved and committed it."""
    from nontainer import Store
    from nontainer.wsgit import register_wsgit

    store = Store(tmp_path / "store")
    ws = store.open("main")
    register_wsgit(ws)
    ws.files.write("/workspace/a.txt", "one\ntwo\nthree\n")
    ws.index.commit("first")
    ws.files.write("/workspace/a.txt", "one\nTWO\nthree\n")
    ws.index.commit("second")
    second = ws.index.head
    ws.files.write("/workspace/a.txt", "one\nLATER\nthree\n")
    ws.index.commit("third")
    ws.revert(second)  # conflicts: markers + an outstanding merge
    assert ws.index.status().merge_source is not None
    marked = ws.files.read("/workspace/a.txt")

    tk = WorkspaceTools(ws)
    script = [
        ("file_write", {"path": "a.txt", "content": "resolved\n"}),
        ("terminal", {"command": "ws-git commit -m resolved"}),
        "RAISE",
        "done",
    ]
    agent = retrying_agent(tk, script)
    try:
        out = agent.run("go")
        assert out.status == "COMPLETED", out.content
        assert ws.files.read("/workspace/a.txt") == marked
        assert ws.index.status().merge_source is not None
    finally:
        ws.close()
        store.close()


@pytest.mark.parametrize("commit", ["call", "turn"])
def test_a_retry_under_a_session_db_leaves_one_run_and_no_stray_files(tmp_path, commit):
    """With the conversation in the branch, files and memory move
    together: the stored history holds the one run that completed, and
    the files hold nothing of the attempt it replaced."""
    from nontainer.adapters.agno_db import RUN_PREFIX, KvgitSessionDb

    ws = seeded_ws()
    anchor = ws.head
    db = KvgitSessionDb(ws, db_path=str(tmp_path / "agno"))
    tk = WorkspaceTools(ws, commit=commit, session_db=db)
    agent = retrying_agent(tk, FAILED_ATTEMPT + ["done"], db=db, session_id=ws.session)

    out = agent.run("go")

    assert out.status == "COMPLETED", out.content
    assert not ws.files.fs.exists("b.txt")
    assert ws.files.fs.read("a.txt") == b"before"
    runs = [k for k in ws.provider.kv.keys() if k.startswith(RUN_PREFIX)]
    assert len(runs) == 1
    diff = ws.diff(anchor, ws.head)
    assert not (diff.added or diff.removed or diff.modified)
    ws.close()


async def test_abegin_turn_rewinds_under_arun_off_the_event_loop():
    """agno's async loop calls a sync pre hook inline on the event loop,
    so ``arun()`` takes the async spelling, which does the restore on a
    worker thread."""
    ws = seeded_ws()
    tk = WorkspaceTools(ws)
    ran_on: list[int] = []
    rewind = tk._rewind

    def spy(run_id):
        ran_on.append(threading.get_ident())
        return rewind(run_id)

    tk._rewind = spy
    agent = retrying_agent(tk, FAILED_ATTEMPT + ["done"], pre_hooks=[tk.abegin_turn])

    out = await agent.arun("go")

    assert out.status == "COMPLETED", out.content
    assert ws.files.fs.read("a.txt") == b"before"
    assert not ws.files.fs.exists("b.txt")
    assert len(ran_on) == 2 and threading.get_ident() not in ran_on
    ws.close()
