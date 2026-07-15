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

from typing import Any, Literal

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
        from ..hints import blocked_import_hint

        hint = blocked_import_hint(result.error)
        if hint:
            parts.append(f"[hint: {hint}]")
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
- helpers/: put reusable code in .py files there and import it QUALIFIED
  from the workspace root — `from helpers import mymod`, never a bare
  `import mymod` (imports resolve from '/'; works in app handlers too)"""

_CACHE_NOTE = """\
- cache: a persistent dict for DATA (picklable values), e.g.
  cache['key'] = value; contents survive across calls and sessions"""

_ONE_CALL_NOTE = """\

Make ONE call per turn; put all the code for a step in a single call."""


def terminal_description(
    ws: Workspace,
    *,
    split: bool,
    apps: Any = None,
    primer: str | None = None,
    python_primer: str | None = None,
) -> str:
    """``primer`` = the terminal tool's host guidance. ``python_primer``
    is only used in terminal-only mode (no separate run_python tool), so
    it lands in the ``python`` builtin section. ``apps``: the
    ``AppsConfig`` when the apps loop is enabled (``True`` accepted for
    the defaults; ``None``/``False`` = no apps section)."""
    desc = _TERMINAL_CORE
    if not split:
        desc += _PYTHON_IN_TERMINAL
        extras = _env_notes(ws)
        if extras:
            desc += "\n\nInside `python`:\n" + extras
        if python_primer:
            desc += "\n\n" + python_primer
    if apps:
        desc += apps_notes(None if isinstance(apps, bool) else apps)
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


# Template, not prose: __SCRIPT_HOSTS__ is filled from
# AppsConfig.script_hosts (.replace, not .format — the handler examples
# are full of literal braces) so the agent is told exactly what the
# walls enforce.
_APPS_NOTES_TEMPLATE = """\

You can build a web app in this workspace (frontend + backend):

/app/index.html          <- entry page (served at the app root)
/app/api/<name>.py       <- backend endpoint at api/<name> (the URL has
                            NO .py: /app/api/scores.py serves api/scores)
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

Rules: ONLY verb functions (get/post/put/delete/patch) are routed — a
function with any other name (def query(), def search()) is NEVER
called by requests; read filters/actions from params inside a verb.
Return dict/list (JSON), str (text), bytes, or Response(status=,
body=); raise HttpError(404, 'msg') for error responses. GET handlers
have a READ-ONLY filesystem and cache. Handlers see the same
environment as your python code (cache, files via open(), injected
objects). Use `with open(...)` for writes.

Test endpoints instantly with curl (no server): curl /api/scores?limit=3,
curl -X POST -d '{"name": "amy"}' /api/scores. Pipelines work:
curl /api/scores | jq .

Frontend: for most apps, plain HTML + DOM + fetch is the MOST RELIABLE
choice. Use RELATIVE urls and module names: fetch('api/scores') — never
fetch('/api/x') (absolute) and never fetch('api/scores.py') (404).
If you want components, use Preact as ES MODULES — copy this known-good
pattern exactly (no UMD <script src> builds, no guessing globals like
`preactHooks`):

    <script type="module">
      import { h, render } from 'https://esm.sh/preact@10';
      import { useState, useEffect } from 'https://esm.sh/preact@10/hooks';
      import htm from 'https://esm.sh/htm';
      const html = htm.bind(h);
      function App() { return html`<h1>hi</h1>`; }
      render(h(App), document.getElementById('app'));
    </script>

Shared backend code: put modules in /helpers (e.g. /helpers/data.py,
then `from helpers import data` in any handler — imports resolve from
the workspace root, so a bare `import data` will NOT find it) —
imports between /app/api files do NOT work. Browser SCRIPTS may only
load from these hosts (enforced by test_app AND published serving):
__SCRIPT_HOSTS__
— plotly.js lives at
https://cdn.jsdelivr.net/npm/plotly.js-dist-min@2. Images, fetches,
styles, and fonts may use any https host (map tiles work); for maps,
plotly's tile-free scattergeo/choropleth need no tiles at all.

