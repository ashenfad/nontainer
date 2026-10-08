"""Running a scenario on a harness, and checking what came of it.

The runner owns the world (a memory store, the starting files), the
timing of outside events (through the :class:`Clock` the harness's
scripted model reads), and the observations. A harness owns its loop:
it implements :class:`Harness` and :class:`HarnessSession`, and maps the
neutral model steps onto a scripted model of its own.

    observed = run(scenario, harness)
    failures = check(scenario, observed)   # {check name: what differed}
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..compaction import Fold, folds
from ..inbox import Inbox
from ..planes import CONVERSATION_PREFIXES
from ..store import Store
from ..turns import (
    RunEnded,
    RunStarted,
    TextDelta,
    ThinkingDelta,
    ToolEnded,
    TurnEvent,
    Usage,
)
from ..workspace import Workspace
from .corpus import (
    CAPABILITIES,
    Checkout,
    EventStep,
    Fork,
    ModelStep,
    RunExp,
    Scenario,
    Step,
    Turn,
)

__all__ = [
    "Clock",
    "CommitView",
    "Harness",
    "HarnessSession",
    "Observed",
    "RunView",
    "applies",
    "check",
    "event_names",
    "run",
]

#: What the clock answers once a turn's script has run out: a reply that
#: ends the turn, so a harness that asks once too often stops rather
#: than hangs. The observation records that it happened.
EXHAUSTED = ModelStep(text="(the script ran out)")


class Clock:
    """The scripted model's side of a scenario.

    A harness's scripted model calls :meth:`next` once per model call.
    It fires the outside events queued ahead of the next reply, through
    ``fire``, and then hands that reply over. The runner loads each
    turn's steps before running it.
    """

    def __init__(self, fire: Callable[[EventStep], None]) -> None:
        self._fire = fire
        self._steps: list[Step] = []
        self.exhausted = False
        self.sent: tuple[str, ...] | None = None

    def load(self, steps: Sequence[Step]) -> None:
        self._steps = list(steps)
        self.sent = None

    def next(self, sent: Sequence[str] | None = None) -> ModelStep:
        """The next reply. ``sent`` is the text of each message the
        request carried, when the harness passes it: what a scenario
        checks to see whether a fold is in force (``TurnExp.folded``)."""
        if sent is not None:
            self.sent = tuple(sent)
        while self._steps and isinstance(self._steps[0], EventStep):
            self._fire(self._steps.pop(0))  # type: ignore[arg-type]
        if not self._steps:
            self.exhausted = True
            return EXHAUSTED
        return self._steps.pop(0)  # type: ignore[return-value]


@dataclass(frozen=True)
class RunView:
    """One stored run, as the harness reads its own format."""

    closing_note: bool
    tool_results: int


@dataclass(frozen=True)
class CommitView:
    """One commit: the tool its stamp names, and the run statuses it
    records."""

    tool: str
    runs: tuple[str, ...] = ()


class HarnessSession(Protocol):
    """A harness driving one session's workspace."""

    inbox: Inbox

    def turn(self, prompt: str) -> list[TurnEvent]:
        """Run one turn to its end; what it streamed."""

    def resume(self) -> list[TurnEvent]:
        """Continue the last turn's interrupted run in place."""

    def wake(self) -> list[TurnEvent]:
        """Run a WOKEN turn: one with no prompt, which opens with what is
        waiting to be delivered (:meth:`~nontainer.turns.Turn.opening`),
        the answers its ``sessions`` helper has landed among it. Needed
        with ``delegation``."""

    def cancel(self) -> None:
        """Stop the run in flight. Called from inside a model call."""

    def runs(self) -> list[RunView]:
        """The stored conversation, oldest run first."""

    def run_statuses(self, info: Mapping[str, Any]) -> tuple[str, ...]:
        """The run statuses a commit's stamp records, as
        :data:`~nontainer.turns.RunStatus` values; empty for a commit
        that records no run."""

    def close(self) -> None: ...


