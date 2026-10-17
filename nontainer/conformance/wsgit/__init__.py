"""The ``ws-git`` script corpus: what the verb prints, pinned.

``ws-git`` is text a model reads, and a port of it in another language
(agex-ts's world) is checked against this corpus rather than against a
reading of the Python. Each :class:`Script` is a run of steps against a
fresh memory store: terminal commands in named sessions, and the few
host steps a script needs to set the scene. Recording a script
(:func:`record`) runs it here and keeps what every command printed, its
stdout, its stderr and its exit code, so a port replays the steps and
compares.

**Commit ids** differ from run to run and from port to port, so the
record names them by order of first appearance in the output: ``<c1>``
for the seven-character spelling ws-git prints, ``<c1*>`` for a whole
id. A command may use those names for a commit the output has already
shown (``ws-git checkout <c2>``); a port substitutes its own ids.

**Steps** (each with the session it acts in, ``main`` by default):

- :class:`Run` — a terminal command. The session is opened on first
  use, at ``/workspace``, with ``ws-git`` registered.
- :class:`Open` — open a session with settings of its own (a root, ignore
  patterns) before anything else touches it.
- :class:`Write` — a host write of exact bytes (what ``echo`` can't
  spell: a file without a final newline, binary content), committed.
- :class:`Tag` — a store tag of the session's current state.
- :class:`Delete` — delete a session.
- :class:`Converse` — a run of the session's conversation, stored the
  way a harness's turn stores one: its body in the neutral trace shape
  (:meth:`~nontainer.traces.TraceRun.to_dict`), landed by a turn that
  ends with the run's status, with whatever the session left
  uncommitted. The store reads the ``"neutral"`` harness with
  :class:`~nontainer.traces.NeutralTraces` and has no source for any
  other, so ``worktree add --trace`` can be pinned without porting a
  harness's parser.

``python -m nontainer.conformance.export`` writes the recorded scripts
under ``wsgit/json/``; ``tests/test_corpus_drift.py`` fails when they
drift from what this records.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from typing import Any, Union

from ...traces import TraceRun

__all__ = [
    "Converse",
    "Delete",
    "Open",
    "Run",
    "SCRIPTS",
    "Script",
    "Step",
    "Tag",
    "Write",
    "dumps",
    "record",
]


@dataclass(frozen=True)
class Run:
    """A terminal command in a session."""

    command: str
    session: str = "main"


@dataclass(frozen=True)
class Open:
    """Open a session with its own settings, before it is used."""

    session: str
    root: str | None = None
    ignore: tuple[str, ...] = ()


@dataclass(frozen=True)
class Write:
    """A host write of exact bytes, committed: ``text`` as UTF-8, or
    ``data`` as base64 for bytes that aren't text."""

    path: str
    text: str | None = None
    data: str | None = None
    session: str = "main"


@dataclass(frozen=True)
class Tag:
    """A store tag naming the session's current state."""

    name: str
    session: str = "main"


@dataclass(frozen=True)
class Delete:
    """Delete a session."""

    session: str


@dataclass(frozen=True)
class Converse:
    """A run stored in the session's conversation by a turn that ends
    with its status (``completed``, ``failed``, ...)."""

    run: TraceRun
    session: str = "main"
    harness: str = "neutral"


Step = Union[Run, Open, Write, Tag, Delete, Converse]


@dataclass(frozen=True)
class Script:
    name: str
    summary: str
    steps: tuple[Step, ...] = field(default_factory=tuple)


#: A commit id as ws-git prints one: whole, or its first seven
#: characters. One pattern, so a text is named left to right.
_ID = re.compile(r"(?<![0-9a-f])(?:[0-9a-f]{40}|[0-9a-f]{7})(?![0-9a-f])")
_NAMED = re.compile(r"<c(\d+)(\*?)>")


