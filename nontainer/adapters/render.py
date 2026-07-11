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
its own filesystem). Supports pipes, redirects (> >> <), heredocs
(cmd <<EOF ... EOF), && || ;, quoting, comments, and these commands: ls, cat, echo, head, tail, tee, grep, find, sed, tr,
sort, uniq, cut, wc, diff, jq, xargs, tar, gzip, zip, mkdir, cp, mv, rm,
touch, pwd, cd, basename, dirname. cwd persists between calls.

Make ONE terminal call per turn: batch related commands into a single
multiline script with ; or && — mutations are then safely sequential."""

_PYTHON_IN_TERMINAL = """\

A `python` command is available (script semantics): `python -c 'code'`,
`python file.py`, or a heredoc `python <<'EOF' ... EOF` for multiline
code without a file. Its stdout flows into pipelines, and piped input is
readable via `sys.stdin` (e.g. `cat data.json | python script.py`);
`sys.argv` and `input()` work too. For anything multiline, prefer the
heredoc or the file_write tool over complex `-c` quoting."""

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


def terminal_description(
    ws: Workspace,
    *,
    split: bool,
    apps: bool = False,
    primer: str | None = None,
    python_primer: str | None = None,
) -> str:
    """``primer`` = the terminal tool's host guidance. ``python_primer``
    is only used in terminal-only mode (no separate run_python tool), so
    it lands in the ``python`` builtin section."""
    desc = _TERMINAL_CORE
    if not split:
        desc += _PYTHON_IN_TERMINAL
        extras = _env_notes(ws)
        if extras:
            desc += "\n\nInside `python`:\n" + extras
        if python_primer:
            desc += "\n\n" + python_primer
    if apps:
        desc += APPS_NOTES
    if primer:
        desc += "\n\n" + primer
    return desc


def python_description(ws: Workspace, *, primer: str | None = None) -> str:
    desc = _PYTHON_TOOL_CORE
    extras = _env_notes(ws)
    if extras:
        desc += "\n" + extras
    if primer:
        desc += "\n\n" + primer
    desc += _ONE_CALL_NOTE
    return desc


FILE_WRITE_DESCRIPTION = """\
Write a file in the workspace (parents created, overwrites). Use this
for any multiline content — scripts, handlers, HTML — instead of shell
redirects with tricky quoting. Writing several files? Issue several
file_write calls in the same turn — that's fine."""

FILE_EDIT_DESCRIPTION = """\
Replace an exact string in a workspace file. old_string must match the
file EXACTLY (including whitespace) and appear exactly once — include
enough surrounding context to make it unique, or set replace_all=true
to replace every occurrence. Prefer this over sed for code edits.
Edits to DIFFERENT files may share a turn; multiple edits to the SAME
file should be sequential turns (parallel order is not guaranteed)."""


APPS_NOTES = """\

You can build a web app in this workspace (frontend + backend):

/app/index.html          <- entry page (served at the app root)
/app/api/<name>.py       <- backend endpoint at api/<name>
/app/api/_helpers.py     <- _-prefixed files: importable, not routable
/app/logs/api.log        <- handler errors + prints (tail it to debug)

Handlers export verb functions; example /app/api/scores.py:

    def get(req):
        limit = int(req.params.get("limit", 10))
        return {"scores": cache.get("scores", [])[:limit]}

    def post(req):
        name = req.require("name")     # 400 if missing from JSON body
        scores = cache.get("scores", []) + [name]
        cache["scores"] = scores       # NOT allowed in get() (read-only)
        return {"ok": True}

Rules: return dict/list (JSON), str (text), bytes, or Response(status=,
body=); raise HttpError(404, 'msg') for error responses. GET handlers
have a READ-ONLY filesystem and cache. Handlers see the same
environment as your python code (cache, files via open(), injected
objects). Use `with open(...)` for writes.

Test endpoints instantly with curl (no server): curl /api/scores?limit=3,
curl -X POST -d '{"name": "amy"}' /api/scores. Pipelines work:
curl /api/scores | jq .

Frontend: plain HTML/JS. Use RELATIVE urls (fetch('api/scores'), never
fetch('/api/x')). For components use Preact via https://esm.sh, or JSX
via <script type="text/babel" data-type="module"> with Babel standalone.

Shared backend code: put modules in /helpers (e.g. /helpers/data.py,
then `import data` in any handler) — imports between /app/api files do
NOT work. Browser SCRIPTS may only load from these CDNs (enforced by
test_app AND published serving): esm.sh, unpkg.com, cdn.jsdelivr.net,
cdn.plot.ly — plotly.js lives at
https://cdn.jsdelivr.net/npm/plotly.js-dist-min@2. Images, fetches,
styles, and fonts may use any https host (map tiles work); for maps,
plotly's tile-free scattergeo/choropleth need no tiles at all."""


