"""The conformance corpus: its format, its codec, the turn events, and
the runner.

The runner is checked against a reference harness written here: a
minimal loop that honors the contract exactly, keeping its conversation
on core's own plane. Every scenario passes on it with no known gaps, so
the scenarios hold together and the runner observes what it should,
apart from any one harness.
"""

import json
import uuid
from dataclasses import replace

import pytest

from nontainer import compaction, conversation
from nontainer.conformance import Clock, RunView, check, event_names, run
from nontainer.conformance.codec import dump, dumps, json_schema, load
from nontainer.conformance.corpus import (
    Expect,
    Scenario,
    TurnExp,
    cancel,
    queue_note,
    says,
    turn,
    writes,
)
from nontainer.conformance.harness import SCENARIOS, by_name
from nontainer.inbox import Inbox
from nontainer.turns import (
    TURN_EVENTS,
    Compacted,
    Delivered,
    DeliveredNote,
    RunEnded,
    RunStarted,
    TextDelta,
    ThinkingDelta,
    ToolEnded,
    ToolStarted,
    TurnEvent,
    Usage,
    event_from_dict,
)

# -- a reference harness ------------------------------------------------------


class ReferenceSession:
    """The contract, and nothing else, on the turn API: each run is a
    turn (``ws.turn``) that runs the clock's replies, writes files
    through the workspace (one commit per call), delivers queued notes
    on tool results (``turn.deliver``), and ends by handing the turn its
    body (with a closing note when it was cancelled or failed). The turn
    stores it, settles the inbox and commits once, stamped with the
    run's status."""

    def __init__(self, ws, clock, budget=None):
        self.ws = ws
        self.clock = clock
        self.budget = budget
        self.inbox = Inbox()
        self._cancelled = False
        self._interrupted = None
        self._run_id = None

    def turn(self, prompt):
        turn = self.ws.turn(uuid.uuid4().hex, inbox=self.inbox, harness="reference")
        return self._run(turn, [{"role": "user", "text": prompt}])

    def resume(self):
        run_id = self._interrupted
        held = conversation.read_runs(self.ws.provider.kv, [run_id])[run_id]
        turn = self.ws.turn(run_id, resume=True, inbox=self.inbox)
        return self._run(turn, held["messages"])

    def cancel(self):
        self._cancelled = True

    def _run(self, turn, messages):
        run_id = self._run_id = turn.run_id
        events = [RunStarted(run_id=run_id)]
        self._cancelled = False
        status, message = "completed", None
        calls = 0
        while True:
            request = self._request(messages, events)
            step = self.clock.next([m["text"] for m in request])
            if self._cancelled:
                status, message = "cancelled", "stopped"
                break
            if step.fail:
                status = "interrupted" if step.fail == "provider" else "failed"
                message = f"{step.fail} error"
                break
            if step.thinking:
                events.append(ThinkingDelta(text=step.thinking))
            if step.text:
                events.append(TextDelta(text=step.text))
                messages.append(
                    {
                        "role": "assistant",
                        "text": step.text,
                        "tokens": step.input_tokens,
                    }
                )
            if not step.tool_calls:
                break
            for call in step.tool_calls:
                calls += 1
                call_id = f"{run_id}-{calls}"
                events.append(
                    ToolStarted(call_id=call_id, name=call.name, args=call.args)
                )
                self.ws.files.write(call.args["path"], call.args["content"])
                self.ws.commit(info={"tool": call.name})
                text, notes = turn.deliver("ok")
                if notes:
                    events.append(
                        Delivered(
                            notes=tuple(
                                DeliveredNote(id=n.id, text=n.text) for n in notes
                            )
                        )
                    )
                messages.append({"role": "tool", "text": text})
                events.append(ToolEnded(call_id=call_id, name=call.name, result="ok"))
        if status in ("cancelled", "failed"):
            messages.append({"role": "note", "text": f"ended early: {message}"})
        turn.end(status, body={"messages": messages}, message=message)
        self._interrupted = run_id if status == "interrupted" else None
        events.append(RunEnded(status=status, message=message))
        return events

    # -- compaction: the reference's own folding, on the core's records --

    def _earlier(self):
        """The earlier runs' messages, each with an id to anchor a fold."""
        kv = self.ws.provider.kv
        index = conversation.read_index(kv)
        if index is None:
            return []
        bodies = conversation.read_runs(kv, index.runs, index)
        return [
            {**m, "id": f"{run_id}:{i}"}
            for run_id in index.runs
            if run_id != self._run_id
            for i, m in enumerate(bodies[run_id]["messages"])
        ]

    def _spliced(self, earlier):
        fold = compaction.in_force(self.ws, [m["id"] for m in earlier])
        if fold is None:
            return earlier
        end = next(i for i, m in enumerate(earlier) if m["id"] == fold.through)
        summary = {"role": "user", "text": compaction.summary_message(fold.summary)}
        return [{**summary, "summary": True}, *earlier[end + 1 :]]

    def _request(self, current, events):
        """What the model is sent: the earlier turns with the fold in
        force spliced in, then the run in progress. Over budget, every
        earlier turn is folded first, the model writing the summary."""
        earlier = self._earlier()
        if self.budget is None:
            return earlier + current
        view = self._spliced(earlier)
        reported = [m.get("tokens", 0) for m in view + current if m.get("tokens")]
        size = reported[-1] if reported else 0
        unfolded = [m for m in view if not m.get("summary")]
        if compaction.Policy(budget=self.budget).due(size) and unfolded:
            texts = [m["text"] for m in view + current]
            step = self.clock.next([*texts, compaction.summary_request()])
            fold = compaction.Fold(
                through=earlier[-1]["id"],
                summary=step.text,
                runs=sum(1 for m in earlier if m["role"] == "user"),
            )
            compaction.record(self.ws, fold)
            events.append(Compacted(through=fold.through, runs=fold.runs))
            view = self._spliced(earlier)
        return view + current

    def runs(self):
        kv = self.ws.provider.kv
        index = conversation.read_index(kv)
        if index is None:
            return []
        bodies = conversation.read_runs(kv, index.runs, index)
        views = []
        for run_id in index.runs:
            messages = bodies[run_id]["messages"]
            views.append(
                RunView(
                    closing_note=messages[-1]["role"] == "note",
                    tool_results=sum(1 for m in messages if m["role"] == "tool"),
                )
            )
        return views

    def run_statuses(self, info):
        runs = info.get("runs")
        return tuple(runs.values()) if isinstance(runs, dict) else ()

    def close(self):
        pass


