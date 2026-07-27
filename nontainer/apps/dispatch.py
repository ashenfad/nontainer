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
from collections.abc import Callable
from dataclasses import dataclass
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
}

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
    """Embedder guidance appended to the apps notes in the tool
    description — e.g. a private component lib's known-good import
    block, available endpoints, house frontend conventions."""


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
        executor mints a fresh, restricted sandbox per call (policy
        memoized, so it's cheap) and realizes the read-only view /
        budget / contract classes its own way. This runtime holds no
        sandbox objects — nothing to build here, nothing to reap in
        ``close``."""
        self._ws = ws
        self._config = config or AppsConfig()
        self._frozen = frozen
        self._log_sink = log_sink
        self._log_broken = False  # warn once when logging fails
        self._log_started = False  # header written on first log write
        self._pending: list[str] = []  # request lines awaiting a free flush
        self._verb_notes: dict[str, int] = {}  # module -> source hash noted
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
        try:
            if api:
                resp = self._dispatch_api(request)
            else:
                resp = self._dispatch_static(request)
        except HttpError as e:
            resp = _error_response(e.status, e.message)
        cap = self._config.max_response_bytes
        if len(resp.content) > cap:
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
        # fresh restricted sandbox per call, policy memoized — so a
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

    def _dispatch_static(self, request: Request) -> WireResponse:
        if request.method.upper() != "GET":
            raise HttpError(405, "static paths are GET-only")
        rel = request.path.strip("/") or "index.html"
        # Normalize `.`/`..` and confine to the app root. Without this,
        # traversal segments escape: `/../secret.md` reads any workspace
        # file and `/./api/h.py` serves backend source (defeating the
        # /api/ split and the _-prefix non-routable rule). normpath
        # collapses the segments; the path must then sit strictly under
        # /app/ (so `/app` itself and a sibling like `/apple` are both
        # rejected).
        path = posixpath.normpath(f"{self._app_root}/{rel}")
        if not path.startswith(self._app_root + "/"):
            raise HttpError(404, f"not found: {request.path}")
        # Backend is never served as static. The /api/ URL prefix routes
        # to handlers, but a static request that normalizes INTO api/
        # (e.g. `/./api/h.py`, `/x/../api/_shared.py`) would otherwise
        # serve raw handler source — the frontend/backend boundary.
        if path == self._api_root or path.startswith(self._api_root + "/"):
            raise HttpError(404, f"not found: {request.path}")
        fs = self._ws.fs
        if not fs.exists(path) or not fs.isfile(path):
            raise HttpError(404, f"not found: {request.path}")
        name = path.rsplit("/", 1)[-1]
        ext = "." + name.rsplit(".", 1)[-1] if "." in name else ""
        ctype = _STATIC_TYPES.get(ext, "application/octet-stream")
        return WireResponse(200, fs.read(path), ctype)

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