VIEW_IMAGE_DESCRIPTION = """\
View an image file from the workspace — a saved matplotlib plot, a
generated chart, a downloaded figure. Returns the image itself, so
you can see what you produced. Supported: png, jpeg, gif, webp."""

_IMAGE_FORMATS = {
    ".png": "png",
    ".jpg": "jpeg",
    ".jpeg": "jpeg",
    ".gif": "gif",
    ".webp": "webp",
}

_MAX_IMAGE_BYTES = 10_000_000


def read_workspace_image(ws: Workspace, path: str) -> tuple[bytes, str]:
    """Read + validate an image for the view_image tool. Returns
    ``(bytes, format)``; raises ``ValueError`` with an agent-actionable
    message (unknown extension, missing file, oversized)."""
    name = path.rsplit("/", 1)[-1]
    ext = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
    fmt = _IMAGE_FORMATS.get(ext)
    if fmt is None:
        raise ValueError(
            f"not a viewable image: {path!r} (supported: "
            f"{', '.join(sorted(_IMAGE_FORMATS))})"
        )
    try:
        data = ws.fs.read(path)
    except Exception as e:
        raise ValueError(f"cannot read {path!r}: {e}") from e
    if len(data) > _MAX_IMAGE_BYTES:
        raise ValueError(
            f"{path!r} is {len(data)} bytes (cap {_MAX_IMAGE_BYTES}); "
            "downscale or re-save it smaller"
        )
    return data, fmt


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
    from ..workspace import _flatten_grants

    stdlib_names = set()
    if cfg.stdlib:
        from ..presets import STDLIB

        stdlib_names = {g.name or g.module.__name__ for g in STDLIB}
    extra_names = sorted(
        {
            (g.name or g.module.__name__)
            for g in _flatten_grants(cfg)
            if (g.name or g.module.__name__) not in stdlib_names
        }
    )
    network_mods = sorted(
        (g.name or g.module.__name__) for g in _flatten_grants(cfg) if g.network
    )
    if cfg.network:
        lines.append("- network: enabled for sandboxed code")
    elif network_mods:
        lines.append(f"- network: only via {', '.join(network_mods)}")
    else:
        lines.append(
            "- network: NONE — the workspace is offline; do not attempt "
            "downloads (browser-side app code may still load scripts "
            "from allowed CDNs)"
        )
    if cfg.stdlib:
        note = "- importable: safe stdlib (math, json, csv, datetime, re, os, pathlib, ...)"
        if extra_names:
            note += f" plus {', '.join(extra_names)}"
        lines.append(note)
    elif extra_names:
        lines.append(f"- importable modules: {', '.join(extra_names)}")
    else:
        lines.append("- no importable modules beyond builtins")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# rich reply artifacts: the `ui = {...}` namespace convention
# ---------------------------------------------------------------------------

PYTHON_UI_NOTE = """

Rich reply artifacts: assign `ui = {"name": value}` at top level, with
the OBJECT as the value — a plotly figure, pandas DataFrame, matplotlib
figure, image, or dict. The harness saves each under /ui/ (no savefig,
no writing into /ui/ yourself) and the result notes its path; embed one
in your reply with markdown image syntax, e.g.
![name](/ui/name.plotly.json), using the exact path from the note.
Unreferenced artifacts display after your reply. (A value that is the
path of a file you already saved is honored too.)"""

_MAX_ARTIFACT_BYTES = 8_000_000
_IMAGE_MAGIC = {
    b"\x89PNG\r\n\x1a\n": "png",
    b"\xff\xd8\xff": "jpg",
    b"GIF8": "gif",
    b"RIFF": "webp",
}


def _ui_write(ws: Workspace, path: str, data: bytes) -> str:
    if len(data) > _MAX_ARTIFACT_BYTES:
        raise ValueError(f"artifact too large ({len(data)} bytes)")
    ws.write_file(path, data)
    return path


