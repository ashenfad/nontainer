"""The agno adapter under the conformance corpus.

:class:`AgnoHarness` drives agno the way the adapter's documentation
tells an embedder to: :class:`~nontainer.adapters.agno.WorkspaceTools`
with its turn hooks and its delivery hook, the conversation in the
branch through :class:`~nontainer.adapters.agno_db.KvgitSessionDb`,
``Agent(retries=0)``, and a streamed ``arun``. The model is
:class:`ScriptedModel`, which asks the runner's clock for each reply.

agno leaves the ending of a run that did not complete to the embedder,
so the driver here ends one the way nontainer documents it:
``keep_aborted_run`` for a cancelled or failed run, then the inbox is
settled. It classifies a run error by agno's ``error_type``: a provider
error interrupts the run, which a later turn may resume in place;
anything else fails it. agno's stream events become
:data:`~nontainer.turns.TurnEvent`\\ s.

    from nontainer.conformance import check, run
    from nontainer.conformance.harness import SCENARIOS

    harness = AgnoHarness()
    for scenario in SCENARIOS:
        print(scenario.name, check(scenario, run(scenario, harness)))
"""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from typing import Any

from agno.agent import Agent
from agno.db.base import SessionType
from agno.exceptions import ModelProviderError
from agno.models.base import Model
from agno.models.response import ModelResponse

from ..conformance.runner import Clock, RunView
from ..inbox import Inbox, Note, split
from ..turns import (
    Delivered,
    DeliveredNote,
    RunEnded,
    RunStarted,
    RunStatus,
    TextDelta,
    ThinkingDelta,
    ToolEnded,
    ToolStarted,
    TurnEvent,
    Usage,
)
from ..workspace import Workspace
from .agno import WorkspaceTools, keep_aborted_run
from .agno_db import KvgitSessionDb

__all__ = ["AgnoHarness", "AgnoSession", "ScriptedModel"]

#: agno's ``error_type`` on a ``RunError`` for a failed provider call:
#: the errors a later turn may resume from.
PROVIDER_ERRORS = frozenset({"model_provider_error", "model_rate_limit_error"})

#: How ``keep_aborted_run``'s closing note begins.
CLOSING_NOTE = "[turn aborted early:"

STOPPED = "stopped by the user"


def _modern_agno() -> bool:
    """Whether this agno stores a run that errored or was cancelled
    with the messages it had, and continues an errored run in place.
    agno 2.8 does both; 2.1 raises a run error out of ``arun``, stores
    a cancelled run with no messages, and continues only a paused run.
    ``continue_run`` gained ``continue_from`` with that change."""
    return "continue_from" in inspect.signature(Agent.acontinue_run).parameters


class ScriptedModel(Model):
    """An agno ``Model`` whose replies come from a conformance
    :class:`~nontainer.conformance.runner.Clock`, one per call."""

    def __init__(self, clock: Clock) -> None:
        super().__init__(id="scripted", name="Scripted", provider="scripted")
        self.clock = clock
        self.calls = 0

    def _reply(self) -> ModelResponse:
        step = self.clock.next()
        self.calls += 1
        if step.fail == "provider":
            raise ModelProviderError(
                "the scripted provider is unavailable", status_code=529
            )
        if step.fail == "error":
            raise RuntimeError("the scripted model failed")
        response = ModelResponse(role="assistant")
        if step.text:
            response.content = step.text
        if step.thinking:
            response.reasoning_content = step.thinking
        if step.tool_calls:
            response.tool_calls = [
                {
                    "id": f"call_{self.calls}_{i}",
                    "type": "function",
                    "function": {"name": call.name, "arguments": json.dumps(call.args)},
                }
                for i, call in enumerate(step.tool_calls)
            ]
        return response

    def invoke(self, messages: Any, **kwargs: Any) -> ModelResponse:
        return self._reply()

    async def ainvoke(self, messages: Any, **kwargs: Any) -> ModelResponse:
        return self._reply()

    def invoke_stream(self, messages: Any, **kwargs: Any) -> Iterator[ModelResponse]:
        yield self._reply()

    async def ainvoke_stream(
        self, messages: Any, **kwargs: Any
    ) -> AsyncIterator[ModelResponse]:
        yield self._reply()

    def _parse_provider_response(self, response: Any, **kwargs: Any) -> ModelResponse:
        return response

    def _parse_provider_response_delta(self, response: Any) -> ModelResponse:
        return response


