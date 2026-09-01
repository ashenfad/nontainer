"""AppRuntime: dispatch requests into agent-authored handlers.

One core function, three consumers (the curl builtin, test_app, and
the live router); see docs/apps.md. Handlers execute through the
workspace's EXTENSION SURFACE (``exec_python(view=...)`` / ``lock`` —
no private access), the ``view`` declaring a restricted, budgeted
execution the executor realizes its own way:

- GET → a read-only fs + read-only cache view (a GET that writes
  raises — structural REST);
- mutating verbs → a normal view; when the provider supports staging
  AND had no pending changes, a handler that raises gets its staged
  writes discarded (per-request atomicity). Requests never mint
  commits.

Tracebacks and handler stdout land in ``<root>/app/logs/api.log`` — the
agent's repair loop is ``tail``, edit, retry.
"""

from __future__ import annotations

import json
import posixpath
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..executor import ViewSpec
from ..workspace import Workspace
from .contract import (
    HttpError,
    Request,
    Response,
    WireResponse,
    make_request,
    normalize,
)

# Fixed names under the workspace root (ws.root, default /workspace):
# the app tree is <root>/app, handlers <root>/app/api, the handler log
# <root>/app/logs/api.log. AppRuntime derives the absolute paths.
APP_DIR = "app"


def app_root(ws: Workspace) -> str:
    """The app tree's absolute path in this workspace."""
    base = "" if ws.root == "/" else ws.root
    return f"{base}/{APP_DIR}"


_VERBS = frozenset({"get", "post", "put", "delete", "patch"})

# Written when the log is first created. An EMPTY log is
# indistinguishable from a broken one to an agent tailing it — it reads
# as "logging is broken" rather than "nothing has errored yet", which
# sends the repair loop chasing phantoms instead of the bug. The header
# plus a line per request make the file evidence that logging works, so
# silence below it is a fact about the app.
_LOG_HEADER = (
    "# api.log — one line per /api request (METHOD path -> status), plus\n"
    "# handler stdout and tracebacks. Nothing below this header means no\n"
    "# request has reached the app yet, NOT that logging is broken.\n"
)


def _query_string(request: Request) -> str:
    """The request's params re-encoded, for log correlation."""
    from urllib.parse import urlencode

    return urlencode(request.params) if request.params else ""


def _error_response(status: int, message: str, **extra: str) -> WireResponse:
    """Error bodies ride as JSON: model-written frontends call
    res.json() unconditionally, so a plain-text error cascades into a
    second, misleading SyntaxError in the app console. JSON keeps
    their catch-blocks functional ({"error": ..., ...})."""
    body = json.dumps({"error": message, **extra}).encode()
    return WireResponse(int(status), body, "application/json")


_STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".txt": "text/plain; charset=utf-8",
    # Vendored bundles bring these. A font survives the octet-stream
    # fallback; wasm does NOT — WebAssembly.instantiateStreaming refuses
    # anything but application/wasm, so a library with a wasm core would
    # fail with nothing in the log to explain it.
    ".wasm": "application/wasm",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".ttf": "font/ttf",
    ".map": "application/json",
}


def _build_assets(static_assets: Mapping[str, str | Path]) -> dict[str, Any]:
    """URL prefix -> read-only, confined filesystem over a host
    directory. Reuses the composition a read-only ``Mount`` gets
    (``ReadOnlyFS(IsolatedFS(dir))``) — same confinement primitive, a
    different plane: this one never touches the workspace."""
    if not static_assets:
        return {}
    from monkeyfs import IsolatedFS, ReadOnlyFS

    out: dict[str, Any] = {}
    for raw, source in static_assets.items():
        prefix = str(raw).strip("/")
        if not prefix or any(p in (".", "..", "") for p in prefix.split("/")):
            raise ValueError(f"static_assets prefix must be a relative path: {raw!r}")
        if prefix == "api" or prefix.startswith("api/"):
            # /api/ routes to handlers before static ever runs, so an
            # asset there would be silently unreachable.
            raise ValueError(
                f"static_assets prefix {raw!r} is unreachable: /api/ routes to handlers"
            )
        real = Path(source).expanduser().resolve()
        if not real.is_dir():
            raise ValueError(f"static_assets source is not a directory: {real}")
        out[prefix] = ReadOnlyFS(IsolatedFS(str(real)))
    return out


