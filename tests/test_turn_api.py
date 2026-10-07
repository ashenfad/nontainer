"""The turn: one run of a harness's loop on a workspace, from its
beginning to how it ended, landing in one commit."""

import asyncio

import pytest

from nontainer import NotSupportedError, Store, TurnInProgress, conversation
from nontainer.conversation import Index
from nontainer.inbox import Inbox


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
    with pytest.raises(RuntimeError, match="already ended"):
        turn.end("failed")
    with pytest.raises(ValueError, match="not a run status"):
        ws.turns.begin("r2").end("sideways")


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
