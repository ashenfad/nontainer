"""The run methods answer on every agno, not only agno 3.

``get_run``/``get_runs`` exist for agno 3's runs table (#190) and were
built from agno 3's row helpers, so on agno 2 a caller probing for them
(``getattr(db, "get_run", ...)``) got an ImportError. agno 2 never calls
them itself; on agno 2 the rows are built locally, in agno 3's shape.
"""

import pytest

pytest.importorskip("agno")

from agno.models.message import Message  # noqa: E402
from agno.run.agent import RunOutput  # noqa: E402
from agno.session.agent import AgentSession  # noqa: E402

from nontainer import Store  # noqa: E402
from nontainer.adapters.agno_db import KvgitSessionDb  # noqa: E402


def _run(i):
    return RunOutput(
        run_id=f"r{i}",
        agent_id="a",
        session_id="s1",
        messages=[Message(role="user", content=f"q{i}")],
    )


@pytest.fixture
def db(tmp_path):
    store = Store(str(tmp_path / "store"))
    ws = store.open("s1")
    db = KvgitSessionDb(ws)
    db.seed(
        AgentSession(
            session_id="s1", agent_id="a", user_id="u", runs=[_run(1), _run(2)]
        )
    )
    yield db
    ws.close()
    store.close()


def test_get_run_answers_on_this_agno(db):
    run = db.get_run("r2")
    assert isinstance(run, RunOutput) and run.run_id == "r2"
    assert db.get_run("nope") is None


def test_get_runs_answers_in_agno_3s_row_shape(db):
    assert [r.run_id for r in db.get_runs(session_id="s1")] == ["r1", "r2"]
    rows, total = db.get_runs(deserialize=False, limit=1, page=2)
    assert total == 2 and [r["run_id"] for r in rows] == ["r2"]
    row = rows[0]
    assert row["run_index"] == 1 and row["session_id"] == "s1"
    assert row["run_type"] == "agent" and row["user_id"] == "u"
