"""The agno seam compaction rides on, checked against agno's real run loop.

Compaction (docs/compaction.md) replaces earlier runs, in what the model
is sent, by a summary, from a ``CompressionManager`` subclass. That
depends on agno behaviour no API promises:

- the manager is called before every model call, the first of a run
  included, with the request's own message list;
- an edit to that list in place is what the model is sent, and stays
  for the rest of the run (so applying a fold has to be idempotent);
- history messages are copies flagged ``from_history``, and agno drops
  every flagged message before it stores the run, so the edit never
  reaches the stored conversation;
- each assistant message carries the input tokens of the call that
  produced it, and keeps them in the copies a later run is sent, which
  is what measuring a request without a tokenizer reads.

Each is pinned here, on the sync path and on the async streaming path
the studio drives.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator, Iterator, List

import pytest

# the compression manager is the seam; agno 2.1.0 has none
pytest.importorskip("agno.compression.manager")

from agno.agent import Agent  # noqa: E402
from agno.compression.manager import CompressionManager  # noqa: E402

try:  # agno 2.6 and later; earlier, a message's metrics are models.metrics'
    from agno.metrics import MessageMetrics  # noqa: E402
except ImportError:  # pragma: no cover - exercised on agno 2.4 and 2.5
    from agno.models.metrics import Metrics as MessageMetrics  # noqa: E402
from agno.models.base import Model  # noqa: E402
from agno.models.message import Message  # noqa: E402
from agno.models.response import ModelResponse  # noqa: E402

from nontainer import Workspace  # noqa: E402
from nontainer.adapters.agno import WorkspaceTools  # noqa: E402
from nontainer.adapters.agno_db import KvgitSessionDb  # noqa: E402
from nontainer.providers.kvgit import KvgitProvider  # noqa: E402

SUMMARY = "SUMMARY of the earlier runs"
ACK = "Understood."


class Recording(Model):
    """A scripted model that records every request and reports usage:
    each call's input tokens are 1000 plus the number of messages."""

    def __init__(self) -> None:
        super().__init__(id="scripted", name="Scripted", provider="scripted")
        self.script: List[Any] = []
        self.requests: List[List[tuple]] = []

    def _next(self, messages: List[Message]) -> ModelResponse:
        self.requests.append(
            [(m.role, str(m.content), bool(m.from_history)) for m in messages]
        )
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
                    "id": f"call_{len(self.requests)}",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)},
                }
            ]
        return response

    def invoke(self, messages: List[Message], **kwargs: Any) -> ModelResponse:
        return self._next(messages)

    async def ainvoke(self, messages: List[Message], **kwargs: Any) -> ModelResponse:
        return self._next(messages)

    def invoke_stream(
        self, messages: List[Message], **kwargs: Any
    ) -> Iterator[ModelResponse]:
        yield self._next(messages)

    async def ainvoke_stream(
        self, messages: List[Message], **kwargs: Any
    ) -> AsyncIterator[ModelResponse]:
        yield self._next(messages)

    def _parse_provider_response(self, response: Any, **kwargs: Any) -> ModelResponse:
        return response

    def _parse_provider_response_delta(self, response: Any) -> ModelResponse:
        return response


class Splicer(CompressionManager):
    """Replaces every history message with a summary pair, as a fold
    would, and records what each call was handed."""

    def __init__(self) -> None:
        super().__init__(compress_token_limit=1)
        self.calls: List[dict] = []

    def should_compress(self, *a: Any, **k: Any) -> bool:
        return True

    async def ashould_compress(self, *a: Any, **k: Any) -> bool:
        return True

    def compress(self, messages: List[Message], run_metrics: Any = None) -> None:
        self._splice(messages)

    async def acompress(self, messages: List[Message], run_metrics: Any = None) -> None:
        self._splice(messages)

    def _splice(self, messages: List[Message]) -> None:
        reported = [
            m.metrics.input_tokens
            for m in messages
            if m.role == "assistant" and m.metrics is not None
        ]
        self.calls.append({"reported": reported, "size": len(messages)})
        # The list persists across the run's calls, so the pair spliced
        # in by the first call is still there at the second: a splice
        # has to recognise its own messages and leave them be.
        span = [
            i
            for i, m in enumerate(messages)
            if m.from_history and m.role != "system" and m.content not in (SUMMARY, ACK)
        ]
        if not span:
            return
        first, last = span[0], span[-1]
        assert span == list(range(first, last + 1)), "history is one contiguous span"
        messages[first : last + 1] = [
            Message(role="user", content=SUMMARY, from_history=True),
            Message(role="assistant", content=ACK, from_history=True),
        ]


def build(tmp_path):
    ws = Workspace(KvgitProvider.open(None, session="chat"))
    db = KvgitSessionDb(ws, db_path=str(tmp_path / "agno"))
    tk = WorkspaceTools(ws, commit="turn", session_db=db)
    splicer = Splicer()
    agent = Agent(
        model=Recording(),
        db=db,
        session_id=ws.session,
        tools=[tk],
        post_hooks=[tk.end_turn],
        add_history_to_context=True,
        num_history_runs=1_000_000,
        compress_tool_results=True,
        compression_manager=splicer,
    )
    return ws, db, agent, splicer


def run_sync(agent, script, message):
    agent.model.script = list(script)
    agent.run(message)


def run_streaming(agent, script, message):
    agent.model.script = list(script)

    async def drive():
        async for _ in agent.arun(message, stream=True, stream_events=True):
            pass

    asyncio.run(drive())


TOOL_TURN = [("file_write", {"path": "/a.txt", "content": "A"}), "wrote it"]


