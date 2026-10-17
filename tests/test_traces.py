"""The neutral trace shape, the neutral source, and the renderer.

The rendered bytes are pinned by the trace corpus
(``nontainer/conformance/traces``, checked by ``test_corpus_drift``);
these check the shape's rules and the renderer's edges."""

import json

import pytest

from nontainer.conformance import export
from nontainer.conformance.traces import CASES
from nontainer.traces import (
    NeutralTraces,
    Trace,
    TraceEvent,
    TraceRun,
    TraceTokens,
    _json,
)


def _run(*events, **kw):
    return TraceRun(
        id=kw.pop("id", "r1"), status=kw.pop("status", "completed"), events=events, **kw
    )


def _trace(*runs):
    return Trace(runs, session="s", commit="abc1234", harness="neutral")


class TestShape:
    def test_a_run_round_trips_through_its_neutral_body(self):
        run = TraceRun(
            id="r1",
            status="failed",
            started=10.5,
            ended=20.0,
            model="m",
            tokens=TraceTokens(input=1, output=2, cache_read=3),
            system="sys",
            events=(
                TraceEvent("user", "hi"),
                TraceEvent(
                    "tool", None, tool="t", args={"a": [1, {"b": None}]}, error=True
                ),
            ),
        )
        body = json.loads(json.dumps(run.to_dict()))
        assert NeutralTraces().read(body) == run

    def test_unrecorded_counts_stay_none_and_are_left_out(self):
        tokens = TraceTokens(input=5, reasoning=0)
        assert tokens.to_dict() == {"input": 5, "reasoning": 0}
        assert TraceTokens.from_dict({"input": 5}).output is None

    @pytest.mark.parametrize(
        "body, says",
        [
            ({"status": "completed"}, "run id"),
            ({"id": "r", "status": None}, "run status"),
            ({"id": "r", "status": "x", "events": [{"kind": "system"}]}, "event kind"),
            ({"id": "r", "status": "x", "tokens": {"input": "5"}}, "tokens.input"),
            ({"id": "r", "status": "x", "tokens": {"input": True}}, "tokens.input"),
            ({"id": "r", "status": "x", "started": "noon"}, "run started"),
            (
                {
                    "id": "r",
                    "status": "x",
                    "events": [{"kind": "tool", "error": "false"}],
                },
                "event error",
            ),
            (
                {"id": "r", "status": "x", "events": [{"kind": "tool", "args": [1]}]},
                "args",
            ),
        ],
    )
    def test_a_malformed_body_is_refused_with_where(self, body, says):
        with pytest.raises(ValueError, match=says):
            NeutralTraces().read(body)

    def test_a_body_that_is_not_an_object_is_refused(self):
        with pytest.raises(ValueError, match="object"):
            NeutralTraces().read([])


class TestDerived:
    def test_counts_input_and_reply_come_from_the_events(self):
        run = _run(
            TraceEvent("user", "first"),
            TraceEvent("tool", "ok", tool="a"),
            TraceEvent("assistant", "an answer"),
            TraceEvent("tool", "boom", tool="b", error=True),
            TraceEvent("user", "second"),
            TraceEvent("assistant", " \n"),
        )
        assert (run.tool_calls, run.tool_errors) == (2, 1)
        assert run.input == "first"
        assert run.reply == "an answer"  # the last with text, not the blank one

    def test_a_run_woken_by_a_note_has_no_input(self):
        assert _run(TraceEvent("assistant", "hi")).input is None

    def test_duration_is_between_the_times_as_they_print(self):
        assert _run(started=0.9, ended=2.1).duration == 2
        assert _run(started=5.0).duration is None
        assert _run(started=5.0, ended=4.0).duration == 0


class TestJson:
    @pytest.mark.parametrize(
        "x, js",
        [
            (1e-7, "1e-7"),
            (1e-6, "0.000001"),
            (1e21, "1e+21"),
            (1e20, "100000000000000000000"),
            (-2.5e-8, "-2.5e-8"),
            (2.0, "2"),
            (-0.0, "0"),
            (1 / 3, "0.3333333333333333"),
            (float("nan"), "null"),
        ],
    )
    def test_floats_print_as_javascript_prints_them(self, x, js):
        assert _json(x) == js

    def test_keys_sort_by_utf16_code_units(self):
        assert _json({"\uff21": 1, "😀": 2, "b": 3, "10": 4, "9": 5}) == (
            '{"10":4,"9":5,"b":3,"😀":2,"\uff21":1}'
        )


class TestTrace:
    def test_paths_and_reads(self):
        trace = _trace(
            _run(
                TraceEvent("user", "hi"), TraceEvent("tool", "x", tool="t"), system="p"
            ),
            _run(id="r2", system="p"),
        )
        paths = trace.paths()
        assert paths == sorted(paths)
        assert len([p for p in paths if p.startswith("system/")]) == 1
        assert "runs/001-r1/events/002-tool-t.md" in paths
        assert trace.read("/index.md") == trace.read("index.md")
        with pytest.raises(FileNotFoundError):
            trace.read("runs/009-nope/run.md")

    def test_a_shared_system_prompt_is_stored_once(self):
        trace = _trace(
            _run(system="p"), _run(id="r2", system="p"), _run(id="r3", system="q")
        )
        assert len([p for p in trace.paths() if p.startswith("system/")]) == 2

    def test_a_result_holding_fences_gets_a_longer_fence(self):
        trace = _trace(_run(TraceEvent("tool", "a ````` b", tool="t")))
        text = trace.read("runs/001-r1/events/001-tool-t.md")
        assert "``````\na ````` b\n``````" in text

    def test_the_index_cuts_by_code_point(self):
        trace = _trace(_run(TraceEvent("user", "🙂" * 100)))
        line = trace.read("index.md").splitlines()[-1]
        assert line.endswith("🙂" * 80 + "…")

    def test_names_become_path_segments(self):
        trace = _trace(_run(TraceEvent("tool", "x", tool="../a b"), id="../../etc"))
        assert "runs/001-______et/events/001-tool-___a_b.md" in trace.paths()


class TestCorpus:
    @pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
    def test_a_port_renders_each_case_from_its_json_alone(self, case):
        recorded = json.loads((export.TRACES_DIR / f"{case.name}.json").read_text())
        source = NeutralTraces()
        trace = Trace(
            [source.read(body) for body in recorded["runs"]],
            session=recorded["session"],
            commit=recorded["commit"],
            harness=recorded["harness"],
        )
        assert trace.files() == recorded["files"]