class Harness(Protocol):
    """A harness under test.

    ``capabilities`` are the :data:`~nontainer.conformance.corpus.
    CAPABILITIES` it has. ``known_gaps`` maps a scenario name to the
    checks (as :func:`check` names them) the harness is known to fail
    today, each with the reason: a test runner expects exactly those to
    fail, so a gap that closes is noticed.
    """

    name: str
    capabilities: frozenset[str]
    known_gaps: Mapping[str, Mapping[str, str]]

    def open(self, ws: Workspace, clock: Clock) -> HarnessSession:
        """A session on ``ws``. A harness with ``compaction`` also takes
        ``budget=`` (tokens), passed for a scenario that sets one; one
        with ``delegation`` takes ``sessions=``, the helper its
        ``sessions`` tool asks through and whose answers it delivers,
        passed for a scenario with delegates. That harness also has
        ``delegation(store, scenario)``, which returns the
        :class:`Delegation` running the scenario's delegates."""
        ...


@dataclass(frozen=True)
class Observed:
    """What came of a scenario, normalized for :func:`check`."""

    turns: tuple[tuple[TurnEvent, ...], ...]
    files: dict[str, str | None]
    runs: tuple[RunView, ...]
    commits: tuple[CommitView, ...]
    inbox: tuple[int, int]
    exhausted: bool = False
    sent: tuple[tuple[str, ...] | None, ...] = ()
    """Per turn, what its last model request carried, as the harness
    passed it to the clock (``None``: it passed nothing)."""
    folds: tuple[Fold, ...] = ()
    """The folds the final workspace records, oldest first."""
    stored_summary: str | None = None
    """A fold's summary found in the stored conversation, which must
    never hold one; ``None`` when none was."""
    summaries: tuple[str, ...] = ()
    """Every fold summary recorded along the way, a rewind's included."""
    delegates: dict[str, DelegateView] = field(default_factory=dict)
    """Every delegate asked in the scenario, at any depth, by the name
    it was asked under."""
    in_force: tuple[str | None, ...] = ()
    """Per turn, the summary of the fold in force as it ended: the
    latest the workspace records then (a rewind takes later ones away),
    or ``None`` when it records none."""


@dataclass(frozen=True)
class DelegateView:
    """One delegate as the scenario saw it: the answer its asker got
    (``status`` empty when none came), how many woken turns it took, the
    delegates of its own it answered without hearing from (by the names
    they were asked under), and whether it left writes uncommitted when
    its runner returned."""

    status: str = ""
    text: str = ""
    wakes: int = 0
    unread: tuple[str, ...] = ()
    uncommitted: bool = False


class Delegation(Protocol):
    """What runs a scenario's delegates, from the harness that has
    ``delegation`` (``Harness.delegation``): it builds the ``sessions``
    helper each session asks through, fires the events a script holds,
    and at the end says how each delegate came back. Core cannot build
    a helper itself; ``nontainer.adapters.corpus_delegates`` is the
    implementation a harness hands over."""

    exhausted: bool

    def helper(self, ws: Workspace) -> Any: ...

    def fire(self, step: EventStep, session: HarnessSession, helper: Any) -> None: ...

    def finish(self) -> dict[str, DelegateView]: ...


def applies(scenario: Scenario, harness: Harness) -> bool:
    """Whether the harness has every capability the scenario needs."""
    return set(scenario.needs) <= set(harness.capabilities)


def _read(ws: Workspace, path: str) -> str | None:
    try:
        return ws.files.read(path).decode()
    except (FileNotFoundError, IsADirectoryError):
        return None


