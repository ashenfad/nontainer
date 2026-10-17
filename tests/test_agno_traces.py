"""AgnoTraces: agno's stored runs, read into the neutral trace shape.

The synthetic bodies need no agno; the last tests drive a real agno
``Agent`` over a scripted model and read what ``KvgitSessionDb``
stored."""

import pytest

from nontainer import conversation
from nontainer.adapters.agno_traces import AgnoTraces
from nontainer.traces import Trace, TraceEvent, TraceTokens

read = AgnoTraces().read


def _call(call_id, name, arguments):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def _result(call_id, content, *, error=False, **extra):
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "content": content,
        "tool_call_error": error,
        **extra,
    }


def _body(*messages, **kw):
    return {"run_id": "r1", "status": "COMPLETED", "messages": list(messages), **kw}


class TestRun:
    @pytest.mark.parametrize(
        "agno, ours",
        [
            ("COMPLETED", "completed"),
            ("RUNNING", "running"),
            ("PENDING", "running"),
            ("PAUSED", "paused"),
            ("CANCELLED", "cancelled"),
            ("ERROR", "failed"),
            ("REGENERATED", "regenerated"),
            (None, "running"),
        ],
    )
    def test_statuses_in_cores_words(self, agno, ours):
        assert read(_body(status=agno)).status == ours

    def test_times_come_from_created_at_and_the_duration(self):
        run = read(_body(created_at=100, metrics={"duration": 12.5}))
        assert (run.started, run.ended) == (100.0, 112.5)
        assert read(_body(created_at=100)).ended is None

    def test_a_count_agno_dropped_for_zero_reads_zero(self):
        run = read(_body(metrics={"input_tokens": 9, "cache_read_tokens": 4}))
        assert run.tokens == TraceTokens(
            input=9, output=0, cache_read=4, cache_write=0, reasoning=0
        )

    def test_without_metrics_every_count_is_unknown(self):
        assert read(_body()).tokens == TraceTokens()

    def test_the_system_prompt_is_the_first_system_message(self):
        run = read(
            _body(
                {"role": "system", "content": "be terse"},
                {"role": "user", "content": "hi"},
            )
        )
        assert run.system == "be terse"
        assert run.events == (TraceEvent("user", "hi"),)

    @pytest.mark.parametrize("body", [[], {"status": "COMPLETED"}, {"run_id": ""}])
    def test_a_body_without_a_run_id_is_refused(self, body):
        with pytest.raises(ValueError):
            read(body)


class TestEvents:
    def test_an_assistant_message_is_its_reasoning_text_then_calls(self):
        run = read(
            _body(
                {"role": "user", "content": "go"},
                {
                    "role": "assistant",
                    "reasoning_content": "look first",
                    "content": "looking",
                    "tool_calls": [_call("c1", "terminal", '{"command": "ls"}')],
                },
                _result("c1", "a.txt"),
                {"role": "assistant", "content": "done"},
            )
        )
        assert run.events == (
            TraceEvent("user", "go"),
            TraceEvent("thinking", "look first"),
            TraceEvent("assistant", "looking"),
            TraceEvent("tool", "a.txt", tool="terminal", args={"command": "ls"}),
            TraceEvent("assistant", "done"),
        )

    def test_a_call_id_used_twice_is_answered_by_the_results_after_it(self):
        run = read(
            _body(
                {"role": "assistant", "tool_calls": [_call("0", "a", "{}")]},
                _result("0", "first"),
                {"role": "assistant", "tool_calls": [_call("0", "b", "{}")]},
                _result("0", "second", error=True),
            )
        )
        assert [(e.tool, e.text, e.error) for e in run.events] == [
            ("a", "first", False),
            ("b", "second", True),
        ]

    def test_a_call_with_no_result_is_in_flight(self):
        run = read(_body({"role": "assistant", "tool_calls": [_call("c1", "t", "{}")]}))
        assert run.events == (TraceEvent("tool", None, tool="t", args={}),)

    def test_a_result_no_call_claims_stands_alone(self):
        run = read(_body(_result("x", "orphan", tool_name="t", tool_args={"a": 1})))
        assert run.events == (TraceEvent("tool", "orphan", tool="t", args={"a": 1}),)

    def test_arguments_that_are_not_an_object_are_kept_as_given(self):
        run = read(
            _body(
                {"role": "assistant", "tool_calls": [_call("c1", "t", "not json")]},
                _result("c1", "ok"),
                {"role": "assistant", "tool_calls": [_call("c2", "t", "[1, 2]")]},
                _result("c2", "ok"),
            )
        )
        assert [e.args for e in run.events] == [
            {"arguments": "not json"},
            {"arguments": "[1, 2]"},
        ]

    def test_replayed_history_is_left_out(self):
        run = read(
            _body(
                {"role": "user", "content": "old", "from_history": True},
                {"role": "assistant", "content": "old reply", "from_history": True},
                {"role": "user", "content": "new"},
            )
        )
        assert run.events == (TraceEvent("user", "new"),)

    def test_content_in_parts_reads_as_its_text(self):
        run = read(
            _body(
                {
                    "role": "user",
                    "content": [{"type": "text", "text": "a"}, "b", {"type": "image"}],
                }
            )
        )
        assert run.events[0].text == "a\nb"


class TestRealRuns:
    """What KvgitSessionDb stores for a real agno run, read back."""

    def test_two_turns(self, tmp_path):
        pytest.importorskip("agno")
        from test_agno_db import build, index_of, kv_of, run_turn

        ws, db, tk, agent = build(tmp_path)
        agent.instructions = "Be terse."
        run_turn(
            agent,
            [
                ("file_write", {"path": "a.txt", "content": "hi\n"}),
                ("run_python", {"code": "1/0"}),
                "wrote it",
            ],
            message="Write a.txt",
        )
        run_turn(
            agent,
            [("terminal", {"command": "cat a.txt"}), "it says hi"],
            message="What's in it?",
        )

        index = index_of(ws)
        bodies = conversation.read_runs(kv_of(ws), list(index.runs), index)
        runs = [read(bodies[rid]) for rid in index.runs]

        first, second = runs
        assert first.status == second.status == "completed"
        assert first.model == "scripted"
        assert first.system is not None and first.system.startswith("Be terse.")
        assert [(e.kind, e.tool) for e in first.events] == [
            ("user", None),
            ("tool", "file_write"),
            ("tool", "run_python"),
            ("assistant", None),
        ]
        assert first.events[1].args == {"path": "a.txt", "content": "hi\n"}
        assert "ZeroDivisionError" in first.events[2].text
        # the second run holds only its own messages, not the first's replayed
        assert [(e.kind, e.text) for e in second.events] == [
            ("user", "What's in it?"),
            ("tool", "hi"),
            ("assistant", "it says hi"),
        ]
        trace = Trace(runs, session=ws.session, commit="abc1234", harness=index.harness)
        assert len([p for p in trace.paths() if p.startswith("system/")]) == 1
        assert "What's in it?" in trace.read("index.md")
