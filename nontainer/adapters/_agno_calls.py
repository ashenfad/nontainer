"""Which of a workspace's tool calls failed, kept for agno's stored runs.

agno marks a tool call failed (``tool_call_error``) only when the tool
function itself fails. nontainer's tools return normally and say in
their text that a call failed (a non-zero exit, python that raised, an
edit that missed), so ``ToolOutput.is_error`` never reached agno's
record, and a trace counted none of those failures. agno has no field
that carries a flag from a tool's result into the message it stores.

So the toolkit keeps a log per workspace. Every call through
``WorkspaceTools`` notes its call id and tool name (agno hands an
entrypoint its ``FunctionCall`` as ``fc``) and whether it failed. ``KvgitSessionDb``
reads the log of the workspace it writes, and flags the failed calls'
tool messages, and their ``tools`` entries, before it stores a run.
Both dbs write through the live workspace the toolkit's tools use, so
the log is found by that object.

**Matching.** Only the newest run in a write takes flags from the log:
agno hands back the session's earlier runs with every write, and their
call ids can recur in the newest one. A finished run closes the log
(:meth:`CallLog.close`), so the calls logged since are the newest run's.
A call is known by its id and its tool's name: the log holds only the
toolkit's calls, and an agent's tool names are distinct, so another
toolkit's call never takes one of its outcomes. A model may reuse an id
from one reply to the next (numbering each reply's calls from zero).
The calls of one reply have distinct ids, and replies run one after
another, so the run's occurrences of a call are the latest logged under
it, in order. Messages replayed from history (``from_history``) belong
to earlier runs and take nothing from the log.

**Keeping a flag.** agno's in-memory run never carries the flag, so a
later write of the same session hands back a stored run unflagged. A
tool message already stored as failed stays failed: flags are matched
by the message's own ``id`` against what is stored.
"""

from __future__ import annotations

import threading
import weakref
from collections import deque
from collections.abc import Mapping
from typing import Any

__all__ = ["OPEN", "CallLog", "flag_failures", "log_of", "note"]

_LIMIT = 4096
"""Calls a log keeps. Matching reads only the latest under each id, so
a run of fewer calls than this is always matched in full."""


class CallLog:
    """One workspace's tool calls, oldest first: each call id with
    its tool's name and whether the call failed, since the last run
    finished."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._calls: deque[tuple[tuple[str, str], bool]] = deque(maxlen=_LIMIT)

    def note(self, call_id: str, tool: str, failed: bool) -> None:
        with self._lock:
            self._calls.append(((call_id, tool), failed))

    def close(self) -> None:
        """A run finished: what it logged is stored, and none of it is
        the next run's."""
        with self._lock:
            self._calls.clear()

    def latest(self, key: tuple[str, str], n: int) -> list[bool]:
        """The outcomes of the latest ``n`` calls under ``key``, a call
        id and a tool name, oldest first; fewer when fewer were logged."""
        with self._lock:
            found = [failed for k, failed in self._calls if k == key]
        return found[-n:] if n > 0 else []


OPEN = frozenset({"RUNNING", "PENDING"})
"""agno's statuses for a run that has not finished: a checkpoint. Any
other status written for the newest run closes the log."""

_LOGS: weakref.WeakKeyDictionary[Any, CallLog] = weakref.WeakKeyDictionary()
_LOGS_LOCK = threading.Lock()


def note(workspace: Any, fc: Any, failed: bool) -> None:
    """Log one call through the toolkit. ``fc`` is the ``FunctionCall``
    agno handed the entrypoint; a call without one (an embedder calling
    the function directly) is not logged."""
    call_id = getattr(fc, "call_id", None)
    tool = getattr(getattr(fc, "function", None), "name", None)
    if call_id is None or tool is None:
        return
    with _LOGS_LOCK:
        log = _LOGS.get(workspace)
        if log is None:
            log = _LOGS[workspace] = CallLog()
    log.note(str(call_id), str(tool), bool(failed))


def log_of(workspace: Any) -> CallLog | None:
    """The workspace's log, if a toolkit has logged a call on it."""
    with _LOGS_LOCK:
        return _LOGS.get(workspace)


def _tool_messages(run: Mapping[str, Any]) -> list[tuple[int, Mapping[str, Any]]]:
    return [
        (i, m)
        for i, m in enumerate(run.get("messages") or ())
        if isinstance(m, Mapping) and m.get("role") == "tool"
    ]


def _by_call(
    items: list[tuple[int, Mapping[str, Any]]],
) -> dict[tuple[str, str], list[int]]:
    """Positions grouped by call id and tool name, in order."""
    out: dict[tuple[str, str], list[int]] = {}
    for i, item in items:
        call_id, tool = item.get("tool_call_id"), item.get("tool_name")
        if call_id is not None and tool is not None:
            out.setdefault((str(call_id), str(tool)), []).append(i)
    return out


def _aligned(positions: list[int], outcomes: list[bool]) -> dict[int, bool]:
    """``outcomes`` matched to ``positions`` from the end of both: the
    latest calls are the latest occurrences."""
    n = min(len(positions), len(outcomes))
    if n == 0:
        return {}
    return dict(zip(positions[-n:], outcomes[-n:]))


def flag_failures(
    run: dict[str, Any], held: Mapping[str, Any] | None, log: CallLog | None
) -> dict[str, Any]:
    """``run`` with ``tool_call_error`` set on each tool message, and
    each ``tools`` entry, of a call that failed: one ``log`` says
    failed, or one whose message ``held``, the stored run, already
    flags. Pass ``log`` only for the newest run of a write. Never clears
    a flag agno set. ``run`` itself when nothing changes."""
    messages = _tool_messages(run)
    stored = {
        m.get("id")
        for _, m in _tool_messages(held or {})
        if m.get("tool_call_error") is True and m.get("id") is not None
    }
    failed_messages: set[int] = {i for i, m in messages if m.get("id") in stored}
    failed_by_call: dict[tuple[str, str], list[bool]] = {}
    own = [(i, m) for i, m in messages if not m.get("from_history")]
    for key, positions in _by_call(own).items():
        logged = log.latest(key, len(positions)) if log is not None else []
        flags = _aligned(positions, logged)
        failed_messages |= {i for i, failed in flags.items() if failed}
        failed_by_call[key] = [i in failed_messages for i in positions]

    tools = run.get("tools") or []
    failed_tools: set[int] = set()
    tool_ids = _by_call([(i, t) for i, t in enumerate(tools) if isinstance(t, Mapping)])
    for key, positions in tool_ids.items():
        flags = _aligned(positions, failed_by_call.get(key, []))
        failed_tools |= {i for i, failed in flags.items() if failed}

    def unflagged(items: Any, indices: set[int]) -> set[int]:
        return {i for i in indices if items[i].get("tool_call_error") is not True}

    all_messages = run.get("messages") or []
    to_message = unflagged(all_messages, failed_messages)
    to_tool = unflagged(tools, failed_tools)
    if not to_message and not to_tool:
        return run
    out = dict(run)
    if to_message:
        out["messages"] = [
            {**m, "tool_call_error": True} if i in to_message else m
            for i, m in enumerate(all_messages)
        ]
    if to_tool:
        out["tools"] = [
            {**t, "tool_call_error": True} if i in to_tool else t
            for i, t in enumerate(tools)
        ]
    return out
