"""Tiers 0 and 1: how a turn ends, and what each ending leaves behind."""

from __future__ import annotations

from ..corpus import (
    CommitExp,
    Expect,
    InboxExp,
    RunExp,
    Scenario,
    TurnExp,
    cancel,
    fails,
    resume,
    says,
    turn,
    writes,
)

A = "/workspace/a.txt"
B = "/workspace/b.txt"

SCENARIOS = (
    Scenario(
        name="a-reply-alone-completes",
        summary=(
            "A turn the model answers with text alone completes, and lands "
            "one run commit."
        ),
        tiers=(1,),
        acts=(turn("say hello", says("hello")),),
        expect=Expect(
            turns=(
                TurnExp(
                    status="completed",
                    events=("RunStarted", "TextDelta", "RunEnded:completed"),
                ),
            ),
            runs=(RunExp(tool_results=0),),
            commits=(CommitExp(tool="turn", runs=("completed",)),),
            inbox=InboxExp(),
        ),
    ),
    Scenario(
        name="a-turn-that-writes-a-file",
        summary=(
            "A turn that calls a tool and then replies completes. The tool's "
            "commit lands, then the run's, and the run keeps the tool's result."
        ),
        tiers=(0, 1),
        acts=(turn("write a", writes(A, "a"), says("wrote a")),),
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
            ),
            files={A: "a"},
            runs=(RunExp(tool_results=1),),
            commits=(
                CommitExp(tool="file_write"),
                CommitExp(tool="turn", runs=("completed",)),
            ),
            inbox=InboxExp(),
        ),
    ),
    Scenario(
        name="a-cancel-after-a-tool-keeps-the-run",
        summary=(
            "A run stopped after its first tool call ends cancelled. Its file "
            "stays, and the run is kept with the tool's result and a closing "
            "note, in one run commit."
        ),
        tiers=(1,),
        needs=("keeps-aborted-runs",),
        acts=(turn("write a", writes(A, "a"), cancel(), says("never reached")),),
        expect=Expect(
            turns=(
                TurnExp(
                    status="cancelled",
                    events=(
                        "RunStarted",
                        "ToolStarted",
                        "ToolEnded",
                        "RunEnded:cancelled",
                    ),
                ),
            ),
            files={A: "a"},
            runs=(RunExp(closing_note=True, tool_results=1),),
            commits=(
                CommitExp(tool="file_write"),
                CommitExp(tool="turn", runs=("cancelled",)),
            ),
            inbox=InboxExp(),
        ),
    ),
    Scenario(
        name="a-failure-keeps-the-run-with-a-closing-note",
        summary=(
            "A run whose model call raises something other than a provider "
            "error ends failed. The run is kept with the tool's result and a "
            "closing note, in one run commit."
        ),
        tiers=(1,),
        needs=("keeps-aborted-runs",),
        acts=(turn("write a", writes(A, "a"), fails("error")),),
        expect=Expect(
            turns=(
                TurnExp(
                    status="failed",
                    events=(
                        "RunStarted",
                        "ToolStarted",
                        "ToolEnded",
                        "RunEnded:failed",
                    ),
                ),
            ),
            files={A: "a"},
            runs=(RunExp(closing_note=True, tool_results=1),),
            commits=(
                CommitExp(tool="file_write"),
                CommitExp(tool="turn", runs=("failed",)),
            ),
            inbox=InboxExp(),
        ),
    ),
    Scenario(
        name="a-provider-error-interrupts-and-resume-continues",
        summary=(
            "A provider error interrupts the run, which is stored as it "
            "stood. Resuming continues the same run in place: one run in the "
            "end, holding the tool results from before and after."
        ),
        tiers=(1,),
        needs=("resume",),
        acts=(
            turn("write a and b", writes(A, "a"), fails("provider")),
            resume(writes(B, "b"), says("wrote both")),
        ),
        expect=Expect(
            turns=(
                TurnExp(
                    status="interrupted",
                    events=(
                        "RunStarted",
                        "ToolStarted",
                        "ToolEnded",
                        "RunEnded:interrupted",
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
            runs=(RunExp(tool_results=2),),
            commits=(
                CommitExp(tool="file_write"),
                CommitExp(tool="turn", runs=("interrupted",)),
                CommitExp(tool="file_write"),
                CommitExp(tool="turn", runs=("completed",)),
            ),
            inbox=InboxExp(),
        ),
    ),
)
