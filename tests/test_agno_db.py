"""KvgitSessionDb: the conversation stored in the workspace branch.

Driven by a real agno ``Agent`` over a scripted model — the run loop,
the toolkit, the workspace and the sandbox all execute; only the LLM is
faked, so no key and no network.
"""

import json
from typing import Any, AsyncIterator, Iterator, List

import pytest

from nontainer import Workspace
from nontainer.errors import NotSupportedError, WorkspaceError
from nontainer.providers import KvgitProvider

pytest.importorskip("agno")

from agno.agent import Agent  # noqa: E402
from agno.models.base import Model  # noqa: E402
from agno.models.message import Message  # noqa: E402
from agno.models.response import ModelResponse  # noqa: E402
from agno.session import AgentSession  # noqa: E402

from nontainer.adapters.agno import WorkspaceTools  # noqa: E402
from nontainer.adapters.agno_db import (  # noqa: E402
    RUN_PREFIX,
    SESSION_KEY,
    KvgitSessionDb,
    fork_session,
)


class ScriptedModel(Model):
    """Canned responses, consumed in order.

    A step is either a string (the assistant's reply, ending the turn)
    or a ``(tool_name, args)`` pair (one tool call, after which the run
    loop re-invokes). An exhausted script answers "done", so a turn
    always terminates.
    """

    def __init__(self, script: List[Any] | None = None) -> None:
        super().__init__(id="scripted", name="Scripted", provider="scripted")
        self.script = list(script or [])
        self.seen: List[Message] = []

    def _next(self) -> ModelResponse:
        step = self.script.pop(0) if self.script else "done"
        response = ModelResponse(role="assistant")
        if isinstance(step, str):
            response.content = step
        else:
            name, args = step
            response.tool_calls = [
                {
                    "id": f"call_{name}_{len(self.script)}",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)},
                }
            ]
        return response

    def invoke(self, messages: List[Message], **kwargs: Any) -> ModelResponse:
        self.seen = list(messages)
        return self._next()

    async def ainvoke(self, messages: List[Message], **kwargs: Any) -> ModelResponse:
        return self._next()

    def invoke_stream(
        self, messages: List[Message], **kwargs: Any
    ) -> Iterator[ModelResponse]:
        yield self._next()

    async def ainvoke_stream(
        self, messages: List[Message], **kwargs: Any
    ) -> AsyncIterator[ModelResponse]:
        yield self._next()

    def _parse_provider_response(self, response: Any, **kwargs: Any) -> ModelResponse:
        return response

    def _parse_provider_response_delta(self, response: Any) -> ModelResponse:
        return response


def write_turn(path: str, content: str) -> list:
    """A turn that writes one file and then replies."""
    return [("file_write", {"path": path, "content": content}), f"wrote {path}"]


def make_ws(session: str = "chat") -> Workspace:
    return Workspace(KvgitProvider.open(None, session=session))


def build(tmp_path, *, checkpoint: str = "turn", session: str = "chat"):
    """Workspace + session db + toolkit + agent, wired as documented."""
    ws = make_ws(session)
    db = KvgitSessionDb(ws, db_path=str(tmp_path / "agno"))
    tk = WorkspaceTools(ws, checkpoint=checkpoint, session_db=db)
    agent = Agent(
        model=ScriptedModel(),
        db=db,
        session_id=ws.session,
        tools=[tk],
        post_hooks=[tk.end_turn],
        # the point of storing the conversation in the branch is that the
        # model sees the branch's history, so put it in the context
        add_history_to_context=True,
        telemetry=False,
    )
    return ws, db, tk, agent


def run_turn(agent, script, message="go"):
    agent.model.script = list(script)
    return agent.run(message)


def kv_of(ws) -> Any:
    return ws._provider.kv


def run_keys(ws) -> list:
    return sorted(k for k in kv_of(ws).keys() if k.startswith(RUN_PREFIX))


# -- the commit trigger --------------------------------------------------------


def test_a_turn_is_one_commit_holding_files_and_conversation(tmp_path):
    ws, db, tk, agent = build(tmp_path)
    before = len(list(ws.history()))

    run_turn(agent, write_turn("a.txt", "A"))

    entries = list(ws.history())
    assert len(entries) == before + 1
    assert entries[0].info == {"tool": "turn"}
    assert not ws.dirty  # the turn is fully committed, nothing left staged
    assert ws.fs.read("a.txt") == b"A"
    assert len(run_keys(ws)) == 1

    session = db.get_session(ws.session)
    assert len(session.runs) == 1

    # second turn: one more commit, one more run key
    run_turn(agent, write_turn("b.txt", "B"))
    assert len(list(ws.history())) == before + 2
    assert len(run_keys(ws)) == 2
    assert len(db.get_session(ws.session).runs) == 2
    ws.close()