After changing the app, ALWAYS verify with the test_app tool before
telling the user it works — it catches what curl can't (frontend
wiring, absolute-URL mistakes, blocked scripts) and reports exactly
what it rejected and why."""


def apps_notes(config: Any = None) -> str:
    """The apps section of the terminal tool description, derived from
    an ``AppsConfig``: the script-host sentence states what the walls
    actually enforce, and ``apps_primer`` (embedder guidance — private
    component libs, house conventions) lands at the end."""
    if config is None:
        from ..apps import AppsConfig

        config = AppsConfig()
    notes = _APPS_NOTES_TEMPLATE.replace(
        "__SCRIPT_HOSTS__", ", ".join(config.script_hosts)
    )
    if config.apps_primer:
        notes += "\n\n" + config.apps_primer
    return notes


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
over read-and-check when you know the expected condition. When an
assert fails, fix the APP, not the assert — weakening an assertion
until it cannot fail (e.g. `x !== '0' || x === '0'`) verifies nothing.
Screenshots are returned as images AND saved to /app/screenshots/.
Backend errors land in /app/logs/api.log (tail it to debug)."""


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
path of a file you already saved is honored too.)
For a dashboard, e.g.
ui = {"kpis": [{"label": "Revenue", "value": "$1.2M", "sublabel": "+8% MoM"}]}
renders a card row; callout items are {"type": "callout", "title",
"body", optional "tone": info|success|warning}.
Artifacts are capped at 8MB. For large scatter/map data use WebGL
traces (scattergl, scattermapbox) and keep the spec lean: per-point
customdata/hover text is the usual size killer — aggregate or sample
it rather than shipping every row."""

_MAX_ARTIFACT_BYTES = 8_000_000


class _ArtifactTooLarge(ValueError):
    """A materialized value blew the artifact cap — carries the size so
    the observation can say WHY (and how to shrink it) instead of
    silently degrading to a repr."""

    def __init__(self, size: int) -> None:
        super().__init__(f"artifact too large ({size} bytes)")
        self.size = size


_IMAGE_MAGIC = {
    b"\x89PNG\r\n\x1a\n": "png",
    b"\xff\xd8\xff": "jpg",
    b"GIF8": "gif",
    b"RIFF": "webp",
}


def _ui_write(ws: Workspace, path: str, data: bytes) -> str:
    if len(data) > _MAX_ARTIFACT_BYTES:
        raise _ArtifactTooLarge(len(data))
    ws.write_file(path, data)
    return path


def _too_large_note(name: str, size: int, mod: str) -> str:
    """Actionable diagnosis for the cap: the agent reads this in the
    tool result and self-corrects; the human sees it where the figure
    would have been."""
    note = (
        f"ui artifact {name!r} NOT rendered: too large "
        f"({size / 1e6:.1f}MB > {_MAX_ARTIFACT_BYTES / 1e6:.0f}MB cap)."
    )
    if mod.startswith("plotly"):
        return note + (
            " The usual culprit is per-point customdata/hover text — drop or"
            " aggregate it. Coordinates are cheap (binary-encoded); WebGL"
            " traces (scattergl, scattermapbox) render 100k+ points fine,"
            " but the spec must still fit the cap."
        )
    return note + " Downsample or aggregate before assigning to `ui`."


def _is_stat(i: object) -> bool:
    """A stat tile: any dict carrying label + value. Untagged is fine —
    it is the shape agents naturally emit — so a tagged callout that also
    happens to hold label/value is disambiguated by ``_is_callout`` first."""
    return isinstance(i, dict) and "label" in i and "value" in i


def _is_callout(i: object) -> bool:
    """A callout: a TAGGED dict (``type == "callout"``) with a title or
    body. The tag is required — an untagged {title, body} would collide
    with too many ordinary dicts to duck-type safely."""
    return (
        isinstance(i, dict)
        and i.get("type") == "callout"
        and ("title" in i or "body" in i)
    )


def _card_row_near_miss(name: object, value: object) -> str | None:
    """The dict-native version of a constructor error: a list where MOST
    items duck-type as cards but some don't would silently miss the cards
    tier — say which item broke the row and why, in the problems channel
    the agent already reads (the 8MB cap's lesson: name the fix, not just
    the failure). None when the list isn't card-shaped enough to diagnose."""
    if not isinstance(value, list) or not value:
        return None
    matched = sum(1 for i in value if _is_stat(i) or _is_callout(i))
    if matched == len(value) or matched * 2 < len(value):
        return None  # a real card row, or not plausibly one
    import reprobate

    bad = next(i for i in value if not (_is_stat(i) or _is_callout(i)))
    return (
        f"{str(name)!r} looks like a card row, but this item is neither a "
        f"stat (needs 'label' and 'value') nor a tagged callout (needs "
        f"'type': 'callout' plus a 'title' or 'body'): "
        f"{reprobate.render(bad, budget=200)}. Fix that item to render cards."
    )