def run(scenario: Scenario, harness: Harness) -> Observed:
    """Run ``scenario`` on ``harness``, in a memory store of its own."""
    unknown = set(harness.capabilities) - set(CAPABILITIES)
    if unknown:
        raise ValueError(f"{harness.name}: unknown capabilities {sorted(unknown)}")
    store = Store(memory=True)
    ws = store.open("scenario")
    holder: dict[str, Any] = {}
    delegates: Delegation | None = (
        harness.delegation(store, scenario)  # type: ignore[attr-defined]
        if "delegation" in scenario.needs
        else None
    )

    def fire(step: EventStep) -> None:
        if delegates is not None:
            delegates.fire(step, holder["session"], holder.get("helper"))
        elif step.what == "cancel":
            holder["session"].cancel()
        elif step.what == "queue_note":
            holder["session"].inbox.put(step.text)
        else:  # pragma: no cover - the format refuses others
            raise ValueError(f"unknown event {step.what!r}")

    clock = Clock(fire)

    def open_session(ws: Workspace) -> HarnessSession:
        options: dict[str, Any] = {}
        if scenario.budget is not None:
            options["budget"] = scenario.budget
        if delegates is not None:
            options["sessions"] = holder["helper"] = delegates.helper(ws)
        return harness.open(ws, clock, **options)

    # The harness session and the workspace it drives are closed on the
    # way out however the scenario ended: a failure is what the runner
    # exists to expose, and a harness may own threads or a loop.
    try:
        for path, text in scenario.world.files.items():
            ws.files.write(path, text)
        if ws.uncommitted:
            ws.commit(info={"tool": "world"})
        start = ws.head
        holder["session"] = open_session(ws)
        turns: list[tuple[TurnEvent, ...]] = []
        sent: list[tuple[str, ...] | None] = []
        ended_at: list[str] = []
        stored_summary: str | None = None
        summaries: list[str] = []
        in_force: list[str | None] = []
        for act in scenario.acts:
            session = holder["session"]
            if isinstance(act, Turn):
                clock.load(act.steps)
                events = session.resume() if act.resume else session.turn(act.prompt)
                turns.append(tuple(events))
                sent.append(clock.sent)
                ended_at.append(ws.head)
                stored_summary = stored_summary or _stored_summary(ws)
                recorded = folds(ws)
                summaries.extend(
                    f.summary for f in recorded if f.summary not in summaries
                )
                in_force.append(recorded[-1].summary if recorded else None)
            elif isinstance(act, Checkout):
                ws.checkout(ended_at[act.after_turn - 1])
            elif isinstance(act, Fork):
                child = ws.fork(act.name, inherit=act.inherit)
                holder.pop("session").close()
                parent, ws = ws, child
                parent.close()
                holder["session"] = open_session(ws)
        session = holder["session"]
        delegated: dict[str, DelegateView] = {}
        if delegates is not None:
            delegated = delegates.finish()
            delegates = None  # finished: the way out has nothing left to do
        expect = scenario.expect
        paths = list(expect.files) + [p for p in expect.absent if p not in expect.files]
        commits = []
        for entry in ws.log():
            if entry.id == start:
                break
            info = entry.info or {}
            commits.append(
                CommitView(
                    tool=str(info.get("tool", "")), runs=session.run_statuses(info)
                )
            )
        return Observed(
            turns=tuple(turns),
            files={p: _read(ws, p) for p in paths},
            runs=tuple(session.runs()),
            commits=tuple(reversed(commits)),
            inbox=(len(session.inbox.pending()), len(session.inbox.delivered())),
            exhausted=clock.exhausted
            or (delegates is not None and delegates.exhausted),
            sent=tuple(sent),
            folds=tuple(folds(ws)),
            stored_summary=stored_summary,
            summaries=tuple(summaries),
            in_force=tuple(in_force),
            delegates=delegated,
        )
    finally:
        try:
            if delegates is not None:
                delegates.finish()  # a scenario that failed: release and join
            if "session" in holder:
                holder.pop("session").close()
        finally:
            ws.close()
            store.close()


