"""Tier 5: delegation. A session asks delegates through its ``sessions``
tool; each delegate runs as a session of the same harness, on a branch
of its own, until nothing it waits on is outstanding
(:func:`~nontainer.sessions.until_settled`).

- A delegate's answer is delivered on its asker's next tool result,
  exactly once.
- A delegate that asked delegates of its own answers only after hearing
  from them, through a woken turn that opens with their answers.
- One that runs out of woken turns answers anyway, naming who it did
  not hear from.
- A note left in a delegate's inbox after its last tool call is read,
  in a woken turn, before it answers.
- A delegate's runner returns with its work committed (checked always).

Delegates start held, and a ``delegate_answers`` event releases one and
waits for its answer, so where an answer lands is fixed by the script.
"""

from __future__ import annotations

from ..corpus import (
    DelegateExp,
    Expect,
    InboxExp,
    RunExp,
    Scenario,
    Turn,
    TurnExp,
    asks,
    delegate,
    delegate_answers,
    queue_note,
    says,
    turn,
    writes,
)

A = "/workspace/a.txt"
B = "/workspace/b.txt"

ASKED = ("RunStarted", "ToolStarted", "ToolEnded")
DELIVERED = ("ToolStarted", "Delivered", "ToolEnded")
ENDS = ("TextDelta", "RunEnded:completed")


def _asks_scout() -> Turn:
    """The parent's turn: ask the scout, let it answer, and read its
    answer on the next tool result."""
    return turn(
        "Find the rates.",
        asks("scout", "find the rates"),
        delegate_answers("scout"),
        writes(A, "a"),
        says("done"),
    )


SCENARIOS = (
    Scenario(
        name="a-delegates-answer-is-delivered-once-on-the-next-tool-result",
        summary=(
            "A delegate's answer lands on its asker's next tool result, "
            "mid-turn, and is delivered there only: the next turn's tool "
            "results carry nothing, and nothing is left in the inbox."
        ),
        tiers=(5,),
        needs=("delegation",),
        delegates={"scout": delegate(says("the rates are 4 and 7"))},
        acts=(
            _asks_scout(),
            turn("And then?", writes(B, "b"), says("ok")),
        ),
        expect=Expect(
            turns=(
                TurnExp(status="completed", events=(*ASKED, *DELIVERED, *ENDS)),
                TurnExp(
                    status="completed",
                    events=("RunStarted", "ToolStarted", "ToolEnded", *ENDS),
                ),
            ),
            files={A: "a", B: "b"},
            runs=(RunExp(tool_results=2), RunExp(tool_results=1)),
            inbox=InboxExp(pending=0, delivered=0),
            delegates={"scout": DelegateExp(mentions=("4 and 7",), wakes=0)},
        ),
    ),
    Scenario(
        name="a-delegate-hears-from-its-own-delegate-before-answering",
        summary=(
            "A delegate that asks a delegate of its own and ends its turn "
            "waiting is woken when that answer lands, reads it in a turn "
            "that opens with it, and answers only then."
        ),
        tiers=(5,),
        needs=("delegation",),
        delegates={
            "scout": delegate(
                asks("helper", "check north"),
                delegate_answers("helper"),
                says("waiting on the helper"),
                says("north is 4, from the helper"),
            ),
            "helper": delegate(says("north is 4")),
        },
        acts=(_asks_scout(),),
        expect=Expect(
            turns=(TurnExp(status="completed", events=(*ASKED, *DELIVERED, *ENDS)),),
            delegates={
                "scout": DelegateExp(
                    mentions=("north is 4, from the helper",), wakes=1
                ),
                "helper": DelegateExp(mentions=("north is 4",), wakes=0),
            },
        ),
    ),
    Scenario(
        name="a-delegate-out-of-wakes-names-who-it-did-not-hear-from",
        summary=(
            "A delegate whose woken turns are spent answers with what it has "
            "rather than wait on, and its answer names the delegate it did "
            "not hear from."
        ),
        tiers=(5,),
        needs=("delegation",),
        delegates={
            "scout": delegate(
                asks("helper", "check north"),
                says("waiting on the helper"),
                wakes=0,
            ),
            "helper": delegate(says("north is 4")),
        },
        acts=(_asks_scout(),),
        expect=Expect(
            turns=(TurnExp(status="completed", events=(*ASKED, *DELIVERED, *ENDS)),),
            delegates={
                "scout": DelegateExp(
                    mentions=("waiting on the helper", "helper"),
                    wakes=0,
                    unread=("helper",),
                ),
            },
        ),
    ),
    Scenario(
        name="a-note-left-for-a-delegate-is-read-before-it-answers",
        summary=(
            "A note that reaches a delegate's inbox after its last tool call "
            "is not stranded: the delegate is woken to read it, and its "
            "answer is the reply it gives then."
        ),
        tiers=(5,),
        needs=("delegation",),
        delegates={
            "scout": delegate(
                writes(B, "draft"),
                queue_note("also give the date"),
                says("draft written"),
                says("written, and dated today"),
            ),
        },
        acts=(_asks_scout(),),
        expect=Expect(
            turns=(TurnExp(status="completed", events=(*ASKED, *DELIVERED, *ENDS)),),
            delegates={"scout": DelegateExp(mentions=("dated today",), wakes=1)},
        ),
    ),
)
