"""A delegate answers once nothing it waits on is outstanding.

``until_settled`` runs a delegate's turns: the task's, then a woken turn
whenever its own delegates answer or a note reaches its inbox, until
neither is pending, or its woken turns run out. The turns here are
scripted functions; what they deliver stands in for a harness's woken
turn (``Turn.opening``).
"""

import asyncio
import threading
import time

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


def test_the_opening_tells_on_delivered_as_a_tool_result_would(store):
    seen = []
    inbox = Inbox(on_delivered=lambda notes: seen.append([n.text for n in notes]))
    ws = store.open("main")
    try:
        inbox.put("the date, please")
        turn = ws.turn("run-1", inbox=inbox)
        assert "the date, please" in (turn.opening() or "")
        assert seen == [["the date, please"]]
        turn.end("completed")
    finally:
        ws.close()


def test_aopening_awaits_an_async_on_delivered(store):
    seen = []

    async def record(notes):
        await asyncio.sleep(0)
        seen.append([n.text for n in notes])

    async def main():
        inbox = Inbox(on_delivered=record)
        ws = store.open("main")
        try:
            inbox.put("the date, please")
            turn = ws.turn("run-1", inbox=inbox)
            assert "the date, please" in (await turn.aopening() or "")
            turn.end("completed")
        finally:
            ws.close()

    asyncio.run(main())
    assert seen == [["the date, please"]]


def test_notes_an_opening_cannot_render_wait_for_the_next_turn(store):
    def broken(note):
        raise RuntimeError("the frame broke")

    inbox = Inbox(frame=broken)
    ws = store.open("main")
    try:
        inbox.put("the date, please")
        turn = ws.turn("run-1", inbox=inbox)
        assert turn.opening() is None
        assert [n.text for n in inbox.pending()] == ["the date, please"]
        turn.end("completed")
        assert [n.text for n in inbox.pending()] == ["the date, please"]
    finally:
        ws.close()


def test_a_helper_closed_while_its_delegate_runs_ends_the_wait(delegate):
    """Closing ends every wait at once, so a loop that kept waiting on
    a closed helper would spin until the run finished."""
    runner = Gated()
    sessions = Sessions(delegate, runner)
    job = None

    def asking(prompt):
        nonlocal job
        if prompt is not None:
            job = sessions.ask("north")
            threading.Timer(0.1, sessions.close).start()
        return "waiting on the helper"

    try:
        began = time.monotonic()
        settled = until_settled(asking, sessions, prompt="find north", poll=5)
        assert time.monotonic() - began < 5  # it ended before the run did
    finally:
        runner.gate.set()
        sessions.close()
    assert settled.unread == (job.name,)


def test_auntil_settled_on_a_closed_helper_lets_the_loop_run(delegate):
    """An async wait on a closed helper returns without suspending, so a
    loop that kept waiting would hold the event loop its own delegates
    need to finish on. Run on a thread of its own, so a regression is a
    failure rather than a hung suite."""

    class Async:
        def __init__(self):
            self.gate = asyncio.Event()

        async def run(self, session, task, *, budget=None):
            await self.gate.wait()
            return f"found {task}"

    out: dict = {}

    async def main():
        runner = Async()
        sessions = Sessions(delegate, runner)

        async def asking(prompt):
            out["job"] = await sessions.aask("north")
            closing = asyncio.ensure_future(sessions.aclose())
            out["closing"] = closing
            return "waiting on the helper"

        out["settled"] = await auntil_settled(asking, sessions, prompt="find north")
        runner.gate.set()
        await out["closing"]

    thread = threading.Thread(target=asyncio.run, args=(main(),), daemon=True)
    thread.start()
    thread.join(10)
    assert not thread.is_alive(), "the loop never came back"
    assert out["settled"].unread == (out["job"].name,)


def test_the_unread_note_offers_the_asker_no_verb_it_cannot_use(delegate):
    """The unread delegates are the delegate's own jobs, which its
    asker's helper does not hold: a ``sessions result`` there finds
    nothing."""
    runner = Gated()
    with Sessions(delegate, runner) as sessions:

        def asking(prompt):
            sessions.ask("north")
            return "waiting on the helper"

        settled = until_settled(asking, sessions, prompt="find north", max_wakes=0)
        runner.gate.set()
    assert "sessions result" not in settled.text
    assert "own branches" in settled.text
