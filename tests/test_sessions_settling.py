"""A delegate answers once nothing it waits on is outstanding.

``until_settled`` runs a delegate's turns: the task's, then a woken turn
whenever its own delegates answer or a note reaches its inbox, until
neither is pending, or its woken turns run out. The turns here are
scripted functions; what they deliver stands in for a harness's woken
turn (``Turn.opening``).
"""

import asyncio
import threading

import pytest

from nontainer import Store
from nontainer.inbox import Inbox
from nontainer.sessions import Sessions, auntil_settled, until_settled
from nontainer.wsgit import register_wsgit


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "store")
    yield s
    s.close()


@pytest.fixture
def delegate(store):
    """The delegate's own workspace: it may delegate in turn."""
    ws = store.open("scout")
    register_wsgit(ws)
    ws.files.write("/workspace/notes.md", "notes\n")
    ws.index.commit("seed")
    yield ws
    ws.close()


class Gated:
    """A runner that answers once ``gate`` is set."""

    def __init__(self) -> None:
        self.gate = threading.Event()

    def run(self, session, task, *, budget=None):
        self.gate.wait(10)
        return f"found {task}"


class Turns:
    """A delegate's turns: the reply each gives, and what a woken turn
    delivered (the answers and notes waiting for it)."""

    def __init__(self, sessions=None, inbox=None, replies=()):
        self.sessions = sessions
        self.inbox = inbox
        self.replies = list(replies)
        self.prompts: list[str | None] = []
        self.delivered: list[list[str]] = []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        if prompt is None:
            got = [str(a) for _, a in self.sessions.take()] if self.sessions else []
            if self.inbox is not None:
                got += [n.text for n in self.inbox.drain()]
            self.delivered.append(got)
        return self.replies.pop(0) if self.replies else "(nothing more)"


def test_a_reply_with_nothing_outstanding_is_the_answer():
    turns = Turns(replies=["the rates are 4 and 7"])
    settled = until_settled(turns, prompt="find the rates")
    assert (settled.reply, settled.wakes, settled.unread) == (
        "the rates are 4 and 7",
        0,
        (),
    )
    assert settled.text == settled.reply
    assert turns.prompts == ["find the rates"]


def test_a_delegate_waits_for_its_own_delegate_then_reads_its_answer(delegate):
    runner = Gated()
    with Sessions(delegate, runner) as sessions:
        turns = Turns(sessions, replies=["asking the helper", "north is 4"])

        def first(prompt):
            if prompt is not None:
                sessions.ask("north")
                runner.gate.set()
            return turns(prompt)

        settled = until_settled(first, sessions, prompt="find north")
    assert settled.reply == "north is 4" and settled.wakes == 1
    assert turns.delivered == [["found north"]]


def test_a_note_left_in_the_inbox_is_read_before_answering():
    inbox = Inbox()
    turns = Turns(inbox=inbox, replies=["draft", "final, with the date"])

    def first(prompt):
        if prompt is not None:
            inbox.put("also give the date")
        return turns(prompt)

    settled = until_settled(first, None, inbox, prompt="summarize")
    assert settled.reply == "final, with the date"
    assert turns.delivered == [["also give the date"]]


def test_a_note_that_comes_while_a_delegate_runs_wakes_without_its_answer(delegate):
    """Waiting for an answer, the loop still looks at the inbox: the
    caller's note is read at once, and the answer when it lands."""
    runner = Gated()
    inbox = Inbox()
    with Sessions(delegate, runner) as sessions:
        turns = Turns(sessions, inbox, replies=["asking", "noted", "north is 4"])

        def scripted(prompt):
            if prompt is not None:
                sessions.ask("north")
                threading.Timer(0.1, inbox.put, ("hurry, please",)).start()
            reply = turns(prompt)
            if prompt is None and turns.delivered[-1] == ["hurry, please"]:
                runner.gate.set()
            return reply

        settled = until_settled(
            scripted, sessions, inbox, prompt="find north", poll=0.05
        )
    assert settled.reply == "north is 4" and settled.wakes == 2
    assert turns.delivered == [["hurry, please"], ["found north"]]


def test_spent_wakes_answer_with_what_is_left_unread(delegate):
    runner = Gated()  # never answers in time
    with Sessions(delegate, runner) as sessions:
        job = None

        def asking(prompt):
            nonlocal job
            if prompt is not None:
                job = sessions.ask("north")
            return "waiting on the helper"

        settled = until_settled(asking, sessions, prompt="find north", max_wakes=0)
        runner.gate.set()
    assert settled.unread == (job.name,) and settled.wakes == 0
    assert job.name in settled.text and settled.text.startswith("waiting on the helper")


def test_spent_wakes_count_the_notes_left():
    inbox = Inbox()
    calls = []

    def chatty(prompt):
        calls.append(prompt)
        if prompt is None:
            inbox.drain()  # a woken turn delivers what was waiting
        inbox.put("one more thing")  # and every turn leaves another note
        return "reply"

    settled = until_settled(chatty, None, inbox, prompt="go", max_wakes=2)
    assert settled.wakes == 2 and settled.notes == 1 and calls == ["go", None, None]


def test_auntil_settled_awaits_the_turns_and_the_answers(delegate):
    class Async:
        def __init__(self):
            self.gate = asyncio.Event()

        async def run(self, session, task, *, budget=None):
            await self.gate.wait()
            return f"found {task}"

    async def main():
        runner = Async()
        sessions = Sessions(delegate, runner)
        delivered = []

        async def turn(prompt):
            if prompt is not None:
                await sessions.aask("north")
                asyncio.get_running_loop().call_later(0.05, runner.gate.set)
                return "asking"
            delivered.append([str(a) for _, a in sessions.take()])
            return "north is 4"

        try:
            return await auntil_settled(turn, sessions, prompt="find north"), delivered
        finally:
            await sessions.aclose()

    settled, delivered = asyncio.run(main())
    assert settled.reply == "north is 4" and settled.wakes == 1
    assert delivered == [["found north"]]


def test_a_woken_turn_opens_with_what_is_waiting(store):
    ws = store.open("main")
    inbox = Inbox()
    try:
        inbox.put("the date, please")
        turn = ws.turn("run-1", inbox=inbox)
        opening = turn.opening()
        assert opening is not None and "the date, please" in opening
        assert inbox.pending() == [] and len(inbox.delivered()) == 1
        turn.end("completed")
        assert inbox.delivered() == []  # settled with the turn
        quiet = ws.turn("run-2", inbox=inbox)
        assert quiet.opening() is None
        quiet.end("completed")
    finally:
        ws.close()
