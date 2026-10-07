"""The turn: one run of a harness's loop on a workspace, from its
beginning to how it ended, landing in one commit."""

import asyncio
import threading
import time

import pytest

from nontainer import NotSupportedError, Store, TurnInProgress, conversation
from nontainer.conversation import Index
from nontainer.inbox import Inbox, split
from nontainer.turns import Turns


@pytest.fixture
def ws():
    store = Store(memory=True)
    ws = store.open("s")
    yield ws
    ws.close()
    store.close()


def _stamp(ws):
    return ws.log(limit=1)[0].info


def _stage(ws, path="/workspace/a.txt", text="a"):
    """A write that stays staged, as a harness's own storage does."""
    with ws.lock:
        ws.files.fs.write(path, text.encode())


# -- ending -----------------------------------------------------------------------


def test_a_turn_lands_what_it_staged_in_one_commit_stamped_with_its_ending(ws):
    turn = ws.turns.begin("r1")
    assert ws.turns.current is turn and turn.open
    _stage(ws)
    commit = turn.end("cancelled", message="stopped")
    assert commit == ws.head and turn.commit == commit
    assert _stamp(ws) == {
        "tool": "turn",
        "runs": {"r1": "cancelled"},
        "message": "stopped",
    }
    assert turn.status == "cancelled" and not turn.open
    assert ws.turns.current is None


def test_a_turn_that_changed_nothing_commits_nothing(ws):
    head = ws.head
    assert ws.turns.begin("r1").end("completed") is None
    assert ws.head == head


def test_a_turn_ends_once(ws):
    turn = ws.turns.begin("r1")
    turn.end("completed")
    with pytest.raises(RuntimeError, match="already ended 'completed'"):
        turn.end("failed")


def test_an_invalid_status_is_refused_and_the_turn_stays_open(ws):
    turn = ws.turn("r1")
    with pytest.raises(ValueError, match="not a run status"):
        turn.end("sideways")
    assert turn.open and ws.turns.current is turn
    _stage(ws)
    turn.end("completed")
    assert ws.turns.current is None
    assert _stamp(ws)["runs"] == {"r1": "completed"}


class HeldInbox(Inbox):
    """An inbox whose settle holds an ending turn inside the workspace
    lock until the test lets it go on."""

    def __init__(self):
        super().__init__()
        self.inside = threading.Event()
        self.go = threading.Event()

    def settle(self):
        self.inside.set()
        self.go.wait(5)
        super().settle()


def test_a_second_end_racing_the_first_is_refused_at_once(ws):
    inbox = HeldInbox()
    turn = ws.turn("r1", inbox=inbox)
    _stage(ws)
    first = threading.Thread(target=turn.end, args=("completed",))
    first.start()
    assert inbox.inside.wait(5)
    # refused rather than queued behind the first on the workspace lock
    with pytest.raises(RuntimeError, match="another end"):
        turn.end("cancelled")
    inbox.go.set()
    first.join(5)
    assert turn.status == "completed"
    assert [e.info for e in ws.log() if e.info.get("tool") == "turn"] == [
        {"tool": "turn", "runs": {"r1": "completed"}}
    ]


def test_whatever_waits_on_the_workspace_lock_sees_the_turn_gone(ws, monkeypatch):
    inbox = HeldInbox()
    turn = ws.turn("r1", inbox=inbox)
    _stage(ws)
    seen = []

    def waiter():
        with ws.lock:
            seen.append(ws.turns.current)

    ender = threading.Thread(target=turn.end, args=("completed",))
    ender.start()
    assert inbox.inside.wait(5)
    other = threading.Thread(target=waiter)
    other.start()
    # widen the gap between the lock and the slot, if there is one
    release = Turns._release

    def slow_release(self, t):
        time.sleep(0.1)
        release(self, t)

    monkeypatch.setattr(Turns, "_release", slow_release)
    inbox.go.set()
    ender.join(5)
    other.join(5)
    assert seen == [None]


