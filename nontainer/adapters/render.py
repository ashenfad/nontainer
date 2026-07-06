"""Shared adapter logic: observation rendering, tool descriptions,
and the exposure-mode heuristic.

Rendering rules (design decisions, README):

- ``PythonResult.namespace`` is for the HOST — never inlined into the
  model's observation; at most a one-line note naming the bindings.
- Truncation is surfaced explicitly; agents handle "output was cut"
  far better than silent loss.
- stderr chatter does not imply failure and is labeled, not dropped.
"""

from __future__ import annotations

from typing import Literal

from ..workspace import PythonResult, TerminalResult, Workspace

ToolsMode = Literal["auto", "terminal", "split"]


def resolve_tools_mode(ws: Workspace, mode: ToolsMode = "auto") -> str:
    """``"auto"`` codifies the session diagnostic: if the python
    environment has namespace magic (cache, host objects), script
    semantics would mislead inside a terminal frame → split tools.
    A plain environment tells no lies as a ``python`` shell command
    → one terminal tool."""
    if mode != "auto":
        return mode
    cfg = ws.python_config
    augmented = ws.cache_enabled or bool(cfg.host_objects)
    return "split" if augmented else "terminal"


# ---------------------------------------------------------------------------
# observation rendering
# ---------------------------------------------------------------------------


def render_terminal(result: TerminalResult) -> str:
    parts: list[str] = []
    if result.stdout:
        parts.append(result.stdout.rstrip("\n"))
    if result.exit_code != 0:
        parts.append(f"[exit code {result.exit_code}]")
    if result.stderr:
        parts.append(f"[stderr]\n{result.stderr.rstrip()}")
    if result.truncated:
        parts.append("[output truncated]")
    return "\n".join(parts) if parts else "(no output)"


def render_python(result: PythonResult) -> str:
    parts: list[str] = []
    if result.stdout:
        parts.append(result.stdout.rstrip("\n"))
    if result.error:
        parts.append(f"[error]\n{result.error.rstrip()}")
    if result.stderr:
        parts.append(f"[stderr]\n{result.stderr.rstrip()}")
    if result.namespace:
        names = ", ".join(sorted(result.namespace))
        parts.append(f"[namespace kept for host: {names}]")
    if result.truncated:
        parts.append("[output truncated]")
    return "\n".join(parts) if parts else "(no output; success)"


# ---------------------------------------------------------------------------
# tool descriptions (the prompt-sized contract)
# ---------------------------------------------------------------------------

_TERMINAL_CORE = """\
Run shell commands in your persistent workspace (a virtual computer with
its own filesystem). Supports pipes, redirects (> >> <), && || ;, quoting,
and these commands: ls, cat, echo, head, tail, tee, grep, find, sed, tr,
sort, uniq, cut, wc, diff, jq, xargs, tar, gzip, zip, mkdir, cp, mv, rm,
touch, pwd, cd, basename, dirname. cwd persists between calls.

Make ONE terminal call per turn: batch related commands into a single
multiline script with ; or && — mutations are then safely sequential."""

_PYTHON_IN_TERMINAL = """\

A `python` command is available (script semantics): `python -c 'code'`,
`python file.py`, or pipe code via stdin. Its stdout flows into pipelines.
Prefer writing scripts to files and running `python script.py` over
complex -c quoting."""

_PYTHON_TOOL_CORE = """\
Run Python code in a sandboxed environment attached to the same workspace
as the terminal (shared files and cwd). Script semantics per call:
variables do NOT persist between calls. What does persist:
- files: read/write with normal open(), visible to the terminal too
- helpers/: put reusable code in .py files there and import it"""

_CACHE_NOTE = """\
- cache: a persistent dict for DATA (picklable values), e.g.
  cache['key'] = value; contents survive across calls and sessions"""

_ONE_CALL_NOTE = """\

Make ONE call per turn; put all the code for a step in a single call."""


def terminal_description(ws: Workspace, *, split: bool) -> str:
    desc = _TERMINAL_CORE
    if not split:
        desc += _PYTHON_IN_TERMINAL
        extras = _env_notes(ws)
        if extras:
            desc += "\n\nInside `python`:\n" + extras
    return desc


def python_description(ws: Workspace) -> str:
    desc = _PYTHON_TOOL_CORE
    extras = _env_notes(ws)
    if extras:
        desc += "\n" + extras
    desc += _ONE_CALL_NOTE
    return desc


TEST_APP_DESCRIPTION = """\
Verify the app under /app in a headless browser — no server needed.
Pass a list of actions, executed in order:
  {"click": "#selector"}          {"type": ["#selector", "text"]}
  {"read": "#selector"}           {"eval": "js expression"}
  {"assert": "js expression"}     (retries until truthy, ~2s)
  {"screenshot": true}            {"wait": ms}
viewport: "desktop" | "tablet" | "mobile".

The app is served under a path prefix: frontend code MUST use relative
URLs (fetch('api/x'), never fetch('/api/x')). Prefer {"assert": ...}
over read-and-check when you know the expected condition. Screenshots
are returned as images AND saved to /app/screenshots/. Backend errors
land in /app/logs/api.log (tail it to debug)."""


def _env_notes(ws: Workspace) -> str:
    lines: list[str] = []
    if ws.cache_enabled:
        lines.append(_CACHE_NOTE)
    cfg = ws.python_config
    if cfg.host_objects:
        names = ", ".join(sorted(cfg.host_objects))
        lines.append(
            f"- injected objects available by name: {names} (live host "
            "resources; call them directly, do not try to construct them)"
        )
    module_names = []
    for entry in cfg.modules:
        mod = getattr(entry, "module", entry)
        module_names.append(getattr(mod, "__name__", str(mod)))
    if module_names:
        lines.append(f"- importable modules: {', '.join(sorted(module_names))}")
    else:
        lines.append("- no importable modules beyond builtins")
    return "\n".join(lines)