class ReferenceHarness:
    name = "reference"
    capabilities = frozenset({"resume", "keeps-aborted-runs", "compaction"})
    known_gaps = {}

    def open(self, ws, clock, *, budget=None):
        return ReferenceSession(ws, clock, budget)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
def test_the_reference_harness_honors_every_scenario(scenario):
    assert check(scenario, run(scenario, ReferenceHarness())) == {}


def test_a_harness_that_breaks_the_contract_is_caught():
    """The same harness with the inbox never settled: the checks that
    look at the inbox fail, and only those."""

    class Leaky(ReferenceSession):
        def _run(self, run_id, messages):
            settle, self.inbox.settle = self.inbox.settle, lambda: None
            try:
                return super()._run(run_id, messages)
            finally:
                self.inbox.settle = settle

    class LeakyHarness(ReferenceHarness):
        def open(self, ws, clock):
            return Leaky(ws, clock)

    scenario = by_name("a-note-rides-the-next-tool-result-once")
    assert set(check(scenario, run(scenario, LeakyHarness()))) == {"inbox"}


def test_a_harness_that_asks_for_more_than_the_script_is_caught():
    scenario = Scenario(
        name="short",
        summary="One reply.",
        tiers=(1,),
        acts=(turn("go", says("done")),),
        expect=Expect(turns=(TurnExp(status="completed"),)),
    )

    class Greedy(ReferenceSession):
        def turn(self, prompt):
            self.clock.next()
            return super().turn(prompt)

    class GreedyHarness(ReferenceHarness):
        def open(self, ws, clock):
            return Greedy(ws, clock)

    assert "script" in check(scenario, run(scenario, GreedyHarness()))