def test_the_turn_commit_carries_the_run_key(tmp_path):
    """Files and conversation are in the SAME commit: rolling the turn
    back must lose both."""
    ws, db, tk, agent = build(tmp_path)
    run_turn(agent, write_turn("a.txt", "A"))
    head = ws.head

    run_turn(agent, write_turn("b.txt", "B"))
    assert len(run_keys(ws)) == 2

    ws.restore(head)
    assert not ws.fs.exists("b.txt")
    assert len(run_keys(ws)) == 1
    assert len(db.get_session(ws.session).runs) == 1
    ws.close()


def test_end_turn_stands_down_when_a_session_db_is_wired(tmp_path):
    ws = make_ws()
    db = KvgitSessionDb(ws, db_path=str(tmp_path / "agno"))
    tk = WorkspaceTools(ws, checkpoint="turn", session_db=db)

    tk.functions["file_write"].entrypoint(path="a.txt", content="A")
    assert ws.dirty
    assert tk.end_turn() is None  # the db owns the commit
    assert ws.dirty  # ... and it has not happened yet
    ws.close()


def test_session_db_must_be_over_the_same_workspace(tmp_path):
    ws = make_ws("chat")
    other = make_ws("other")
    db = KvgitSessionDb(other, db_path=str(tmp_path / "agno"))
    with pytest.raises(ValueError, match="same workspace"):
        WorkspaceTools(ws, checkpoint="turn", session_db=db)
    ws.close()
    other.close()


def test_per_call_mode_commits_its_trailing_write(tmp_path):
    """Every mutating call commits; the db's own write closes the turn,
    so the head at the next user message includes the conversation."""
    ws, db, tk, agent = build(tmp_path, checkpoint="call")
    before = len(list(ws.history()))

    run_turn(agent, write_turn("a.txt", "A"))

    entries = list(ws.history())
    assert len(entries) == before + 2  # the file_write call, then the turn
    assert entries[0].info == {"tool": "turn"}
    assert not ws.dirty
    assert len(db.get_session(ws.session).runs) == 1
    ws.close()


def test_a_session_write_with_no_run_does_not_commit(tmp_path):
    """agno creating the record before the first run stages, it does not
    commit; the write rides into the turn's commit."""
    ws = make_ws()
    db = KvgitSessionDb(ws, db_path=str(tmp_path / "agno"))
    before = len(list(ws.history()))

    db.upsert_session(AgentSession(session_id=ws.session, agent_id="a"))

    assert len(list(ws.history())) == before
    assert ws.dirty
    assert db.get_session(ws.session) is not None
    ws.close()


# -- rewind --------------------------------------------------------------------


def test_restore_rewinds_the_conversation_with_the_files(tmp_path):
    ws, db, tk, agent = build(tmp_path)
    run_turn(agent, write_turn("a.txt", "A"), message="first")
    head = ws.head
    kept = db.get_session(ws.session).runs[-1].run_id
    run_turn(agent, write_turn("b.txt", "B"), message="second")
    rewound = db.get_session(ws.session).runs[-1].run_id

    ws.restore(head)
    run_turn(agent, write_turn("c.txt", "C"), message="third")

    run_ids = [r.run_id for r in db.get_session(ws.session).runs]
    assert len(run_ids) == 2
    assert run_ids[0] == kept
    assert rewound not in run_ids

    # what the model was shown on the third turn: no rewound turn in it
    said = [str(m.content) for m in agent.model.seen if m.role == "user"]
    assert "first" in said and "third" in said
    assert "second" not in said
    assert ws.fs.exists("a.txt") and ws.fs.exists("c.txt")
    assert not ws.fs.exists("b.txt")
    ws.close()


def test_a_stale_session_is_refused_and_writes_nothing(tmp_path):
    """The cache_session=True shape: restore, then a run written from a
    session object that still holds the rewound turn."""
    ws, db, tk, agent = build(tmp_path)
    run_turn(agent, write_turn("a.txt", "A"))
    head = ws.head
    run_turn(agent, write_turn("b.txt", "B"))

    stale = db.get_session(ws.session, deserialize=False)
    assert len(stale["runs"]) == 2
    ws.restore(head)
    before = dict(kv_of(ws).get(SESSION_KEY))

    # the stale object appends a third run to its two
    third = dict(stale["runs"][-1])
    third["run_id"] = "run-from-the-future"
    stale["runs"] = [*stale["runs"], third]

    with pytest.raises(NotSupportedError, match="cache_session"):
        db.upsert_session(AgentSession.from_dict(stale))

    assert dict(kv_of(ws).get(SESSION_KEY)) == before
    assert RUN_PREFIX + "run-from-the-future" not in kv_of(ws)
    assert len(db.get_session(ws.session).runs) == 1
    ws.close()