class AgnoSession:
    """One session's workspace, driven by agno."""

    def __init__(self, ws: Workspace, clock: Clock) -> None:
        self.ws = ws
        self.inbox = Inbox(on_delivered=self._delivered)
        self.db = KvgitSessionDb(ws)
        self.tk = WorkspaceTools(ws, session_db=self.db, inbox=self.inbox)
        self.agent = Agent(
            model=ScriptedModel(clock),
            db=self.db,
            session_id=ws.session,
            tools=[self.tk],
            pre_hooks=[self.tk.begin_turn],
            post_hooks=[self.tk.end_turn],
            tool_hooks=[self.tk.deliver],
            add_history_to_context=True,
            telemetry=False,
            retries=0,
        )
        self._events: list[TurnEvent] = []
        self._run_id: str | None = None
        self._interrupted: str | None = None

    # -- the harness session ------------------------------------------------

    def turn(self, prompt: str) -> list[TurnEvent]:
        return self._drive(
            lambda: self.agent.arun(prompt, stream=True, stream_events=True)
        )

    def resume(self) -> list[TurnEvent]:
        run_id = self._interrupted
        if run_id is None:
            raise RuntimeError("no interrupted run to resume")
        return self._drive(
            lambda: self.agent.acontinue_run(
                run_id=run_id,
                session_id=self.ws.session,
                stream=True,
                stream_events=True,
            )
        )

    def cancel(self) -> None:
        # a staticmethod on recent agno, an instance method on 2.1:
        # called through the instance, both take the run id alone
        self.agent.cancel_run(self._run_id)

    def runs(self) -> list[RunView]:
        session = self.db.get_session(
            session_id=self.ws.session, session_type=SessionType.AGENT
        )
        views = []
        for run in (getattr(session, "runs", None) or []) if session else []:
            # agno 2.1 stores the history it sent with each run, flagged
            messages = [
                m for m in (run.messages or []) if not getattr(m, "from_history", False)
            ]
            last = messages[-1] if messages else None
            closing = (
                last is not None
                and last.role == "assistant"
                and str(last.content or "").startswith(CLOSING_NOTE)
            )
            tools = sum(1 for m in messages if m.role == "tool")
            views.append(RunView(closing_note=closing, tool_results=tools))
        return views

    def run_statuses(self, info: Mapping[str, Any]) -> tuple[str, ...]:
        runs = info.get("runs")
        if not isinstance(runs, Mapping):
            return ()
        return tuple(str(getattr(s, "value", s)).lower() for s in runs.values())

    def close(self) -> None:
        pass

    # -- the driver ---------------------------------------------------------

    def _delivered(self, notes: list[Note]) -> None:
        # Called on the tool's thread while the run loop waits for the
        # tool, so it lands after the tool's start and before its end.
        self._events.append(
            Delivered(
                notes=tuple(
                    DeliveredNote(
                        id=n.id, text=n.text, kind=n.kind, label=n.label, job=n.job
                    )
                    for n in notes
                )
            )
        )

    def _drive(self, start: Callable[[], Any]) -> list[TurnEvent]:
        self._events = []
        self._run_id = None
        state: dict[str, Any] = {"cancelled": False, "error": None, "raised": None}

        async def follow() -> None:
            async for ev in start():
                self._observe(ev, state)

        try:
            asyncio.run(follow())
        except Exception as e:  # noqa: BLE001 - agno 2.1 raises run errors
            state["raised"] = e
        self._events.append(self._end(state))
        return list(self._events)

    def _observe(self, ev: Any, state: dict[str, Any]) -> None:
        run_id = getattr(ev, "run_id", None)
        if run_id and self._run_id is None:
            self._run_id = run_id
            self._events.append(RunStarted(run_id=run_id))
        kind = getattr(ev, "event", "")
        if kind == "RunContent":
            thinking = getattr(ev, "reasoning_content", None)
            if isinstance(thinking, str) and thinking:
                self._events.append(ThinkingDelta(text=thinking))
            text = getattr(ev, "content", None)
            if isinstance(text, str) and text:
                self._events.append(TextDelta(text=text))
        elif kind == "ReasoningContentDelta":
            thinking = getattr(ev, "reasoning_content", None)
            if isinstance(thinking, str) and thinking:
                self._events.append(ThinkingDelta(text=thinking))
        elif kind == "ToolCallStarted":
            tool = getattr(ev, "tool", None)
            self._events.append(
                ToolStarted(
                    call_id=str(getattr(tool, "tool_call_id", "") or ""),
                    name=str(getattr(tool, "tool_name", "") or ""),
                    args=dict(getattr(tool, "tool_args", None) or {}),
                )
            )
        elif kind == "ToolCallCompleted":
            tool = getattr(ev, "tool", None)
            result = getattr(tool, "result", "")
            result = split(result)[0] if isinstance(result, str) else str(result or "")
            self._events.append(
                ToolEnded(
                    call_id=str(getattr(tool, "tool_call_id", "") or ""),
                    name=str(getattr(tool, "tool_name", "") or ""),
                    result=result,
                    is_error=bool(getattr(tool, "tool_call_error", False)),
                )
            )
        elif kind == "ModelRequestCompleted":
            tokens = getattr(ev, "input_tokens", None)
            if tokens:
                self._events.append(
                    Usage(
                        input_tokens=int(tokens),
                        cached_tokens=int(getattr(ev, "cache_read_tokens", 0) or 0),
                    )
                )
        elif kind == "RunCancelled":
            state["cancelled"] = True
        elif kind == "RunError":
            state["error"] = (
                getattr(ev, "content", None) or "run error",
                getattr(ev, "error_type", None),
            )

    def _end(self, state: dict[str, Any]) -> RunEnded:
        """How the run ended, and what that ending leaves behind."""
        status: RunStatus
        message: str | None = None
        raised = state["raised"]
        if raised is not None:
            status = (
                "interrupted" if isinstance(raised, ModelProviderError) else "failed"
            )
            message = str(raised)
        elif state["cancelled"]:
            status, message = "cancelled", STOPPED
        elif state["error"] is not None:
            text, error_type = state["error"]
            status = "interrupted" if error_type in PROVIDER_ERRORS else "failed"
            message = str(text)
        else:
            status = "completed"
        if status in ("cancelled", "failed"):
            keep_aborted_run(self.db, self.ws.session, self._run_id, message or "")
        if status != "completed":
            # The model read what rode out on the run's tool results, and
            # the kept (or resumable) run holds those messages.
            self.inbox.settle()
        self._interrupted = self._run_id if status == "interrupted" else None
        return RunEnded(status=status, message=message)