def test_a_harness_that_stores_a_summary_is_caught():
    """A fold's summary must never reach the stored conversation,
    whatever a harness stores its runs as: checked for every scenario."""

    class Storing(ReferenceSession):
        def _request(self, current, events):
            request = super()._request(current, events)
            current.extend(m for m in request if m.get("summary") and m not in current)
            return request

    class StoringHarness(ReferenceHarness):
        def open(self, ws, clock, *, budget=None):
            return Storing(ws, clock, budget)

    scenario = by_name("a-request-over-budget-folds-the-earlier-turns")
    assert "summary" in check(scenario, run(scenario, StoringHarness()))


def test_a_fold_unchecked_when_the_harness_passes_nothing_it_sent():
    """``folded`` needs what the request carried; a harness that keeps it
    from the clock fails that check rather than passing it unseen."""

    class Silent(ReferenceSession):
        def __init__(self, ws, clock, budget=None):
            super().__init__(ws, clock, budget)
            next_reply = clock.next
            self.clock = type(
                "Quiet", (), {"next": lambda _, sent=None: next_reply()}
            )()

    class SilentHarness(ReferenceHarness):
        def open(self, ws, clock, *, budget=None):
            return Silent(ws, clock, budget)

    scenario = by_name("a-request-over-budget-folds-the-earlier-turns")
    problems = check(scenario, run(scenario, SilentHarness()))
    assert set(problems) == {"turn1.folded", "turn2.folded"}
    assert "didn't pass" in problems["turn2.folded"]


def test_a_budget_needs_compaction():
    with pytest.raises(ValueError, match="a budget needs 'compaction'"):
        replace(by_name("a-request-over-budget-folds-the-earlier-turns"), needs=())


def test_a_stream_must_open_with_run_started_and_close_with_run_ended():
    """Checked on the raw stream, for every turn: a usage report after
    the ending would otherwise pass, because the event comparison leaves
    usage out, and a scenario that asserts no events would not look."""

    class Late(ReferenceSession):
        def _run(self, run_id, messages):
            return [*super()._run(run_id, messages), Usage(input_tokens=1)]

    class Headless(ReferenceSession):
        def _run(self, run_id, messages):
            return super()._run(run_id, messages)[1:]

    def harness_of(cls):
        class Harness(ReferenceHarness):
            def open(self, ws, clock):
                return cls(ws, clock)

        return Harness()

    writes_a_file = by_name("a-turn-that-writes-a-file")
    failures = check(writes_a_file, run(writes_a_file, harness_of(Late)))
    assert set(failures) == {"turn1.stream"}
    assert "closed with Usage" in failures["turn1.stream"]

    rewinds = by_name("a-checkout-rewinds-the-conversation-with-the-files")
    assert all(t.events is None for t in rewinds.expect.turns)
    failures = check(rewinds, run(rewinds, harness_of(Headless)))
    assert set(failures) == {"turn1.stream", "turn2.stream", "turn3.stream"}


def test_the_session_is_closed_when_a_turn_raises():
    closed = []

    class Broken(ReferenceSession):
        def turn(self, prompt):
            raise RuntimeError("the loop broke")

        def close(self):
            closed.append(self)

    class BrokenHarness(ReferenceHarness):
        def open(self, ws, clock):
            return Broken(ws, clock)

    with pytest.raises(RuntimeError, match="the loop broke"):
        run(by_name("a-reply-alone-completes"), BrokenHarness())
    assert len(closed) == 1
    assert closed[0].ws._closed


def test_every_session_and_workspace_the_runner_opens_is_closed_once():
    opened, closed = [], []

    class Tracked(ReferenceSession):
        def close(self):
            closed.append(self)

    class TrackedHarness(ReferenceHarness):
        def open(self, ws, clock):
            opened.append(Tracked(ws, clock))
            return opened[-1]

    run(by_name("a-full-fork-carries-the-conversation"), TrackedHarness())
    assert len(opened) == 2 and closed == opened
    # the parent's workspace too, closed when the scenario moved to the fork
    assert all(session.ws._closed for session in opened)


# -- the format -----------------------------------------------------------------


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
def test_every_scenario_reads_back_from_its_json(scenario):
    assert load(Scenario, json.loads(dumps(scenario))) == scenario


