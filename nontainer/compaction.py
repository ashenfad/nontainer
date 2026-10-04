"""Compaction: keeping a long conversation inside the model's window.

Past a token budget, every earlier turn of a conversation is replaced,
in what the model is sent, by one summary. The run in progress stays
as it is. The stored conversation is never rewritten and the person's
transcript keeps everything; only the model's view changes. Each fold
is recorded under ``__compaction__/`` in the workspace, so a rewind
takes it back with the turns it folded and a fork carries it with the
conversation. docs/compaction.md is the design.

This module is the harness-neutral half: the record and where it
lives, the policy, the texts, and the shrinking and chunking a fold
too large for one request needs. It knows ids, token counts and text,
and nothing about any harness. An adapter maps a harness's messages
onto it; ``nontainer.adapters.agno_compaction`` is agno's.

**Anchors.** A fold names the last message it covers by that message's
id in the harness (``Fold.through``), an opaque string here. A fold
whose anchor is not in the history an adapter is handed — an edit
unsaid that turn, or the conversation was dropped — is not in force,
and an earlier one whose anchor is still there takes its place. With
none, the full history is sent: a cut in the wrong place would hand
the model a summary of turns that, as far as the person can see,
never happened, while a request that is too long is visible and
corrects itself at the next fold.
"""

from __future__ import annotations

import math
import time
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

from .errors import WorkspaceError
from .planes import COMPACTION_PREFIX

if TYPE_CHECKING:
    from .workspace import Workspace

__all__ = [
    "ACK",
    "MARK",
    "Fold",
    "Item",
    "Policy",
    "chunks",
    "estimate_tokens",
    "folds",
    "in_force",
    "is_ours",
    "record",
    "reduce",
    "summary_message",
    "summary_request",
    "transcript",
]

FOLD_PREFIX = COMPACTION_PREFIX + "fold/"

#: The prefix on the id of every message an adapter inserts. It is how
#: an adapter recognises its own summary pair when it meets it again
#: (a harness may keep one message list across a run's model calls),
#: and how a session db knows to leave those messages out of a stored
#: run whatever the harness's own settings say.
MARK = "nontainer-compaction:"


def is_ours(message_id: Any) -> bool:
    """Whether ``message_id`` names a message compaction inserted."""
    return isinstance(message_id, str) and message_id.startswith(MARK)


# -- the record ------------------------------------------------------------------


@dataclass(frozen=True)
class Fold:
    """One fold: the earlier turns up to ``through``, replaced by
    ``summary`` in what the model is sent.

    ``first`` is ``None`` for a fold from the start of the conversation,
    which is every fold basic compaction makes: each covers what the one
    before it did and more. It is there for chaptering, a fold over a
    range in the middle, which is the same record with ``first`` set.
    """

    through: str
    """Id of the last message the fold covers, as the harness names it."""
    summary: str
    runs: int = 0
    """How many turns it covers, for a person reading the record."""
    tokens_before: int = 0
    """The size of the request that crossed the budget."""
    tokens_after: int = 0
    """The same request with the fold applied, estimated."""
    model: str = ""
    """What wrote the summary."""
    at: float = field(default_factory=time.time)
    first: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Fold:
        known = {k: data[k] for k in cls.__dataclass_fields__ if k in data}
        return cls(**known)


def _kv(ws: Workspace) -> Any:
    """The workspace's key-value plane, where the files, the cache and
    the stored conversation also live: one commit covers them all."""
    return ws.provider.kv


def folds(ws: Workspace) -> list[Fold]:
    """Every fold recorded in ``ws``, oldest first."""
    kv = _kv(ws)
    keys = sorted(
        k for k in list(kv.keys()) if isinstance(k, str) and k.startswith(FOLD_PREFIX)
    )
    out = []
    for key in keys:
        value = kv.get(key)
        if isinstance(value, dict):
            out.append(Fold.from_dict(value))
    return out


def record(ws: Workspace, fold: Fold) -> str:
    """Add ``fold`` to ``ws`` and return its key.

    One key per fold, never rewritten: the latest is the one in force,
    and the earlier ones say what the agent remembered at any commit.
    The write is left for the next commit to carry, which is the turn's
    own: a fold made during a turn lands with that turn, and a rewind to
    before it takes the fold away with it.
    """
    if ws.frozen:
        raise WorkspaceError("A frozen workspace cannot record a fold.")
    with ws.lock:
        kv = _kv(ws)
        n = sum(
            1
            for k in list(kv.keys())
            if isinstance(k, str) and k.startswith(FOLD_PREFIX)
        )
        key = f"{FOLD_PREFIX}{n + 1:06d}"
        kv[key] = fold.to_dict()
    return key


def in_force(
    ws_or_folds: Workspace | Sequence[Fold], ids: Iterable[str]
) -> Fold | None:
    """The fold to apply to a history whose message ids are ``ids``.

    The latest fold whose anchor is among them; ``None`` when no fold's
    is, and then the full history is sent (see the module docstring).
    """
    recorded = ws_or_folds if isinstance(ws_or_folds, Sequence) else folds(ws_or_folds)
    present = set(ids)
    for fold in reversed(recorded):
        if fold.through in present:
            return fold
    return None


# -- the policy ------------------------------------------------------------------