#: Where the adapter falls short of the contract today, by scenario and
#: check. The run's commit is the db's, stamped with agno's own run
#: status (an errored run reads ``error``, interrupted or failed alike),
#: and keeping an aborted run writes the session again, which commits a
#: second time.
KNOWN_GAPS: dict[str, dict[str, str]] = {
    "a-cancel-after-a-tool-keeps-the-run": {
        "commits": "keeping the cancelled run lands a second run commit",
    },
    "a-failure-keeps-the-run-with-a-closing-note": {
        "commits": (
            "the run commit is stamped 'error', and keeping the run lands a second one"
        ),
    },
    "a-provider-error-interrupts-and-resume-continues": {
        "commits": "the interrupted run's commit is stamped 'error'",
    },
}


#: The gaps on agno 2.1, which has neither capability, so the scenarios
#: above do not apply to it. It runs no post hook for a streamed run, so
#: ``end_turn`` never settles the notes a completed turn delivered.
KNOWN_GAPS_AGNO_2_1: dict[str, dict[str, str]] = {
    name: {"inbox": "agno 2.1 runs no post hook for a streamed run"}
    for name in (
        "a-note-rides-the-next-tool-result-once",
        "a-note-after-the-last-tool-waits-for-the-next-turn",
    )
}


class AgnoHarness:
    """The agno adapter as a conformance harness."""

    name = "agno"

    def __init__(self) -> None:
        modern = _modern_agno()
        self.capabilities: frozenset[str] = frozenset(
            {"resume", "keeps-aborted-runs"} if modern else ()
        )
        gaps = KNOWN_GAPS if modern else KNOWN_GAPS_AGNO_2_1
        self.known_gaps: dict[str, dict[str, str]] = {
            name: dict(checks) for name, checks in gaps.items()
        }

    def open(self, ws: Workspace, clock: Clock) -> AgnoSession:
        return AgnoSession(ws, clock)
