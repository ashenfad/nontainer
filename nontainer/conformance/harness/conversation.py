"""Tier 2: the conversation lives in the branch, with the files."""

from __future__ import annotations

from ..corpus import (
    Expect,
    RunExp,
    Scenario,
    TurnExp,
    checkout,
    fork,
    says,
    turn,
    writes,
)

A = "/workspace/a.txt"
B = "/workspace/b.txt"

SCENARIOS = (
    Scenario(
        name="a-checkout-rewinds-the-conversation-with-the-files",
        summary=(
            "Checking out the commit an earlier turn ended on brings back "
            "that turn's files and conversation together: the later turn's "
            "file and run are gone, and the next turn follows the first."
        ),
        tiers=(2,),
        acts=(
            turn("write a", writes(A, "a"), says("wrote a")),
            turn("write b", writes(B, "b"), says("wrote b")),
            checkout(after_turn=1),
            turn("what now?", says("a is written")),
        ),
        expect=Expect(
            turns=(
                TurnExp(status="completed"),
                TurnExp(status="completed"),
                TurnExp(status="completed"),
            ),
            files={A: "a"},
            absent=(B,),
            runs=(RunExp(tool_results=1), RunExp(tool_results=0)),
        ),
    ),
    Scenario(
        name="a-full-fork-carries-the-conversation",
        summary=(
            "A fork made with inherit='full' holds the parent's files and "
            "conversation, and its next turn adds to that conversation."
        ),
        tiers=(2,),
        acts=(
            turn("write a", writes(A, "a"), says("wrote a")),
            fork("scenario.full", inherit="full"),
            turn("still there?", says("yes")),
        ),
        expect=Expect(
            turns=(TurnExp(status="completed"), TurnExp(status="completed")),
            files={A: "a"},
            runs=(RunExp(tool_results=1), RunExp(tool_results=0)),
        ),
    ),
    Scenario(
        name="a-fresh-fork-starts-without-the-conversation",
        summary=(
            "A fork made with inherit='fresh' holds the parent's files and no "
            "conversation: its first turn is its first run."
        ),
        tiers=(2,),
        acts=(
            turn("write a", writes(A, "a"), says("wrote a")),
            fork("scenario.fresh", inherit="fresh"),
            turn("hello", says("hello")),
        ),
        expect=Expect(
            turns=(TurnExp(status="completed"), TurnExp(status="completed")),
            files={A: "a"},
            runs=(RunExp(tool_results=0),),
        ),
    ),
)