def _normalize_card(i: dict) -> dict:
    """One duck-typed item -> its canonical card dict. Callouts checked
    first so a tagged callout never masquerades as a stat. Unknown keys
    are dropped; legacy stat shapes (delta -> sublabel, unit -> value)
    are folded so older agent output still renders."""
    if _is_callout(i):
        tone = i.get("tone")
        if tone not in ("info", "success", "warning"):
            tone = "info"  # never infer sentiment; unknown/absent -> info
        return {
            "type": "callout",
            "title": str(i["title"]) if "title" in i else "",
            "body": str(i["body"]) if "body" in i else "",
            "tone": tone,
        }
    item: dict = {"type": "stat", "label": str(i["label"]), "value": i["value"]}
    if "unit" in i:
        item["value"] = f"{i['value']}{i['unit']}"  # legacy: unit onto value
    if "sublabel" in i:
        item["sublabel"] = str(i["sublabel"])
    elif "delta" in i:
        item["sublabel"] = str(i["delta"])  # legacy: delta folds into sublabel
    return item


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

    # cards tier: a list of stat / callout dicts is a dashboard row — a
    # declarative shape with no other plausible rendering, so duck-type it
    # (zero sandbox imports) rather than demand a marker. A stat is any dict
    # with label+value (tagged or not — the shape agents naturally produce);
    # a callout must be tagged (type "callout") with a title or body. If a
    # single element is neither, the whole list falls through to the JSON
    # floor. The renderer never infers sentiment from a value's sign —
    # direction lives in the sublabel's words, tone only on callouts.
    if (
        isinstance(value, list)
        and value
        and all(_is_stat(i) or _is_callout(i) for i in value)
    ):
        items = [_normalize_card(i) for i in value[:24]]  # cap: a wall past
        # two dozen is noise. default=str: stat values are routinely numpy
        # scalars (df.sum()), which json.dumps rejects — degrade them to
        # strings, not the repr fallback.
        return _ui_write(
            ws,
            f"/ui/{name}.cards.json",
            _json.dumps({"items": items}, default=str).encode(),
        )

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