class _Names:
    """Commit ids ↔ ``<cN>`` names, numbered by first appearance."""

    def __init__(self) -> None:
        self.known: set[str] = set()
        self.number: dict[str, int] = {}
        self.ids: list[str] = []

    def learn(self, store: Any) -> None:
        repo = store._kvgit_repo()
        if repo is None:
            return
        for branch in list(repo.branches):
            for commit in repo.log(branch=branch):
                self.known.add(commit.hash)
        for commit in dict(repo.tags.items()).values():
            self.known.add(commit)

    def _name(self, commit: str) -> int:
        if commit not in self.number:
            self.ids.append(commit)
            self.number[commit] = len(self.ids)
        return self.number[commit]

    def hide(self, text: str) -> str:
        def name(m: re.Match[str]) -> str:
            word = m.group(0)
            if len(word) == 40:
                return f"<c{self._name(word)}*>" if word in self.known else word
            found = [c for c in self.known if c.startswith(word)]
            return f"<c{self._name(found[0])}>" if len(found) == 1 else word

        return _ID.sub(name, text)

    def fill(self, command: str) -> str:
        def put(m: re.Match[str]) -> str:
            n = int(m.group(1))
            if n > len(self.ids):
                raise ValueError(f"<c{n}> is used before the output showed it")
            commit = self.ids[n - 1]
            return commit if m.group(2) else commit[:7]

        return _NAMED.sub(put, command)


def record(script: Script) -> dict[str, Any]:
    """Run ``script`` on a fresh memory store; the steps with what each
    command printed, commit ids named."""
    from ...store import Store
    from ...traces import NeutralTraces
    from ...wsgit import register_wsgit

    store = Store(memory=True, traces={"neutral": NeutralTraces()})
    sessions: dict[str, Any] = {}
    names = _Names()

    def session(name: str, **settings: Any) -> Any:
        if name not in sessions:
            ws = store.open(name, **settings)
            register_wsgit(ws)
            sessions[name] = ws
        return sessions[name]

    steps: list[dict[str, Any]] = []
    try:
        for step in script.steps:
            if isinstance(step, Run):
                ws = session(step.session)
                result = ws.terminal(names.fill(step.command))
                names.learn(store)
                steps.append(
                    {
                        "kind": "run",
                        "session": step.session,
                        "command": step.command,
                        "stdout": names.hide(result.stdout),
                        "stderr": names.hide(result.stderr),
                        "exit_code": result.exit_code,
                    }
                )
            elif isinstance(step, Open):
                if step.session in sessions:
                    raise ValueError(f"{step.session!r} is open already")
                settings: dict[str, Any] = {}
                if step.root is not None:
                    settings["root"] = step.root
                if step.ignore:
                    settings["ignore"] = list(step.ignore)
                session(step.session, **settings)
                names.learn(store)
                steps.append(
                    {
                        "kind": "open",
                        "session": step.session,
                        "root": step.root,
                        "ignore": list(step.ignore),
                    }
                )
            elif isinstance(step, Write):
                ws = session(step.session)
                if step.data is not None:
                    content: Any = base64.b64decode(step.data)
                else:
                    content = (step.text or "").encode()
                ws.files.write(step.path, content)
                names.learn(store)
                steps.append(
                    {
                        "kind": "write",
                        "session": step.session,
                        "path": step.path,
                        "text": step.text,
                        "data": step.data,
                    }
                )
            elif isinstance(step, Tag):
                store.tags.add(session(step.session), step.name)
                names.learn(store)
                steps.append(
                    {"kind": "tag", "session": step.session, "name": step.name}
                )
            elif isinstance(step, Converse):
                ws = session(step.session)
                turn = ws.turns.begin(step.run.id, harness=step.harness)
                turn.end(step.run.status, body=step.run.to_dict())  # type: ignore[arg-type]
                names.learn(store)
                steps.append(
                    {
                        "kind": "converse",
                        "session": step.session,
                        "harness": step.harness,
                        "run": step.run.to_dict(),
                    }
                )
            elif isinstance(step, Delete):
                held = sessions.pop(step.session, None)
                if held is not None:
                    held.close()
                store.delete(step.session, min_age=0)
                steps.append({"kind": "delete", "session": step.session})
            else:  # pragma: no cover - the types above are all there are
                raise TypeError(f"unknown step {step!r}")
    finally:
        for ws in sessions.values():
            ws.close()
        store.close()
    return {"name": script.name, "summary": script.summary, "steps": steps}


def dumps(recorded: dict[str, Any]) -> str:
    return json.dumps(recorded, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


from .scripts import SCRIPTS  # noqa: E402 - the scripts use the step types
