"""nontainer's failed tool calls, flagged in agno's stored runs.

nontainer's tools say a call failed in their text and return normally,
so agno stored every one as a success (``tool_call_error: false``). The
toolkit now logs each call's outcome by call id and tool name, and ``KvgitSessionDb``
flags the failed ones in the run it stores (``_agno_calls``). The
matching is checked on bare dicts first, then through a real agno
``Agent``."""

import pytest

from nontainer import conversation
from nontainer.adapters._agno_calls import CallLog, flag_failures, log_of, note
from nontainer.adapters.agno_traces import AgnoTraces


def _log(*calls):
    """Calls as (call id, failed), all of the toolkit's terminal, or as
    (call id, tool, failed)."""
    log = CallLog()
    for call in calls:
        call_id, tool, failed = (
            call if len(call) == 3 else (call[0], "terminal", call[1])
        )
        log.note(call_id, tool, failed)
    return log


def _tool(call_id, *, mid=None, error=False, tool="terminal", history=False):
    message = {
        "role": "tool",
        "id": mid or f"m-{call_id}",
        "tool_call_id": call_id,
        "tool_name": tool,
        "content": "x",
        "tool_call_error": error,
    }
    if history:
        message["from_history"] = True
    return message


def _execution(call_id, tool="terminal"):
    return {"tool_call_id": call_id, "tool_name": tool, "tool_call_error": False}


def _run(*messages, tools=None):
    run = {"run_id": "r", "messages": list(messages)}
    if tools is not None:
        run["tools"] = tools
    return run


def _flags(run):
    return [m.get("tool_call_error") for m in run["messages"] if m["role"] == "tool"]


class TestMatching:
    def test_a_failed_call_is_flagged_on_its_message_and_tools_entry(self):
        run = _run(
            _tool("a"),
            _tool("b"),
            tools=[_execution("a"), _execution("b")],
        )
        out = flag_failures(run, None, _log(("a", True), ("b", False)))
        assert _flags(out) == [True, False]
        assert [t["tool_call_error"] for t in out["tools"]] == [True, False]
        assert _flags(run) == [False, False]  # the input is not changed

    def test_an_id_reused_across_replies_matches_the_calls_in_order(self):
        run = _run(_tool("call_0", mid="m1"), _tool("call_0", mid="m2"))
        out = flag_failures(run, None, _log(("call_0", False), ("call_0", True)))
        assert _flags(out) == [False, True]

    def test_an_earlier_run_under_the_same_id_is_not_this_ones(self):
        # the log's first entry is the previous run's call_0
        run = _run(_tool("call_0"))
        assert _flags(
            flag_failures(run, None, _log(("call_0", True), ("call_0", False)))
        ) == [False]

    def test_another_toolkits_call_under_the_same_id_takes_nothing(self):
        # call_0 twice: the workspace's failed terminal, then another
        # toolkit's search, which the log never saw
        run = _run(_tool("call_0", mid="m1"), _tool("call_0", mid="m2", tool="search"))
        assert _flags(flag_failures(run, None, _log(("call_0", True)))) == [True, False]

    def test_history_in_the_messages_takes_nothing_from_the_log(self):
        # store_history_messages: an earlier run's call_0 is replayed
        # here, and only this run's call is in tools
        run = _run(
            _tool("call_0", mid="old", history=True),
            _tool("call_0", mid="new"),
            tools=[_execution("call_0")],
        )
        out = flag_failures(run, None, _log(("call_0", True)))
        assert _flags(out) == [False, True]
        assert out["tools"][0]["tool_call_error"] is True

    def test_more_outcomes_than_entries_align_from_the_end(self):
        run = _run(
            _tool("call_0", mid="m1"),
            _tool("call_0", mid="m2"),
            tools=[_execution("call_0")],
        )
        out = flag_failures(run, None, _log(("call_0", False), ("call_0", True)))
        assert [t["tool_call_error"] for t in out["tools"]] == [True]

    def test_a_finished_run_closes_the_log(self):
        log = _log(("call_0", True))
        log.close()
        assert (
            flag_failures(_run(_tool("call_0")), None, log)["messages"][0][
                "tool_call_error"
            ]
            is False
        )

    def test_a_stored_flag_is_kept_by_message_id(self):
        held = _run(_tool("a", mid="m1", error=True))
        out = flag_failures(_run(_tool("a", mid="m1")), held, None)
        assert _flags(out) == [True]

    def test_a_flag_agno_set_is_never_cleared(self):
        run = _run(_tool("a", error=True))
        assert flag_failures(run, None, _log(("a", False))) is run

    def test_nothing_to_flag_returns_the_run_itself(self):
        run = _run(_tool("a"))
        assert flag_failures(run, None, _log(("a", False))) is run
        assert flag_failures(run, None, None) is run

    def test_a_call_without_its_function_call_is_not_logged(self):
        ws = object.__new__(type("W", (), {}))
        note(ws, None, True)
        assert log_of(ws) is None


class TestThroughAgno:
    def test_failures_are_flagged_and_stay_flagged(self, tmp_path):
        pytest.importorskip("agno")
        from test_agno_db import build, index_of, kv_of, run_turn

        ws, db, tk, agent = build(tmp_path)
        agent.tool_hooks = [tk.deliver]  # the inbox hook passes the call through
        run_turn(
            agent,
            [
                ("run_python", {"code": "1/0"}),
                ("terminal", {"command": "exit 3"}),
                ("terminal", {"command": "echo ok"}),
                "done",
            ],
            message="one",
        )
        # the scripted model names this call call_terminal_1, as it named
        # the first run's echo: the first run must keep its success
        run_turn(agent, [("terminal", {"command": "exit 1"}), "done"], message="two")

        index = index_of(ws)
        bodies = conversation.read_runs(kv_of(ws), list(index.runs), index)
        first, second = (AgnoTraces().read(bodies[rid]) for rid in index.runs)
        assert [(e.tool, e.error) for e in first.events if e.kind == "tool"] == [
            ("run_python", True),
            ("terminal", True),
            ("terminal", False),
        ]
        assert [e.error for e in second.events if e.kind == "tool"] == [True]
        assert [t["tool_call_error"] for t in bodies[index.runs[0]]["tools"]] == [
            True,
            True,
            False,
        ]
        # agno hands the first run back unflagged on the second turn; it
        # is not stored again, so the second turn's commit names one run
        stamps = [e.info["runs"] for e in ws.log() if e.info.get("tool") == "turn"]
        assert list(stamps[0]) == [index.runs[1]]

    def test_the_call_is_not_in_any_schema(self, tmp_path):
        pytest.importorskip("agno")
        from test_agno_db import build

        _, _, tk, _ = build(tmp_path)
        functions = tk.get_functions() if hasattr(tk, "get_functions") else tk.functions
        for function in functions.values():
            function.process_entrypoint()
            assert "fc" not in function.parameters["properties"], function.name
