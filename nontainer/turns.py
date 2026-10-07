"""A turn on a workspace, and what it streams.

**The turn.** ``ws.turn(run_id)`` opens one turn: the span in which one
run of a harness's loop drives the session. A workspace has one open at
a time, and ending it does what every ending needs, whatever the loop:
the run is stored (when the harness hands its body over), the inbox's
delivered notes are settled, and ONE commit lands, stamped with the run
and how it ended. A harness that owns its loop uses the context
manager; one built on hooks, whose start and end happen in different
callbacks, uses ``ws.turns.begin`` and :meth:`Turn.end`::

    with ws.turn(run_id, inbox=inbox, harness="mine") as turn:
        ...                               # the loop: model calls, tools
        turn.complete(body=run)           # or leave the block normally
    # cancelled (CancelledError) or failed (any other exception) on the
    # way out, and the exception goes on; turn.interrupt(message) for an
    # error the harness will resume from with ws.turn(run_id, resume=True)

    async with ws.turn(run_id) as turn:   # the same object, async
        ...

**What a turn streams.** A harness that owns its loop yields
turn events directly; one built on hooks (the agno adapter)
maps its own events onto them in its driver. Either way a consumer — a
UI, a transcript, the conformance runner — reads one shape whatever
loop produced it. Most events mark the contract's own moments: a run
starting and how it ended, a tool call and its result, notes delivered
on that result, a fold. The deltas and the usage report are here so
that a consumer can follow any loop the same way.

Every event is a frozen dataclass whose ``kind`` is its class name, so
a JSON reader can tell them apart without the class
(:func:`event_from_dict` is the reverse of ``dataclasses.asdict``).
Stored formats stay the harness's own: this is the live stream only.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Literal, Union

from . import conversation
from .conversation import Index
from .errors import NotSupportedError, TurnInProgress
from .inbox import Inbox, Note, NoteKind

if TYPE_CHECKING:
    from .workspace import Workspace

_logger = logging.getLogger(__name__)

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
    "Source",
    "Turn",
    "TurnEvent",
    "Turns",
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


# -- the turn -------------------------------------------------------------------

_KEEP: Any = object()

Source = Callable[[Inbox], "list[Note]"]
"""Something that adds notes at each delivery, given the turn's inbox:
:func:`nontainer.sessions.answer_notes` for a session's delegate
answers. Notes a source adds should be minted delivered
(``inbox.deliver_now``), so the turn's end settles them."""


