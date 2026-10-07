"""What a turn streams: one vocabulary for every harness.

A harness that owns its loop yields these directly; one built on hooks
(the agno adapter) maps its own events onto them in its driver. Either
way a consumer — a UI, a transcript, the conformance runner — reads one
shape whatever loop produced it.

Most members mark the contract's own moments: a run starting and how
it ended, a tool call and its result, notes delivered on that result,
a fold. The deltas and the usage report are here so that a consumer
can follow any loop the same way.

Every event is a frozen dataclass whose ``kind`` is its class name, so
a JSON reader can tell them apart without the class
(:func:`event_from_dict` is the reverse of ``dataclasses.asdict``).
Stored formats stay the harness's own: this is the live stream only.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Union

from .inbox import NoteKind

__all__ = [
    "RUN_STATUSES",
    "Compacted",
    "Delivered",
    "DeliveredNote",
    "RunEnded",
    "RunStarted",
    "RunStatus",
    "TURN_EVENTS",
    "TextDelta",
    "ThinkingDelta",
    "ToolEnded",
    "ToolStarted",
    "TurnEvent",
    "Usage",
    "event_from_dict",
]

RunStatus = Literal["completed", "cancelled", "interrupted", "failed"]
"""How a run ended.

``completed`` ran to its end. ``cancelled`` was stopped from outside.
``interrupted`` hit an error the harness may resume from (a provider
failure): the run is kept as it stood, and resuming continues it in
place. ``failed`` hit any other error. A cancelled or failed run is
kept with a closing note, so the model remembers the work it did before
the cut."""

RUN_STATUSES: tuple[RunStatus, ...] = (
    "completed",
    "cancelled",
    "interrupted",
    "failed",
)


@dataclass(frozen=True, kw_only=True)
class RunStarted:
    """First, always. ``run_id`` is the handle a cancel names."""

    kind: Literal["RunStarted"] = "RunStarted"
    run_id: str


@dataclass(frozen=True, kw_only=True)
class TextDelta:
    """A piece of the model's reply, as it streams."""

    kind: Literal["TextDelta"] = "TextDelta"
    text: str


@dataclass(frozen=True, kw_only=True)
class ThinkingDelta:
    """A piece of the model's reasoning, as it streams."""

    kind: Literal["ThinkingDelta"] = "ThinkingDelta"
    text: str


@dataclass(frozen=True, kw_only=True)
class ToolStarted:
    """Before a tool runs. ``call_id`` is the provider's id for the
    call, and the matching :class:`ToolEnded` carries the same one."""

    kind: Literal["ToolStarted"] = "ToolStarted"
    call_id: str
    name: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, kw_only=True)
class DeliveredNote:
    """One note as it reached the model: the fields of
    :class:`~nontainer.inbox.Note` a reader needs, without the
    structured answer behind a delegate's note."""

    id: str
    text: str
    kind: NoteKind = "principal"
    label: str = ""
    job: str | None = None


@dataclass(frozen=True, kw_only=True)
class Delivered:
    """Notes that rode out on a tool result: queued messages, or
    delegate answers. Comes before that result's :class:`ToolEnded`."""

    kind: Literal["Delivered"] = "Delivered"
    notes: tuple[DeliveredNote, ...]


@dataclass(frozen=True, kw_only=True)
class ToolEnded:
    """After a tool returns. ``result`` is the tool's own output, with
    any delivered notes left out (they are the :class:`Delivered` event
    before this one). ``is_error`` says the call failed."""

    kind: Literal["ToolEnded"] = "ToolEnded"
    call_id: str
    name: str
    result: str = ""
    is_error: bool = False


@dataclass(frozen=True, kw_only=True)
class Usage:
    """Once per model call, where the harness reports it: the size of
    the request, and how much of it the provider read from its cache."""

    kind: Literal["Usage"] = "Usage"
    input_tokens: int
    cached_tokens: int = 0


@dataclass(frozen=True, kw_only=True)
class Compacted:
    """A new fold (:class:`~nontainer.compaction.Fold`): the turns up to
    message ``through`` are replaced by a summary in what the model is
    sent from here on."""

    kind: Literal["Compacted"] = "Compacted"
    through: str
    runs: int = 0
    first: str | None = None


@dataclass(frozen=True, kw_only=True)
class RunEnded:
    """Last, always: how the run ended, and why when it did not
    complete."""

    kind: Literal["RunEnded"] = "RunEnded"
    status: RunStatus
    message: str | None = None


TurnEvent = Union[
    RunStarted,
    TextDelta,
    ThinkingDelta,
    ToolStarted,
    Delivered,
    ToolEnded,
    Usage,
    Compacted,
    RunEnded,
]

TURN_EVENTS: tuple[type, ...] = (
    RunStarted,
    TextDelta,
    ThinkingDelta,
    ToolStarted,
    Delivered,
    ToolEnded,
    Usage,
    Compacted,
    RunEnded,
)


def event_from_dict(data: Mapping[str, Any]) -> TurnEvent:
    """The event a ``dataclasses.asdict`` of one reads back to, picked
    by its ``kind``. Raises ``ValueError`` for an unknown kind or a
    field that does not fit."""
    from .conformance.codec import load

    return load(TurnEvent, data)