@pytest.mark.parametrize("status", ["completed", "cancelled", "interrupted", "failed"])
def test_every_ending_settles_the_inbox(ws, status):
    inbox = Inbox()
    inbox.put("also b")
    turn = ws.turn("r1", inbox=inbox)
    assert len(inbox.drain()) == 1  # delivered on a tool result
    turn.end(status)
    assert inbox.delivered() == [] and inbox.pending() == []


def test_an_unnamed_turn_is_bound_once_and_stamped_by_its_id(ws):
    turn = ws.turn()
    turn.bind("r1")
    turn.bind("r1")
    with pytest.raises(ValueError, match="cannot become"):
        turn.bind("r2")
    _stage(ws)
    turn.complete()
    assert _stamp(ws)["runs"] == {"r1": "completed"}


def test_a_turn_that_never_named_its_run_is_stamped_without_runs(ws):
    turn = ws.turn()
    _stage(ws)
    turn.end("failed", message="the run never started")
    assert _stamp(ws) == {"tool": "turn", "message": "the run never started"}


# -- one at a time ----------------------------------------------------------------


def test_a_second_turn_is_refused_until_the_first_ends(ws):
    first = ws.turn("r1")
    with pytest.raises(TurnInProgress, match="r1"):
        ws.turn("r2")
    first.end("completed")
    ws.turn("r2").end("completed")


def test_an_end_that_raises_still_frees_the_workspace(ws):
    turn = ws.turn()  # no run id, so storing a body has nothing to file it under
    with pytest.raises(ValueError, match="bind it first"):
        turn.complete(body={"messages": []})
    assert ws.turns.current is None
    ws.turn("r2").end("completed")


def test_a_fork_takes_turns_of_its_own(ws):
    ws.files.write("/workspace/a.txt", "a")
    ws.turn("r1")
    child = ws.fork("s.child")
    try:
        child.turn("c1").end("completed")
    finally:
        child.close()


# -- the context manager ------------------------------------------------------------


def test_leaving_the_block_completes_the_turn(ws):
    with ws.turn("r1") as turn:
        _stage(ws)
    assert turn.status == "completed"
    assert _stamp(ws)["runs"] == {"r1": "completed"}


def test_an_exception_fails_the_turn_and_goes_on(ws):
    with pytest.raises(RuntimeError, match="boom"):
        with ws.turn("r1") as turn:
            _stage(ws)
            raise RuntimeError("boom")
    assert turn.status == "failed"
    assert _stamp(ws) == {
        "tool": "turn",
        "runs": {"r1": "failed"},
        "message": "RuntimeError: boom",
    }


def test_a_cancel_cancels_the_turn_and_goes_on(ws):
    with pytest.raises(asyncio.CancelledError):
        with ws.turn("r1") as turn:
            _stage(ws)
            raise asyncio.CancelledError
    assert turn.status == "cancelled"
    assert _stamp(ws)["runs"] == {"r1": "cancelled"}


def test_a_block_that_ended_its_turn_is_left_alone(ws):
    with ws.turn("r1") as turn:
        _stage(ws)
        turn.interrupt("the provider is down")
    assert turn.status == "interrupted"
    assert _stamp(ws)["message"] == "the provider is down"


def test_the_async_form_ends_the_same_ways():
    async def go(ws):
        async with ws.turn("r1") as done:
            _stage(ws, text="1")
        try:
            async with ws.turn("r2") as cut:
                _stage(ws, text="2")
                raise asyncio.CancelledError
        except asyncio.CancelledError:
            pass
        return done, cut

    store = Store(memory=True)
    ws = store.open("s")
    try:
        done, cut = asyncio.run(go(ws))
        assert (done.status, cut.status) == ("completed", "cancelled")
        assert [e.info.get("runs") for e in ws.log(limit=2)] == [
            {"r2": "cancelled"},
            {"r1": "completed"},
        ]
    finally:
        ws.close()
        store.close()


