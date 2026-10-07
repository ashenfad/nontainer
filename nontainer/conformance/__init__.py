"""Conformance: scenarios that pin the harness contract, and the runner
that checks a harness against them.

- :mod:`.corpus`: the scenario format and its builders.
- :mod:`.harness`: the harness corpus (``SCENARIOS``), with its JSON
  under ``harness/json/``.
- :mod:`.runner`: :func:`run` a scenario on a :class:`Harness`, then
  :func:`check` what came of it.
- :mod:`.codec`: the format's JSON and JSON Schema, which other
  languages read (``schema/``).

The agno adapter's harness is
``nontainer.adapters.agno_conformance.AgnoHarness``.
"""

from __future__ import annotations

from .runner import (
    Clock,
    CommitView,
    Harness,
    HarnessSession,
    Observed,
    RunView,
    applies,
    check,
    event_names,
    run,
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
