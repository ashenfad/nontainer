"""A tool call refused before it runs: ``refuse`` from an agno tool hook.

The refusals go through real agno function calls under a scripted
model. agno flags the call failed because the hook raised, the model
reads text that says the call did not run, the stored run keeps the
flag, and the toolkit's log tells the refusal from a failure. CI runs
this file on every agno its matrix pins (``-k agno``)."""

import asyncio
import copy
import inspect
import logging
import pickle

import pytest

pytest.importorskip("agno")

from nontainer import conversation  # noqa: E402
from nontainer.adapters._agno_calls import CallLog, flag_failures, log_of  # noqa: E402
from nontainer.adapters.agno import (  # noqa: E402
    ToolRefused,
    _RefusalLog,
    refuse,
    tool_failed,
    tool_outcome,
    tool_refusal,
)

BUSY = (
    "not run: the call was refused before it started (every slot is in use); "
    "nothing changed, so make the same call again after 30 seconds"
)


def _stream(agent, method="run"):
    """Streaming kwargs for this agno: 2.x calls the event flag
    ``stream_intermediate_steps``, 3 calls it ``stream_events``."""
    params = inspect.signature(getattr(agent, method)).parameters
    flag = "stream_events" if "stream_events" in params else "stream_intermediate_steps"
    return {"stream": True, flag: True}


def _read(ws, tool):
    """What a harness reads from a ``ToolCallCompleted`` as it arrives."""
    return {
        "tool": tool,
        "outcome": tool_outcome(ws, tool),
        "refusal": tool_refusal(ws, tool),
        "failed": tool_failed(ws, tool),
    }


def _turn(agent, ws, script, message="go"):
    agent.model.script = list(script)
    return [
        _read(ws, ev.tool)
        for ev in agent.run(message, **_stream(agent))
        if getattr(ev, "event", "") == "ToolCallCompleted"
    ]


async def _aturn(agent, ws, script, message="go"):
    agent.model.script = list(script)
    return [
        _read(ws, ev.tool)
        async for ev in agent.arun(message, **_stream(agent, "arun"))
        if getattr(ev, "event", "") == "ToolCallCompleted"
    ]


def _gate(ws, refused=("terminal",)):
    """An embedder's capacity check: the named tools are refused."""

    def gate(function_name, function, arguments):
        if function_name in refused:
            refuse(ws, "busy", "every slot is in use", retry_after=30)
        return function(**arguments)

    return gate


def _stored(ws):
    """The newest stored run, as the session db wrote it."""
    from test_agno_db import index_of, kv_of

    index = index_of(ws)
    return conversation.read_runs(kv_of(ws), list(index.runs), index)[index.runs[-1]]


def _tool_messages(run):
    return [m for m in run["messages"] if m["role"] == "tool"]


