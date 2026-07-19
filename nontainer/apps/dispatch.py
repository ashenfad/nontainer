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

Tracebacks and handler stdout land in ``/app/logs/api.log`` — the
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

APP_ROOT = "/app"
API_ROOT = f"{APP_ROOT}/api"
LOG_PATH = f"{APP_ROOT}/logs/api.log"

_VERBS = frozenset({"get", "post", "put", "delete", "patch"})


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
    """Dispatch for one workspace's ``/app``. Build once, reuse."""

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
        ``/app/logs/api.log`` for the authoring loop.

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
        self._verb_notes: dict[str, int] = {}  # module -> source hash noted
        self._contract = (Request, Response, HttpError)

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
        try:
            if request.path.startswith("/api/"):
                resp = self._dispatch_api(request)
            else:
                resp = self._dispatch_static(request)
        except HttpError as e:
            resp = _error_response(e.status, e.message)
        cap = self._config.max_response_bytes
        if len(resp.content) > cap:
            return _error_response(500, "response too large")
        return resp

    # -- api -------------------------------------------------------------

    def _dispatch_api(self, request: Request) -> WireResponse:
        name = request.path[len("/api/") :].strip("/")
        if not name or "/" in name or name.startswith("_"):
            raise HttpError(404, f"no such endpoint: {request.path}")
        handler_path = f"{API_ROOT}/{name}.py"
        fs = self._ws.fs
        if not fs.exists(handler_path):
            # agents mirror the FILENAME into the url
            # (fetch('api/explorer.py')) and then debug the backend for
            # an hour — label the door
            if name.endswith(".py"):
                bare = name[:-3]
                if bare and fs.exists(f"{API_ROOT}/{bare}.py"):
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
        from urllib.parse import urlencode

        qs = urlencode(request.params) if request.params else ""
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
            return _error_response(500, "internal error", log="/app/logs/api.log")

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
        # Normalize `.`/`..` and confine to APP_ROOT. Without this,
        # traversal segments escape: `/../secret.md` reads any workspace
        # file and `/./api/h.py` serves backend source (defeating the
        # /api/ split and the _-prefix non-routable rule). normpath
        # collapses the segments; the path must then sit strictly under
        # /app/ (so `/app` itself and a sibling like `/apple` are both
        # rejected).
        path = posixpath.normpath(f"{APP_ROOT}/{rel}")
        if not path.startswith(APP_ROOT + "/"):
            raise HttpError(404, f"not found: {request.path}")
        # Backend is never served as static. The /api/ URL prefix routes
        # to handlers, but a static request that normalizes INTO api/
        # (e.g. `/./api/h.py`, `/x/../api/_shared.py`) would otherwise
        # serve raw handler source — the frontend/backend boundary.
        if path == API_ROOT or path.startswith(API_ROOT + "/"):
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

    def _log(self, message: str) -> None:
        try:
            if self._log_sink is not None:
                # frozen serving: VFS is read-only, so route off it
                self._log_sink(message.rstrip())
                return
            fs = self._ws.fs
            fs.makedirs(f"{APP_ROOT}/logs", exist_ok=True)
            stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
            fs.write(LOG_PATH, f"[{stamp}] {message.rstrip()}\n".encode(), mode="a")
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
