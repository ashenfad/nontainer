"""agno's stored runs, read into the neutral trace shape.

:class:`AgnoTraces` is the :class:`~nontainer.traces.TraceSource` for
conversations written by ``KvgitSessionDb``: each run under
``__conversation__/runs/<id>`` is an agno ``RunOutput`` as a dict. It
parses that dict and nothing else, so it needs no agno install: a
curator reads an agno session's trace without the ``[agno]`` extra.

What it reads from a run:

- **status**: agno's ``RunStatus`` in core's words (``ERROR`` is
  ``failed``, ``PENDING`` is ``running``); anything else lowercased.
- **times**: ``created_at`` starts the run, and ``created_at`` plus
  ``metrics.duration`` ends it, when both are there.
- **tokens**: ``metrics``. agno counts all five and drops a zero when it
  stores them, so with ``metrics`` present a missing count is ``0``;
  without it, every count is unknown.
- **the system prompt**: the first ``system`` (or ``developer``) message.
- **events**, from ``messages`` in order, leaving out any replayed from
  history: a ``user`` message; an ``assistant`` message's reasoning, its
  text, then one tool event per call it made, whose result is the
  matching ``tool`` message among those that follow it (call ids repeat
  across a run, so only the results right after a call answer it); and
  a ``tool`` message no call claims, on its own.

A tool event is an error when agno says so (``tool_call_error``). agno
sets that only when a tool function itself fails. nontainer's own tools
report a failed call in their text and return normally, so until the
toolkit records its failures in the run, their errors read as results.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from ..traces import TraceEvent, TraceRun, TraceTokens

__all__ = ["AgnoTraces"]

_STATUSES = {
    "COMPLETED": "completed",
    "RUNNING": "running",
    "PENDING": "running",
    "PAUSED": "paused",
    "CANCELLED": "cancelled",
    "ERROR": "failed",
}

_TOKENS = {
    "input": "input_tokens",
    "output": "output_tokens",
    "cache_read": "cache_read_tokens",
    "cache_write": "cache_write_tokens",
    "reasoning": "reasoning_tokens",
}


def _text(content: Any) -> str | None:
    """A message's content as text: a string as is, a list of parts by
    their text, anything else as JSON."""
    if content is None or isinstance(content, str):
        return content
    if isinstance(content, Sequence) and not isinstance(content, (bytes, bytearray)):
        texts = []
        for part in content:
            if isinstance(part, str):
                texts.append(part)
            elif isinstance(part, Mapping) and isinstance(part.get("text"), str):
                texts.append(part["text"])
        return "\n".join(texts) if texts else None
    try:
        return json.dumps(content, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(content)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _status(value: Any) -> str:
    raw = str(getattr(value, "value", value) or "running")
    return _STATUSES.get(raw.upper(), raw.lower())


def _tokens(metrics: Any) -> TraceTokens:
    if not isinstance(metrics, Mapping):
        return TraceTokens()
    counts = {}
    for ours, theirs in _TOKENS.items():
        n = metrics.get(theirs, 0)
        counts[ours] = n if isinstance(n, int) and not isinstance(n, bool) else None
    return TraceTokens(**counts)


def _args(call: Mapping[str, Any], result: Mapping[str, Any] | None) -> dict | None:
    """A call's arguments: the ones it sent, parsed, or else the ones
    its result recorded. Arguments that don't parse to an object are
    kept as the string they were, under ``arguments``."""
    raw = (call.get("function") or {}).get("arguments")
    if isinstance(raw, Mapping):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {"arguments": raw}
        return dict(parsed) if isinstance(parsed, Mapping) else {"arguments": raw}
    if result is not None and isinstance(result.get("tool_args"), Mapping):
        return dict(result["tool_args"])
    return None


def _tool_event(
    call: Mapping[str, Any] | None, result: Mapping[str, Any] | None
) -> TraceEvent:
    name = None
    if call is not None:
        name = (call.get("function") or {}).get("name")
    if name is None and result is not None:
        name = result.get("tool_name")
    if call is not None:
        args = _args(call, result)
    elif result is not None and isinstance(result.get("tool_args"), Mapping):
        args = dict(result["tool_args"])
    else:
        args = None
    return TraceEvent(
        "tool",
        None if result is None else (_text(result.get("content")) or ""),
        tool=None if name is None else str(name),
        args=args,
        error=bool(result is not None and result.get("tool_call_error") is True),
    )


class AgnoTraces:
    """The trace source for runs an agno ``Agent`` stored through
    ``KvgitSessionDb``. Register it under the harness name the index
    carries: ``Store(..., traces={"agno": AgnoTraces()})``."""

    def read(self, body: Any) -> TraceRun:
        if not isinstance(body, Mapping):
            raise ValueError(f"an agno run is an object, got {type(body).__name__}")
        run_id = body.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            raise ValueError(f"an agno run has no run_id: {run_id!r}")
        metrics = body.get("metrics")
        started = _number(body.get("created_at"))
        duration = (
            _number(metrics.get("duration")) if isinstance(metrics, Mapping) else None
        )
        ended = (
            started + duration if started is not None and duration is not None else None
        )

        messages = [
            m
            for m in body.get("messages") or ()
            if isinstance(m, Mapping) and not m.get("from_history")
        ]
        system = next(
            (
                _text(m.get("content"))
                for m in messages
                if m.get("role") in ("system", "developer")
            ),
            None,
        )
        model = body.get("model")
        return TraceRun(
            id=run_id,
            status=_status(body.get("status")),
            started=started,
            ended=ended,
            model=model if isinstance(model, str) else None,
            tokens=_tokens(metrics),
            system=system,
            events=tuple(_events(messages)),
        )


def _events(messages: list[Mapping[str, Any]]) -> list[TraceEvent]:
    events: list[TraceEvent] = []
    i = 0
    while i < len(messages):
        message = messages[i]
        role = message.get("role")
        i += 1
        if role == "user":
            events.append(TraceEvent("user", _text(message.get("content"))))
        elif role == "assistant":
            reasoning = _text(message.get("reasoning_content"))
            if reasoning:
                events.append(TraceEvent("thinking", reasoning))
            text = _text(message.get("content"))
            if text:
                events.append(TraceEvent("assistant", text))
            # the results of this message's calls: the tool messages right after it
            results: list[Mapping[str, Any]] = []
            while i < len(messages) and messages[i].get("role") == "tool":
                results.append(messages[i])
                i += 1
            unclaimed = list(results)
            for call in message.get("tool_calls") or ():
                if not isinstance(call, Mapping):
                    continue
                result = next(
                    (r for r in unclaimed if r.get("tool_call_id") == call.get("id")),
                    None,
                )
                if result is not None:
                    unclaimed.remove(result)
                events.append(_tool_event(call, result))
            events.extend(_tool_event(None, r) for r in unclaimed)
        elif role == "tool":
            events.append(_tool_event(None, message))
    return events