def test_every_scenario_fits_the_schema():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json_schema(Scenario, title="Scenario")
    for scenario in SCENARIOS:
        jsonschema.validate(dump(scenario), schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**dump(SCENARIOS[0]), "acts": [{"kind": "nap"}]}, schema)


def test_scenario_names_are_unique_and_by_name_finds_them():
    names = [s.name for s in SCENARIOS]
    assert len(names) == len(set(names))
    assert by_name(names[0]) is SCENARIOS[0]
    with pytest.raises(KeyError):
        by_name("no-such-scenario")


def test_a_scenario_refuses_an_unknown_capability_or_a_missing_turn_expectation():
    base = SCENARIOS[0]
    with pytest.raises(ValueError, match="unknown capabilities"):
        replace(base, needs=("telepathy",))
    with pytest.raises(ValueError, match="turn expectation"):
        replace(base, expect=replace(base.expect, turns=()))


def test_the_loader_refuses_data_that_does_not_fit():
    data = dump(SCENARIOS[0])
    with pytest.raises(ValueError, match="unknown field"):
        load(Scenario, {**data, "extra": 1})
    with pytest.raises(ValueError, match="kind 'nap'"):
        load(Scenario, {**data, "acts": [{"kind": "nap"}]})
    turns = [{**data["expect"]["turns"][0], "status": "sideways"}]
    with pytest.raises(ValueError, match="sideways"):
        load(Scenario, {**data, "expect": {**data["expect"], "turns": turns}})


# -- the turn events ------------------------------------------------------------

EVENTS = (
    RunStarted(run_id="r1"),
    TextDelta(text="hi"),
    ThinkingDelta(text="hmm"),
    ToolStarted(call_id="c1", name="file_write", args={"path": "a"}),
    Delivered(notes=(DeliveredNote(id="n1", text="also b", kind="principal"),)),
    ToolEnded(call_id="c1", name="file_write", result="ok", is_error=False),
    Usage(input_tokens=10, cached_tokens=4),
    Compacted(through="m9", runs=3),
    RunEnded(status="cancelled", message="stopped"),
)


def test_every_turn_event_kind_is_covered_here():
    assert {type(e) for e in EVENTS} == set(TURN_EVENTS)
    assert all(e.kind == type(e).__name__ for e in EVENTS)


@pytest.mark.parametrize("event", EVENTS, ids=lambda e: e.kind)
def test_a_turn_event_reads_back_from_its_dict(event):
    assert event_from_dict(json.loads(json.dumps(dump(event)))) == event


def test_turn_events_fit_their_schema():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json_schema(TurnEvent, title="TurnEvent")
    for event in EVENTS:
        jsonschema.validate(dump(event), schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"kind": "RunEnded", "status": "sideways"}, schema)


def test_an_unknown_event_kind_is_refused():
    with pytest.raises(ValueError, match="Telepathy"):
        event_from_dict({"kind": "Telepathy"})


def test_event_names_merge_deltas_and_name_the_ending():
    events = [
        RunStarted(run_id="r"),
        ThinkingDelta(text="a"),
        ThinkingDelta(text="b"),
        TextDelta(text="c"),
        TextDelta(text="d"),
        Usage(input_tokens=5),
        ToolStarted(call_id="1", name="t"),
        ToolEnded(call_id="1", name="t", is_error=True),
        RunEnded(status="failed"),
    ]
    assert event_names(events) == (
        "RunStarted",
        "ThinkingDelta",
        "TextDelta",
        "ToolStarted",
        "ToolEnded:error",
        "RunEnded:failed",
    )


# -- the clock ------------------------------------------------------------------


def test_the_clock_fires_events_before_the_reply_they_precede():
    fired = []
    clock = Clock(fired.append)
    clock.load([writes("/workspace/a", "a"), queue_note("x"), cancel(), says("y")])
    assert clock.next().tool_calls and fired == []
    assert clock.next().text == "y"
    assert [e.what for e in fired] == ["queue_note", "cancel"]
    assert not clock.exhausted
    clock.next()
    assert clock.exhausted
