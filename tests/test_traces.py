"""The neutral trace shape, the neutral source, and the renderer.

The rendered bytes are pinned by the trace corpus
(``nontainer/conformance/traces``, checked by ``test_corpus_drift``);
these check the shape's rules and the renderer's edges."""

import json

import pytest

from nontainer.conformance import export
from nontainer.conformance.traces import CASES
from nontainer.traces import (
    FORK_TOOL,
    NeutralTraces,
    Trace,
    TraceCommit,
    TraceEvent,
    TraceRun,
    TraceTokens,
    _json,
    run_ranges,
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


def _turn(*ids, tool="turn"):
    return {"tool": tool, "runs": {i: "completed" for i in ids}}


class TestRunRanges:
    def test_a_run_is_what_came_after_the_run_before_through_its_landing(self):
        infos = [{}, {"tool": "put"}, _turn("a"), {"tool": "terminal"}, _turn("b")]
        assert run_ranges(["a", "b"], infos) == {"a": range(0, 3), "b": range(3, 5)}

    def test_the_last_landing_is_the_one(self):
        # a checkpoint, then the end: the work between them is the run's
        infos = [_turn("a"), {"tool": "terminal"}, _turn("a"), _turn("b")]
        assert run_ranges(["a", "b"], infos) == {"a": range(0, 3), "b": range(3, 4)}

    def test_only_turns_land_a_run(self):
        # agex-ts's fold stamps a count under the same key
        infos = [{"tool": "fold", "runs": 3}, {"tool": "x", "runs": {"a": "done"}}]
        assert run_ranges(["a"], infos) == {}
        assert run_ranges(["a"], [{"tool": "turn", "runs": ["a"]}]) == {
            "a": range(0, 1)
        }

    def test_a_run_that_never_landed_has_no_range_and_takes_nothing(self):
        infos = [_turn("a"), {"tool": "terminal"}, _turn("c")]
        assert run_ranges(["a", "b", "c"], infos) == {
            "a": range(0, 1),
            "c": range(1, 3),
        }

    def test_a_fork_is_where_the_childs_first_run_starts(self):
        # a fresh fork: the parent's work, then the fork, then the child's
        infos = [{"tool": "terminal"}, {"tool": FORK_TOOL}, {"tool": "x"}, _turn("c")]
        assert run_ranges(["c"], infos) == {"c": range(2, 4)}

    def test_a_fork_after_the_parents_runs_leaves_them_their_ranges(self):
        # a full fork: the parent's run keeps its range, and what the
        # parent did after it is the tree the child started from
        infos = [
            {"tool": "x"},
            _turn("p"),
            {"tool": "put"},
            {"tool": FORK_TOOL},
            {"tool": "y"},
            _turn("c"),
        ]
        assert run_ranges(["p", "c"], infos) == {
            "p": range(0, 2),
            "c": range(4, 6),
        }

    def test_the_fork_marker_is_ws_gits(self):
        from nontainer import agentgit

        assert FORK_TOOL == agentgit.FORK_TOOL

    def test_a_run_landing_before_the_run_before_it_has_an_empty_range(self):
        infos = [_turn("b"), _turn("a")]
        assert list(run_ranges(["a", "b"], infos)["b"]) == []


class TestChanges:
    def test_a_commit_round_trips_and_sorts_its_paths(self):
        commit = TraceCommit("abc1234", "terminal", added=("b", "a"), removed=("z",))
        assert commit.added == ("a", "b")
        assert commit.paths == ("a", "b", "z")
        assert TraceCommit.from_dict(commit.to_dict()) == commit

    @pytest.mark.parametrize("paths", ["foo.py", 3, ["a", 1]])
    def test_a_path_field_must_be_a_list_of_paths(self, paths):
        with pytest.raises(ValueError, match="list of paths"):
            TraceCommit.from_dict({"id": "abc1234", "added": paths})

    def test_new_runs_need_a_since(self):
        with pytest.raises(ValueError, match="since="):
            Trace([], session="s", commit="c", harness="h", new={"r"})

    def test_a_run_without_changes_is_not_known(self):
        trace = Trace([_run()], session="s", commit="c", harness="h")
        assert "files:-" in trace.read("index.md")
        assert "Not known" in trace.read("runs/001-r1/changed.md")


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
            changes={
                rid: [TraceCommit.from_dict(c) for c in commits]
                for rid, commits in recorded["changes"].items()
            },
            since=recorded["since"],
            new=recorded["new"],
        )
        assert trace.files() == recorded["files"]