class TestThroughAgno:
    def test_a_refused_call_fails_in_agnos_record_and_reads_as_refused(self, tmp_path):
        from test_agno_db import build

        ws, db, tk, agent = build(tmp_path)
        agent.tool_hooks = [tk.deliver, _gate(ws)]
        calls = _turn(
            agent,
            ws,
            [
                ("terminal", {"command": "touch refused.txt"}),
                ("run_python", {"code": "1/0"}),
                ("file_write", {"path": "ok.txt", "content": "ok"}),
                "done",
            ],
        )

        assert [c["outcome"] for c in calls] == ["refused", "failed", "ok"]
        assert [c["failed"] for c in calls] == [True, True, False]
        refused = calls[0]
        # agno's own record of the call: flagged, with the refusal's text
        assert refused["tool"].tool_name == "terminal"
        assert refused["tool"].tool_call_error is True
        assert refused["tool"].result == BUSY
        assert (refused["refusal"].reason, refused["refusal"].retry_after) == (
            "busy",
            30.0,
        )
        assert [c["refusal"] for c in calls[1:]] == [None, None]
        # the call never ran
        assert not ws.files.fs.exists("refused.txt")
        assert ws.files.fs.read("ok.txt") == b"ok"

        # the model read the refusal as the call's result, and the stored
        # run keeps every flag, the refusal's under the call's own id
        run = _stored(ws)
        messages = _tool_messages(run)
        assert [(m["tool_name"], m["tool_call_error"]) for m in messages] == [
            ("terminal", True),
            ("run_python", True),
            ("file_write", False),
        ]
        assert messages[0]["content"] == BUSY
        assert messages[0]["tool_call_id"] == refused["tool"].tool_call_id
        assert [t["tool_call_error"] for t in run["tools"]] == [True, True, False]
        ws.close()

    def test_the_model_reads_the_refusal(self, tmp_path):
        from test_agno_db import build

        ws, db, tk, agent = build(tmp_path)
        agent.tool_hooks = [_gate(ws)]
        agent.model.script = [("terminal", {"command": "echo hi"}), "done"]
        agent.run("go")
        # the model's second request carries the call and its result
        tool = [m for m in agent.model.seen if m.role == "tool"]
        assert [(m.content, m.tool_call_error) for m in tool] == [(BUSY, True)]
        ws.close()

    @pytest.mark.parametrize("hook", ["sync", "async"])
    def test_refusing_under_arun(self, tmp_path, hook):
        """A sync hook over the toolkit's sync tools runs the call on a
        thread; an async hook runs it on the loop, its frames awaiting
        one another. The call is found either way."""
        from test_agno_db import build

        ws, db, tk, agent = build(tmp_path)

        async def agate(function_name, function, arguments):
            if function_name == "terminal":
                refuse(ws, "busy", "every slot is in use", retry_after=30)
            return await function(**arguments)

        agent.tool_hooks = [_gate(ws) if hook == "sync" else agate]
        calls = asyncio.run(
            _aturn(agent, ws, [("terminal", {"command": "touch x"}), "done"])
        )
        assert [(c["outcome"], c["tool"].result) for c in calls] == [("refused", BUSY)]
        assert not ws.files.fs.exists("x")
        ws.close()

    def test_another_tools_refusal_reads_as_failed(self, tmp_path):
        """The log holds only the toolkit's calls, so a refusal of an
        embedder's own tool is agno's failure and nothing more, and the
        log keeps nothing under it to match a later call to."""
        from test_agno_db import build

        ws, db, tk, agent = build(tmp_path)

        def search(query: str) -> str:
            """Search the web."""
            return f"results for {query}"

        agent.tools = [tk, search]
        agent.tool_hooks = [_gate(ws, refused=("search",))]
        calls = _turn(agent, ws, [("search", {"query": "x"}), "done"])
        assert [(c["outcome"], c["failed"], c["tool"].result) for c in calls] == [
            ("failed", True, BUSY)
        ]
        log = log_of(ws)
        assert log is not None
        assert log.last((calls[0]["tool"].tool_call_id, "search")) is None
        ws.close()

    def test_a_refusal_prints_no_traceback(self, tmp_path):
        from agno.utils import log as agno_log
        from test_agno_db import build

        class Held(logging.Handler):
            def __init__(self):
                super().__init__()
                self.records = []

            def emit(self, record):
                self.records.append(record)

        held = Held()
        agno_log.logger.addHandler(held)
        try:
            ws, db, tk, agent = build(tmp_path)
            agent.tool_hooks = [_gate(ws)]
            _turn(agent, ws, [("terminal", {"command": "echo hi"}), "done"])
        finally:
            agno_log.logger.removeHandler(held)
        assert [r for r in held.records if r.exc_info] == []
        assert [r for r in held.records if r.levelno >= logging.ERROR] == []
        # agno's one-line warning that the function did not run stays
        assert any("Could not run function" in r.getMessage() for r in held.records)
        ws.close()


class TestRefuse:
    def test_outside_an_agno_call_it_raises_and_logs_nothing(self):
        ws = object.__new__(type("W", (), {}))
        with pytest.raises(ToolRefused) as raised:
            refuse(ws, "busy", retry_after=2)
        assert str(raised.value) == (
            "not run: the call was refused before it started (busy); nothing "
            "changed, so make the same call again after 2 seconds"
        )
        assert log_of(ws) is None


