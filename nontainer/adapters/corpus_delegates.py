"""Delegates in the harness corpus: what runs a scenario's delegates.

A harness with ``delegation`` hands the conformance runner one of these
(``Harness.delegation(store, scenario)``), since the runner is core and
cannot build a ``sessions`` helper itself. Each delegate runs as a
session of that same harness, opened on the delegate's branch with its
script (:class:`~nontainer.conformance.corpus.Delegate`) as its clock,
and driven through :func:`~nontainer.sessions.until_settled`::

    class MyHarness:
        def delegation(self, store, scenario):
            return CorpusDelegates(self, store, scenario)
"""

from __future__ import annotations

import threading
import time
from collections.abc import Sequence
from typing import Any

from ..conformance.corpus import Delegate, EventStep, Scenario
from ..conformance.runner import Clock, DelegateView, HarnessSession
from ..sessions import SEPARATOR, Sessions, until_settled
from ..store import Store
from ..turns import TextDelta, TurnEvent
from ..workspace import Workspace

__all__ = ["PATIENCE", "CorpusDelegates"]

#: How long a scenario waits for a delegate before calling it stuck.
PATIENCE = 30.0


def _short(session: str) -> str:
    """The name a delegate was asked under: its session's last part."""
    return session.rsplit(SEPARATOR, 1)[-1]


def _reply(events: Sequence[TurnEvent]) -> str:
    return "".join(ev.text for ev in events if isinstance(ev, TextDelta))


class CorpusDelegates:
    """The runner every delegate of a scenario runs on: a session of the
    harness under test, opened on the delegate's branch with its script
    (:class:`~nontainer.conformance.corpus.Delegate`) as its clock, and
    driven through :func:`~nontainer.sessions.until_settled`. A delegate
    is held until a ``delegate_answers`` event releases it, so where its
    answer lands is fixed by the script; at the end every one still held
    is released."""

    def __init__(self, harness: Any, store: Store, scenario: Scenario) -> None:
        self.harness = harness
        self.store = store
        self.scenario = scenario
        self.helpers: list[Any] = []
        self.views: dict[str, DelegateView] = {}
        self.exhausted = False
        self._gates: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    def helper(self, ws: Workspace) -> Any:
        """A ``Sessions`` helper over ``ws`` whose delegates run here."""
        helper = Sessions(ws, self)
        with self._lock:
            self.helpers.append(helper)
        return helper

    def _gate(self, name: str) -> threading.Event:
        with self._lock:
            return self._gates.setdefault(name, threading.Event())

    def run(self, session: str, task: str, *, budget: Any = None) -> str:
        name = _short(session)
        self._gate(name).wait(PATIENCE)
        script = self.scenario.delegates.get(name, Delegate())
        child = self.store.open(session)
        holder: dict[str, Any] = {}
        clock = Clock(lambda step: self.fire(step, holder["session"], holder["helper"]))
        clock.load(script.steps)
        helper = self.helper(child)
        opened = self.harness.open(child, clock, sessions=helper)  # type: ignore[call-arg]
        holder.update(session=opened, helper=helper)

        def run_turn(prompt: str | None) -> str:
            return _reply(opened.turn(prompt) if prompt is not None else opened.wake())

        try:
            settled = until_settled(
                run_turn,
                helper,
                opened.inbox,
                prompt=task,
                max_wakes=script.wakes,
                poll=0.05,
            )
            uncommitted = child.uncommitted
        finally:
            opened.close()
            child.close()
        with self._lock:
            self.exhausted = self.exhausted or clock.exhausted
            self.views[name] = DelegateView(
                wakes=settled.wakes,
                unread=tuple(_short(n) for n in settled.unread),
                uncommitted=uncommitted,
            )
        return settled.text

    def fire(self, step: EventStep, session: HarnessSession, helper: Any) -> None:
        """An outside event, for a session whose helper is ``helper``."""
        if step.what == "cancel":
            session.cancel()
        elif step.what == "queue_note":
            session.inbox.put(step.text)
        elif step.what == "delegate_answers":
            self.answer(helper, step.text)
        else:  # pragma: no cover - the format refuses others
            raise ValueError(f"unknown event {step.what!r}")

    def answer(self, helper: Any, name: str) -> None:
        """Release the delegate ``name`` and wait for its answer to land
        in ``helper``."""
        self._gate(name).set()
        deadline = time.monotonic() + PATIENCE
        while time.monotonic() < deadline:
            jobs = [j for j in helper.list() if _short(j.name) == name]
            if jobs and all(j.status != "running" for j in jobs):
                return
            if not helper.wait(timeout=0.05):
                time.sleep(0.01)
        raise RuntimeError(f"delegate {name!r} did not answer in {PATIENCE}s")

    def finish(self) -> dict[str, DelegateView]:
        """Release every delegate still held, close every helper (which
        waits for their runs), and say how each delegate's answer came
        back to its asker."""
        with self._lock:
            for gate in self._gates.values():
                gate.set()
            helpers = list(self.helpers)
        answers: dict[str, tuple[str, str]] = {}
        for helper in helpers:
            helper.close()
            for job in helper.list():
                text = ""
                if job.status not in ("running", "cancelled", "expired"):
                    text = str(helper.result(job.name))
                answers[_short(job.name)] = (job.status, text)
        with self._lock:
            views = dict(self.views)
        for name, (status, text) in answers.items():
            seen = views.get(name, DelegateView())
            views[name] = DelegateView(
                status=status,
                text=text,
                wakes=seen.wakes,
                unread=seen.unread,
                uncommitted=seen.uncommitted,
            )
        return views