def _folded(
    want: bool,
    sent: tuple[str, ...] | None,
    summaries: Sequence[str],
    current: str | None,
    first: str,
) -> str | None:
    """What is wrong with whether a turn's last request was folded: a
    folded one carries the summary of the fold in force (``current``)
    and no other, in place of the first turn; one that isn't carries no
    summary."""
    if sent is None:
        return "the harness didn't pass the clock what it sent"
    text = "\n".join(sent)
    carried = [s for s in summaries if s and s in text]
    if not want:
        return "its last request carried a fold's summary" if carried else None
    if current is None:
        return "no fold is recorded to be in force"
    if current not in carried:
        return "its last request didn't carry the summary of the fold in force"
    if len(carried) > 1:
        return "its last request carried another fold's summary too"
    if first and first in text:
        return f"its last request still carried the first turn ({first!r})"
    return None


def _stored_summary(ws: Workspace) -> str | None:
    """A summary of one of ``ws``'s folds that its stored conversation
    holds, whatever format the harness stores runs in; ``None`` when it
    holds none, as it never should."""
    summaries = [f.summary for f in folds(ws) if f.summary]
    if not summaries:
        return None
    kv = ws.provider.kv
    for key in list(kv.keys()):
        if isinstance(key, str) and key.startswith(CONVERSATION_PREFIXES):
            stored = json.dumps(kv.get(key), default=str)
            for summary in summaries:
                if json.dumps(summary)[1:-1] in stored:
                    return summary
    return None


def event_names(events: Sequence[TurnEvent]) -> tuple[str, ...]:
    """A turn's events as :class:`~nontainer.conformance.corpus.TurnExp`
    compares them: kinds in order, consecutive deltas of one kind merged,
    ``Usage`` left out, ``ToolEnded:error`` for a failed tool, and the
    ending as ``RunEnded:<status>``."""
    names: list[str] = []
    for ev in events:
        if isinstance(ev, Usage):
            continue
        if isinstance(ev, (TextDelta, ThinkingDelta)):
            if names and names[-1] == ev.kind:
                continue
            names.append(ev.kind)
        elif isinstance(ev, ToolEnded):
            names.append("ToolEnded:error" if ev.is_error else "ToolEnded")
        elif isinstance(ev, RunEnded):
            names.append(f"RunEnded:{ev.status}")
        else:
            names.append(ev.kind)
    return tuple(names)


def _misshapen(events: Sequence[TurnEvent]) -> str | None:
    """What is wrong with a turn's stream as a whole, or ``None``: every
    turn opens with one ``RunStarted`` and closes with one ``RunEnded``,
    whatever else it streams in between."""
    if not events:
        return "the turn streamed nothing"
    starts = sum(isinstance(ev, RunStarted) for ev in events)
    ends = sum(isinstance(ev, RunEnded) for ev in events)
    if (
        starts == 1
        and ends == 1
        and isinstance(events[0], RunStarted)
        and isinstance(events[-1], RunEnded)
    ):
        return None
    return (
        f"{starts} RunStarted and {ends} RunEnded; it opened with "
        f"{events[0].kind} and closed with {events[-1].kind}"
    )


def _ending(events: Sequence[TurnEvent]) -> str | None:
    ends = [ev for ev in events if isinstance(ev, RunEnded)]
    return ends[-1].status if ends else None


