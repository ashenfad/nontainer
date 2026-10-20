"""The trace corpus: what a rendered trace holds, pinned byte for byte.

A trace is text a model reads, rendered by :class:`nontainer.traces.Trace`
from the neutral shape every source returns. A port of the renderer
(agex-ts's world) is checked against this corpus rather than against a
reading of the Python. Each :class:`Case` is a conversation in the
neutral shape, with the session, commit and harness the trace names,
what each run changed (:class:`~nontainer.traces.TraceCommit` lists by
run id; a run not named has no known landing), and the ``since`` ref
and the runs new since it; :func:`record` renders it and keeps every
file, so a port renders the same runs and compares. Which commits are a
run's is pinned by the ws-git corpus, which reads them from history.

``python -m nontainer.conformance.export`` writes the recorded cases
under ``traces/json/``; ``tests/test_corpus_drift.py`` fails when they
drift from what this records.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ...traces import Trace, TraceCommit, TraceRun

__all__ = ["CASES", "Case", "dumps", "record"]


@dataclass(frozen=True)
class Case:
    """A conversation to render, and what to call it."""

    name: str
    about: str
    runs: tuple[TraceRun, ...]
    session: str = "analyst-42"
    commit: str = "a3f9c2e"
    harness: str = "neutral"
    changes: dict[str, tuple[TraceCommit, ...]] = field(
        default_factory=dict, hash=False
    )
    since: str | None = None
    new: tuple[str, ...] = ()


def record(case: Case) -> dict[str, Any]:
    """The case, as a port reads it, with every file it renders."""
    trace = Trace(
        case.runs,
        session=case.session,
        commit=case.commit,
        harness=case.harness,
        changes=case.changes,
        since=case.since,
        new=case.new,
    )
    return {
        "name": case.name,
        "about": case.about,
        "session": case.session,
        "commit": case.commit,
        "harness": case.harness,
        "runs": [r.to_dict() for r in case.runs],
        "changes": {
            rid: [c.to_dict() for c in commits] for rid, commits in case.changes.items()
        },
        "since": case.since,
        "new": sorted(case.new),
        "files": trace.files(),
    }


def dumps(recorded: dict[str, Any]) -> str:
    return json.dumps(recorded, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


from .cases import CASES  # noqa: E402 - the cases use Case
