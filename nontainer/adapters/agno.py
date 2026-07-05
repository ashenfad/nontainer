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

from agno.tools import Toolkit

from ..workspace import Workspace
from .render import (
    ToolsMode,
    python_description,
    render_python,
    render_terminal,
    resolve_tools_mode,
    terminal_description,
)

_INSTRUCTIONS = """\
You have a persistent workspace (files, shell{and_python}). Make ONE
workspace tool call per turn and batch related commands/code within it —
mutations are then safely sequential. The workspace persists across the
whole session{versioned_note}."""


class WorkspaceTools(Toolkit):
    """agno Toolkit over a nontainer :class:`Workspace`."""

    def __init__(
        self,
        workspace: Workspace,
        *,
        tools: ToolsMode = "auto",
        **kwargs,
    ) -> None:
        self._ws = workspace
        self._lock = threading.Lock()
        mode = resolve_tools_mode(workspace, tools)
        split = mode == "split"

        def terminal(command: str) -> str:
            """Run a shell script in the persistent workspace."""
            with self._lock:
                return render_terminal(self._ws.terminal(command))

        terminal.__doc__ = terminal_description(workspace, split=split)

        registered = [terminal]

        if split:

            def run_python(code: str) -> str:
                """Run Python in the sandboxed workspace environment."""
                with self._lock:
                    return render_python(self._ws.run_python(code))

            run_python.__doc__ = python_description(workspace)
            registered.append(run_python)

        instructions = _INSTRUCTIONS.format(
            and_python=", and sandboxed python" if split else "",
            versioned_note=(
                "; every mutating call is checkpointed"
                if workspace.caps.versioned
                else ""
            ),
        )

        super().__init__(
            name="nontainer_workspace",
            tools=registered,
            instructions=instructions,
            add_instructions=True,
            **kwargs,
        )
