"""The harness corpus: the scenarios that pin the harness contract.

Every harness that drives a nontainer session runs these: the agno
adapter in nontainer's own tests, other harnesses in theirs. The JSON
beside this package (``json/<name>.json``) is generated from the
modules here by ``python -m nontainer.conformance.export``.
"""

from __future__ import annotations

from ..corpus import Scenario
from . import compaction, conversation, endings, inbox

__all__ = ["SCENARIOS", "by_name"]

SCENARIOS: tuple[Scenario, ...] = (
    *endings.SCENARIOS,
    *conversation.SCENARIOS,
    *inbox.SCENARIOS,
    *compaction.SCENARIOS,
)


def by_name(name: str) -> Scenario:
    """The scenario called ``name``; ``KeyError`` when there is none."""
    for scenario in SCENARIOS:
        if scenario.name == name:
            return scenario
    raise KeyError(name)