def check(scenario: Scenario, observed: Observed) -> dict[str, str]:
    """Every way ``observed`` differs from the scenario's expectations,
    by check name: ``turn<N>.stream`` (N from 1; the stream opens with
    ``RunStarted`` and closes with ``RunEnded``, checked for every turn),
    ``turn<N>.status``, ``turn<N>.events``, ``turn<N>.folded``,
    ``files``, ``runs``, ``commits``, ``inbox``, ``folds``,
    ``delegate.<name>.<part>`` (and ``.uncommitted``, checked always),
    ``summary``
    (a fold's summary in the stored conversation, checked always) and
    ``script``. Empty when everything holds."""
    expect = scenario.expect
    failures: dict[str, str] = {}
    first = next((a.prompt for a in scenario.acts if isinstance(a, Turn)), "")
    pad = len(observed.turns)
    sent = list(observed.sent) + [None] * (pad - len(observed.sent))
    current = list(observed.in_force) + [None] * (pad - len(observed.in_force))
    for n, (want, got, request, summary) in enumerate(
        zip(expect.turns, observed.turns, sent, current), start=1
    ):
        if want.folded is not None:
            problem = _folded(want.folded, request, observed.summaries, summary, first)
            if problem:
                failures[f"turn{n}.folded"] = problem
        shape = _misshapen(got)
        if shape is not None:
            failures[f"turn{n}.stream"] = shape
        status = _ending(got)
        if status != want.status:
            failures[f"turn{n}.status"] = f"ended {status!r}, not {want.status!r}"
        if want.events is not None:
            names = event_names(got)
            if names != tuple(want.events):
                failures[f"turn{n}.events"] = f"{list(names)} != {list(want.events)}"
    if len(observed.turns) != len(expect.turns):
        failures["turns"] = f"{len(observed.turns)} turn(s) ran"
    wrong = {
        path: got
        for path, got in observed.files.items()
        if (path in expect.files and got != expect.files[path])
        or (path in expect.absent and path not in expect.files and got is not None)
    }
    if wrong:
        failures["files"] = f"{wrong}"
    if expect.runs is not None:

        def fits(got: RunView, want: RunExp) -> bool:
            return got.closing_note == want.closing_note and (
                want.tool_results is None or got.tool_results == want.tool_results
            )

        if len(observed.runs) != len(expect.runs) or not all(
            map(fits, observed.runs, expect.runs)
        ):
            failures["runs"] = f"{list(observed.runs)} != {list(expect.runs)}"
    if expect.commits is not None:
        got_commits = [(c.tool, tuple(c.runs)) for c in observed.commits]
        want_commits = [(c.tool, tuple(c.runs)) for c in expect.commits]
        if got_commits != want_commits:
            failures["commits"] = f"{got_commits} != {want_commits}"
    if expect.inbox is not None:
        want_inbox = (expect.inbox.pending, expect.inbox.delivered)
        if observed.inbox != want_inbox:
            failures["inbox"] = f"(pending, delivered) {observed.inbox} != {want_inbox}"
    if expect.folds is not None:
        got_folds = [(f.runs, f.first is not None) for f in observed.folds]
        want_folds = [(f.runs, f.ranged) for f in expect.folds]
        if got_folds != want_folds:
            failures["folds"] = f"(runs, ranged) {got_folds} != {want_folds}"
    if expect.delegates is not None:
        for name, want in expect.delegates.items():
            seen = observed.delegates.get(name)
            if seen is None:
                failures[f"delegate.{name}"] = "it was never asked"
                continue
            if seen.status != want.status:
                failures[f"delegate.{name}.status"] = (
                    f"{seen.status or 'no answer'!r}, not {want.status!r}"
                )
            missing = [w for w in want.mentions if w not in seen.text]
            if missing:
                failures[f"delegate.{name}.mentions"] = (
                    f"its answer doesn't say {missing}: {seen.text[:120]!r}"
                )
            if want.wakes is not None and seen.wakes != want.wakes:
                failures[f"delegate.{name}.wakes"] = (
                    f"{seen.wakes} woken turn(s), not {want.wakes}"
                )
            if tuple(sorted(seen.unread)) != tuple(sorted(want.unread)):
                failures[f"delegate.{name}.unread"] = (
                    f"answered without {list(seen.unread)}, not {list(want.unread)}"
                )
    for name, seen in observed.delegates.items():
        if seen.uncommitted:
            failures[f"delegate.{name}.uncommitted"] = (
                "its runner returned with writes uncommitted"
            )
    if observed.stored_summary is not None:
        failures["summary"] = (
            f"the stored conversation holds a fold's summary: "
            f"{observed.stored_summary[:80]!r}"
        )
    if observed.exhausted:
        failures["script"] = (
            "the harness asked the model for more than the script holds"
        )
    return failures