@dataclass(frozen=True)
class Policy:
    """When to fold, and whether a request fits at all.

    ``budget`` is the request size, in tokens, past which every earlier
    turn is folded. ``window`` is the model's context length, when the
    embedder knows it; a fold whose summary request would not fit in it
    is made from a reduced history instead (see ``reduce`` and
    ``chunks``). Without it, the reduced path is taken only once the
    provider has refused the plain request as too long.
    """

    budget: int
    window: int | None = None

    def __post_init__(self) -> None:
        if self.budget <= 0:
            raise ValueError(f"budget must be positive, got {self.budget}")
        if self.window is not None and self.window < self.budget:
            raise ValueError(
                f"window ({self.window}) is smaller than budget ({self.budget}): "
                "a fold would only happen after the provider refused a request"
            )

    def due(self, tokens: int) -> bool:
        return tokens >= self.budget

    def fits(self, tokens: int) -> bool:
        return self.window is None or tokens < self.window


def estimate_tokens(text: str) -> int:
    """About four characters a token: what is used for the part of a
    request the provider has not measured yet."""
    return math.ceil(len(text) / 4) if text else 0


# -- the texts -------------------------------------------------------------------

_WHAT_TO_KEEP = """\
Write it for yourself: it is what you will continue from, and the person \
should not have to repeat anything. Keep:
- the person's goals, requests and preferences, in their own words where \
the wording matters;
- decisions made, and why; what was tried and ruled out;
- what has been done: files written or changed (with paths), commands \
run and what came of them, what works and what does not yet;
- work handed to other agents, and what came back;
- the task in progress, open questions, and the next steps;
- anything the person asked you to remember.
Leave out routine tool output and plans that were superseded. If the \
conversation already begins with an earlier summary, carry forward what \
still matters from it: there is only ever one summary."""

_REQUEST = (
    """\
Your context is nearly full. Everything above this message is about to be \
replaced by a summary that you write now. After it you will have the \
summary, the latest message, and whatever you have done since that \
message, to work from.

"""
    + _WHAT_TO_KEEP
    + """

Do not call any tools. Reply with the summary alone, without a preamble."""
)

_FROM_TRANSCRIPT = (
    """\
Below is {what} of a conversation between a person and an AI agent \
working in a persistent workspace. Summarise it for that agent, which \
will continue from your summary in place of the conversation.

"""
    + _WHAT_TO_KEEP
    + """

Reply with the summary alone, without a preamble."""
)

ACK = "Understood. I'll continue from this summary."


def summary_request(
    transcript_text: str | None = None,
    *,
    prior: str | None = None,
    part: str | None = None,
) -> str:
    """The instruction that asks for a summary.

    With no ``transcript_text`` it is the cache-friendly request: sent
    after the agent's own messages, which the provider has already seen.
    With one, it is the reduced path's, where the conversation arrives
    as text: ``prior`` is the summary so far, when chunks are folded in
    one at a time, and ``part`` says which chunk this is.
    """
    if transcript_text is None:
        return _REQUEST
    what = "a transcript" if part is None else f"{part} of a transcript"
    text = _FROM_TRANSCRIPT.format(what=what)
    if prior:
        text += (
            "\n\nThe earlier part of the conversation is already summarised "
            "here; fold the transcript into it and reply with one summary "
            "covering both:\n\n<summary>\n" + prior + "\n</summary>"
        )
    return text + "\n\n<transcript>\n" + transcript_text + "\n</transcript>"


def summary_message(summary: str) -> str:
    """What the model is sent in place of the turns a fold covers."""
    return (
        "[The conversation before this point was compacted to fit the "
        "context window. This is the summary you wrote then. The person "
        "still sees the whole conversation.]\n\n" + summary
    )


# -- a fold too large for one request -----------------------------------------


@dataclass(frozen=True)
class Item:
    """One message of a history, as text: what the reduced path works on.

    ``kind`` is ``"tool_call"`` for an assistant's call (``text`` its
    name and arguments), ``"tool_result"`` for what came back, and
    ``"text"`` for everything else.
    """

    role: str
    text: str
    kind: str = "text"


def transcript(items: Iterable[Item]) -> str:
    """The items as one plain-text transcript."""
    lines = []
    for item in items:
        label = {"tool_call": "tool call", "tool_result": "tool result"}.get(
            item.kind, item.role
        )
        lines.append(f"[{label}]\n{item.text}")
    return "\n\n".join(lines)


def reduce(
    items: Iterable[Item], *, result_chars: int = 300, argument_chars: int = 2000
) -> list[Item]:
    """The items with tool output cut down: each tool result to a short
    placeholder that keeps its opening, and each long tool-call
    argument to its first ``argument_chars`` characters. What a person
    or agent said is kept whole."""
    out = []
    for item in items:
        if item.kind == "tool_result" and len(item.text) > result_chars:
            head = item.text[:result_chars].rstrip()
            text = f"[{len(item.text):,} characters; it began:]\n{head}\n[…]"
            out.append(Item(item.role, text, item.kind))
        elif item.kind == "tool_call" and len(item.text) > argument_chars:
            cut = len(item.text) - argument_chars
            text = item.text[:argument_chars] + f"\n[… {cut:,} more characters]"
            out.append(Item(item.role, text, item.kind))
        else:
            out.append(item)
    return out


def chunks(items: Sequence[Item], limit: int) -> list[list[Item]]:
    """``items`` in order, packed into lists each estimated under
    ``limit`` tokens, split only between items. An item over the limit
    on its own is cut to fit."""
    if limit <= 0:
        raise ValueError(f"limit must be positive, got {limit}")
    out: list[list[Item]] = []
    current: list[Item] = []
    size = 0
    for item in items:
        cost = estimate_tokens(item.text) + 4
        if cost > limit:
            keep = max(0, (limit - 4) * 4 - 40)
            item = Item(item.role, item.text[:keep] + "\n[… cut to fit]", item.kind)
            cost = estimate_tokens(item.text) + 4
        if current and size + cost > limit:
            out.append(current)
            current, size = [], 0
        current.append(item)
        size += cost
    if current:
        out.append(current)
    return out