# -- one session per workspace -------------------------------------------------


def test_a_foreign_session_id_is_refused(tmp_path):
    ws, db, tk, agent = build(tmp_path)
    run_turn(agent, ["hello"])

    with pytest.raises(NotSupportedError, match="fork_session"):
        db.upsert_session(AgentSession(session_id="somewhere-else", agent_id="a"))
    assert kv_of(ws)[SESSION_KEY]["session_id"] == ws.session
    ws.close()


@pytest.mark.skipif(
    not hasattr(Agent, "fork_session"),
    reason="Agent.fork_session arrived after the declared agno floor",
)
def test_agno_fork_session_cannot_write_a_second_session(tmp_path):
    """agno's own fork deep-copies the runs into a new session id
    through the SAME db, which assumes one db holding many sessions.
    The single-session guard refuses the write.

    agno logs and swallows exceptions from upsert_session, so the raise
    does not reach the caller — what the caller sees is that nothing
    was written."""
    ws, db, tk, agent = build(tmp_path)
    run_turn(agent, write_turn("a.txt", "A"))

    agent.fork_session()

    assert kv_of(ws)[SESSION_KEY]["session_id"] == ws.session
    assert len(run_keys(ws)) == 1
    ws.close()


def test_team_sessions_are_not_supported(tmp_path):
    from agno.session import TeamSession

    ws = make_ws()
    db = KvgitSessionDb(ws, db_path=str(tmp_path / "agno"))
    with pytest.raises(NotSupportedError, match="TeamSession"):
        db.upsert_session(TeamSession(session_id=ws.session, team_id="t"))
    assert SESSION_KEY not in kv_of(ws)
    ws.close()


def test_a_run_without_a_run_id_is_refused(tmp_path):
    ws = make_ws()
    db = KvgitSessionDb(ws, db_path=str(tmp_path / "agno"))
    session = AgentSession.from_dict(
        {
            "session_id": ws.session,
            "agent_id": "a",
            "runs": [{"agent_id": "a", "run_id": None}],
        }
    )
    session.runs[0].run_id = None
    with pytest.raises(WorkspaceError, match="run_id"):
        db.upsert_session(session)
    ws.close()


# -- fork ----------------------------------------------------------------------


def test_fork_session_inherits_the_conversation(tmp_path):
    ws, db, tk, agent = build(tmp_path)
    run_turn(agent, write_turn("a.txt", "A"))
    run_turn(agent, write_turn("b.txt", "B"))
    parent_runs = [r.run_id for r in db.get_session(ws.session).runs]

    child = fork_session(ws, "what-if", conversation="inherit")
    child_db = KvgitSessionDb(child, db_path=str(tmp_path / "agno"))

    assert not child.dirty  # the rewrite is committed: the head is consistent
    session = child_db.get_session("what-if")
    assert session is not None
    assert [r.run_id for r in session.runs] == parent_runs
    assert kv_of(child)[SESSION_KEY]["forked_from_session_id"] == ws.session
    assert child.fs.read("a.txt") == b"A" and child.fs.read("b.txt") == b"B"
    # the parent is untouched
    assert kv_of(ws)[SESSION_KEY]["session_id"] == ws.session
    child.close()
    ws.close()


def test_fork_session_fresh_keeps_the_files_and_drops_the_chat(tmp_path):
    ws, db, tk, agent = build(tmp_path)
    run_turn(agent, write_turn("a.txt", "A"))

    child = fork_session(ws, "clean-slate", conversation="fresh")
    child_db = KvgitSessionDb(child, db_path=str(tmp_path / "agno"))

    session = child_db.get_session("clean-slate")
    assert session.runs == []
    assert run_keys(child) == []
    assert child.fs.read("a.txt") == b"A"
    assert len(db.get_session(ws.session).runs) == 1  # parent keeps its chat
    child.close()
    ws.close()


def test_the_fork_marker_survives_the_next_turn(tmp_path):
    """agno's session object has no lineage field, so the first upsert
    after a fork must not erase it."""
    ws, db, tk, agent = build(tmp_path)
    run_turn(agent, write_turn("a.txt", "A"))
    child = fork_session(ws, "branch-b")

    child_db = KvgitSessionDb(child, db_path=str(tmp_path / "agno"))
    child_tk = WorkspaceTools(child, checkpoint="turn", session_db=child_db)
    child_agent = Agent(
        model=ScriptedModel(),
        db=child_db,
        session_id="branch-b",
        tools=[child_tk],
        post_hooks=[child_tk.end_turn],
        add_history_to_context=True,
        telemetry=False,
    )
    run_turn(child_agent, write_turn("c.txt", "C"))

    record = kv_of(child)[SESSION_KEY]
    assert record["forked_from_session_id"] == ws.session
    assert record["session_id"] == "branch-b"
    assert len(child_db.get_session("branch-b").runs) == 2
    child.close()
    ws.close()