def _materialize_one(ws: Workspace, name: str, value: object) -> str:
    """One value -> one workspace file. The sniff order is a THEMING
    hierarchy, most-declarative first: spec formats let the shell
    render (and theme) the artifact itself; html gives it partial say;
    pixels give it none. See the adapter docs in docs/api.md."""
    import json as _json

    mod = type(value).__module__ or ""

    # reference tier: a string naming an existing workspace file is a
    # POINTER, not content. Agents predictably save a file themselves
    # (plt.savefig(...)) and put its path in `ui` — honor the near-miss
    # instead of json-encoding the path string.
    if isinstance(value, str) and value.startswith("/"):
        try:
            if ws.fs.exists(value) and not ws.fs.isdir(value):
                return value
        except Exception:
            pass  # unreadable path: fall through to the data tier

    # spec tier: shell-rendered, shell-themed
    if mod.startswith("plotly") and hasattr(value, "to_json"):
        return _ui_write(ws, f"/ui/{name}.plotly.json", value.to_json().encode())
    if mod.startswith("pandas") and hasattr(value, "columns"):
        total = len(value)
        payload = _json.loads(
            value.head(200).to_json(orient="split", date_format="iso")
        )
        payload["total"] = total  # renderers say "showing N of total"
        return _ui_write(ws, f"/ui/{name}.table.json", _json.dumps(payload).encode())

    # pixel tier
    if mod.startswith("matplotlib") and hasattr(value, "savefig"):
        import io as _io

        buf = _io.BytesIO()
        value.savefig(buf, format="png", bbox_inches="tight")
        return _ui_write(ws, f"/ui/{name}.png", buf.getvalue())
    if mod.startswith("PIL") and hasattr(value, "save"):
        import io as _io

        buf = _io.BytesIO()
        value.save(buf, format="PNG")
        return _ui_write(ws, f"/ui/{name}.png", buf.getvalue())
    if isinstance(value, (bytes, bytearray)):
        data = bytes(value)
        for magic, ext in _IMAGE_MAGIC.items():
            if data.startswith(magic):
                return _ui_write(ws, f"/ui/{name}.{ext}", data)
        return _ui_write(ws, f"/ui/{name}.bin", data)

    # html tier: the scientific-python display ecosystem for free
    bundle_fn = getattr(value, "_repr_mimebundle_", None)
    if callable(bundle_fn):
        try:
            bundle = bundle_fn()
            if isinstance(bundle, tuple):
                bundle = bundle[0]
        except Exception:
            bundle = {}
        if isinstance(bundle, dict):
            if "text/html" in bundle:
                return _ui_write(
                    ws, f"/ui/{name}.html", str(bundle["text/html"]).encode()
                )
            if "image/png" in bundle:
                import base64 as _b64

                raw = bundle["image/png"]
                data = _b64.b64decode(raw) if isinstance(raw, str) else raw
                return _ui_write(ws, f"/ui/{name}.png", data)
    html_fn = getattr(value, "_repr_html_", None)
    if callable(html_fn):
        return _ui_write(ws, f"/ui/{name}.html", str(html_fn()).encode())

    # data tier: never fail — always render SOMETHING
    text = _json.dumps(value, indent=2, default=str)
    return _ui_write(ws, f"/ui/{name}.json", text.encode())


def materialize_ui(ws: Workspace, ui: object) -> list[tuple[str, str]]:
    """Turn the agent's ``ui = {name: value}`` namespace binding into
    workspace artifacts under ``/ui/`` (checkpointed writes). Returns
    ``[(name, path)]`` for the observation note. Values that defeat
    every renderer land as a capped ``repr`` — a debuggable floor, not
    a silent drop."""
    import re as _re

    if not isinstance(ui, dict):
        return []
    out: list[tuple[str, str]] = []
    for raw_name, value in list(ui.items())[:20]:
        name = _re.sub(r"[^\w.-]+", "-", str(raw_name)).strip("-.") or "artifact"
        try:
            path = _materialize_one(ws, name, value)
        except Exception:
            try:
                # slice sized values BEFORE repr — the fallback fires
                # exactly when something was too big (>8MB artifact
                # cap), and repr of a huge bytes/str builds the whole
                # representation in memory first
                preview = (
                    value[:10_000]
                    if isinstance(value, (str, bytes, bytearray))
                    else value
                )
                path = _ui_write(
                    ws, f"/ui/{name}.txt", repr(preview)[:10_000].encode()
                )
            except Exception:
                continue
        out.append((str(raw_name), path))
    return out
