"""Tier 4: context management. A harness folds earlier turns in its own
way; these pin what anyone can observe of a fold.

- Past the budget, the request about to be sent folds the earlier turns
  into a summary, recorded in the workspace's ``__compaction__/`` plane
  and streamed as ``Compacted``.
- The fold stays in force: later requests carry the summary in place of
  the turns it covers, with no new summary until the budget is crossed
  again, and a new fold takes in the one before it.
- A fold's summary never enters the stored conversation (checked for
  every scenario).
- The record goes with the conversation: a rewind takes a fold back, a
  full fork carries it, and a fresh fork starts without it.

A step's ``input_tokens`` is the size the provider reports for the
request it answers, which is how a turn crosses the budget, and the
reply to the harness's request for a summary is a step of the script
like any other (:func:`~nontainer.conformance.corpus.summarizes`).
"""

from __future__ import annotations

from ..corpus import (
    Expect,
    FoldExp,
    RunExp,
    Scenario,
    Turn,
    TurnExp,
    checkout,
    fork,
    says,
    summarizes,
    turn,
)

BUDGET = 1000

FIRST = "Plan the garden beds."
SUMMARY = "SUMMARY: the person is planning a garden; the beds go along the north wall."
SECOND_SUMMARY = (
    "SUMMARY: the garden's beds go along the north wall, watered by drip lines."
)

PLAIN = ("RunStarted", "TextDelta", "RunEnded:completed")
FOLDS = ("RunStarted", "Compacted", "TextDelta", "RunEnded:completed")


def _first() -> Turn:
    """The first turn, whose request the provider reports over budget."""
    return turn(FIRST, says("Along the north wall.", input_tokens=5 * BUDGET))


def _folding() -> Turn:
    """The second turn: its first request is over budget, so the harness
    folds the first turn before sending it."""
    return turn(
        "Now the watering.",
        summarizes(SUMMARY),
        says("Drip lines.", input_tokens=BUDGET // 4),
    )


SCENARIOS = (
    Scenario(
        name="a-request-over-budget-folds-the-earlier-turns",
        summary=(
            "A request that would go over the budget is sent with the earlier "
            "turns folded into a summary: the fold is recorded in the "
            "workspace and streamed as Compacted, and the stored conversation "
            "keeps both turns whole."
        ),
        tiers=(4,),
        needs=("compaction",),
        budget=BUDGET,
        acts=(_first(), _folding()),
        expect=Expect(
            turns=(
                TurnExp(status="completed", events=PLAIN, folded=False),
                TurnExp(status="completed", events=FOLDS, folded=True),
            ),
            runs=(RunExp(), RunExp()),
            folds=(FoldExp(runs=1),),
        ),
    ),
    Scenario(
        name="a-fold-stays-in-force-without-another-summary",
        summary=(
            "Once made, a fold is in force for the requests after it: they "
            "carry its summary in place of the turns it covers, with no new "
            "summary while they stay under the budget."
        ),
        tiers=(4,),
        needs=("compaction",),
        budget=BUDGET,
        acts=(
            _first(),
            _folding(),
            turn("And the shed?", says("By the gate.", input_tokens=BUDGET // 3)),
        ),
        expect=Expect(
            turns=(
                TurnExp(status="completed", events=PLAIN),
                TurnExp(status="completed", events=FOLDS),
                TurnExp(status="completed", events=PLAIN, folded=True),
            ),
            runs=(RunExp(), RunExp(), RunExp()),
            folds=(FoldExp(runs=1),),
        ),
    ),
    Scenario(
        name="a-second-fold-takes-in-the-first",
        summary=(
            "Crossing the budget again folds the summary so far and the turns "
            "since into one new summary: there is only ever one in force, and "
            "it covers every turn folded."
        ),
        tiers=(4,),
        needs=("compaction",),
        budget=BUDGET,
        acts=(
            _first(),
            turn(
                "Now the watering.",
                summarizes(SUMMARY),
                says("Drip lines.", input_tokens=5 * BUDGET),
            ),
            turn(
                "And the shed?",
                summarizes(SECOND_SUMMARY),
                says("By the gate.", input_tokens=BUDGET // 4),
            ),
        ),
        expect=Expect(
            turns=(
                TurnExp(status="completed", events=PLAIN),
                TurnExp(status="completed", events=FOLDS),
                TurnExp(status="completed", events=FOLDS, folded=True),
            ),
            runs=(RunExp(), RunExp(), RunExp()),
            folds=(FoldExp(runs=1), FoldExp(runs=2)),
        ),
    ),
    Scenario(
        name="a-rewind-takes-back-its-folds",
        summary=(
            "A fold lands with the turn that made it, so checking out a "
            "commit from before that turn takes the fold back with it."
        ),
        tiers=(2, 4),
        needs=("compaction",),
        budget=BUDGET,
        acts=(_first(), _folding(), checkout(after_turn=1)),
        expect=Expect(
            turns=(
                TurnExp(status="completed", events=PLAIN),
                TurnExp(status="completed", events=FOLDS),
            ),
            runs=(RunExp(),),
            folds=(),
        ),
    ),
    Scenario(
        name="a-full-fork-carries-the-folds",
        summary=(
            "A fork that carries the conversation carries its folds: the "
            "fork's next request is sent the summary, with no new one."
        ),
        tiers=(2, 4),
        needs=("compaction",),
        budget=BUDGET,
        acts=(
            _first(),
            _folding(),
            fork("scenario.carried", inherit="full"),
            turn("And the shed?", says("By the gate.", input_tokens=BUDGET // 3)),
        ),
        expect=Expect(
            turns=(
                TurnExp(status="completed", events=PLAIN),
                TurnExp(status="completed", events=FOLDS),
                TurnExp(status="completed", events=PLAIN, folded=True),
            ),
            runs=(RunExp(), RunExp(), RunExp()),
            folds=(FoldExp(runs=1),),
        ),
    ),
    Scenario(
        name="a-fresh-fork-drops-the-folds",
        summary=(
            "A fork made without the conversation starts without its folds "
            "too: the fork's first request carries no summary."
        ),
        tiers=(2, 4),
        needs=("compaction",),
        budget=BUDGET,
        acts=(
            _first(),
            _folding(),
            fork("scenario.fresh", inherit="fresh"),
            turn("Start the shed plans.", says("A lean-to.")),
        ),
        expect=Expect(
            turns=(
                TurnExp(status="completed", events=PLAIN),
                TurnExp(status="completed", events=FOLDS),
                TurnExp(status="completed", events=PLAIN, folded=False),
            ),
            runs=(RunExp(),),
            folds=(),
        ),
    ),
)
