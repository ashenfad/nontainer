"""agno adapter: a Toolkit exposing one Workspace to an agno Agent.

Usage::

    from agno.agent import Agent
    from nontainer import workspace
    from nontainer.adapters.agno import WorkspaceTools

    ws = workspace("user-42")
    agent = Agent(model=..., tools=[WorkspaceTools(ws)])

One toolkit == one workspace == one session. For per-conversation
sessions, construct a fresh ``workspace(session_id)`` + toolkit per
conversation (kvgit branches make this cheap).

Concurrency: agno's ``arun()`` executes sync tools CONCURRENTLY on
separate threads — including parallel tool calls from a single model
turn — while nontainer workspaces are single-threaded by contract.
Every tool call therefore holds a per-workspace ``threading.Lock``:
parallel calls serialize safely (each atomic + checkpointed) instead
of corrupting staged state. Under sync ``run()`` the lock is
uncontended. The toolkit ``instructions`` carry the one-call-per-turn
convention so serialization stays a backstop, not the norm.

Exposure follows ``resolve_tools_mode`` (``"auto"`` default): a plain
python environment gets a single ``terminal`` tool (with the `python`
builtin); an augmented one (cache / host objects) additionally gets a
dedicated ``run_python`` tool whose description announces the magic.
"""

from __future__ import annotations

import threading
from typing import Any

from agno.tools import Toolkit

from ..workspace import Workspace
from .render import (
    FILE_EDIT_DESCRIPTION,
    FILE_WRITE_DESCRIPTION,
    ToolsMode,
    python_description,
    render_python,
    render_terminal,
    resolve_tools_mode,
    terminal_description,
)

_INSTRUCTIONS = """\
You have a persistent workspace (files, shell{and_python}). Make ONE
terminal{or_python} call per turn, batching related commands/code within
it — mutations are then safely sequential. file_write/file_edit are
different: you MAY issue several in one turn (they execute safely), but
keep edits to the SAME file to one per turn since parallel-call order
is not guaranteed. The workspace persists across the whole
session{versioned_note}."""


class WorkspaceTools(Toolkit):
    """agno Toolkit over a nontainer :class:`Workspace`."""

    def _end_turn(self, *args: Any, **kwargs: Any) -> None:
        """Commit the turn's staged work (one commit per agent turn).
        Tolerant signature so it slots into agno ``post_hooks``; also
        callable directly by embedders after ``agent.run(...)``. No-op
        when nothing changed or the workspace is unversioned."""
        ws = self._ws
        if ws.caps.versioned and ws._provider.dirty:
            with self._lock:
                ws.checkpoint(info={"tool": "turn"})

    def __init__(
        self,
        workspace: Workspace,
        *,
        tools: ToolsMode = "auto",
        apps: Any = None,
        checkpoint: str = "call",
        **kwargs,
    ) -> None:
        """``apps``: an ``AppRuntime`` (from ``nontainer.apps.
        enable_apps``) — when given, a ``test_app`` tool is registered
        whose screenshots come back as real images (agno ``ToolResult``
        media) in addition to being saved under /app/screenshots/.

        ``checkpoint``: commit granularity on versioned workspaces.
        ``"call"`` (default) commits after each mutating tool call —
        maximum durability, chattier history. ``"turn"`` is the agex
        model — one commit per agent turn; wire :meth:`end_turn` as an
        agno run-level hook::

            tk = WorkspaceTools(ws, checkpoint="turn")
            agent = Agent(model=..., tools=[tk], post_hooks=[tk.end_turn])

        Turn mode defers commits to the hook, so a crash mid-turn can
        lose that turn's staged work (kvgit staging is in-memory)."""
        self._ws = workspace
        self._lock = threading.Lock()
        if checkpoint not in ("call", "turn"):
            raise ValueError(f"checkpoint must be 'call' or 'turn': {checkpoint!r}")
        self._turn_checkpoints = checkpoint == "turn"
        if self._turn_checkpoints:
            workspace.autocheckpoint = False
        mode = resolve_tools_mode(workspace, tools)
        split = mode == "split"

        def terminal(command: str) -> str:
            """Run a shell script in the persistent workspace."""
            with self._lock:
                return render_terminal(self._ws.terminal(command))

        terminal.__doc__ = terminal_description(
            workspace, split=split, apps=apps is not None
        )

        def file_write(path: str, content: str) -> str:
            """Write a file in the workspace."""
            with self._lock:
                written = self._ws.write_file(path, content)
                return f"wrote {written}"

        file_write.__doc__ = FILE_WRITE_DESCRIPTION

        def file_edit(
            path: str,
            old_string: str,
            new_string: str,
            replace_all: bool = False,
        ) -> str:
            """Exact-string replacement in a workspace file."""
            from ..errors import WorkspaceError

            with self._lock:
                try:
                    out = self._ws.edit_file(
                        path, old_string, new_string, replace_all=replace_all
                    )
                except WorkspaceError as e:
                    return f"edit failed: {e}"
                if out.mode == "already_applied":
                    return f"no-op: replacement already present in {path}"
                note = "" if out.mode == "exact" else f" (matched via {out.mode})"
                return f"replaced {out.count} occurrence(s) in {path}{note}"

        file_edit.__doc__ = FILE_EDIT_DESCRIPTION

        registered = [terminal, file_write, file_edit]

        if split:

            def run_python(code: str) -> str:
                """Run Python in the sandboxed workspace environment."""
                with self._lock:
                    return render_python(self._ws.run_python(code))

            run_python.__doc__ = python_description(workspace)
            registered.append(run_python)

        if apps is not None:
            from agno.media import Image
            from agno.tools.function import ToolResult

            from .render import TEST_APP_DESCRIPTION
            from ..apps import render_test_app

            def test_app(
                actions: list[dict], viewport: str = "desktop"
            ) -> ToolResult:
                """Verify the app headlessly."""
                with self._lock:
                    result = apps.test_app(actions, viewport=viewport)
                    shots = [
                        Image(content=self._ws.fs.read(p), format="png", id=p)
                        for p in result.screenshots
                    ]
                return ToolResult(
                    content=render_test_app(result),
                    images=shots or None,
                )

            test_app.__doc__ = TEST_APP_DESCRIPTION
            registered.append(test_app)

        instructions = _INSTRUCTIONS.format(
            and_python=", and sandboxed python" if split else "",
            or_python=" / run_python" if split else "",
            versioned_note=(
                "; every mutating call is checkpointed"
                if workspace.caps.versioned
                else ""
            ),
        )

        self.end_turn = self._end_turn  # bindable as an agno post_hook

        super().__init__(
            name="nontainer_workspace",
            tools=registered,
            instructions=instructions,
            add_instructions=True,
            **kwargs,
        )
