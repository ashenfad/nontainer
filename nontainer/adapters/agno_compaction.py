"""Compaction for agno (``[agno]`` extra, agno 3.0 or later).

Wire it as the agent's compression manager::

    from nontainer.compaction import Policy
    from nontainer.adapters.agno_compaction import CompactingCompression

    agent = Agent(
        ...,
        db=db,
        add_history_to_context=True,
        num_history_runs=sys.maxsize,     # every earlier run; agno's default is 3
        compression_manager=CompactingCompression(ws, Policy(budget=150_000)),
    )

agno calls a compression manager before every model call with the
full list of messages that call will send. This one never compresses
a tool result. It splices the fold in force over the earlier runs'
messages, and when the request is still over budget it folds every
earlier run into a new summary first. docs/compaction.md is the design;
``nontainer.compaction`` is the harness-neutral half.

What it relies on, pinned by tests/test_agno_compaction_seam.py:

- the list is the request's own, and an edit made to it in place is
  what the model is sent, for the rest of the run;
- earlier runs arrive as copies flagged ``from_history``, which agno
  leaves out of the run it stores. That is only so while the agent's
  ``store_history_messages`` is off, its default, so the summary pair
  also carries ids marked as compaction's, and nontainer's session dbs
  drop marked messages from a run before writing it;
- every assistant message carries the input tokens of the call that
  produced it, history copies included, which is how a request is
  measured without a tokenizer.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING, Any, Optional

try:
    _agno_version = version("agno")
except PackageNotFoundError:  # pragma: no cover - an unpackaged agno
    _agno_version = "0"
if int(_agno_version.split(".")[0]) < 3:
    raise ImportError(
        f"nontainer.adapters.agno_compaction needs agno 3.0 or later "
        f"(installed: {_agno_version}): it rides on agno's compression "
        "manager, which earlier releases lack or call differently."
    )

from agno.compression.manager import CompressionManager  # noqa: E402
from agno.models.message import Message  # noqa: E402

from ..compaction import (  # noqa: E402
    ACK,
    MARK,
    Fold,
    Item,
    Policy,
    chunks,
    estimate_tokens,
    folds,
    in_force,
    is_ours,
    record,
    reduce,
    summary_message,
    summary_request,
    transcript,
)

if TYPE_CHECKING:
    from ..workspace import Workspace

log = logging.getLogger(__name__)

__all__ = ["CompactingCompression"]

_SUMMARY_ID = MARK + "summary:"
_ACK_ID = MARK + "ack:"


class CompactingCompression(CompressionManager):
    """agno's compression manager, used to fold earlier runs.

    ``on_fold`` is called with each new ``Fold`` once it is recorded,
    which is the embedder's moment to show it (the studio puts a marker
    in the transcript).

    ``summary_model``, when given, writes summaries instead of the
    agent's own model. It is sent the conversation as a transcript, so
    it never benefits from the prompt cache the agent's model already
    holds; the default, the agent's own model sent the same messages
    and the same tools, usually costs less for a long history.
    """

    def __init__(
        self,
        ws: Workspace,
        policy: Policy,
        *,
        on_fold: Callable[[Fold], Any] | None = None,
        summary_model: Any = None,
    ) -> None:
        # True is agno's gate: it calls a manager only when this is
        # set. Nothing here compresses a tool result.
        super().__init__(compress_tool_results=True, compress_token_limit=policy.budget)
        self.ws = ws
        self.policy = policy
        self.on_fold = on_fold
        self.summary_model = summary_model
        self._tools: Any = None
        self._agent_model: Any = None

    # -- agno's calls --------------------------------------------------------

    def should_compress(
        self,
        messages: list[Message],
        tools: Optional[list] = None,
        model: Any = None,
        response_format: Any = None,
    ) -> bool:
        # `compress` is not handed the tools or the model, and the
        # summary request needs both to match the agent's own.
        self._tools = tools
        self._agent_model = model or self.model
        return any(m.from_history and m.role != "system" for m in messages)

    async def ashould_compress(
        self,
        messages: list[Message],
        tools: Optional[list] = None,
        model: Any = None,
        response_format: Any = None,
    ) -> bool:
        return self.should_compress(messages, tools, model, response_format)

    def compress(self, messages: list[Message], run_metrics: Any = None) -> None:
        try:
            self._compact(messages)
        except Exception:  # never fail the agent's own call over this
            log.exception("compaction failed; sending the request uncompacted")

    async def acompress(self, messages: list[Message], run_metrics: Any = None) -> None:
        # One implementation: the summary call goes through the model's
        # sync path in a worker thread, so the server's loop keeps
        # moving while it runs. The list is not touched by anything
        # else meanwhile; the run is waiting on this call. Recording a
        # fold takes the workspace lock from that thread, as the
        # workspace's own tools do from theirs, so the caller must not
        # hold it across a run (it could not run a tool either).
        await asyncio.to_thread(self.compress, messages, run_metrics)

    # -- the work ------------------------------------------------------------

    def _compact(self, messages: list[Message]) -> None:
        tokens = _measure(messages, self._tools)
        before = _size(messages)
        self._apply(messages)
        tokens -= before - _size(messages)
        if not self.policy.due(tokens):
            return
        span = _history(messages)
        if not any(not is_ours(messages[i].id) for i in span):
            return  # the run in progress alone is over budget; see the docs
        self._fold(messages, span, tokens)

    def _apply(self, messages: list[Message]) -> None:
        """Splice the fold in force over the messages it covers.

        Applying is idempotent. The list persists across a run's calls,
        so the pair spliced in before the first is still there before
        the second; its id names the anchor of the fold it stands for,
        and a fold already applied is left alone.
        """
        span = _history(messages)
        if not span:
            return
        applied = _applied_anchor(messages, span)
        present = {messages[i].id for i in span if not is_ours(messages[i].id)}
        if applied is not None:
            present.add(applied)
        fold = in_force(folds(self.ws), present)
        if fold is None or fold.through == applied:
            return
        end = next(i for i in span if messages[i].id == fold.through)
        messages[span[0] : end + 1] = _pair(fold)

    def _fold(self, messages: list[Message], span: list[int], tokens: int) -> None:
        """Fold every earlier run into a new summary, record it, and
        splice it in."""
        start, end = span[0], span[-1]
        while is_ours(messages[end].id):
            end -= 1
        through = messages[end].id
        if not through:
            return  # nothing to anchor to; leave the request as it is
        prefix = messages[: end + 1]
        summary, wrote = self._summarize(prefix)
        if not summary:
            return
        # every turn the summary covers: those folded now, plus those the
        # summary it replaces already covered
        covered = [m for m in messages[start : end + 1] if not is_ours(m.id)]
        applied = _applied_anchor(messages, span)
        earlier = next(
            (f.runs for f in reversed(folds(self.ws)) if f.through == applied), 0
        )
        fold = Fold(
            through=through,
            summary=summary,
            runs=earlier + sum(1 for m in covered if m.role == "user"),
            tokens_before=tokens,
            model=wrote,
        )
        pair = _pair(fold)
        after = tokens - _size(messages[start : end + 1]) + _size(pair)
        fold = Fold(**{**fold.to_dict(), "tokens_after": max(after, 0)})
        record(self.ws, fold)
        messages[start : end + 1] = _pair(fold)
        if self.on_fold is not None:
            try:
                self.on_fold(fold)
            except Exception:
                log.exception("on_fold raised; the fold stands")

    def _summarize(self, prefix: list[Message]) -> tuple[str, str]:
        """The summary of ``prefix``, and the name of the model that
        wrote it; an empty summary when none could be written."""
        model = self._agent_model or self.model
        if self.summary_model is None and model is not None:
            request = list(prefix) + [Message(role="user", content=summary_request())]
            needed = _measure(prefix, self._tools) + estimate_tokens(summary_request())
            if self.policy.fits(needed):
                try:
                    reply = model.response(
                        request, tools=self._tools, tool_choice="none"
                    )
                    text = _text(reply)
                    if text:
                        return text, _name(model)
                except Exception as exc:
                    log.warning(
                        "summary request failed (%s); summarising a reduced history",
                        exc,
                    )
        writer = self.summary_model or model
        if writer is None:
            return "", ""
        return self._summarize_reduced(prefix, writer), _name(writer)

    def _summarize_reduced(self, prefix: list[Message], writer: Any) -> str:
        """The fallback: the history as a transcript with tool output cut
        down, and in chunks when even that is too large, each folded
        into the summary so far."""
        items = reduce(_items(prefix))
        room = (self.policy.window or self.policy.budget) // 2
        parts = chunks(items, room)
        summary: str | None = None
        for n, part in enumerate(parts, 1):
            label = None if len(parts) == 1 else f"part {n} of {len(parts)}"
            text = summary_request(transcript(part), prior=summary, part=label)
            try:
                reply = writer.response([Message(role="user", content=text)])
            except Exception as exc:
                log.warning("reduced summary failed (%s); not folding", exc)
                return ""
            summary = _text(reply) or summary
        return summary or ""


# -- helpers -----------------------------------------------------------------------


def _history(messages: list[Message]) -> list[int]:
    """Indexes of the earlier runs' messages, compaction's own pair
    included: what a fold may cover."""
    return [i for i, m in enumerate(messages) if m.from_history and m.role != "system"]


def _applied_anchor(messages: list[Message], span: list[int]) -> str | None:
    for i in span:
        mid = messages[i].id
        if isinstance(mid, str) and mid.startswith(_SUMMARY_ID):
            return mid[len(_SUMMARY_ID) :]
    return None


def _pair(fold: Fold) -> list[Message]:
    return [
        Message(
            role="user",
            content=summary_message(fold.summary),
            from_history=True,
            id=_SUMMARY_ID + fold.through,
        ),
        Message(
            role="assistant", content=ACK, from_history=True, id=_ACK_ID + fold.through
        ),
    ]


def _content(message: Message) -> str:
    parts = [] if message.content is None else [str(message.content)]
    for call in message.tool_calls or []:
        parts.append(json.dumps(call, default=str))
    return "\n".join(parts)


def _size(messages: list[Message]) -> int:
    return sum(estimate_tokens(_content(m)) + 4 for m in messages)


def _measure(messages: list[Message], tools: Any) -> int:
    """The request's size in tokens: what the provider reported for the
    latest call that produced one of these messages, plus an estimate
    for everything after it; all estimated when nothing was reported."""
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        metrics = getattr(m, "metrics", None)
        reported = (
            getattr(metrics, "input_tokens", None) if metrics is not None else None
        )
        if m.role == "assistant" and reported:
            # the reply itself is input to the next call, as is all after it
            return int(reported) + _size(messages[i:])
    tool_text = json.dumps(tools, default=str) if tools else ""
    return _size(messages) + estimate_tokens(tool_text)


def _items(messages: list[Message]) -> list[Item]:
    out = []
    for m in messages:
        if m.role == "system":
            continue
        if m.content is not None and str(m.content):
            kind = "tool_result" if m.role == "tool" else "text"
            out.append(Item(m.role, str(m.content), kind))
        for call in m.tool_calls or []:
            out.append(Item(m.role, json.dumps(call, default=str), "tool_call"))
    return out


def _text(reply: Any) -> str:
    content = getattr(reply, "content", None)
    return content.strip() if isinstance(content, str) else ""


def _name(model: Any) -> str:
    return str(getattr(model, "id", None) or type(model).__name__)