# -- storing the run ------------------------------------------------------------------


def test_a_run_body_joins_the_conversation_in_the_turns_commit(ws):
    with ws.turn("r1", harness="mine") as turn:
        turn.complete(body={"said": "one"}, record={"title": "t"})
    with ws.turn("r2") as turn:  # the harness is read off the index now
        turn.complete(body={"said": "two"})
    index = conversation.index_of(ws)
    assert index == Index(harness="mine", session="s", runs=("r1", "r2"))
    kv = ws.provider.kv
    assert conversation.read_runs(kv, index.runs) == {
        "r1": {"said": "one"},
        "r2": {"said": "two"},
    }
    assert conversation.read_record(kv) == {"title": "t"}
    assert not ws.uncommitted


def test_a_body_needs_a_harness_and_refuses_another_harnesss_conversation(ws):
    with pytest.raises(ValueError, match="harness"):
        ws.turn("r1").complete(body={})
    conversation.write(ws.provider.kv, Index(harness="agno", session="s"))
    with pytest.raises(NotSupportedError, match="agno"):
        ws.turn("r2", harness="mine").complete(body={})


def test_a_resume_names_a_run_the_conversation_holds(ws):
    with ws.turn("r1", harness="mine") as turn:
        turn.interrupt("overloaded", body={"said": "half"})
    with pytest.raises(ValueError, match="no run 'nope'"):
        ws.turn("nope", resume=True)
    with pytest.raises(ValueError, match="names it"):
        ws.turn(resume=True)
    with ws.turn("r1", resume=True) as turn:
        assert turn.resume
        turn.complete(body={"said": "all"})
    assert conversation.index_of(ws).runs == ("r1",)
    assert conversation.read_runs(ws.provider.kv, ["r1"]) == {"r1": {"said": "all"}}


# -- delivery ---------------------------------------------------------------------


def test_deliver_appends_what_is_queued_and_says_what_it_delivered(ws):
    inbox = Inbox()
    note = inbox.put("also mention the date")
    with ws.turn("r1", inbox=inbox) as turn:
        text, notes = turn.deliver("wrote a.txt")
        assert notes == [note]
        assert text.startswith("wrote a.txt") and "also mention the date" in text
        assert split(text) == ("wrote a.txt", inbox.render(notes))
        assert inbox.pending() == [] and inbox.delivered() == [note]
        # nothing else queued: the next result goes through untouched
        assert turn.deliver("wrote b.txt") == ("wrote b.txt", [])
    assert inbox.delivered() == []  # the end settles


def test_a_turn_without_an_inbox_delivers_nothing(ws):
    with ws.turn("r1") as turn:
        assert turn.deliver("ok") == ("ok", [])
        assert turn.collect() == []


def test_sources_add_their_notes_after_the_queue(ws):
    inbox = Inbox()
    inbox.put("from the person")

    def source(given):
        assert given is inbox
        return [given.deliver_now("from a source", kind="mechanism", label="x")]

    with ws.turn("r1", inbox=inbox, sources=[source]) as turn:
        _, notes = turn.deliver("ok")
    assert [n.text for n in notes] == ["from the person", "from a source"]
    assert inbox.delivered() == []


def test_notes_that_cannot_be_attached_wait_for_the_next_result(ws):
    def broken(note):
        raise RuntimeError("no frame today")

    inbox = Inbox(frame=broken)
    note = inbox.put("hold on")
    with ws.turn("r1", inbox=inbox) as turn:
        assert turn.deliver("ok") == ("ok", [])
        assert inbox.pending() == [note] and inbox.delivered() == []
        inbox.frame = None
        assert turn.deliver("ok again")[1] == [note]