def _content_type(path: str) -> str:
    """Content type by extension, octet-stream when unknown."""
    name = path.rsplit("/", 1)[-1]
    ext = "." + name.rsplit(".", 1)[-1] if "." in name else ""
    return _STATIC_TYPES.get(ext, "application/octet-stream")


# Trailer appended to handler source. Catches HttpError in-sandbox so
# intentional errors come back structured, not as tracebacks.
_TRAILER = """

try:
    nt__resp = {verb}(nt__req)
    nt__http = None
except HttpError as nt__e:
    nt__resp = None
    nt__http = (nt__e.status, nt__e.message)
"""


# Where browser SCRIPTS may load from. One declaration drives all four
# surfaces that used to be hand-synced: test_app's request interception,
# the served-HTML CSP script-src, the agent-facing APPS_NOTES sentence,
# and curl's external-URL error message — so what verifies headlessly,
# what serves published, and what the agent is TOLD can never disagree.
DEFAULT_SCRIPT_HOSTS = (
    "esm.sh",
    "unpkg.com",
    "cdn.jsdelivr.net",
    "cdn.plot.ly",
    "cdn.tailwindcss.com",
)


@dataclass(frozen=True)
class AppsConfig:
    request_timeout: float = 5.0
    # request_timeout is the real per-request guard (same sandbox
    # checkpoint checks both); the tick limit only backstops it and
    # must not fire on an honest handler looping over a big frame.
    request_tick_limit: int = 10_000_000
    max_response_bytes: int = 2_000_000
    script_hosts: tuple[str, ...] = DEFAULT_SCRIPT_HOSTS
    """Hosts browser scripts may load from (test_app enforcement, served
    CSP, and the agent guidance all derive from this one tuple)."""
    apps_primer: str | None = None
    """Embedder guidance APPENDED to the apps notes in the tool
    description — available endpoints, house conventions, anything
    additive. To change what the agent is told about frontend
    libraries, use ``frontend_notes``: that replaces, and appending a
    correction underneath the built-in block leaves the wrong
    instruction both first and more emphatic."""
    static_assets: Mapping[str, str | Path] = field(default_factory=dict)
    """URL prefix -> host directory of fixed files served WITH the app
    but absent from the workspace: a vendored component library, fonts,
    a charting bundle. ``{"vendor": "/srv/appassets"}`` serves
    ``/srv/appassets/mui.js`` at ``vendor/mui.js``.

    This is to the browser what ``host_objects`` is to handlers — an
    embedder-supplied capability reached at request time, not workspace
    state. So it is deliberately NOT a :class:`~nontainer.Mount`: the
    agent cannot read, edit, or list these files (it is told as much,
    and ``curl vendor/x.js`` still works for a peek), they never enter
    the versioning plane or a remote executor's guest tree, and a
    published snapshot needs no copy of them.

    They are same-origin, so ``script_hosts`` needs no entry — ``'self'``
    is always allowed by the served CSP. The two exemptions assets get
    from handler rules are deliberate: no ``max_response_bytes`` cap
    (the embedder chose these bytes; the cap exists to catch runaway
    handler output), and precedence over a workspace file at the same
    path, which is noted in ``api.log`` rather than shadowed silently.

    Declare it on the ONE config an embedder passes to both
    ``enable_apps`` and ``build_router``: assets missing from the
    serving side are an app that verifies green and 404s published."""
    frontend_notes: str | None = None
    """What libraries the app can use and where they come from —
    the one part of the apps notes that is a statement about SUPPLY,
    which only the embedder knows.

    ``None`` keeps the default block (Preact/htm from esm.sh, plotly
    from jsdelivr — the right answer when nothing is vendored and the
    CDN allowlist is reachable). ``""`` omits it. A string REPLACES it:

        AppsConfig(
            static_assets={"vendor": assets},
            frontend_notes=(
                "Charts: <script src='vendor/plotly.min.js'></script>.\\n"
                "Components: import from 'vendor/preact.mjs'."
            ),
        )

    Replacing matters most for an air-gapped deployment, where the
    default block would tell the agent — emphatically, and by example —
    to fetch from hosts that do not resolve. Import
    ``nontainer.adapters.render.DEFAULT_FRONTEND_NOTES`` to extend the
    default rather than discard it.

    It also carries the default CHOICE — "plain DOM is the most reliable
    choice" is the first line of the built-in block, not template prose —
    so an embedder that vendors a design system is not contradicted by
    the library it embeds. Overriding this therefore replaces the
    recommended APPROACH as well as the library list.

    What stays regardless, because it is about the SHAPE of the code
    rather than which approach to take: relative URLs, and the rule
    against swapping a named import for a ``<script src>`` build or a
    guessed global.

    Declared after ``static_assets`` so the 0.3.3 positional signature
    still binds that one sixth."""
    csp: str | None = None
    """The Content-Security-Policy served HTML carries — and, since
    0.3.5, the one ``test_app`` enforces during verification.

    ``None`` derives it from ``script_hosts`` via ``serve.build_csp``;
    ``""`` disables it; a string is used verbatim.

    The resolved policy is what goes on the wire, unconditionally: a
    handler that returns its own ``Content-Security-Policy`` header has
    it dropped, because contained code choosing its own containment is
    not a policy an embedder configured.

    It lives here rather than only on ``build_router`` because a policy
    declared in one place and verified against another is the divergence
    this config exists to prevent. test_app reproduces the ORIGIN rules
    by intercepting requests, but a CSP also governs BEHAVIOUR — `eval`,
    `new Function`, blob workers, blob module scripts — and none of that
    involves a request to intercept. Sending the real header is the only
    way verification sees those.

    ``build_router(csp=...)`` still wins where an embedder passes it,
    for compatibility; prefer setting it here so both halves agree.

    Declared last: see ``frontend_notes``."""