def materialize_ui(
    ws: Workspace, ui: object
) -> tuple[list[tuple[str, str]], list[str]]:
    """Turn the agent's ``ui = {name: value}`` namespace binding into
    workspace artifacts under ``/ui/`` (checkpointed writes). Returns
    ``(artifacts, problems)``: ``[(name, path)]`` for the observation
    note, plus diagnosis strings for values that could not be rendered
    as intended (today: the size cap) — the adapter puts those in the
    tool result so the agent can self-correct. Values that defeat every
    renderer land as a capped ``repr`` — a debuggable floor, not a
    silent drop."""
    import re as _re

    if not isinstance(ui, dict):
        # Envelope forgiveness: agents predictably assign the card LIST
        # straight to `ui` (observed twice, different models — the items
        # were perfect, only the dict wrapper was missing). A bare list
        # with exactly one plausible meaning is adopted under a default
        # name; any other non-dict still renders nothing.
        if (
            isinstance(ui, list)
            and ui
            and all(_is_stat(i) or _is_callout(i) for i in ui)
        ):
            ui = {"cards": ui}
        else:
            near_miss = _card_row_near_miss("ui", ui)
            return [], ([near_miss] if near_miss else [])
    out: list[tuple[str, str]] = []
    problems: list[str] = []
    for raw_name, value in list(ui.items())[:20]:
        near_miss = _card_row_near_miss(raw_name, value)
        if near_miss:
            problems.append(near_miss)  # value still lands (JSON floor)
        name = _re.sub(r"[^\w.-]+", "-", str(raw_name)).strip("-.") or "artifact"
        try:
            path = _materialize_one(ws, name, value)
        except _ArtifactTooLarge as e:
            # the one failure agents hit in practice — say WHY, in both
            # the artifact slot (human) and the problems note (agent)
            msg = _too_large_note(str(raw_name), e.size, type(value).__module__ or "")
            problems.append(msg)
            try:
                path = _ui_write(ws, f"/ui/{name}.txt", msg.encode())
            except Exception:
                continue
        except Exception:
            try:
                # slice sized values BEFORE repr — repr of a huge
                # bytes/str builds the whole representation in memory
                preview = (
                    value[:10_000]
                    if isinstance(value, (str, bytes, bytearray))
                    else value
                )
                path = _ui_write(ws, f"/ui/{name}.txt", repr(preview)[:10_000].encode())
            except Exception:
                continue
        out.append((name, path))
    return out, problems


# ---------------------------------------------------------------------------
# the artifacts-note contract: a blessed, round-trippable line so harnesses
# parse tool results with a public function, never a private regex. Builder
# and parser live side by side so the grammar stays honest.
# ---------------------------------------------------------------------------


def artifact_kind(path: str) -> str:
    """Suffix -> render kind, the single source of truth mirroring
    studio's ``Artifact.svelte`` dispatch. The compound spec suffixes
    (``.plotly.json`` / ``.table.json`` / ``.cards.json``) MUST be
    tested before the bare ``.json`` floor — a plain ``.json`` still
    means ``"json"`` here, though consumers may content-sniff it as
    plotly, as studio does."""
    lower = path.lower()
    if lower.endswith(".plotly.json"):
        return "plotly"
    if lower.endswith(".table.json"):
        return "table"
    if lower.endswith(".cards.json"):
        return "cards"
    if lower.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
        return "image"
    if lower.endswith(".html"):
        return "html"
    if lower.endswith(".json"):
        return "json"
    if lower.endswith(".txt"):
        return "text"
    return "binary"


def artifacts_note(artifacts: list[tuple[str, str]]) -> str:
    """Build the model-facing ``[ui artifacts: ...]`` line — the affordance
    the agent reads to embed ``![name](/ui/...)`` in its prose. Names must
    arrive pre-sanitized (``materialize_ui`` guarantees this): the parser's
    grammar hinges on ``", "`` and ``" -> "`` never occurring inside a name.
    Returns ``""`` for no artifacts so callers append unconditionally."""
    if not artifacts:
        return ""
    listing = ", ".join(f"{name} -> {path}" for name, path in artifacts)
    return f"\n[ui artifacts: {listing}]"


def parse_artifacts_note(text: str) -> list[tuple[str, str]]:
    """Blessed inverse of ``artifacts_note`` — recover ``(name, path)``
    pairs from a tool-result string. The note rides mid-string (appended
    after ``render_python`` output, before any ``[ui note: ...]`` problem
    lines), so the match anchors on the bracketed prefix, not the string
    bounds. Sanitized names make the grammar unambiguous. Returns ``[]``
    when there is no note."""
    import re as _re

    m = _re.search(r"\[ui artifacts: (.*?)\]", text)
    if not m:
        return []
    pairs: list[tuple[str, str]] = []
    for seg in m.group(1).split(", "):
        sm = _re.fullmatch(r"([\w.-]+) -> (/\S+)", seg)
        if sm:
            pairs.append((sm.group(1), sm.group(2)))
    return pairs
