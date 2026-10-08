"""The scenario format: what a harness is asked to do, and what must
come of it.

A :class:`Scenario` runs a harness on a real nontainer workspace with a
scripted model in place of the LLM. Its acts are turns (each a prompt
and the model's script), checkouts and forks between them. Its
expectations are normalized observations: how each turn ended and the
kinds of event it streamed, the files, the stored runs, the commits,
the inbox. Exact text, token counts and timing are not asserted,
because they legitimately differ between harnesses.

**The scripted model is the clock.** A turn's ``steps`` are model
replies (:class:`ModelStep`) with outside events (:class:`EventStep`)
between them. An event fires when the model is asked for the reply
after it, before that reply is returned: "cancel after the first tool
call" is a cancel step after the step that calls the tool. No threads,
no sleeps, and every harness gets the same timing.

Scenarios are written here in Python, with the builders below, and
exported as JSON (one file per scenario, by
``nontainer.conformance.export``) for harnesses in other languages.
Every member of a union carries a ``kind``, because JSON loses the
class; :mod:`~nontainer.conformance.codec` reads them back.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from ..turns import RunStatus

__all__ = [
    "CAPABILITIES",
    "Checkout",
    "CommitExp",
    "EventStep",
    "Expect",
    "FoldExp",
    "Fork",
    "InboxExp",
    "ModelStep",
    "RunExp",
    "Scenario",
    "ToolCall",
    "Turn",
    "TurnExp",
    "World",
    "calls",
    "cancel",
    "checkout",
    "fails",
    "fork",
    "queue_note",
    "resume",
    "says",
    "summarizes",
    "thinks",
    "turn",
    "writes",
]

CAPABILITIES: dict[str, str] = {
    "resume": "continues an interrupted run in place, keeping what it holds",
    "keeps-aborted-runs": (
        "stores a cancelled or failed run with the messages it had, so it "
        "can be kept with a closing note"
    ),
    "compaction": (
        "past a token budget, folds earlier turns into a summary in what the "
        "model is sent, recording each fold in __compaction__/"
    ),
}
"""What a scenario may need beyond the base contract, by name. A
harness declares the ones it has; a scenario that needs one it lacks
does not apply to it."""


# -- the model's script ---------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class ToolCall:
    """One tool call in a model reply."""

    name: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, kw_only=True)
class ModelStep:
    """One model reply: text, reasoning, tool calls, or a failure.

    ``fail`` makes the call raise instead of replying: ``"provider"``
    with a provider error (an overloaded endpoint, say), which a
    harness may resume from; ``"error"`` with any other exception.

    ``input_tokens`` is the size of the request this reply answers, as
    a provider reports it (0 reports nothing): how a scenario puts a
    conversation over a compaction budget.
    """

    kind: Literal["model"] = "model"
    text: str = ""
    thinking: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    fail: Literal["", "provider", "error"] = ""
    input_tokens: int = 0


@dataclass(frozen=True, kw_only=True)
class EventStep:
    """Something from outside the loop, fired when the model is asked
    for the next reply.

    ``cancel`` stops the run in flight. ``queue_note`` queues ``text``
    in the session's inbox, as a person typing mid-turn does.
    """

    kind: Literal["event"] = "event"
    what: Literal["cancel", "queue_note"]
    text: str = ""


Step = ModelStep | EventStep


# -- acts -----------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class Turn:
    """One turn: ``prompt`` and the model's script for it.

    With ``resume``, the turn continues the last turn's interrupted run
    in place instead of starting a new one, and has no prompt.
    """

    kind: Literal["turn"] = "turn"
    prompt: str = ""
    resume: bool = False
    steps: tuple[Step, ...] = ()


@dataclass(frozen=True, kw_only=True)
class Checkout:
    """Check out the commit the ``after_turn``-th turn (counting from 1)
    ended on: the files and the conversation as they stood then."""

    kind: Literal["checkout"] = "checkout"
    after_turn: int


@dataclass(frozen=True, kw_only=True)
class Fork:
    """Fork the session as ``name`` and carry on in the fork. ``full``
    carries the conversation, ``fresh`` starts the fork without one."""

    kind: Literal["fork"] = "fork"
    name: str
    inherit: Literal["full", "fresh"] = "full"


Act = Turn | Checkout | Fork


# -- the world --------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class World:
    """The workspace a scenario starts in: files by path."""

    files: dict[str, str] = field(default_factory=dict)


# -- expectations -------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class TurnExp:
    """How one turn ended, and what it streamed.

    ``events`` are :class:`~nontainer.turns.TurnEvent` kinds in order,
    normalized: consecutive deltas of one kind are one entry, ``Usage``
    is left out (not every harness reports it), a failed tool reads
    ``ToolEnded:error``, and the last entry names the ending, as in
    ``RunEnded:cancelled``. ``None`` leaves the stream unchecked.
    """

    status: RunStatus
    events: tuple[str, ...] | None = None
    folded: bool | None = None
    """Whether the turn's last model request was sent the summary of the
    fold in force (and no other) in place of the turns it covers, the
    first turn's prompt among them, or no summary at all; ``None``
    leaves it unchecked."""


@dataclass(frozen=True, kw_only=True)
class RunExp:
    """One run of the stored conversation: whether it closes with a
    note saying the turn ended early, and how many tool results it
    keeps (``None``: unchecked)."""

    closing_note: bool = False
    tool_results: int | None = None


@dataclass(frozen=True, kw_only=True)
class CommitExp:
    """One commit: the tool its stamp names, and the statuses of the
    runs it records (a run commit's; empty for a tool's commit)."""

    tool: str
    runs: tuple[RunStatus, ...] = ()


@dataclass(frozen=True, kw_only=True)
class InboxExp:
    """The session's inbox at the end: notes still queued, and notes
    delivered but not yet settled."""

    pending: int = 0
    delivered: int = 0


@dataclass(frozen=True, kw_only=True)
class FoldExp:
    """One fold recorded in the workspace's ``__compaction__/`` plane:
    how many turns it covers, and whether it is over a range in the
    middle (``first`` set) rather than from the start."""

    runs: int
    ranged: bool = False


@dataclass(frozen=True, kw_only=True)
class Expect:
    """What must come of a scenario. ``turns`` has one entry per
    :class:`Turn` act, in order. ``files`` maps a path to its content at
    the end, and ``absent`` names paths that must not exist. ``runs``
    is the stored conversation of the session the scenario ends in,
    oldest first; ``commits`` the commits it made on that session's
    branch, oldest first; ``folds`` the folds its workspace records,
    oldest first. ``None`` leaves a part unchecked."""

    turns: tuple[TurnExp, ...]
    files: dict[str, str] = field(default_factory=dict)
    absent: tuple[str, ...] = ()
    runs: tuple[RunExp, ...] | None = None
    commits: tuple[CommitExp, ...] | None = None
    inbox: InboxExp | None = None
    folds: tuple[FoldExp, ...] | None = None


@dataclass(frozen=True, kw_only=True)
class Scenario:
    """One scenario: a world, the acts run in it, and what must come of
    them. ``tiers`` are the contract tiers it pins; ``needs`` are the
    :data:`CAPABILITIES` a harness must have for it to apply.
    ``budget`` is the compaction budget, in tokens, a harness opens the
    session with; it needs ``compaction``."""

    name: str
    summary: str
    tiers: tuple[int, ...]
    needs: tuple[str, ...] = ()
    world: World = field(default_factory=World)
    budget: int | None = None
    acts: tuple[Act, ...]
    expect: Expect

    def __post_init__(self) -> None:
        unknown = set(self.needs) - set(CAPABILITIES)
        if unknown:
            raise ValueError(f"{self.name}: unknown capabilities {sorted(unknown)}")
        if self.budget is not None and "compaction" not in self.needs:
            raise ValueError(f"{self.name}: a budget needs 'compaction'")
        turns = sum(1 for a in self.acts if isinstance(a, Turn))
        if len(self.expect.turns) != turns:
            raise ValueError(
                f"{self.name}: {turns} turn(s) but {len(self.expect.turns)} "
                "turn expectation(s)"
            )


# -- builders --------------------------------------------------------------------


def says(text: str, *, input_tokens: int = 0) -> ModelStep:
    """A reply of text alone, which ends the turn; ``input_tokens`` is
    the size the provider reports for the request it answers."""
    return ModelStep(text=text, input_tokens=input_tokens)


def summarizes(summary: str) -> ModelStep:
    """The reply to a harness's request for a summary, when it folds:
    the model asked for one is the scripted model like any other."""
    return ModelStep(text=summary)


def thinks(thinking: str, text: str = "") -> ModelStep:
    """A reply that reasons first."""
    return ModelStep(thinking=thinking, text=text)


def calls(name: str, **args: Any) -> ModelStep:
    """A reply that calls one tool."""
    return ModelStep(tool_calls=(ToolCall(name=name, args=args),))


def writes(path: str, content: str) -> ModelStep:
    """A reply that writes one file."""
    return calls("file_write", path=path, content=content)


def fails(how: Literal["provider", "error"] = "provider") -> ModelStep:
    """A model call that raises instead of replying."""
    return ModelStep(fail=how)


def cancel() -> EventStep:
    """Stop the run in flight."""
    return EventStep(what="cancel")


def queue_note(text: str) -> EventStep:
    """Queue a note in the session's inbox."""
    return EventStep(what="queue_note", text=text)


def turn(prompt: str, *steps: Step) -> Turn:
    """A turn: the prompt, then the model's script."""
    return Turn(prompt=prompt, steps=steps)


def resume(*steps: Step) -> Turn:
    """A turn that continues the last turn's interrupted run."""
    return Turn(resume=True, steps=steps)


def checkout(after_turn: int) -> Checkout:
    """Back to where the ``after_turn``-th turn ended."""
    return Checkout(after_turn=after_turn)


def fork(name: str, inherit: Literal["full", "fresh"] = "full") -> Fork:
    """Fork the session and carry on in the fork."""
    return Fork(name=name, inherit=inherit)
