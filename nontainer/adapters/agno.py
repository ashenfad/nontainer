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
turn. ``Workspace`` enforces its own single-writer invariant (mutating
calls hold an internal lock), so parallel calls serialize safely (each
atomic + checkpointed) even without adapter help. The toolkit keeps a
per-workspace ``threading.Lock`` anyway: it additionally fences
adapter-level work around the call (``test_app`` + screenshot reads,
turn-commit checks) and is uncontended under sync ``run()``. The
toolkit ``instructions`` carry the one-call-per-turn convention so
serialization stays a backstop, not the norm.

Exposure follows ``resolve_tools_mode`` (``"auto"`` default): a plain
python environment gets a single ``terminal`` tool (with the `python`
builtin); an augmented one (cache / host objects) additionally gets a
dedicated ``run_python`` tool whose description announces the magic.
"""

from __future__ import annotations

import threading
from typing import Any

from agno.media import Image
from agno.tools import Toolkit
from agno.tools.function import ToolResult

from ..workspace import Workspace
from .render import (
    FILE_EDIT_DESCRIPTION,
    FILE_WRITE_DESCRIPTION,
    PYTHON_UI_NOTE,
    VIEW_IMAGE_DESCRIPTION,
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

    def _end_turn(self, *args: Any, **kwargs: Any) -> str | None:
        """Commit the turn's staged work (one commit per agent turn).
        Tolerant signature so it slots into agno ``post_hooks``; also
        callable directly by embedders after ``agent.run(...)``.
        Returns the turn's commit id, or None when nothing changed or
        the workspace is unversioned."""
        ws = self._ws
        if ws.caps.versioned and ws.dirty:
            with self._lock:
                return ws.checkpoint(info={"tool": "turn"})
        return None

    def __init__(
        self,
        workspace: Workspace,
        *,
        tools: ToolsMode = "auto",
        apps: Any = None,
        checkpoint: str = "call",
        terminal_primer: str | None = None,
        python_primer: str | None = None,
        vision: bool = True,
        **kwargs,
    ) -> None:
        """``apps``: an ``AppRuntime`` (from ``nontainer.apps.
        enable_apps``) — when given, a ``test_app`` tool is registered
        whose screenshots come back as real images (agno ``ToolResult``
        media) in addition to being saved under /app/screenshots/.

        ``vision``: whether the driving model accepts image input.
        With ``False``, ``view_image`` isn't registered and ``test_app``
        screenshots stay path-only (still saved to the workspace) —
        attaching media a model can't take errors the whole next call
        ("no endpoints support image input"), losing the turn.

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
        if python_primer and not split:
            import warnings

            warnings.warn(
                "python_primer set but tools resolved to terminal-only "
                "(no run_python tool); it will appear in the terminal "
                "tool's python section. Put python-tool guidance in "
                "terminal_primer if that's not intended.",
                stacklevel=2,
            )

        def terminal(command: str) -> str:
            """Run a shell script in the persistent workspace."""
            with self._lock:
                return render_terminal(self._ws.terminal(command))

        terminal.__doc__ = terminal_description(
            workspace,
            split=split,
            apps=apps is not None,
            primer=terminal_primer,
            python_primer=None if split else python_primer,
        )

        def file_write(path: str, content: str) -> str:
            """Write a file in the workspace."""
            with self._lock:
                written = self._ws.write_file(path, content)
                return f"wrote {written.path} ({written.size} bytes)"

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

        def view_image(path: str) -> ToolResult:
            """View an image file from the workspace."""
            from .render import read_workspace_image

            try:
                data, fmt = read_workspace_image(self._ws, path)
            except ValueError as e:
                return ToolResult(content=f"view_image failed: {e}")
            return ToolResult(
                content=f"{path} ({fmt}, {len(data)} bytes)",
                images=[Image(content=data, format=fmt, id=path)],
            )

        view_image.__doc__ = VIEW_IMAGE_DESCRIPTION

        registered = [terminal, file_write, file_edit]
        if vision:
            registered.append(view_image)

        if split:

            def run_python(code: str) -> str:
                """Run Python in the sandboxed workspace environment."""
                from .render import materialize_ui

                with self._lock:
                    try:
                        ui_before = set(self._ws.fs.list("/ui"))
                    except Exception:
                        ui_before = set()
                    result = self._ws.run_python(code)
                    text = render_python(result)
                    # the `ui = {...}` convention: namespace values become
                    # workspace artifacts the model can embed in its reply
                    artifacts, problems = materialize_ui(
                        self._ws, result.namespace.get("ui")
                    )
                    # near-miss adoption: agents predictably write INTO
                    # /ui themselves (fig.write_json('/ui/x.json'),
                    # savefig) instead of assigning objects to `ui` —
                    # without a note those files display nowhere. New
                    # files the call created join the artifacts note.
                    try:
                        ui_after = set(self._ws.fs.list("/ui"))
                    except Exception:
                        ui_after = set()
                    claimed = {p for _, p in artifacts}
                    for fname in sorted(ui_after - ui_before):
                        path = f"/ui/{fname}"
                        if path not in claimed and self._ws.fs.isfile(path):
                            artifacts.append((fname, path))
                    if artifacts:
                        listing = ", ".join(f"{n} -> {p}" for n, p in artifacts)
                        text += f"\n[ui artifacts: {listing}]"
                    for problem in problems:  # e.g. the 8MB cap, with the fix
                        text += f"\n[ui note: {problem}]"
                    return text

            run_python.__doc__ = (
                python_description(workspace, primer=python_primer) + PYTHON_UI_NOTE
            )
            registered.append(run_python)

        if apps is not None:
            from ..apps import render_test_app
            from .render import TEST_APP_DESCRIPTION

            # actions is annotated loose: models routinely send the list
            # as a JSON STRING, and agno's pydantic layer would reject it
            # on the annotation BEFORE coerce_actions gets its chance
            def test_app(
                actions: "list[dict] | str", viewport: str = "desktop"
            ) -> ToolResult:
                """Verify the app headlessly."""
                from ..apps.testapp import coerce_actions

                try:
                    actions = coerce_actions(actions)
                except ValueError as e:
                    return ToolResult(content=f"test_app failed: {e}")
                with self._lock:
                    result = apps.test_app(actions, viewport=viewport)
                    shots = (
                        [
                            Image(content=self._ws.fs.read(p), format="png", id=p)
                            for p in result.screenshots
                        ]
                        if vision
                        else []
                    )
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

        # skills discovery: catalog /skills/*/SKILL.md frontmatter —
        # access needs no tools (the terminal reads them; scripts run
        # sandboxed through the normal tools)
        from ..skills import catalog as skills_catalog

        instructions += skills_catalog(workspace)

        self.end_turn = self._end_turn  # bindable as an agno post_hook

        super().__init__(
            name="nontainer_workspace",
            tools=registered,
            instructions=instructions,
            add_instructions=True,
            **kwargs,
        )