class Turn:
    """One turn on a workspace, from :meth:`Turns.begin` to :meth:`end`.

    ``run_id`` names the run. A loop that learns the id only once the
    run has started opens the turn without one and binds it then
    (:meth:`bind`). ``inbox`` is the session's :class:`~nontainer.inbox.Inbox`,
    whose notes :meth:`deliver` hands the model and the end settles;
    ``sources`` add notes of their own at each delivery (a delegate's
    answer, say). ``harness`` names the harness, which storing a run
    body needs (see :mod:`nontainer.conversation`).

    A turn is a context manager, sync or async. Leaving the block
    normally completes it; a ``CancelledError`` (or any other exception
    that is not an ``Exception``) cancels it, any other exception fails
    it, and the exception goes on either way. A block that ended the
    turn itself (:meth:`complete`, :meth:`interrupt`, :meth:`end`) is
    left alone on the way out. The async form ends the turn on a worker
    thread, except for a cancel: the loop may be closing under it, so
    that one ends inline.
    """

    def __init__(
        self,
        turns: Turns,
        run_id: str | None,
        *,
        resume: bool,
        inbox: Inbox | None,
        harness: str | None,
        sources: Iterable[Source] = (),
    ) -> None:
        self._turns = turns
        self._run_id = run_id
        self.resume = resume
        self.inbox = inbox
        self.harness = harness
        self.sources: tuple[Source, ...] = tuple(sources)
        self.status: RunStatus | None = None
        """How the turn ended; ``None`` while it is open."""
        self.commit: str | None = None
        """The commit the end landed, if it landed one."""
        self._ending = False

    def __repr__(self) -> str:
        state = self.status or "open"
        return f"<turn {self._run_id or '(unnamed)'} on {self._ws.session!r}: {state}>"

    @property
    def _ws(self) -> Workspace:
        return self._turns._ws

    @property
    def run_id(self) -> str | None:
        return self._run_id

    @property
    def open(self) -> bool:
        return self.status is None

    def bind(self, run_id: str) -> None:
        """Name the run, once: for a loop that mints the id itself."""
        if self._run_id is not None and self._run_id != run_id:
            raise ValueError(
                f"this turn's run is {self._run_id!r}; it cannot become {run_id!r}"
            )
        self._run_id = run_id

    # -- delivery -----------------------------------------------------------

    def collect(self) -> list[Note]:
        """The notes to deliver on the tool result landing now: the
        inbox's queue, then what each source adds. They are delivered
        from here, not settled. Empty without an inbox.

        A source that raises is logged and skipped, so it costs neither
        the tool result nor the other notes; any notes it had minted
        before it raised go back to the queue for the next result.

        For a result that is not text (one carrying images, say), call
        this, attach the notes yourself (``inbox.render(notes)``), and
        ``inbox.restore(notes)`` if they cannot be attached; otherwise
        use :meth:`deliver`.
        """
        inbox = self.inbox
        if inbox is None:
            return []
        notes = inbox.drain()
        for source in self.sources:
            before = {note.id for note in inbox.delivered()}
            try:
                notes.extend(source(inbox))
            except Exception:  # noqa: BLE001 - the tool result wins
                _logger.warning(
                    "a delivery source raised; its notes wait for the next tool result",
                    exc_info=True,
                )
                stray = [n for n in inbox.delivered() if n.id not in before]
                if stray:
                    inbox.restore(stray)
        return notes

    def deliver(self, result: str) -> tuple[str, list[Note]]:
        """``result`` with the notes queued for it appended, and those
        notes; ``(result, [])`` when there are none.

        A tool result is where a running agent reads new text, so this
        is where notes queued mid-turn reach it: call it on each tool
        result before the model sees it. A tool that raised delivers
        nothing, so the notes wait for the next result. The notes are
        delivered, not settled: the turn's end settles them, and a loop
        that throws an attempt away puts them back with
        ``inbox.requeue()``. Notes that cannot be appended (a frame that
        raises) go back to the queue and the result goes on unchanged.

        The inbox's ``on_delivered`` hears of the notes; an awaitable it
        returns is discarded here, so use :meth:`adeliver` from a loop.
        """
        text, notes = self._attach(result)
        if notes and self.inbox is not None:
            self.inbox.announce(notes, allow_async=False)
        return text, notes

    async def adeliver(self, result: str) -> tuple[str, list[Note]]:
        """:meth:`deliver`, awaiting what ``on_delivered`` returns.

        A cancel that lands while it waits on the callback puts the notes
        back in the queue before it goes on: the result carrying them was
        never handed back, so the model has not read them, and the turn's
        end must not settle them.
        """
        text, notes = self._attach(result)
        if notes and self.inbox is not None:
            outcome = self.inbox.announce(notes, allow_async=True)
            if outcome is not None:
                try:
                    await outcome
                except Exception:  # noqa: BLE001 - the tool result wins
                    _logger.warning("inbox on_delivered raised", exc_info=True)
                except BaseException:
                    self.inbox.restore(notes)
                    raise
        return text, notes

    def _attach(self, result: str) -> tuple[str, list[Note]]:
        notes = self.collect()
        if not notes or self.inbox is None:
            return result, []
        try:
            return result + self.inbox.render(notes), notes
        except Exception:  # noqa: BLE001 - the tool result wins
            _logger.warning(
                "%d note(s) could not be appended to a tool result; they stay "
                "queued for the next one",
                len(notes),
                exc_info=True,
            )
            self.inbox.restore(notes)
            return result, []

    # -- ending -------------------------------------------------------------------

    def complete(self, *, body: Any = None, record: Any = _KEEP) -> str | None:
        """End the turn ``completed``. See :meth:`end`."""
        return self.end("completed", body=body, record=record)

    def interrupt(
        self, message: str, *, body: Any = None, record: Any = _KEEP
    ) -> str | None:
        """End the turn ``interrupted``: an error the harness will resume
        from, with ``ws.turn(run_id, resume=True)``. See :meth:`end`."""
        return self.end("interrupted", body=body, record=record, message=message)

    def end(
        self,
        status: RunStatus,
        *,
        body: Any = None,
        record: Any = _KEEP,
        message: str | None = None,
    ) -> str | None:
        """End the turn; the id of the commit that landed, or ``None``.

        In order: the run's ``body`` is stored under the run's id when
        one is given (and the harness's session ``record`` with it, or
        removed with ``None``), the run joining the conversation's index;
        the inbox's delivered notes are settled; and one commit lands
        with everything the turn left staged, stamped ``{"tool":
        "turn", "runs": {run_id: status}}`` (with ``"message"`` when one
        is given). Nothing is committed when nothing changed, or on a
        workspace that takes no commits.

        A cancelled or failed run should be kept with a closing note,
        so the model remembers the work it did before the cut. The note
        is in the harness's own format, so the harness adds it to the
        body (or its own storage) before ending.

        A status that is not a :data:`RunStatus` is refused before
        anything happens, and the turn stays open for a valid end. A turn
        ends once: a second end is refused, including one racing the
        first from another thread. Once the end has begun, the
        workspace's turn slot is released whatever happens, so an end
        that raises does not leave the workspace refusing turns. The
        slot is released before the workspace lock is, so whatever waits
        on that lock sees the turn gone.
        """
        if status not in RUN_STATUSES:
            raise ValueError(f"not a run status: {status!r}")
        with self._turns._lock:
            if self.status is not None:
                raise RuntimeError(f"this turn already ended {self.status!r}")
            if self._ending:
                raise RuntimeError("this turn already ended: another end is landing it")
            self._ending = True
        ws = self._ws
        with ws.lock:
            try:
                if body is not None or record is not _KEEP:
                    self._store(body, record)
                if self.inbox is not None:
                    self.inbox.settle()
                info: dict[str, Any] = {"tool": "turn"}
                if self._run_id is not None:
                    info["runs"] = {self._run_id: status}
                if message:
                    info["message"] = message
                if not ws.frozen and ws.caps.versioned and ws.uncommitted:
                    self.commit = ws.commit(info=info)
            finally:
                self.status = status
                self._turns._release(self)
        return self.commit

    def _store(self, body: Any, record: Any) -> None:
        if self._run_id is None:
            raise ValueError("storing a run needs its id: bind it first")
        kv = self._ws.provider.kv
        index = conversation.read_index(kv)
        harness = self.harness or (index.harness if index is not None else None)
        if harness is None:
            raise ValueError("storing a run needs the harness's name: harness=")
        if index is not None and index.harness != harness:
            raise NotSupportedError(
                f"this branch holds a conversation {index.harness!r} wrote; "
                f"{harness!r} cannot add a run to it"
            )
        if index is None:
            index = Index(harness=harness, session=self._ws.session)
        runs = index.runs
        if self._run_id not in runs:
            runs = (*runs, self._run_id)
        index = replace(index, harness=harness, session=self._ws.session, runs=runs)
        kwargs: dict[str, Any] = {}
        if record is not _KEEP:
            kwargs["record"] = record
        conversation.write(
            kv,
            index,
            runs={self._run_id: body} if body is not None else None,
            **kwargs,
        )

    # -- the context manager --------------------------------------------------

    def __enter__(self) -> Turn:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        if self.status is not None or self._ending:
            return False
        if exc_type is None:
            status: RunStatus = "completed"
            message = None
        elif not issubclass(exc_type, Exception):
            status, message = "cancelled", "cancelled"
        else:
            status, message = "failed", f"{exc_type.__name__}: {exc}"
        try:
            self.end(status, message=message)
        except Exception:
            if exc_type is None:
                raise
            # the block's own exception is the one to see
            _logger.warning("ending turn %s failed too", self._run_id, exc_info=True)
        return False

    async def __aenter__(self) -> Turn:
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        if self.status is not None or self._ending:
            return False
        if exc_type is not None and not issubclass(exc_type, Exception):
            return self.__exit__(exc_type, exc, tb)
        await asyncio.to_thread(self.__exit__, exc_type, exc, tb)
        return False


