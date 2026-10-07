"""Workspace tools, defined once (shared by every harness adapter).

Every harness that hands a model a nontainer workspace offers the same
tools: ``terminal``, ``file_write``, ``file_edit``, ``view_image``, and,
depending on the session, ``run_python``, ``test_app`` and ``sessions``.
A :class:`Toolset` is the one definition of them: what each is called,
what its description says, what it takes, and what a call does and
returns, in terms no harness owns.

An adapter does two things with it. It wraps each tool in the callable
its framework introspects: agno and FastMCP both derive a tool's schema
from a Python signature, so each keeps a thin typed wrapper whose body
is one call into the toolset. And it maps a :class:`ToolOutput` onto its
own result shape (a string, an agno ``ToolResult`` with images, a list
of MCP content blocks). A loop that takes schemas directly reads
:attr:`Tool.parameters` and calls :attr:`Tool.call`.

The toolset holds one ``threading.Lock`` that fences each call's work
around the workspace, so an adapter running tools on several threads
gets the same serialization from every wrapper. The workspace enforces
its own single-writer rule beneath it; the fence also covers the reads
around a call (a screenshot read back after ``test_app``). ``sessions``
calls take no fence: the helper serializes its own job table, and a
``wait=true`` ask would otherwise stall every other tool for as long as
the delegate runs.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from ..workspace import Workspace
from .render import (
    FILE_EDIT_DESCRIPTION,
    FILE_WRITE_DESCRIPTION,
    VIEW_IMAGE_DESCRIPTION,
    ToolsMode,
    python_description,
    render_python,
    render_terminal,
    resolve_tools_mode,
    terminal_description,
)

__all__ = ["Tool", "ToolImage", "ToolOutput", "Toolset"]


@dataclass(frozen=True)
class ToolImage:
    """An image a tool call returns for the model to see."""

    data: bytes
    format: str
    """``png``, ``jpeg``, ``gif`` or ``webp``."""
    source: str | None = None
    """The workspace path the image was read from, when it was."""


@dataclass(frozen=True)
class ToolOutput:
    """What a tool call returns, in no harness's terms.

    ``text`` is what the model reads. ``is_error`` says the call did not
    do what it was asked: a failed edit, an image that could not be
    read, a ``test_app`` that could not run, a command that exited
    non-zero, python that raised. The text says so either way; the flag
    is for a harness that marks failed calls (an event stream, a
    transcript), not for the model.
    """

    text: str
    images: tuple[ToolImage, ...] = ()
    written: str | None = None
    """The workspace path this call wrote, when writing a file was the
    call's point (``file_write``), so a harness can hand the client a
    link to it."""
    is_error: bool = False


@dataclass(frozen=True)
class Tool:
    """One tool: its name, the description a model reads, a JSON Schema
    for its arguments, and the call."""

    name: str
    description: str
    parameters: Mapping[str, Any] = field(hash=False)
    call: Callable[..., ToolOutput] = field(hash=False)

    async def acall(self, **arguments: Any) -> ToolOutput:
        """The call on a worker thread, so an event loop stays free for
        as long as the tool runs."""
        return await asyncio.to_thread(self.call, **arguments)


def _object(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required}


_STRING = {"type": "string"}
_OBJECT = {"type": "object", "additionalProperties": True}

_PARAMETERS: dict[str, dict[str, Any]] = {
    "terminal": _object({"command": _STRING}, ["command"]),
    "file_write": _object({"path": _STRING, "content": _STRING}, ["path", "content"]),
    "file_edit": _object(
        {
            "path": _STRING,
            "old_string": _STRING,
            "new_string": _STRING,
            "replace_all": {"type": "boolean"},
        },
        ["path", "old_string", "new_string"],
    ),
    "view_image": _object({"path": _STRING}, ["path"]),
    "run_python": _object({"code": _STRING}, ["code"]),
    # Lists and objects are also accepted as JSON strings: models send
    # them that way often enough that refusing one on the schema would
    # cost a turn the coercion below never needed to.
    "test_app": _object(
        {
            "actions": {"anyOf": [{"type": "array", "items": _OBJECT}, _STRING]},
            "viewport": {"anyOf": [_STRING, _OBJECT]},
            "bind": {"anyOf": [_OBJECT, _STRING, {"type": "null"}]},
        },
        ["actions"],
    ),
    "sessions": _object(
        {
            "action": _STRING,
            "task": _STRING,
            "name": _STRING,
            "paths": {
                "anyOf": [
                    {"type": "array", "items": _STRING},
                    _STRING,
                    {"type": "null"},
                ]
            },
            "inherit": _STRING,
            "fork_from": _STRING,
            "resume": _STRING,
            "wait": {"type": "boolean"},
        },
        ["action"],
    ),
}


class Toolset:
    """The tools one workspace offers, and their calls.

    ``tools`` picks the exposure (``"auto"``: a ``run_python`` tool only
    when the python environment has cache or host objects; see
    ``resolve_tools_mode``). ``apps`` (an ``AppRuntime``) adds
    ``test_app``. ``sessions`` (a ``SessionRunner`` or a built
    ``nontainer.sessions.Sessions``) adds ``sessions``; a runner builds
    a helper the toolset holds as :attr:`sessions`. ``vision`` says
    whether the model takes images: without it there is no
    ``view_image``, and ``test_app`` returns its screenshots as paths
    only. The primers extend the terminal and python descriptions.
    """

    def __init__(
        self,
        workspace: Workspace,
        *,
        tools: ToolsMode = "auto",
        apps: Any = None,
        sessions: Any = None,
        terminal_primer: str | None = None,
        python_primer: str | None = None,
        vision: bool = True,
    ) -> None:
        self.workspace = workspace
        self.lock = threading.Lock()
        self.apps = apps
        self.vision = vision
        self.split = resolve_tools_mode(workspace, tools) == "split"

        self.sessions: Any = None
        if sessions is not None:
            from ..sessions import Sessions

            self.sessions = (
                sessions
                if isinstance(sessions, Sessions)
                else Sessions(workspace, sessions)
            )

        apps_config = apps.config if apps is not None else None
        descriptions = {
            "terminal": terminal_description(
                workspace,
                split=self.split,
                apps=apps_config,
                primer=terminal_primer,
                python_primer=None if self.split else python_primer,
            ),
            "file_write": FILE_WRITE_DESCRIPTION,
            "file_edit": FILE_EDIT_DESCRIPTION,
            "view_image": VIEW_IMAGE_DESCRIPTION,
        }
        if self.split:
            descriptions["run_python"] = python_description(
                workspace, apps=apps_config, primer=python_primer
            )
        if apps is not None:
            from .render import test_app_description

            descriptions["test_app"] = test_app_description(workspace)
        if self.sessions is not None:
            from .render import SESSIONS_DESCRIPTION

            descriptions["sessions"] = SESSIONS_DESCRIPTION
        self._descriptions = descriptions

    # ------------------------------------------------------------------
    # the set
    # ------------------------------------------------------------------

    def tools(self) -> list[Tool]:
        """The tools this workspace offers, in a stable order."""
        calls: dict[str, Callable[..., ToolOutput]] = {
            "terminal": self.terminal,
            "file_write": self.file_write,
            "file_edit": self.file_edit,
        }
        if self.vision:
            calls["view_image"] = self.view_image
        if self.split:
            calls["run_python"] = self.run_python
        if self.apps is not None:
            calls["test_app"] = self.test_app
        if self.sessions is not None:
            calls["sessions"] = self.sessions_action
        return [
            Tool(name, self._descriptions[name], _PARAMETERS[name], call)
            for name, call in calls.items()
        ]

    def description(self, name: str) -> str:
        """The description of the tool called ``name``."""
        return self._descriptions[name]

    # ------------------------------------------------------------------
    # the calls
    # ------------------------------------------------------------------

    def terminal(self, command: str) -> ToolOutput:
        """Run a shell script in the persistent workspace."""
        with self.lock:
            result = self.workspace.terminal(command)
        return ToolOutput(render_terminal(result), is_error=result.exit_code != 0)

    def file_write(self, path: str, content: str) -> ToolOutput:
        """Write a file in the workspace."""
        with self.lock:
            written = self.workspace.files.write(path, content)
        return ToolOutput(
            f"wrote {written.path} ({written.size} bytes)", written=written.path
        )

    def file_edit(
        self,
        path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> ToolOutput:
        """Exact-string replacement in a workspace file."""
        from ..errors import WorkspaceError

        with self.lock:
            try:
                out = self.workspace.files.edit(
                    path, old_string, new_string, replace_all=replace_all
                )
            except WorkspaceError as e:
                return ToolOutput(f"edit failed: {e}", is_error=True)
        if out.mode == "already_applied":
            return ToolOutput(f"no-op: replacement already present in {path}")
        note = "" if out.mode == "exact" else f" (matched via {out.mode})"
        return ToolOutput(f"replaced {out.count} occurrence(s) in {path}{note}")

    def view_image(self, path: str) -> ToolOutput:
        """View an image file from the workspace."""
        from .render import read_workspace_image

        with self.lock:
            try:
                data, fmt = read_workspace_image(self.workspace, path)
            except ValueError as e:
                return ToolOutput(f"view_image failed: {e}", is_error=True)
        return ToolOutput(
            f"{path} ({fmt}, {len(data)} bytes)",
            images=(ToolImage(data, fmt, source=path),),
        )

    def run_python(self, code: str) -> ToolOutput:
        """Run Python in the sandboxed workspace environment.

        The result reports the ``ui = {...}`` artifacts the call saved,
        and files it wrote into the ui directory itself, which agents
        predictably do (``fig.write_json(...)``, ``savefig``) instead of
        assigning to ``ui``: without a note those display nowhere.
        """
        from ..ui import materialize_ui, ui_root
        from .render import artifacts_note

        ws = self.workspace
        with self.lock:
            ui_dir = ui_root(ws)
            try:
                ui_before = set(ws.files.fs.list(ui_dir))
            except Exception:
                ui_before = set()
            result = ws.run_python(code)
            text = render_python(result)
            # run_python has already materialized ``ui``, so the value
            # here is an ArtifactPath and this pass adds only the names
            artifacts, problems = materialize_ui(ws, result.namespace.get("ui"))
            try:
                ui_after = set(ws.files.fs.list(ui_dir))
            except Exception:
                ui_after = set()
            claimed = {p for _, p in artifacts}
            for fname in sorted(ui_after - ui_before):
                path = f"{ui_dir}/{fname}"
                if path not in claimed and ws.files.fs.isfile(path):
                    artifacts.append((fname, path))
        text += artifacts_note(artifacts)
        # Problems from both passes, each once: run_python's own (the
        # 8MB cap and its remediation) would otherwise vanish from the
        # result the agent reads.
        seen = set()
        for problem in (*result.ui_problems, *problems):
            if problem not in seen:
                seen.add(problem)
                text += f"\n[ui note: {problem}]"
        return ToolOutput(text, is_error=bool(result.error))

    def test_app(
        self,
        actions: Any,
        viewport: Any = "desktop",
        bind: Any = None,
    ) -> ToolOutput:
        """Verify the app headlessly.

        ``actions``, ``viewport`` and ``bind`` may each arrive as JSON
        strings; they are coerced before anything runs."""
        from ..apps import render_test_app
        from ..apps.testapp import coerce_actions, coerce_bind, coerce_viewport

        if self.apps is None:
            raise RuntimeError("test_app needs an AppRuntime (Toolset(apps=...))")
        try:
            actions = coerce_actions(actions)
            viewport = coerce_viewport(viewport)
            binding = coerce_bind(bind)
        except ValueError as e:
            return ToolOutput(f"test_app failed: {e}", is_error=True)
        with self.lock:
            try:
                result = self.apps.test_app(actions, viewport=viewport, bind=binding)
            except ValueError as e:
                return ToolOutput(f"test_app failed: {e}", is_error=True)
            shots = (
                tuple(
                    ToolImage(self.workspace.files.fs.read(p), "png", source=p)
                    for p in result.screenshots
                )
                if self.vision
                else ()
            )
        return ToolOutput(render_test_app(result), images=shots)

    def sessions_action(
        self,
        action: str,
        task: str = "",
        name: str = "",
        paths: Any = None,
        inherit: str = "",
        fork_from: str = "",
        resume: str = "",
        wait: bool = False,
    ) -> ToolOutput:
        """Delegate to a fork of this session, and read it back."""
        from ..sessions import run_action

        if self.sessions is None:
            raise RuntimeError("sessions needs a runner (Toolset(sessions=...))")
        return ToolOutput(
            run_action(
                self.sessions,
                action,
                task=task,
                name=name,
                paths=paths,
                inherit=inherit,
                fork_from=fork_from,
                resume=resume,
                wait=wait,
            )
        )