def test_rewind_then_fork_branches_from_the_checkpoint(tmp_path):
    ws, db, tk, agent = build(tmp_path)
    run_turn(agent, write_turn("a.txt", "A"))
    head = ws.head
    run_turn(agent, write_turn("b.txt", "B"))

    ws.restore(head)
    child = fork_session(ws, "from-the-past")
    child_db = KvgitSessionDb(child, db_path=str(tmp_path / "agno"))

    assert len(child_db.get_session("from-the-past").runs) == 1
    assert not child.fs.exists("b.txt")
    child.close()
    ws.close()


def test_fork_session_rejects_an_unknown_conversation_mode(tmp_path):
    ws = make_ws()
    with pytest.raises(ValueError, match="inherit"):
        fork_session(ws, "nope", conversation="maybe")
    ws.close()


# -- the rest of the sessions table --------------------------------------------


def test_get_session_only_answers_for_its_own_branch(tmp_path):
    ws, db, tk, agent = build(tmp_path)
    run_turn(agent, ["hello"])

    assert db.get_session(ws.session) is not None
    assert db.get_session("another-session") is None
    assert db.get_session(ws.session, user_id="nobody") is None
    raw = db.get_session(ws.session, deserialize=False)
    assert raw["session_id"] == ws.session and len(raw["runs"]) == 1
    assert "run_ids" not in raw
    ws.close()


def test_get_sessions_returns_at_most_one(tmp_path):
    ws, db, tk, agent = build(tmp_path)
    assert db.get_sessions() == []
    run_turn(agent, ["hello"])

    found = db.get_sessions()
    assert len(found) == 1 and found[0].session_id == ws.session
    rows, total = db.get_sessions(deserialize=False)
    assert total == 1 and rows[0]["session_id"] == ws.session
    assert db.get_sessions(user_id="nobody") == []
    ws.close()


def test_rename_and_delete(tmp_path):
    ws, db, tk, agent = build(tmp_path)
    run_turn(agent, ["hello"])

    renamed = db.rename_session(ws.session, None, "a better name")
    assert renamed.session_data["session_name"] == "a better name"
    assert db.rename_session("not-here", None, "x") is None

    assert db.delete_session("not-here") is False
    assert db.delete_session(ws.session) is True
    assert db.get_session(ws.session) is None
    assert run_keys(ws) == []
    ws.close()


def test_runs_are_stored_as_plain_dicts_one_key_each(tmp_path):
    """No pickled agno objects: a branch must not depend on agno's class
    layout, and dumping one to plain files must stay trivial."""
    ws, db, tk, agent = build(tmp_path)
    run_turn(agent, write_turn("a.txt", "A"))

    key = run_keys(ws)[0]
    run = kv_of(ws)[key]
    assert isinstance(run, dict)
    json.dumps(run)  # JSON-shaped, not an object graph
    record = kv_of(ws)[SESSION_KEY]
    assert record["run_ids"] == [run["run_id"]]
    assert "runs" not in record
    json.dumps(record)
    ws.close()


def test_the_conversation_is_out_of_the_agents_reach(tmp_path):
    ws, db, tk, agent = build(tmp_path)
    run_turn(agent, ["hello"])

    assert SESSION_KEY not in list(ws.cache)
    with pytest.raises(ValueError, match="__"):
        ws.cache[SESSION_KEY] = "nope"
    ws.close()


def test_tool_results_are_reassembled_from_the_run_keys(tmp_path):
    ws, db, tk, agent = build(tmp_path)
    run_turn(agent, write_turn("a.txt", "A"))

    rows = db.get_tool_results_for_session(ws.session)
    assert [row["tool_name"] for row in rows] == ["file_write"]
    assert rows[0]["session_id"] == ws.session
    assert "a.txt" in str(rows[0]["result"])
    assert db.get_tool_results_for_session("someone-else") == []
    assert db.get_tool_results_for_session(ws.session, limit=0) == []
    ws.close()


def test_the_inherited_tables_stay_on_disk(tmp_path):
    """Memories and the other cross-session tables are not versioned
    with the branch: they go to db_path, untouched."""
    from agno.db.schemas.memory import UserMemory

    ws = make_ws()
    db = KvgitSessionDb(ws, db_path=str(tmp_path / "agno"))
    db.upsert_user_memory(UserMemory(memory="likes tea", user_id="u1"))

    assert db.get_user_memories(user_id="u1")
    assert not any(k.startswith("__agno__") for k in kv_of(ws).keys())
    assert (tmp_path / "agno").is_dir()
    ws.close()
