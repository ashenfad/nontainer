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

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from ..inbox import Inbox
from ..store import Store
from ..turns import RunEnded, TextDelta, ThinkingDelta, ToolEnded, TurnEvent, Usage
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

    def load(self, steps: Sequence[Step]) -> None:
        self._steps = list(steps)

    def next(self) -> ModelStep:
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

    def open(self, ws: Workspace, clock: Clock) -> HarnessSession: ...


@dataclass(frozen=True)
class Observed:
    """What came of a scenario, normalized for :func:`check`."""

    turns: tuple[tuple[TurnEvent, ...], ...]
    files: dict[str, str | None]
    runs: tuple[RunView, ...]
    commits: tuple[CommitView, ...]
    inbox: tuple[int, int]
    exhausted: bool = False


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
    holder: dict[str, HarnessSession] = {}

    def fire(step: EventStep) -> None:
        session = holder["session"]
        if step.what == "cancel":
            session.cancel()
        elif step.what == "queue_note":
            session.inbox.put(step.text)
        else:  # pragma: no cover - the format refuses others
            raise ValueError(f"unknown event {step.what!r}")

    clock = Clock(fire)
    try:
        for path, text in scenario.world.files.items():
            ws.files.write(path, text)
        if ws.uncommitted:
            ws.commit(info={"tool": "world"})
        start = ws.head
        holder["session"] = harness.open(ws, clock)
        turns: list[tuple[TurnEvent, ...]] = []
        ended_at: list[str] = []
        for act in scenario.acts:
            session = holder["session"]
            if isinstance(act, Turn):
                clock.load(act.steps)
                events = session.resume() if act.resume else session.turn(act.prompt)
                turns.append(tuple(events))
                ended_at.append(ws.head)
            elif isinstance(act, Checkout):
                ws.checkout(ended_at[act.after_turn - 1])
            elif isinstance(act, Fork):
                child = ws.fork(act.name, inherit=act.inherit)
                session.close()
                ws = child
                holder["session"] = harness.open(ws, clock)
        session = holder["session"]
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
        observed = Observed(
            turns=tuple(turns),
            files={p: _read(ws, p) for p in paths},
            runs=tuple(session.runs()),
            commits=tuple(reversed(commits)),
            inbox=(len(session.inbox.pending()), len(session.inbox.delivered())),
            exhausted=clock.exhausted,
        )
        session.close()
        return observed
    finally:
        ws.close()
        store.close()


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


def _ending(events: Sequence[TurnEvent]) -> str | None:
    ends = [ev for ev in events if isinstance(ev, RunEnded)]
    return ends[-1].status if ends else None


def check(scenario: Scenario, observed: Observed) -> dict[str, str]:
    """Every way ``observed`` differs from the scenario's expectations,
    by check name: ``turn<N>.status`` and ``turn<N>.events`` (N from 1),
    ``files``, ``runs``, ``commits``, ``inbox`` and ``script``. Empty
    when everything holds."""
    expect = scenario.expect
    failures: dict[str, str] = {}
    for n, (want, got) in enumerate(zip(expect.turns, observed.turns), start=1):
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
    if observed.exhausted:
        failures["script"] = (
            "the harness asked the model for more than the script holds"
        )
    return failures
