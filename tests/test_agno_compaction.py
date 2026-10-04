"""CompactingCompression: compaction for agno, through agno's real run loop.

A scripted model stands in for the provider and reports usage: each
call's input tokens are 1000 plus the number of messages it was sent,
so a budget of 1000 is crossed by any request that follows a reported
call, and a budget of a million never is. The model's script is shared
by the agent's calls and the summary calls compaction makes, in the
order they happen.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator, Iterator, List

import pytest

pytest.importorskip("agno.compression.manager")

from agno.agent import Agent  # noqa: E402
from agno.metrics import MessageMetrics  # noqa: E402
from agno.models.base import Model  # noqa: E402
from agno.models.message import Message  # noqa: E402
from agno.models.response import ModelResponse  # noqa: E402

from nontainer import Workspace  # noqa: E402
from nontainer.adapters.agno import WorkspaceTools  # noqa: E402
from nontainer.adapters.agno_compaction import CompactingCompression  # noqa: E402
from nontainer.adapters.agno_db import KvgitSessionDb  # noqa: E402
from nontainer.compaction import MARK, Policy, folds  # noqa: E402
from nontainer.providers.kvgit import KvgitProvider  # noqa: E402

HUGE = 10**6


class Scripted(Model):
    def __init__(self) -> None:
        super().__init__(id="scripted", name="Scripted", provider="scripted")
        self.script: List[Any] = []
        self.calls: List[dict] = []  # {"messages": [(role, text)], "kwargs": {...}}
        self.fail_when_tools_forbidden = False

    def _next(self, messages: List[Message], kwargs: dict) -> ModelResponse:
        self.calls.append(
            {"messages": [(m.role, str(m.content)) for m in messages], "kwargs": kwargs}
        )
        if self.fail_when_tools_forbidden and kwargs.get("tool_choice") == "none":
            raise RuntimeError("prompt is too long")
        step = self.script.pop(0) if self.script else "done"
        response = ModelResponse(role="assistant")
        response.response_usage = MessageMetrics(
            input_tokens=1000 + len(messages), output_tokens=1
        )
        if isinstance(step, str):
            response.content = step
        else:
            name, args = step
            response.tool_calls = [
                {
                    "id": f"call_{len(self.calls)}",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)},
                }
            ]
        return response

    def invoke(self, messages, **kwargs):
        return self._next(messages, kwargs)

    async def ainvoke(self, messages, **kwargs):
        return self._next(messages, kwargs)

    def invoke_stream(self, messages, **kwargs) -> Iterator[ModelResponse]:
        yield self._next(messages, kwargs)

    async def ainvoke_stream(self, messages, **kwargs) -> AsyncIterator[ModelResponse]:
        yield self._next(messages, kwargs)

    def _parse_provider_response(self, response: Any, **kwargs: Any) -> ModelResponse:
        return response

    def _parse_provider_response_delta(self, response: Any) -> ModelResponse:
        return response

    # -- reading what happened --

    def agent_calls(self) -> List[dict]:
        return [c for c in self.calls if c["kwargs"].get("tool_choice") != "none"]

    def summary_calls(self) -> List[dict]:
        return [c for c in self.calls if c["kwargs"].get("tool_choice") == "none"]


def build(tmp_path, policy: Policy, *, store_history: bool = False, **kw):
    ws = Workspace(KvgitProvider.open(None, session="chat"))
    db = KvgitSessionDb(ws, db_path=str(tmp_path / "agno"))
    tk = WorkspaceTools(ws, commit="turn", session_db=db)
    seen: list = []
    compaction = CompactingCompression(ws, policy, on_fold=seen.append, **kw)
    agent = Agent(
        model=Scripted(),
        db=db,
        session_id=ws.session,
        tools=[tk],
        post_hooks=[tk.end_turn],
        add_history_to_context=True,
        num_history_runs=HUGE,
        store_history_messages=store_history,
        compression_manager=compaction,
    )
    return ws, db, agent, seen


def run_sync(agent, script, message):
    agent.model.script = list(script)
    agent.run(message)


def run_streaming(agent, script, message):
    agent.model.script = list(script)

    async def drive():
        async for _ in agent.arun(message, stream=True, stream_events=True):
            pass

    asyncio.run(drive())


PATHS = pytest.mark.parametrize(
    "run", [run_sync, run_streaming], ids=["sync", "stream"]
)
TOOL_TURN = [("file_write", {"path": "/a.txt", "content": "A"}), "wrote it"]


def sent(call: dict) -> List[tuple]:
    return [(r, t) for r, t in call["messages"] if r != "system"]


def stored(db, ws) -> List[List[str]]:
    runs = db.get_session(ws.session).runs
    return [
        [str(m.content) for m in run.messages if m.role != "system"] for run in runs
    ]


# -- under budget ------------------------------------------------------------------


@PATHS
def test_under_budget_nothing_changes(tmp_path, run):
    ws, db, agent, seen = build(tmp_path, Policy(budget=HUGE))
    run(agent, ["one"], "turn 1")
    run(agent, ["two"], "turn 2")
    assert folds(ws) == [] and seen == []
    assert agent.model.summary_calls() == []
    assert sent(agent.model.calls[-1]) == [
        ("user", "turn 1"),
        ("assistant", "one"),
        ("user", "turn 2"),
    ]


# -- a fold ---------------------------------------------------------------------


@PATHS
def test_over_budget_every_earlier_run_is_folded(tmp_path, run):
    ws, db, agent, seen = build(tmp_path, Policy(budget=1000))
    run(agent, ["one"], "turn 1")
    run(agent, ["SUMMARY ONE", "two"], "turn 2")

    [fold] = folds(ws)
    assert fold.summary == "SUMMARY ONE" and fold.runs == 1
    assert fold.tokens_before >= 1000 and fold.model == "scripted"
    assert seen == [fold]

    # the summary request: the agent's own messages, its own tools, no calls
    [summary] = agent.model.summary_calls()
    assert sent(summary)[:2] == [("user", "turn 1"), ("assistant", "one")]
    assert sent(summary)[-1][1].startswith("Your context is nearly full")
    agent_call = agent.model.agent_calls()[-1]
    assert summary["kwargs"]["tools"] == agent_call["kwargs"]["tools"]
    assert agent_call["kwargs"]["tools"]

    # the agent's request: the summary in place of turn 1
    texts = sent(agent_call)
    assert texts[0][0] == "user" and texts[0][1].endswith("SUMMARY ONE")
    assert texts[1] == ("assistant", "Understood. I'll continue from this summary.")
    assert texts[2] == ("user", "turn 2")
    assert not any(t in ("turn 1", "one") for _, t in texts)


@PATHS
def test_the_stored_conversation_keeps_everything(tmp_path, run):
    ws, db, agent, _ = build(tmp_path, Policy(budget=1000))
    run(agent, ["one"], "turn 1")
    run(agent, ["S", "two"], "turn 2")
    run(agent, ["S2", "three"], "turn 3")  # over budget again: a second fold
    assert len(folds(ws)) == 2
    assert stored(db, ws) == [["turn 1", "one"], ["turn 2", "two"], ["turn 3", "three"]]


@PATHS
def test_a_fold_is_applied_again_without_another_summary(tmp_path, run):
    """Under budget after the fold, the next turn reuses it: no summary
    call, and one summary pair however many calls the run makes."""
    ws, db, agent, _ = build(tmp_path, Policy(budget=1000))
    run(agent, ["one"], "turn 1")
    run(agent, ["S", "two"], "turn 2")
    agent.compression_manager.policy = Policy(budget=HUGE)
    before = len(agent.model.calls)
    run(agent, TOOL_TURN, "turn 3")

    calls = agent.model.calls[before:]
    assert (
        len(calls) == 2
        and agent.model.summary_calls() == agent.model.summary_calls()[:1]
    )
    for call in calls:
        texts = sent(call)
        assert texts[0][1].endswith("S")
        assert sum(1 for _, t in texts if t.endswith("S") and t.startswith("[")) == 1
        assert ("user", "turn 2") in texts and ("assistant", "two") in texts
        assert ("user", "turn 1") not in texts


@PATHS
def test_a_second_fold_takes_in_the_first(tmp_path, run):
    ws, db, agent, seen = build(tmp_path, Policy(budget=1000))
    run(agent, ["one"], "turn 1")
    run(agent, ["SUMMARY ONE", "two"], "turn 2")
    run(agent, ["SUMMARY TWO", "three"], "turn 3")

    first, second = folds(ws)
    assert (first.runs, second.runs) == (1, 2)
    # the second summary was written from the first and turn 2
    request = sent(agent.model.summary_calls()[-1])
    assert request[0][1].endswith("SUMMARY ONE")
    assert ("user", "turn 2") in request
    texts = sent(agent.model.agent_calls()[-1])
    assert texts[0][1].endswith("SUMMARY TWO")
    assert not any(t.endswith("SUMMARY ONE") for _, t in texts)


@PATHS
def test_a_fold_mid_run_leaves_the_run_and_it_returns_once(tmp_path, run):
    """Crossing the budget between a run's calls folds the earlier runs
    and keeps the run in progress whole. On the next turn that run is
    an earlier run, sent in full after the summary."""
    ws, db, agent, _ = build(tmp_path, Policy(budget=HUGE))
    run(agent, ["one"], "turn 1")
    # make the budget fall between turn 2's two calls
    compaction = agent.compression_manager
    original = compaction._compact

    def cross_after_first_call(messages):
        if any(m.role == "tool" and not m.from_history for m in messages):
            compaction.policy = Policy(budget=1000)
        original(messages)

    compaction._compact = cross_after_first_call
    run(agent, [TOOL_TURN[0], "MID SUMMARY", "wrote it"], "turn 2")

    [fold] = folds(ws)
    assert fold.summary == "MID SUMMARY" and fold.runs == 1
    last = sent(agent.model.agent_calls()[-1])
    assert last[0][1].endswith("MID SUMMARY")
    assert last[2] == ("user", "turn 2")
    assert any(r == "tool" for r, _ in last)

    compaction._compact = original
    compaction.policy = Policy(budget=HUGE)
    run(agent, ["three"], "turn 3")
    texts = sent(agent.model.agent_calls()[-1])
    assert texts[0][1].endswith("MID SUMMARY")
    assert ("user", "turn 2") in texts and ("assistant", "wrote it") in texts


@PATHS
def test_a_run_alone_over_budget_folds_nothing(tmp_path, run):
    ws, db, agent, _ = build(tmp_path, Policy(budget=1000))
    run(agent, TOOL_TURN, "turn 1")
    assert folds(ws) == [] and agent.model.summary_calls() == []


# -- failing open ------------------------------------------------------------------


def test_a_rewind_to_before_the_fold_sends_the_full_history(tmp_path):
    ws, db, agent, _ = build(tmp_path, Policy(budget=1000))
    run_sync(agent, ["one"], "turn 1")
    before_fold = ws.provider.head
    run_sync(agent, ["S", "two"], "turn 2")
    assert len(folds(ws)) == 1

    ws.checkout(before_fold)
    assert folds(ws) == []
    agent.compression_manager.policy = Policy(budget=HUGE)
    run_sync(agent, ["two again"], "turn 2, edited")
    texts = sent(agent.model.calls[-1])
    assert texts[:2] == [("user", "turn 1"), ("assistant", "one")]


# -- a fold too large for one request ---------------------------------------------


@PATHS
def test_a_summary_request_that_fails_falls_back_to_a_transcript(tmp_path, run):
    ws, db, agent, _ = build(tmp_path, Policy(budget=1000))
    run(agent, ["one"], "turn 1")
    agent.model.fail_when_tools_forbidden = True
    run(agent, ["FROM TRANSCRIPT", "two"], "turn 2")

    [fold] = folds(ws)
    assert fold.summary == "FROM TRANSCRIPT"
    [request] = [c for c in agent.model.calls if "<transcript>" in c["messages"][-1][1]]
    assert len(request["messages"]) == 1
    assert request["kwargs"].get("tool_choice") != "none"
    assert "[user]\nturn 1" in request["messages"][0][1]


@PATHS
def test_a_history_past_the_window_is_summarised_in_chunks(tmp_path, run):
    """Where the plain request would not fit the model's window, the
    history goes as a reduced transcript, in chunks when even that is
    too large, each folded into the summary so far."""
    ws, db, agent, _ = build(tmp_path, Policy(budget=HUGE))
    for n in range(4):
        run(agent, [f"reply {n} " + "x" * 600], f"turn {n} " + "y" * 600)
    agent.compression_manager.policy = Policy(budget=1000, window=1200)
    run(agent, ["PART 1", "PART 2", "PART 3", "PART 4", "done"], "turn 4")

    [fold] = folds(ws)
    chunked = [
        c["messages"][-1][1]
        for c in agent.model.calls
        if c["messages"] and "<transcript>" in c["messages"][-1][1]
    ]
    assert len(chunked) > 1
    assert "part 1 of" in chunked[0] and "<summary>" not in chunked[0]
    assert "<summary>\nPART 1\n</summary>" in chunked[1]
    assert fold.summary == f"PART {len(chunked)}"
    assert agent.model.summary_calls() == []  # the plain request never went


def test_a_separate_summary_model_gets_a_transcript(tmp_path):
    writer = Scripted()
    writer.script = ["WRITTEN ELSEWHERE"]
    ws, db, agent, _ = build(tmp_path, Policy(budget=1000), summary_model=writer)
    run_sync(agent, ["one"], "turn 1")
    run_sync(agent, ["two"], "turn 2")
    [fold] = folds(ws)
    assert fold.summary == "WRITTEN ELSEWHERE" and fold.model == "scripted"
    [call] = writer.calls
    assert "<transcript>" in call["messages"][0][1]
    assert agent.model.summary_calls() == []


# -- the stored conversation, whatever the agent's flags ---------------------------


def test_the_summary_pair_is_never_stored_even_with_history_stored(tmp_path):
    ws, db, agent, _ = build(tmp_path, Policy(budget=1000), store_history=True)
    run_sync(agent, ["one"], "turn 1")
    run_sync(agent, ["S", "two"], "turn 2")
    runs = db.get_session(ws.session).runs
    ids = [m.id for run in runs for m in run.messages]
    assert not any(isinstance(i, str) and i.startswith(MARK) for i in ids)


def test_a_failure_inside_compaction_never_fails_the_agents_call(tmp_path):
    ws, db, agent, _ = build(tmp_path, Policy(budget=1000))
    run_sync(agent, ["one"], "turn 1")

    def broken(*a, **k):
        raise RuntimeError("store unavailable")

    agent.compression_manager._apply = broken
    run_sync(agent, ["two"], "turn 2")
    assert stored(db, ws)[-1] == ["turn 2", "two"]