class TestToolRefused:
    @pytest.mark.parametrize(
        "retry_after, tail",
        [
            (None, ""),
            (0, ", so make the same call again"),
            (0.2, ", so make the same call again after 1 second"),
            (30, ", so make the same call again after 30 seconds"),
        ],
    )
    def test_the_text_says_it_did_not_run_and_when_to_repeat_it(
        self, retry_after, tail
    ):
        refused = ToolRefused("busy", "the queue is full", retry_after)
        assert str(refused) == (
            "not run: the call was refused before it started (the queue is "
            "full); nothing changed" + tail
        )

    def test_the_reason_stands_in_for_an_empty_message(self):
        assert "(busy)" in str(ToolRefused("busy"))

    @pytest.mark.parametrize(
        "args", [("",), ("busy", "", -1), ("busy", "", float("inf"))]
    )
    def test_bad_fields_are_refused(self, args):
        with pytest.raises(ValueError):
            ToolRefused(*args)

    def test_a_copy_or_a_pickle_keeps_the_fields_and_the_text(self):
        refused = ToolRefused("busy", "the queue is full", 30)
        for again in (copy.copy(refused), pickle.loads(pickle.dumps(refused))):
            assert (again.reason, again.message, again.retry_after) == (
                "busy",
                "the queue is full",
                30.0,
            )
            assert str(again) == str(refused)


class TestMatching:
    """A refused call takes its place among its tool's logged calls, so
    a model that reuses a call id still has each call matched to its
    own outcome. agno flagged the refused call itself."""

    def _tool(self, mid, error=False):
        return {
            "role": "tool",
            "id": mid,
            "tool_call_id": "call_0",
            "tool_name": "terminal",
            "content": "x",
            "tool_call_error": error,
        }

    def _flags(self, run):
        return [m["tool_call_error"] for m in run["messages"]]

    def _log(self):
        log = CallLog()
        log.track(["terminal"])
        return log

    def test_a_failure_before_a_refusal_under_the_same_id_is_flagged(self):
        log = self._log()
        log.note("call_0", "terminal", True)
        assert log.refuse("call_0", "terminal", ToolRefused("busy"))
        run = {"messages": [self._tool("m1"), self._tool("m2", error=True)]}
        assert self._flags(flag_failures(run, None, log)) == [True, True]

    def test_a_success_after_a_refusal_under_the_same_id_is_not(self):
        log = self._log()
        assert log.refuse("call_0", "terminal", ToolRefused("busy"))
        log.note("call_0", "terminal", False)
        run = {"messages": [self._tool("m1", error=True), self._tool("m2")]}
        assert self._flags(flag_failures(run, None, log)) == [True, False]

    def test_an_untracked_tools_refusal_is_not_logged(self):
        log = self._log()
        assert not log.refuse("call_0", "search", ToolRefused("busy"))
        assert log.last(("call_0", "search")) is None


class TestTheLogFilter:
    def _record(self, msg, exc=None, level=logging.ERROR):
        exc_info = (type(exc), exc, None) if exc is not None else None
        return logging.LogRecord(
            "agno", level, "/x/function.py", 1, msg, None, exc_info
        )

    def test_the_refusals_own_record_is_dropped(self):
        refused = ToolRefused("busy")
        assert not _RefusalLog().filter(self._record(refused, refused))

    def test_the_warning_keeps_its_line_and_loses_the_traceback(self):
        refused = ToolRefused("busy")
        record = self._record(
            "Could not run function terminal(command=ls)", refused, logging.WARNING
        )
        assert _RefusalLog().filter(record)
        assert record.exc_info is None

    def test_any_other_exception_is_kept_whole(self):
        error = ValueError("boom")
        record = self._record(error, error)
        assert _RefusalLog().filter(record)
        assert record.exc_info is not None
