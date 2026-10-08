"""The stored conversation, in any harness's format.

A session's branch can hold the conversation that produced its files,
written by whatever harness drove it, in the same commit as those
files. That is what makes a checkout rewind memory with the files and a
fork carry it (see ``docs/agno-sessions.md`` for the agno adapter's
side). Core never reads a harness's format. It reads this module's
index, which says which harness wrote the conversation, which session
it belongs to, its runs in order, and the session it was forked from.
That is all forking, deleting and lineage need.

The plane (``planes.CONVERSATION_PREFIX``)::

    __conversation__/index        core's Index, as a dict
    __conversation__/record       the harness's own session record (opaque)
    __conversation__/runs/<id>    one run each, in the harness's format

**The legacy plane.** Before this module, the agno adapter kept the
conversation under ``__agno__/``: its session record (``run_ids``,
``session_id``, lineage in ``session_data``) and one key per run. A head
without an index is read from there. The first write migrates it: the
runs and the record move to this plane in the write's own commit, and
the legacy keys go, so a head never holds both. History is append-only,
so older commits keep the legacy plane, and a checkout or fork of one
brings it back into a live head; it is read the same way and migrates on
its next write.

Every function here takes the mapping the keys live in: a workspace's
``provider.kv``, a frozen snapshot, or a forked provider's ``kv``.
Writes are staged; committing is the caller's.
"""

from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from .planes import (
    COMPACTION_PREFIX,
    CONVERSATION_INDEX_KEY,
    CONVERSATION_PREFIX,
    CONVERSATION_RECORD_KEY,
    CONVERSATION_RUN_PREFIX,
    LEGACY_CONVERSATION_PREFIX,
    LEGACY_RUN_PREFIX,
    LEGACY_SESSION_KEY,
)

if TYPE_CHECKING:
    from .workspace import Workspace

__all__ = [
    "FORMAT",
    "Index",
    "clear",
    "index_at",
    "index_of",
    "read_index",
    "read_record",
    "read_runs",
    "rebind",
    "wipe",
    "write",
]

FORMAT = 1
"""The index's own format. Bumped only if the index's shape changes."""

_LEGACY_HARNESS = "agno"


@dataclass(frozen=True)
class Index:
    """What core knows about a stored conversation."""

    harness: str
    """Which harness wrote it (``"agno"``, ``"agex"``): the one that can
    read its record and runs."""

    session: str | None
    """The session the conversation belongs to. A fork rebinds it."""

    runs: tuple[str, ...] = ()
    """The run ids, in order."""

    forked_from: str | None = None
    """The session this conversation was forked from, if it was."""

    legacy: bool = field(default=False, compare=False)
    """Read from the legacy ``__agno__/`` plane; not stored."""

    def __post_init__(self) -> None:
        object.__setattr__(self, "runs", tuple(str(r) for r in self.runs))

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": FORMAT,
            "harness": self.harness,
            "session": self.session,
            "runs": list(self.runs),
            "forked_from": self.forked_from,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Index:
        return cls(
            harness=str(data.get("harness") or ""),
            session=data.get("session"),
            runs=tuple(data.get("runs") or ()),
            forked_from=data.get("forked_from"),
        )


def _get_many(kv: Any, keys: list[str]) -> dict[str, Any]:
    """The values of those ``keys`` that ``kv`` holds, in one batched
    read where the mapping offers ``get_many``."""
    if not keys:
        return {}
    batch = getattr(kv, "get_many", None)
    if batch is not None:
        return dict(batch(*keys))
    found = {}
    for key in keys:
        value = kv.get(key)
        if value is not None:
            found[key] = value
    return found


def _from_legacy(record: Mapping[str, Any]) -> Index:
    lineage = (record.get("session_data") or {}).get("forked_from_session_id")
    return Index(
        harness=_LEGACY_HARNESS,
        session=record.get("session_id"),
        runs=tuple(record.get("run_ids") or ()),
        forked_from=lineage,
        legacy=True,
    )


def read_index(kv: Any) -> Index | None:
    """The conversation's index, or ``None`` where no conversation is
    stored. A head without an index is read from the legacy plane."""
    data = kv.get(CONVERSATION_INDEX_KEY)
    if isinstance(data, Mapping):
        return Index.from_dict(data)
    legacy = kv.get(LEGACY_SESSION_KEY)
    if isinstance(legacy, Mapping):
        return _from_legacy(legacy)
    return None


def index_of(ws: Workspace) -> Index | None:
    """``read_index`` of a workspace's own state, staged writes included."""
    return read_index(ws.provider.kv)


def index_at(provider: Any, commit: str) -> Index | None:
    """``read_index`` of one commit in ``provider``'s history: the
    conversation as it stood there, whichever plane it was on."""
    data = provider.key_at(commit, CONVERSATION_INDEX_KEY)
    if isinstance(data, Mapping):
        return Index.from_dict(data)
    legacy = provider.key_at(commit, LEGACY_SESSION_KEY)
    if isinstance(legacy, Mapping):
        return _from_legacy(legacy)
    return None


def _legacy(kv: Any, index: Index | None) -> bool:
    """Whether the conversation is on the legacy plane: from ``index``
    when the caller has read it already, which saves the reads."""
    if index is not None:
        return index.legacy
    return (
        kv.get(CONVERSATION_INDEX_KEY) is None
        and kv.get(LEGACY_SESSION_KEY) is not None
    )


