"""The agno adapter against the harness corpus.

Every scenario the adapter has the capabilities for runs on it. Each
check passes, except the ones ``AgnoHarness.known_gaps`` names for that
scenario, and those must still fail: a gap that closes is taken off the
list rather than left to hide a regression.
"""

import pytest

pytest.importorskip("agno")

from nontainer.adapters.agno_conformance import AgnoHarness  # noqa: E402
from nontainer.conformance import applies, check, run  # noqa: E402
from nontainer.conformance.harness import SCENARIOS  # noqa: E402

HARNESS = AgnoHarness()


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
def test_the_agno_adapter_honors_the_scenario(scenario):
    if not applies(scenario, HARNESS):
        missing = sorted(set(scenario.needs) - HARNESS.capabilities)
        pytest.skip(f"this agno lacks {missing}")
    failures = check(scenario, run(scenario, HARNESS))
    gaps = HARNESS.known_gaps.get(scenario.name, {})
    unexpected = {k: v for k, v in failures.items() if k not in gaps}
    assert not unexpected, unexpected
    closed = sorted(set(gaps) - set(failures))
    assert not closed, f"known gap(s) {closed} now pass: take them off the list"


def test_every_known_gap_names_a_scenario():
    names = {s.name for s in SCENARIOS}
    assert set(HARNESS.known_gaps) <= names