class Turns:
    """A workspace's turns (``ws.turns``): at most one open at a time."""

    def __init__(self, ws: Workspace) -> None:
        self._ws = ws
        self._lock = threading.Lock()
        self._current: Turn | None = None

    @property
    def current(self) -> Turn | None:
        """The open turn, or ``None``."""
        return self._current

    def begin(
        self,
        run_id: str | None = None,
        *,
        resume: bool = False,
        inbox: Inbox | None = None,
        harness: str | None = None,
        sources: Iterable[Source] = (),
    ) -> Turn:
        """Open a turn; see :class:`Turn`.

        Refused with :class:`~nontainer.TurnInProgress` while another
        turn is open on this workspace. ``resume`` continues the run
        ``run_id`` in place, which the conversation must hold: an
        interrupted run keeps its id and what it had recorded.
        """
        ws = self._ws
        ws._check_open()
        if resume:
            if run_id is None:
                raise ValueError("resuming a run names it: run_id=")
            index = conversation.read_index(ws.provider.kv)
            if index is None or run_id not in index.runs:
                raise ValueError(
                    f"session {ws.session!r} holds no run {run_id!r} to resume"
                )
        with self._lock:
            if self._current is not None:
                raise TurnInProgress(
                    f"turn {self._current.run_id or '(unnamed)'} is still open on "
                    f"session {ws.session!r}; end it before beginning another"
                )
            self._current = Turn(
                self,
                run_id,
                resume=resume,
                inbox=inbox,
                harness=harness,
                sources=sources,
            )
            return self._current

    def _release(self, turn: Turn) -> None:
        with self._lock:
            if self._current is turn:
                self._current = None