def read_record(kv: Any, index: Index | None = None) -> Any:
    """The harness's own session record, or ``None``. On the legacy
    plane it is the agno session dict without its ``run_ids``, which
    the index carries. A copy: the caller may rewrite it in place. Pass
    the ``index`` already read to skip reading it again."""
    if _legacy(kv, index):
        record = kv.get(LEGACY_SESSION_KEY)
        if isinstance(record, Mapping):
            record = {k: v for k, v in record.items() if k != "run_ids"}
    else:
        record = kv.get(CONVERSATION_RECORD_KEY)
    return copy.deepcopy(record)


def read_runs(
    kv: Any, run_ids: Iterable[str], index: Index | None = None
) -> dict[str, Any]:
    """The stored runs among ``run_ids``, by id, as copies: one batched
    read, from whichever plane holds the conversation. An id with no
    stored run is absent from the answer. Pass the ``index`` already
    read to skip reading it again."""
    prefix = LEGACY_RUN_PREFIX if _legacy(kv, index) else CONVERSATION_RUN_PREFIX
    ids = [str(r) for r in run_ids]
    found = _get_many(kv, [prefix + rid for rid in ids])
    return {
        rid: copy.deepcopy(found[prefix + rid]) for rid in ids if prefix + rid in found
    }


_KEEP: Any = object()


def _keys_under(kv: Any, *prefixes: str) -> list[str]:
    return [k for k in list(kv.keys()) if isinstance(k, str) and k.startswith(prefixes)]


def write(
    kv: Any,
    index: Index,
    *,
    record: Any = _KEEP,
    runs: Mapping[str, Any] | None = None,
    drop: Iterable[str] = (),
) -> None:
    """Store ``index``, with ``record`` when given (``None`` removes it),
    the ``runs`` by id, and without the runs named in ``drop``. Staged.

    On a head still on the legacy plane, this is the migration: every
    run the new index keeps that the write does not supply is carried
    over, the legacy record becomes the record unless one is given, and
    every legacy key is removed, in this same write.
    """
    runs = dict(runs or {})
    dropped = {str(r) for r in drop}
    legacy = kv.get(LEGACY_SESSION_KEY)
    if legacy is not None:
        carry = [rid for rid in index.runs if rid not in runs and rid not in dropped]
        found = _get_many(kv, [LEGACY_RUN_PREFIX + rid for rid in carry])
        for rid in carry:
            value = found.get(LEGACY_RUN_PREFIX + rid)
            if value is not None:
                runs[rid] = value
        if record is _KEEP and isinstance(legacy, Mapping):
            record = {k: v for k, v in legacy.items() if k != "run_ids"}
        for key in _keys_under(kv, LEGACY_CONVERSATION_PREFIX):
            del kv[key]
    for rid in dropped:
        key = CONVERSATION_RUN_PREFIX + rid
        if kv.get(key) is not None:
            del kv[key]
    for rid, body in runs.items():
        kv[CONVERSATION_RUN_PREFIX + str(rid)] = body
    if record is None:
        if kv.get(CONVERSATION_RECORD_KEY) is not None:
            del kv[CONVERSATION_RECORD_KEY]
    elif record is not _KEEP:
        kv[CONVERSATION_RECORD_KEY] = record
    kv[CONVERSATION_INDEX_KEY] = index.to_dict()


def rebind(kv: Any, child: str, *, parent: str) -> bool:
    """Make an inherited conversation the CHILD's own, for a fork that
    carries it; whether anything was written.

    A branch holds one session's conversation, and a fork is a new
    session, so the index is rebound to ``child`` and records where it
    came from. That is the session the index names (a fork from another
    session's commit carries THAT session's conversation), or
    ``parent`` when it names none. The harness's record is untouched:
    the index is what says whose conversation it is.
    """
    index = read_index(kv)
    if index is None or index.session == child:
        return False
    write(kv, replace(index, session=child, forked_from=index.session or parent))
    return True


def reclaim(kv: Any, session: str, *, lineage: str | None = None) -> bool:
    """Make a restored conversation that names another session this
    SESSION's own; whether anything was written.

    A fork's history begins with its parent's commits, so restoring one
    of them restores an index naming the parent, and a branch holds one
    session's conversation. The index is rebound as :func:`rebind`
    rebinds a fork's: ``forked_from`` is ``lineage``, the session this
    one records being forked from, or else the session the restored
    index names.
    """
    index = read_index(kv)
    if index is None or index.session in (None, session):
        return False
    write(kv, replace(index, session=session, forked_from=lineage or index.session))
    return True


def wipe(kv: Any) -> bool:
    """Remove the conversation and compaction's folds over it, from both
    planes, for a fork that starts fresh; whether anything was there."""
    keys = _keys_under(
        kv, CONVERSATION_PREFIX, LEGACY_CONVERSATION_PREFIX, COMPACTION_PREFIX
    )
    for key in keys:
        del kv[key]
    return bool(keys)


def clear(kv: Any) -> bool:
    """Remove the conversation from both planes, leaving compaction's
    folds (a fold whose anchor is gone simply stops applying); whether
    anything was there."""
    keys = _keys_under(kv, CONVERSATION_PREFIX, LEGACY_CONVERSATION_PREFIX)
    for key in keys:
        del kv[key]
    return bool(keys)
