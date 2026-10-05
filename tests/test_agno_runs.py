"""agno 3's run methods read and remove the branch's runs (#190).

agno 3 moved runs out of the session row and gave ``BaseDb``
``get_run``, ``get_runs``, ``delete_run`` and ``delete_runs``. Inherited
from ``JsonDb``, they looked for a runs file under ``db_path`` that never
exists: every run read as absent, and a delete reported success having
removed nothing. A run upserted and then deleted stayed in the session.
"""

import pytest

pytest.importorskip("agno")

from agno.db.base import BaseDb, SessionType  # noqa: E402

if not hasattr(BaseDb, "get_run"):
    pytest.skip("agno 2 has no runs table", allow_module_level=True)

from agno.models.message import Message  # noqa: E402
from agno.run.agent import RunOutput  # noqa: E402
from agno.run.base import RunStatus  # noqa: E402
from agno.session.agent import AgentSession  # noqa: E402
from test_agno_db import build, run_turn, write_turn  # noqa: E402

from nontainer import Store  # noqa: E402
from nontainer.adapters.agno_db import KvgitSessionDb, KvgitStoreDb  # noqa: E402
from nontainer.compaction import Fold, in_force, record  # noqa: E402


def _run(i, session="s1", status=RunStatus.completed):
    return RunOutput(
        run_id=f"r{i}",
        agent_id="a",
        session_id=session,
        status=status,
        messages=[Message(id=f"m{i}", role="user", content=f"q{i}")],
    )


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "store"))
    yield s
    s.close()


@pytest.fixture
def opened(store):
    live = {}

    def open_session(sid):
        if sid not in live:
            live[sid] = store.open(sid)
        return live[sid]

    yield open_session
    for ws in live.values():
        ws.close()


def _seed(ws, *runs, session=None):
    KvgitSessionDb(ws).seed(
        AgentSession(
            session_id=session or ws.session, agent_id="a", user_id="u", runs=list(runs)
        )
    )


def _ids(session):
    return [r.run_id for r in (session.runs or [])]


# -- one branch ------------------------------------------------------------------


def test_get_run_and_get_runs_answer_what_get_session_holds(opened):
    ws = opened("s1")
    _seed(ws, _run(1), _run(2, status=RunStatus.error))
    db = KvgitSessionDb(ws)

    assert _ids(db.get_session("s1", SessionType.AGENT)) == ["r1", "r2"]
    run = db.get_run("r1")
    assert isinstance(run, RunOutput) and run.run_id == "r1"
    assert db.get_run("nope") is None
    assert [r.run_id for r in db.get_runs(session_id="s1")] == ["r1", "r2"]
    assert db.get_runs(session_id="other") == []
    # agno's filters and its row shape
    assert [r.run_id for r in db.get_runs(status=RunStatus.error)] == ["r2"]
    rows, total = db.get_runs(deserialize=False, limit=1, page=2)
    assert total == 2 and [r["run_id"] for r in rows] == ["r2"]
    row = db.get_run("r1", deserialize=False)
    assert row["run_index"] == 0 and row["session_id"] == "s1"
    assert row["run_data"]["run_id"] == "r1" and row["run_type"] == "agent"


def test_get_runs_sees_a_live_turn(tmp_path):
    ws, db, tk, agent = build(tmp_path)
    out = run_turn(agent, write_turn("a.txt", "A"))
    assert [r.run_id for r in db.get_runs()] == [out.run_id]
    assert db.get_run(out.run_id).run_id == out.run_id
    ws.close()


def test_delete_run_removes_it_and_commits_between_turns(opened):
    ws = opened("s1")
    _seed(ws, _run(1), _run(2))
    db = KvgitSessionDb(ws)
    before = len(list(ws.log()))

    assert db.delete_run("r2") is True
    assert db.delete_run("r2") is False  # already gone
    assert _ids(db.get_session("s1", SessionType.AGENT)) == ["r1"]
    assert db.get_run("r2") is None
    # a delete between turns is committed, as delete_session is
    entries = list(ws.log())
    assert len(entries) == before + 1 and entries[0].info == {"tool": "delete_run"}
    # and a forward change: the commit before it still holds the run
    assert not ws.uncommitted


def test_a_run_upserted_then_deleted_never_lands(opened):
    """The issue's sequence: the upsert is staged, the delete takes it
    back out of the staging, and the turn's commit holds neither."""
    ws = opened("s1")
    _seed(ws, _run(1))
    db = KvgitSessionDb(ws)
    db.upsert_run(run=_run(3), session_id="s1", user_id="u")
    db.delete_runs(["r3"])
    ws.commit(info={"tool": "turn"})
    assert _ids(db.get_session("s1", SessionType.AGENT)) == ["r1"]
    assert db.get_run("r3") is None


def test_a_fold_anchored_in_a_deleted_run_stops_applying(opened):
    """Compaction anchors a fold on a message id; with the run holding
    it gone, the fold is not in force and the full history is sent."""
    ws = opened("s1")
    _seed(ws, _run(1), _run(2))
    record(ws, Fold(through="m2", summary="earlier"))
    db = KvgitSessionDb(ws)
    ids = [m.id for r in db.get_session("s1").runs for m in r.messages]
    assert in_force(ws, ids) is not None
    db.delete_run("r2")
    ids = [m.id for r in db.get_session("s1").runs for m in r.messages]
    assert in_force(ws, ids) is None


# -- the whole store ---------------------------------------------------------


def test_the_store_finds_a_run_by_id_alone(store, opened, tmp_path):
    """agno 3 and AgentOS ask by run id with no session; the store finds
    the session that holds it, committed or not."""
    _seed(opened("s1"), _run(1))
    _seed(opened("s2"), _run(2, session="s2"))
    db = KvgitStoreDb(store, open=opened, db_path=str(tmp_path / "agno"))

    assert db.get_run("r2").run_id == "r2"
    assert db.get_run("nope") is None
    assert sorted(r.run_id for r in db.get_runs()) == ["r1", "r2"]
    assert [r.run_id for r in db.get_runs(session_id="s2")] == ["r2"]
    assert db.get_runs(session_id="absent") == []


def test_the_store_deletes_where_the_run_is(store, opened, tmp_path):
    _seed(opened("s1"), _run(1))
    _seed(opened("s2"), _run(2, session="s2"), _run(3, session="s2"))
    db = KvgitStoreDb(store, open=opened, db_path=str(tmp_path / "agno"))

    assert db.delete_run("r2") is True
    assert db.delete_run("nope") is False
    db.delete_runs(["r1", "r3"])
    assert db.get_runs() == []
    assert _ids(db.get_session("s1", SessionType.AGENT)) == []
    assert _ids(db.get_session("s2", SessionType.AGENT)) == []


def test_the_store_takes_back_a_run_staged_through_it(store, opened, tmp_path):
    """The repro's sequence through the store: the staged run is not in
    any committed head, so it is found through the session the store
    wrote it to."""
    ws = opened("s1")
    _seed(ws, _run(1))
    db = KvgitStoreDb(store, open=opened, db_path=str(tmp_path / "agno"))
    db.upsert_run(run=_run(3), session_id="s1", user_id="u")
    assert db.get_run("r3").run_id == "r3"
    db.delete_runs(["r3"])
    ws.commit(info={"tool": "turn"})
    assert _ids(db.get_session("s1", SessionType.AGENT)) == ["r1"]