def test_on_delivered_hears_of_each_delivery(ws):
    heard = []
    inbox = Inbox(on_delivered=heard.append)
    inbox.put("one")
    with ws.turn("r1", inbox=inbox) as turn:
        turn.deliver("a")
        turn.deliver("b")  # nothing to deliver, nothing heard
    assert [[n.text for n in notes] for notes in heard] == [["one"]]


def test_a_callback_that_raises_costs_only_a_warning(ws, caplog):
    def angry(notes):
        raise RuntimeError("nope")

    inbox = Inbox(on_delivered=angry)
    inbox.put("still lands")
    with ws.turn("r1", inbox=inbox) as turn:
        text, notes = turn.deliver("ok")
    assert "still lands" in text and len(notes) == 1
    assert "on_delivered raised" in caplog.text


def test_an_async_callback_is_awaited_by_adeliver_and_discarded_by_deliver(ws, caplog):
    heard = []

    async def record(notes):
        heard.extend(n.text for n in notes)

    inbox = Inbox(on_delivered=record)

    async def go():
        async with ws.turn("r1", inbox=inbox) as turn:
            inbox.put("awaited")
            text, _ = await turn.adeliver("ok")
            assert "awaited" in text
            inbox.put("discarded")
            text, _ = turn.deliver("ok")
            assert "discarded" in text

    asyncio.run(go())
    assert heard == ["awaited"]
    assert "cannot await" in caplog.text


def test_a_delegates_answer_arrives_as_a_mechanism_note(ws):
    """``answer_notes`` as a turn source: what the session's helper has
    taken arrives on the next tool result, framed as the mechanism, and
    is settled with the turn."""
    from nontainer import Answer
    from nontainer.sessions import answer_notes

    class Helper:
        def __init__(self, answers):
            self.answers = answers

        def take(self):
            taken, self.answers = self.answers, []
            return taken

    helper = Helper([("s.child", Answer(text="the totals add up"))])
    inbox = Inbox()
    with ws.turn(
        "r1", inbox=inbox, sources=[lambda i: answer_notes(helper, i)]
    ) as turn:
        text, (note,) = turn.deliver("ok")
        assert turn.deliver("ok") == ("ok", [])  # taken once
    assert (note.kind, note.label, note.job) == (
        "mechanism",
        "delegate s.child",
        "s.child",
    )
    assert note.answer.text == "the totals add up"
    assert "the totals add up" in text and "delegation mechanism" in text
    assert inbox.delivered() == []


def test_a_source_that_raises_costs_neither_the_result_nor_the_queue(ws, caplog):
    inbox = Inbox()
    queued = inbox.put("from the person")

    def broken(given):
        given.deliver_now("half made", kind="mechanism")
        raise RuntimeError("the source broke")

    def fine(given):
        return [given.deliver_now("from a working source", kind="mechanism")]

    with ws.turn("r1", inbox=inbox, sources=[broken, fine]) as turn:
        text, notes = turn.deliver("ok")
        assert [n.text for n in notes] == ["from the person", "from a working source"]
        assert text.startswith("ok") and "from the person" in text
        # what the broken source minted waits for the next result
        assert [n.text for n in inbox.pending()] == ["half made"]
        assert queued in inbox.delivered()
    assert "delivery source raised" in caplog.text
    assert [n.text for n in inbox.pending()] == ["half made"]


def test_a_cancel_during_an_async_callback_puts_the_notes_back(ws):
    """The cancel lands before the result carrying the notes is handed
    back, so the model never read them: they stay queued, and the turn's
    end does not settle them."""
    reached = []

    async def slow(notes):
        reached.append(notes)
        await asyncio.sleep(10)

    inbox = Inbox(on_delivered=slow)
    note = inbox.put("keep me")

    async def go():
        async with ws.turn("r1", inbox=inbox) as turn:
            task = asyncio.ensure_future(turn.adeliver("ok"))
            while not reached:
                await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        return turn

    turn = asyncio.run(go())
    assert turn.status == "completed"
    assert inbox.pending() == [note] and inbox.delivered() == []
