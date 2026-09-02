"""KvgitStoreDb: one agno db over a whole kvgit store, a branch per session.

The embedder's registry is a dict of live workspaces over one disk
store; the db routes each session call to a view over the live
workspace and lists sessions by reading branch heads.
"""

import pytest

from nontainer import workspace
from nontainer.errors import NotSupportedError

pytest.importorskip("agno")

from agno.agent import Agent  # noqa: E402
from agno.session import AgentSession  # noqa: E402
from test_agno_db import ScriptedModel, run_turn, write_turn  # noqa: E402

from nontainer.adapters.agno import WorkspaceTools  # noqa: E402
from nontainer.adapters.agno_db import (  # noqa: E402
    RUN_PREFIX,
    SESSION_KEY,
    KvgitStoreDb,
    fork_session,
)


class Registry:
    """The embedder's half: live workspaces over one store, by session."""

    def __init__(self, store):
        self.store = store
        self.live = {}

    def open(self, session_id):
        ws = self.live.get(session_id)
        if ws is None:
            ws = self.live[session_id] = workspace(session_id, store=self.store)
        return ws

    def close(self):
        for ws in self.live.values():
            ws.close()
        self.live.clear()


@pytest.fixture
def registry(tmp_path):
    reg = Registry(tmp_path / "store")
    yield reg
    reg.close()


def build(registry, tmp_path, session_id, *, user_id=None):
    """A store db plus an agent on one of its sessions, wired as documented."""
    db = KvgitStoreDb(
        registry.store, open=registry.open, db_path=str(tmp_path / "agno")
    )
    ws = registry.open(session_id)
    tk = WorkspaceTools(ws, checkpoint="turn", session_db=db)
    agent = Agent(
        model=ScriptedModel(),
        db=db,
        session_id=session_id,
        user_id=user_id,
        tools=[tk],
        post_hooks=[tk.end_turn],
        add_history_to_context=True,
        telemetry=False,
    )
    return db, ws, agent


def kv_of(ws):
    return ws._provider.kv


# -- routing -------------------------------------------------------------------


def test_turns_route_to_the_live_workspace_and_commit_once(registry, tmp_path):
    db, ws, agent = build(registry, tmp_path, "chat-1")
    before = len(list(ws.history()))

    run_turn(agent, write_turn("a.txt", "A"))

    assert len(list(ws.history())) == before + 1
    assert not ws.dirty
    assert ws.fs.read("a.txt") == b"A"
    assert len(db.get_session("chat-1").runs) == 1


def test_get_session_of_an_unknown_id_creates_nothing(registry, tmp_path):
    db, ws, agent = build(registry, tmp_path, "chat-1")
    assert db.get_session("never-opened") is None
    assert "never-opened" not in registry.live
    assert "never-opened" not in db._branches()


def test_the_toolkit_accepts_a_store_db_that_owns_its_workspace(registry, tmp_path):
    db = KvgitStoreDb(
        registry.store, open=registry.open, db_path=str(tmp_path / "agno")
    )
    ws = registry.open("chat-1")
    tk = WorkspaceTools(ws, checkpoint="turn", session_db=db)
    assert tk.end_turn() is None

    stranger = workspace("chat-1", store=tmp_path / "elsewhere")
    with pytest.raises(ValueError, match="same workspace"):
        WorkspaceTools(stranger, checkpoint="turn", session_db=db)
    stranger.close()


# -- listing -------------------------------------------------------------------


def test_get_sessions_lists_every_branch_in_the_store(registry, tmp_path):
    db, ws1, agent1 = build(registry, tmp_path, "chat-1", user_id="ann")
    run_turn(agent1, write_turn("a.txt", "A"))
    _, ws2, agent2 = build(registry, tmp_path, "chat-2", user_id="bob")
    run_turn(agent2, write_turn("b.txt", "B"))

    sessions = db.get_sessions()
    assert sorted(s.session_id for s in sessions) == ["chat-1", "chat-2"]
    assert all(isinstance(s, AgentSession) and s.runs for s in sessions)

    assert [s.session_id for s in db.get_sessions(user_id="ann")] == ["chat-1"]

    rows, total = db.get_sessions(deserialize=False, limit=1, sort_by="created_at")
    assert total == 2 and len(rows) == 1
    assert rows[0]["runs"] and rows[0]["runs"][0]["run_id"]


def test_listing_reads_committed_heads_without_opening(registry, tmp_path):
    db, ws, agent = build(registry, tmp_path, "chat-1")
    run_turn(agent, write_turn("a.txt", "A"))
    registry.close()  # nothing open now

    listed = db.get_sessions()
    assert [s.session_id for s in listed] == ["chat-1"]
    assert len(listed[0].runs) == 1
    assert registry.live == {}  # listing opened no workspace


