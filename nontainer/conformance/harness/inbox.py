"""Tier 3: notes queued mid-turn ride the next tool result, once."""

from __future__ import annotations

from ..corpus import (
    Expect,
    InboxExp,
    Scenario,
    TurnExp,
    cancel,
    queue_note,
    says,
    turn,
    writes,
)

A = "/workspace/a.txt"
B = "/workspace/b.txt"

SCENARIOS = (
    Scenario(
        name="a-note-rides-the-next-tool-result-once",
        summary=(
            "A note queued while the turn runs is delivered on the next tool "
            "result, before that result's end event, and not again on the "
            "one after. The completed turn settles it."
        ),
        tiers=(3,),
        acts=(
            turn(
                "write a and b",
                queue_note("also mention the date"),
                writes(A, "a"),
                writes(B, "b"),
                says("wrote both"),
            ),
        ),
        expect=Expect(
            turns=(
                TurnExp(
                    status="completed",
                    events=(
                        "RunStarted",
                        "ToolStarted",
                        "Delivered",
                        "ToolEnded",
                        "ToolStarted",
                        "ToolEnded",
                        "TextDelta",
                        "RunEnded:completed",
                    ),
                ),
            ),
            files={A: "a", B: "b"},
            inbox=InboxExp(),
        ),
    ),
    Scenario(
        name="a-note-after-the-last-tool-waits-for-the-next-turn",
        summary=(
            "A note queued after the turn's last tool call has no result to "
            "ride: it stays queued, and the next turn's first tool result "
            "delivers it."
        ),
        tiers=(3,),
        acts=(
            turn("write a", writes(A, "a"), queue_note("and b"), says("wrote a")),
            turn("go on", writes(B, "b"), says("wrote b")),
        ),
        expect=Expect(
            turns=(
                TurnExp(
                    status="completed",
                    events=(
                        "RunStarted",
                        "ToolStarted",
                        "ToolEnded",
                        "TextDelta",
                        "RunEnded:completed",
                    ),
                ),
                TurnExp(
                    status="completed",
                    events=(
                        "RunStarted",
                        "ToolStarted",
                        "Delivered",
                        "ToolEnded",
                        "TextDelta",
                        "RunEnded:completed",
                    ),
                ),
            ),
            files={A: "a", B: "b"},
            inbox=InboxExp(),
        ),
    ),
    Scenario(
        name="a-cancel-settles-what-was-delivered",
        summary=(
            "A cancelled run keeps its messages, so the model read the notes "
            "on its tool results. The cancel settles them, and the next turn "
            "does not deliver them again."
        ),
        tiers=(3,),
        needs=("keeps-aborted-runs",),
        acts=(
            turn(
                "write a",
                queue_note("and b"),
                writes(A, "a"),
                cancel(),
                says("never reached"),
            ),
            turn("go on", writes(B, "b"), says("wrote b")),
        ),
        expect=Expect(
            turns=(
                TurnExp(
                    status="cancelled",
                    events=(
                        "RunStarted",
                        "ToolStarted",
                        "Delivered",
                        "ToolEnded",
                        "RunEnded:cancelled",
                    ),
                ),
                TurnExp(
                    status="completed",
                    events=(
                        "RunStarted",
                        "ToolStarted",
                        "ToolEnded",
                        "TextDelta",
                        "RunEnded:completed",
                    ),
                ),
            ),
            files={A: "a", B: "b"},
            inbox=InboxExp(),
        ),
    ),
)
