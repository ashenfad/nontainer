"""The agno adapter under a turn: the db and the toolkit stage their
writes while ``ws.turn`` is open, and the turn's end lands them once."""

import threading
import time

import pytest

pytest.importorskip("agno")

from agno.agent import Agent  # noqa: E402
from agno.run.base import RunStatus  # noqa: E402
from agno.session import AgentSession  # noqa: E402
from test_agno_db import ScriptedModel, build, run_turn, write_turn  # noqa: E402

from nontainer import Store  # noqa: E402
from nontainer.adapters import agno as agno_adapter  # noqa: E402
from nontainer.adapters.agno import (  # noqa: E402
    WorkspaceTools,
    finish_turn,
    status_of_error,
)
from nontainer.adapters.agno_db import KvgitSessionDb  # noqa: E402
from nontainer.inbox import Inbox  # noqa: E402
from nontainer.turns import Turns  # noqa: E402


def _turn_commits(ws):
    return [e.info for e in ws.log() if e.info.get("tool") == "turn"]


def test_the_db_stages_under_a_turn_and_the_turn_commits_once(tmp_path):
    ws, db, tk, agent = build(tmp_path, commit="turn")
    with ws.turn(inbox=tk.inbox) as turn:
        out = run_turn(agent, write_turn("/workspace/a.txt", "A"))
        turn.bind(out.run_id)
        # agno has persisted the run, and it is still staged
        assert ws.uncommitted and _turn_commits(ws) == []
    assert not ws.uncommitted
    assert _turn_commits(ws) == [{"tool": "turn", "runs": {out.run_id: "completed"}}]
    assert [r.run_id for r in db.get_session(ws.session).runs] == [out.run_id]
    ws.close()


def test_a_management_write_under_a_turn_rides_its_commit(tmp_path):
    ws, db, tk, agent = build(tmp_path)
    first = run_turn(agent, ["one"])
    head = ws.head
    with ws.turn("r2") as turn:
        assert db.delete_run(first.run_id) is True
        assert ws.head == head and ws.uncommitted
        turn.end("completed")
    assert ws.log(limit=1)[0].info == {"tool": "turn", "runs": {"r2": "completed"}}
    ws.close()


def test_a_delete_queued_behind_an_ending_turn_commits_itself(tmp_path, monkeypatch):
    """A management write that waits on the workspace lock while a turn
    ends gets the lock once the turn is gone: it is not staged under a
    turn whose commit has already landed."""
    ws, db, tk, agent = build(tmp_path)
    first = run_turn(agent, ["one"])
    inside, go = threading.Event(), threading.Event()

    class Held(Inbox):
        def settle(self):
            inside.set()
            go.wait(5)
            super().settle()

    turn = ws.turn("r2", inbox=Held())
    with ws.lock:
        ws.files.fs.write("/workspace/a.txt", b"a")
    ender = threading.Thread(target=turn.end, args=("completed",))
    ender.start()
    assert inside.wait(5)
    deleter = threading.Thread(target=db.delete_run, args=(first.run_id,))
    deleter.start()
    release = Turns._release

    def slow_release(self, t):
        time.sleep(0.1)
        release(self, t)

    monkeypatch.setattr(Turns, "_release", slow_release)
    go.set()
    ender.join(5)
    deleter.join(5)
    assert not ws.uncommitted
    assert ws.log(limit=1)[0].info == {"tool": "delete_run"}
    ws.close()


def test_end_turn_stands_down_under_a_turn(tmp_path):
    """Turn granularity with no session db: the post hook commits a turn
    the embedder runs without ``ws.turn``, and leaves one with it to the
    turn's end."""
    store = Store(memory=True)
    ws = store.open("s")
    tk = WorkspaceTools(ws, commit="turn")
    agent = Agent(
        model=ScriptedModel(),
        tools=[tk],
        post_hooks=[tk.end_turn],
        telemetry=False,
    )
    with ws.turn("r1") as turn:
        run_turn(agent, write_turn("/workspace/a.txt", "A"))
        assert ws.uncommitted
        turn.end("completed")
    assert ws.log(limit=1)[0].info == {"tool": "turn", "runs": {"r1": "completed"}}
    run_turn(agent, write_turn("/workspace/b.txt", "B"))
    assert not ws.uncommitted and ws.log(limit=1)[0].info == {"tool": "turn"}
    ws.close()
    store.close()


def _cancelled_run(db, ws, run_id="r1"):
    """A run agno stored as cancelled, its tool result kept."""
    session = db.get_session(ws.session)
    data = session.to_dict() if session else {"session_id": ws.session}
    data.setdefault("agent_id", "a")
    run = {
        "run_id": run_id,
        "agent_id": "a",
        "session_id": ws.session,
        "status": RunStatus.cancelled.value,
        "messages": [
            {"role": "user", "content": "write a"},
            {"role": "tool", "content": "ok", "tool_call_id": "c1"},
        ],
    }
    data["runs"] = [*(data.get("runs") or []), run]
    db.upsert_session(AgentSession.from_dict(data))


def test_finish_turn_keeps_a_cancelled_run_and_commits_once(tmp_path):
    ws, db, tk, agent = build(tmp_path)
    tk.inbox.put("also b")
    tk.inbox.drain()
    turn = ws.turn("r1", inbox=tk.inbox)
    _cancelled_run(db, ws)
    assert finish_turn(turn, db, ws.session, "cancelled", "stopped by the user")
    assert _turn_commits(ws) == [
        {"tool": "turn", "runs": {"r1": "cancelled"}, "message": "stopped by the user"}
    ]
    run = db.get_session(ws.session).runs[-1]
    assert run.status == RunStatus.completed  # kept, so agno's history has it
    assert run.messages[-1].content.startswith("[turn aborted early: stopped by")
    assert tk.inbox.delivered() == []
    ws.close()


def test_a_run_that_cannot_be_kept_still_ends_its_turn(tmp_path, monkeypatch, caplog):
    ws, db, tk, agent = build(tmp_path)

    def broken(*args, **kwargs):
        raise RuntimeError("the db is gone")

    monkeypatch.setattr(agno_adapter, "keep_aborted_run", broken)
    turn = ws.turn("r1")
    ws.files.fs.write("/workspace/a.txt", b"a")
    finish_turn(turn, db, ws.session, "failed", "boom")
    assert turn.status == "failed" and ws.turns.current is None
    assert "could not keep aborted run r1" in caplog.text
    ws.close()


def test_an_interrupted_run_is_left_for_a_resume(tmp_path, monkeypatch):
    ws, db, tk, agent = build(tmp_path)
    kept = []
    monkeypatch.setattr(agno_adapter, "keep_aborted_run", lambda *a: kept.append(a))
    finish_turn(ws.turn("r1"), db, ws.session, "interrupted", "overloaded")
    assert kept == []
    ws.close()


def test_status_of_error_reads_agnos_error_type():
    assert status_of_error("model_provider_error") == "interrupted"
    assert status_of_error("model_rate_limit_error") == "interrupted"
    assert status_of_error("RuntimeError") == "failed"
    assert status_of_error(None) == "failed"


def test_a_db_bound_to_another_workspace_is_not_held_by_this_ones_turn(tmp_path):
    """The turn belongs to a workspace, and so does the db: a turn open
    on one session does not hold another session's writes."""
    store = Store(memory=True)
    a, b = store.open("a"), store.open("b")
    db_b = KvgitSessionDb(b)
    with a.turn("ra"):
        _cancelled_run(db_b, b, "rb")
        assert not b.uncommitted
    a.close()
    b.close()
    store.close()