@pytest.mark.parametrize("run", [run_sync, run_streaming], ids=["sync", "stream"])
def test_a_fold_spliced_in_place_is_what_the_model_is_sent(tmp_path, run):
    ws, db, agent, splicer = build(tmp_path)
    run(agent, ["one"], "turn 1")
    run(agent, ["two"], "turn 2")
    first_call = len(agent.model.requests)
    run(agent, TOOL_TURN, "turn 3")

    # called before every model call: turn 3 made two
    calls = agent.model.requests[first_call:]
    assert len(calls) == 2
    assert len(splicer.calls) == len(agent.model.requests)

    # both of turn 3's requests carry the summary and none of turns 1-2
    for request in calls:
        sent = [(role, text) for role, text, _ in request if role != "system"]
        assert sent[:3] == [
            ("user", SUMMARY),
            ("assistant", ACK),
            ("user", "turn 3"),
        ]
        assert not any(t in ("turn 1", "turn 2", "one", "two") for _, t in sent)
        roles = [r for r, _ in sent if r in ("user", "assistant")]
        assert all(a != b for a, b in zip(roles, roles[1:])), roles


@pytest.mark.parametrize("run", [run_sync, run_streaming], ids=["sync", "stream"])
def test_the_splice_never_reaches_the_stored_conversation(tmp_path, run):
    ws, db, agent, splicer = build(tmp_path)
    run(agent, ["one"], "turn 1")
    run(agent, ["two"], "turn 2")
    run(agent, TOOL_TURN, "turn 3")

    runs = db.get_session(ws.session).runs
    assert len(runs) == 3
    stored = [
        [str(m.content) for m in run.messages if m.role != "system"] for run in runs
    ]
    assert all(SUMMARY not in texts for texts in stored)
    assert stored[0][:2] == ["turn 1", "one"]
    assert stored[1][:2] == ["turn 2", "two"]
    assert stored[2][0] == "turn 3" and stored[2][-1] == "wrote it"


@pytest.mark.parametrize("run", [run_sync, run_streaming], ids=["sync", "stream"])
def test_usage_rides_on_the_history_copies(tmp_path, run):
    """A run's first request can be measured from the last stored run's
    assistant message, before anything is spliced."""
    ws, db, agent, splicer = build(tmp_path)
    run(agent, ["one"], "turn 1")
    reported_in_turn_1 = 1000 + len(agent.model.requests[-1])
    run(agent, ["two"], "turn 2")

    first_call_of_turn_2 = splicer.calls[1]
    assert first_call_of_turn_2["reported"] == [reported_in_turn_1]


class Summarizing(Splicer):
    """Writes the summary with the agent's own model, from inside
    ``compress``: the same messages and the same tools, with tool calls
    forbidden. ``compress`` is not handed the tools; ``should_compress``
    is, so they are kept from there."""

    def __init__(self) -> None:
        super().__init__()
        self.tools: Any = None
        self.model: Any = None
        self.summaries: List[str] = []

    def should_compress(self, messages, tools=None, model=None, **k: Any) -> bool:
        self.tools, self.model = tools, model
        return True

    async def ashould_compress(
        self, messages, tools=None, model=None, **k: Any
    ) -> bool:
        self.tools, self.model = tools, model
        return True

    def _request(self, messages: List[Message]) -> List[Message]:
        return list(messages) + [Message(role="user", content="Summarise.")]

    def compress(self, messages: List[Message], run_metrics: Any = None) -> None:
        if any(m.from_history and m.content not in (SUMMARY, ACK) for m in messages):
            reply = self.model.response(
                self._request(messages), tools=self.tools, tool_choice="none"
            )
            self.summaries.append(reply.content)
        self._splice(messages)

    async def acompress(self, messages: List[Message], run_metrics: Any = None) -> None:
        if any(m.from_history and m.content not in (SUMMARY, ACK) for m in messages):
            reply = await self.model.aresponse(
                self._request(messages), tools=self.tools, tool_choice="none"
            )
            self.summaries.append(reply.content)
        self._splice(messages)


class ToolRecording(Recording):
    def __init__(self) -> None:
        super().__init__()
        self.kwargs: List[dict] = []

    def invoke(self, messages, **kwargs):
        self.kwargs.append(kwargs)
        return super().invoke(messages, **kwargs)

    async def ainvoke(self, messages, **kwargs):
        self.kwargs.append(kwargs)
        return await super().ainvoke(messages, **kwargs)

    def invoke_stream(self, messages, **kwargs):
        self.kwargs.append(kwargs)
        yield from super().invoke_stream(messages, **kwargs)

    async def ainvoke_stream(self, messages, **kwargs):
        self.kwargs.append(kwargs)
        async for chunk in super().ainvoke_stream(messages, **kwargs):
            yield chunk


@pytest.mark.parametrize("run", [run_sync, run_streaming], ids=["sync", "stream"])
def test_the_summary_call_sends_the_agents_own_tools(tmp_path, run):
    """From inside ``compress``, the agent's model writes the summary
    with the tool list the agent's own request carries, and with tool
    calls forbidden. The run then goes on as normal."""
    ws, db, agent, _ = build(tmp_path)
    summarizer = Summarizing()
    agent.compression_manager = summarizer
    agent.model = ToolRecording()
    run(agent, ["one"], "turn 1")
    agent.model.script = []
    run(agent, ["SUMMARY TEXT", "two"], "turn 2")

    assert summarizer.summaries == ["SUMMARY TEXT"]
    summary_call, agent_call = agent.model.kwargs[-2:]
    assert summary_call.get("tool_choice") == "none"
    assert summary_call.get("tools") == agent_call.get("tools")
    assert agent_call.get("tools")  # the comparison is not of two Nones
    assert agent.model.requests[-2][-1][:2] == ("user", "Summarise.")
    assert db.get_session(ws.session).runs[-1].content == "two"