class AppRuntime:
    """Dispatch for one workspace's ``<root>/app``. Build once, reuse."""

    def __init__(
        self,
        ws: Workspace,
        config: AppsConfig | None = None,
        *,
        frozen: bool = False,
        log_sink: "Callable[[str], None] | None" = None,
    ) -> None:
        """``frozen=True`` (live serving of a published snapshot): every
        verb runs read-only — no mutation, so requests are concurrent
        and need no lock. ``log_sink`` routes handler stdout/errors off
        the (read-only) VFS; default is the VFS log at
        ``<root>/app/logs/api.log`` for the authoring loop.

        Handler executions are ``exec_python(view=...)`` calls: the
        executor gives each call a restricted sandbox of its own (policy
        memoized, worker pooled under process/kernel isolation, so it's
        cheap) and realizes the read-only view / budget / contract
        classes its own way. This runtime holds no sandbox objects —
        nothing to build here, nothing to reap in ``close``."""
        self._ws = ws
        self._config = config or AppsConfig()
        self._frozen = frozen
        self._log_sink = log_sink
        self._log_broken = False  # warn once when logging fails
        self._log_started = False  # header written on first log write
        self._pending: list[str] = []  # request lines awaiting a free flush
        self._verb_notes: dict[str, int] = {}  # module -> source hash noted
        self._shadow_notes: set[str] = set()  # asset collisions noted
        self._assets = _build_assets(self._config.static_assets)
        self._contract = (Request, Response, HttpError)
        # Path layout, derived once from the workspace root.
        self._app_root = app_root(ws)
        self._api_root = f"{self._app_root}/api"
        self._log_path = f"{self._app_root}/logs/api.log"

    @property
    def config(self) -> AppsConfig:
        """The runtime's config — adapters read ``script_hosts`` /
        ``apps_primer`` from here to build tool descriptions."""
        return self._config

    def close(self) -> None:
        """No-op, retained for API stability (embedders call it): the
        runtime no longer holds long-lived sandbox workers — each
        handler call mints and reaps its own via ``exec_python(view=)``.
        """

    # -- the core --------------------------------------------------------

    def dispatch(self, request: Request) -> WireResponse:
        if self._frozen:
            # Frozen serving: read-only VFS, no workspace lock — the
            # executor makes concurrency safe its own way (LocalExecutor:
            # a fresh per-request sandbox, genuinely parallel;
            # DudExecutor: one guest channel, internally serialized).
            return self._dispatch(request)
        # Mutable (authoring) dispatch is a mutating workspace call and
        # serializes like one, under the workspace's own single-writer
        # lock: with ordinary tool calls, with test_app's concurrent
        # route callbacks, and with screenshot writes. RLock — the curl
        # builtin dispatches from inside a locked terminal() call.
        with self._ws.lock:
            return self._dispatch(request)

    def _dispatch(self, request: Request) -> WireResponse:
        api = request.path.startswith("/api/")
        asset = False
        try:
            if api:
                resp = self._dispatch_api(request)
            else:
                resp, asset = self._dispatch_static(request)
        except HttpError as e:
            resp = _error_response(e.status, e.message)
        # Declared assets skip the cap. It exists to catch a handler
        # returning something runaway; an asset's size is a decision the
        # embedder already made, and a vendored charting bundle clears
        # the 2MB default on its own.
        cap = self._config.max_response_bytes
        if not asset and len(resp.content) > cap:
            resp = _error_response(500, "response too large")
        # Only /api requests: static assets are high-volume and
        # low-signal, and would bury the tracebacks the log exists for.
        if api:
            self._pending.append(self._request_line(request, resp.status))
            self._flush_if_free()
        return resp

    # -- api -------------------------------------------------------------

    def _dispatch_api(self, request: Request) -> WireResponse:
        name = request.path[len("/api/") :].strip("/")
        if not name or "/" in name or name.startswith("_"):
            raise HttpError(404, f"no such endpoint: {request.path}")
        handler_path = f"{self._api_root}/{name}.py"
        fs = self._ws.fs
        if not fs.exists(handler_path):
            # agents mirror the FILENAME into the url
            # (fetch('api/explorer.py')) and then debug the backend for
            # an hour — label the door
            if name.endswith(".py"):
                bare = name[:-3]
                if bare and fs.exists(f"{self._api_root}/{bare}.py"):
                    raise HttpError(
                        404,
                        f"no such endpoint: {request.path} — endpoints are"
                        f" module names WITHOUT .py: try /api/{bare}",
                    )
                raise HttpError(
                    404,
                    f"no such endpoint: {request.path} — endpoints are"
                    " module names WITHOUT the .py extension",
                )
            raise HttpError(404, f"no such endpoint: {request.path}")

        verb = request.method.lower()
        if verb not in _VERBS:
            raise HttpError(405, f"unsupported method: {request.method}")
        source = fs.read(handler_path).decode("utf-8")
        self._note_nonverb_functions(name, source)
        # Cheap verb check before spending a sandbox execution.
        if not re.search(rf"^[ \t]*def[ \t]+{verb}[ \t]*\(", source, re.M):
            raise HttpError(405, f"{request.method} not supported by {name}")

        # Frozen serving: every verb is read-only (no mutation, so
        # requests are concurrent). Authoring: GET is read-only, mutating
        # verbs stage writes with per-request atomicity.
        readonly = self._frozen or verb == "get"
        ws = self._ws
        atomic = not readonly and ws.caps.staging and not ws.dirty

        # The view declares the intent; the executor realizes it (a
        # restricted sandbox held exclusively for this call — so a
        # read-only GET can't mutate, contract classes are in scope,
        # and the per-request budget applies, whichever executor runs
        # it). No sandbox object crosses back here.
        view = ViewSpec(
            readonly_fs=readonly,
            readonly_cache=readonly,
            timeout=self._config.request_timeout,
            tick_limit=self._config.request_tick_limit,
            extra_classes=self._contract,
        )
        result = ws.exec_python(
            source + _TRAILER.format(verb=verb),
            inputs={"nt__req": request},
            view=view,
            # handlers are scripts: a stray module-level bare
            # expression must not echo reprs into api.log
            echo="none",
        )

        # The query string in the tag is what lets an agent correlate
        # log entries with requests — identical bare error lines read
        # as "stale log" and send the repair loop chasing phantoms.
        qs = _query_string(request)
        where = f"{name}:{verb}" + (f" ?{qs}" if qs else "")
        if result.stdout:
            self._log(f"[{where}] stdout:\n{result.stdout}")
        if result.error is not None:
            if atomic:
                ws.discard()
            from ..hints import error_hint

            hint = error_hint(result.error)
            suffix = f"\n[hint: {hint}]" if hint else ""
            self._log(f"[{where}] ERROR:\n{result.error}{suffix}")
            return _error_response(500, "internal error", log=self._log_path)

        http = result.namespace.get("nt__http")
        if http is not None:
            status, message = http
            return _error_response(int(status), str(message))

        try:
            return normalize(result.namespace.get("nt__resp"))
        except TypeError as e:
            if atomic:
                ws.discard()
            self._log(f"[{where}] BAD RETURN: {e}")
            return _error_response(500, str(e))

    def test_app(
        self,
        actions: list[dict[str, Any]] | None = None,
        *,
        viewport: str | dict[str, int] = "desktop",
        **kwargs: Any,
    ) -> Any:
        """Headless verification via Playwright (see testapp.py).
        Requires the [apps] extra + `playwright install chromium`."""
        from .testapp import run_test_app

        return run_test_app(self, actions, viewport=viewport, **kwargs)

    # -- static ------------------------------------------------------------

    def _dispatch_static(self, request: Request) -> tuple[WireResponse, bool]:
        """Serve a static path. The flag says the bytes came from a
        declared ``static_assets`` directory, which exempts them from
        ``max_response_bytes`` (see :meth:`_dispatch`)."""
        if request.method.upper() != "GET":
            raise HttpError(405, "static paths are GET-only")
        # Normalize `.`/`..` and confine to the app root FIRST, before
        # anything reads the path. Without this, traversal segments
        # escape: `/../secret.md` reads any workspace file and
        # `/./api/h.py` serves backend source (defeating the /api/ split
        # and the _-prefix non-routable rule). normpath collapses the
        # segments; the path must then sit strictly under /app/ (so
        # `/app` itself and a sibling like `/apple` are both rejected).
        #
        # Asset matching happens on the CANONICAL path for the same
        # reason it happens at all: `/x/../vendor/lib.js` must not miss
        # the prefix and fall through to a workspace file, which would
        # quietly break the asset-over-workspace precedence for any
        # caller that preserves dot segments.
        path = posixpath.normpath(
            f"{self._app_root}/{request.path.strip('/') or 'index.html'}"
        )
        if not path.startswith(self._app_root + "/"):
            raise HttpError(404, f"not found: {request.path}")
        rel = path[len(self._app_root) + 1 :]
        asset = self._asset_response(rel, request)
        if asset is not None:
            return asset, True
        # Backend is never served as static. The /api/ URL prefix routes
        # to handlers, but a static request that normalizes INTO api/
        # (e.g. `/./api/h.py`, `/x/../api/_shared.py`) would otherwise
        # serve raw handler source — the frontend/backend boundary.
        if path == self._api_root or path.startswith(self._api_root + "/"):
            raise HttpError(404, f"not found: {request.path}")
        fs = self._ws.fs
        if not fs.exists(path) or not fs.isfile(path):
            raise HttpError(404, f"not found: {request.path}")
        return WireResponse(200, fs.read(path), _content_type(path)), False

    def _asset_response(self, rel: str, request: Request) -> WireResponse | None:
        """Serve ``rel`` from a declared asset directory, or ``None`` if
        no prefix claims it. Assets take precedence over a workspace file
        at the same path — predictable, and it stops an agent shadowing
        the design system by accident — but silent shadowing is its own
        failure mode, so the collision is noted in api.log."""
        for prefix, fs in self._assets.items():
            if rel != prefix and not rel.startswith(prefix + "/"):
                continue
            # `rel` is already canonical (see _dispatch_static), so this
            # cannot contain dot segments — but the guard is cheap and
            # this method must not depend on its caller for confinement.
            inner = posixpath.normpath(rel[len(prefix) :].lstrip("/") or ".")
            if inner in (".", "") or inner.startswith(".."):
                raise HttpError(404, f"not found: {request.path}")
            if not fs.exists(inner) or not fs.isfile(inner):
                raise HttpError(404, f"not found: {request.path}")
            self._note_shadowed_asset(rel)
            return WireResponse(200, fs.read(inner), _content_type(inner))
        return None

    def _note_shadowed_asset(self, rel: str) -> None:
        """An agent that writes app/vendor/x.js and then cannot see its
        change would debug the app; say what happened instead. Once per
        path — a page reloads its scripts on every run.

        BUFFERED, not written: this fires on a static GET, and serving a
        page is the read-only request that most often precedes a POST.
        Writing here would dirty a clean workspace, and ``_dispatch_api``
        gates per-request atomicity on ``not ws.dirty`` — so the note
        would silently cost the next mutating handler its rollback. Same
        reasoning as the request-line buffer; see ``_flush_if_free``."""
        if rel in self._shadow_notes:
            return
        path = posixpath.normpath(f"{self._app_root}/{rel}")
        if not self._ws.fs.exists(path):
            return
        self._shadow_notes.add(rel)
        self._pending.append(
            f"[assets] note: {path} is shadowed — {rel!r} is served from a "
            "read-only asset directory supplied by the host, so your file is "
            "NOT being served. Use a different path."
        )
        self._flush_if_free()

    _TOP_DEF_RE = re.compile(r"^def[ \t]+([A-Za-z]\w*)[ \t]*\(", re.M)

    def _note_nonverb_functions(self, name: str, source: str) -> None:
        """Agents write RPC-style handlers (``def query(req)``) that
        dispatch never routes — silently dead endpoints they then debug
        from the frontend. Note it in api.log, once per module version
        (the log is the documented repair loop)."""
        marker = hash(source)
        if self._verb_notes.get(name) == marker:
            return
        self._verb_notes[name] = marker
        stray = [
            fn
            for fn in self._TOP_DEF_RE.findall(source)
            if fn not in _VERBS and not fn.startswith("_")
        ]
        if stray:
            listing = ", ".join(f"{fn}()" for fn in dict.fromkeys(stray))
            self._log(
                f"[{name}] note: {listing} defined but not an HTTP verb — "
                f"requests only ever call {'/'.join(sorted(_VERBS))}; an "
                "endpoint action must live inside a verb function (or its "
                "own api file)"
            )

    # -- logging -------------------------------------------------------------

    def _request_line(self, request: Request, status: int) -> str:
        """One line per /api request, whatever happened. Errors already
        write a tagged traceback just above this line; recording the
        SUCCESSES is what makes an empty log mean "no request arrived"
        instead of "logging is broken"."""
        qs = _query_string(request)
        path = request.path + (f"?{qs}" if qs else "")
        return f"{request.method.upper()} {path} -> {status}"

    def flush_log(self) -> None:
        """Write any buffered request lines to the log. Callers flush at
        a point where dirtying the workspace is harmless — ``test_app``
        does so when a run ends, which is where an agent looks next."""
        with self._ws.lock:
            self._flush()

    def _flush_if_free(self) -> None:
        """Flush only when writing costs nothing that matters.

        A read-only request that found a clean workspace must LEAVE it
        clean. ``_dispatch_api`` gates per-request atomicity on
        ``not ws.dirty``, so a diagnostic write here would silently
        disable handler rollback for the next mutating request — and
        the page-GET-then-POST order makes that the common flow, not a
        corner case. The runtime cannot simply claim the dirt as its
        own and discard anyway: ``discard()`` is all-or-nothing at the
        provider level and the protocol exposes only a boolean, so
        "my log line" is indistinguishable from a screenshot written
        mid-run — which rollback would then destroy. So we buffer
        instead, and flush when the workspace is dirty regardless (the
        line is free), when there is no staging to protect, or when a
        sink routes the log off the VFS entirely.
        """
        ws = self._ws
        if self._log_sink is not None or not ws.caps.staging or ws.dirty:
            self._flush()

    def _flush(self) -> None:
        pending, self._pending = self._pending, []
        for line in pending:
            self._write_log(line)

    def _log(self, message: str) -> None:
        """Write a diagnostic. Buffered request lines go out first, so
        the log stays in request order and a traceback always sits
        beneath the request that produced it."""
        self._flush()
        self._write_log(message)

    def _write_log(self, message: str) -> None:
        try:
            if self._log_sink is not None:
                # frozen serving: VFS is read-only, so route off it
                self._log_sink(message.rstrip())
                return
            fs = self._ws.fs
            fs.makedirs(f"{self._app_root}/logs", exist_ok=True)
            if not self._log_started:
                # Header on creation, not at enable_apps: pre-creating
                # would materialize <root>/app before the agent has
                # built anything, and "does an app exist yet?" is a
                # question embedders answer with isdir(<root>/app).
                self._log_started = True
                if not fs.exists(self._log_path):
                    fs.write(self._log_path, _LOG_HEADER.encode())
            stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
            fs.write(
                self._log_path, f"[{stamp}] {message.rstrip()}\n".encode(), mode="a"
            )
        except Exception as e:
            # Logging must never break dispatch — but going silently
            # blind is worse: the agent's documented repair loop is
            # tailing this log. Warn the host once per runtime so a
            # broken/full fs (or a raising log_sink) is visible.
            if not self._log_broken:
                self._log_broken = True
                import warnings

                warnings.warn(
                    f"apps: handler log write failed ({e!r}); further "
                    "handler diagnostics from this runtime will be dropped",
                    RuntimeWarning,
                    stacklevel=2,
                )


def enable_apps(ws: Workspace, config: AppsConfig | None = None) -> AppRuntime:
    """Wire the apps runtime into a workspace: builds the AppRuntime
    and registers the ``curl`` terminal builtin. Returns the runtime
    (also the live router's dispatch source)."""
    runtime = AppRuntime(ws, config)
    from .curl import make_curl_command

    ws.register_command("curl", make_curl_command(runtime))
    return runtime


def request(method: str, url: str, **kwargs: Any) -> Request:
    """Convenience re-export of :func:`make_request`."""
    return make_request(method, url, **kwargs)