def test_search_past_sessions_sees_the_other_sessions(registry, tmp_path):
    """agno's own tool, through the store: the current session is
    skipped and the others come back with previews."""
    db, ws1, agent1 = build(registry, tmp_path, "chat-1", user_id="ann")
    run_turn(agent1, write_turn("a.txt", "A"))
    _, ws2, agent2 = build(registry, tmp_path, "chat-2", user_id="ann")
    run_turn(agent2, write_turn("b.txt", "B"))

    from agno.agent._default_tools import get_search_past_sessions_function

    tool = get_search_past_sessions_function(
        agent2, user_id="ann", current_session_id="chat-2"
    )
    import json

    previews = json.loads(tool())
    assert [p["session_id"] for p in previews] == ["chat-1"]


# -- fork ----------------------------------------------------------------------


def test_agno_fork_session_forks_the_files_and_copies_the_chat(registry, tmp_path):
    db, ws, agent = build(registry, tmp_path, "chat-1", user_id="ann")
    run_turn(agent, write_turn("a.txt", "A"))
    run_turn(agent, write_turn("b.txt", "B"))
    parent_ids = [r.run_id for r in db.get_session("chat-1").runs]

    child_id = agent.fork_session()

    assert child_id in db._branches()
    child = registry.open(child_id)
    assert child.fs.read("a.txt") == b"A" and child.fs.read("b.txt") == b"B"
    assert not child.dirty

    session = db.get_session(child_id)
    assert session.session_data["forked_from_session_id"] == "chat-1"
    assert len(session.runs) == 2
    assert [r.run_id for r in session.runs] != parent_ids  # agno's copy, re-keyed
    assert all(r.forked_from_session_id == "chat-1" for r in session.runs)
    assert len(db.get_session("chat-1").runs) == 2  # the parent is untouched

    # the fork carries on as an ordinary session
    tk = WorkspaceTools(child, checkpoint="turn", session_db=db)
    child_agent = Agent(
        model=ScriptedModel(),
        db=db,
        session_id=child_id,
        tools=[tk],
        add_history_to_context=True,
        telemetry=False,
    )
    run_turn(child_agent, write_turn("c.txt", "C"))
    assert len(db.get_session(child_id).runs) == 3
    assert child.fs.read("c.txt") == b"C"


def test_the_workspace_fork_shows_its_lineage_in_the_listing(registry, tmp_path):
    db, ws, agent = build(registry, tmp_path, "chat-1")
    run_turn(agent, write_turn("a.txt", "A"))

    child = fork_session(ws, "what-if")
    registry.live["what-if"] = child

    rows, _ = db.get_sessions(deserialize=False)
    by_id = {row["session_id"]: row for row in rows}
    assert by_id["what-if"]["session_data"]["forked_from_session_id"] == "chat-1"
    assert len(by_id["what-if"]["runs"]) == 1


def test_a_new_id_without_lineage_opens_a_fresh_branch(registry, tmp_path):
    db, ws, agent = build(registry, tmp_path, "chat-1")
    db.upsert_session(AgentSession(session_id="brand-new", agent_id="a"))
    assert "brand-new" in registry.live
    assert db.get_session("brand-new") is not None
    assert kv_of(registry.open("brand-new")).get(RUN_PREFIX + "x") is None


# -- the rest of the session table --------------------------------------------


def test_delete_clears_the_conversation_and_leaves_the_branch(registry, tmp_path):
    db, ws, agent = build(registry, tmp_path, "chat-1")
    run_turn(agent, write_turn("a.txt", "A"))

    assert db.delete_session("chat-1") is True
    assert kv_of(ws).get(SESSION_KEY) is None
    assert ws.fs.read("a.txt") == b"A"
    assert "chat-1" in db._branches()
    assert db.delete_session("nope") is False


def test_rename_routes_to_the_branch(registry, tmp_path):
    db, ws, agent = build(registry, tmp_path, "chat-1")
    run_turn(agent, write_turn("a.txt", "A"))
    renamed = db.rename_session("chat-1", None, "The plan")
    assert renamed.session_data["session_name"] == "The plan"
    assert db.rename_session("nope", None, "x") is None


def test_team_sessions_are_refused_before_anything_opens(registry, tmp_path):
    from agno.session import TeamSession

    db = KvgitStoreDb(
        registry.store, open=registry.open, db_path=str(tmp_path / "agno")
    )
    with pytest.raises(NotSupportedError):
        db.upsert_session(TeamSession(session_id="t1", team_id="team"))
    assert registry.live == {}
