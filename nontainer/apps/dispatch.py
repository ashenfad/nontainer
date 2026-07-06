"""AppRuntime: dispatch requests into agent-authored handlers.

One core function, three consumers (curl builtin → M1, test_app → M2,
live router → M3); see docs/apps.md. Handlers execute through the
workspace's ``_exec_python`` machinery with dedicated sandboxes:

- GET → a sandbox over ``ReadOnlyFS`` + a read-only cache view (a GET
  that writes raises — structural REST);
- mutating verbs → a normal sandbox; when the provider supports
  staging AND had no pending changes, a handler that raises gets its
  staged writes discarded (per-request atomicity). Requests never
  mint commits.

Tracebacks and handler stdout land in ``/app/logs/api.log`` — the
agent's repair loop is ``tail``, edit, retry.
"""

from __future__ import annotations

import posixpath
import re
import time
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from typing import Any, Iterator

from ..workspace import Workspace
from .contract import HttpError, Request, Response, WireResponse, make_request, normalize

APP_ROOT = "/app"
API_ROOT = f"{APP_ROOT}/api"
LOG_PATH = f"{APP_ROOT}/logs/api.log"

_VERBS = frozenset({"get", "post", "put", "delete", "patch"})

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


@dataclass(frozen=True)
class AppsConfig:
    request_timeout: float = 5.0
    request_tick_limit: int = 200_000
    max_response_bytes: int = 2_000_000


class _ReadOnlyCache(MutableMapping):
    """Read-only cache view for GET handlers (structural REST).

    Inherits MutableMapping so derived mutators (pop, clear, update,
    setdefault) route through __setitem__/__delitem__ and raise
    PermissionError instead of AttributeError (PR#1 review)."""

    def __init__(self, cache: Mapping) -> None:
        self._cache = cache

    def __getitem__(self, key: str) -> Any:
        return self._cache[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._cache)

    def __len__(self) -> int:
        return len(self._cache)

    def __contains__(self, key: object) -> bool:
        return key in self._cache

    def get(self, key: str, default: Any = None) -> Any:
        return self._cache.get(key, default)

    def __setitem__(self, key: str, value: Any) -> None:
        raise PermissionError("cache is read-only in GET handlers")

    def __delitem__(self, key: str) -> None:
        raise PermissionError("cache is read-only in GET handlers")


class AppRuntime:
    """Dispatch for one workspace's ``/app``. Build once, reuse."""

    def __init__(self, ws: Workspace, config: AppsConfig | None = None) -> None:
        self._ws = ws
        self._config = config or AppsConfig()
        contract = (Request, Response, HttpError)
        budgets = dict(
            timeout=self._config.request_timeout,
            tick_limit=self._config.request_tick_limit,
        )
        from monkeyfs import ReadOnlyFS

        self._rw_sandbox = ws._build_sandbox(extra_classes=contract, **budgets)
        self._ro_sandbox = ws._build_sandbox(
            extra_classes=contract,
            filesystem=ReadOnlyFS(ws.fs),
            **budgets,
        )

    # -- the core --------------------------------------------------------

    def dispatch(self, request: Request) -> WireResponse:
        try:
            if request.path.startswith("/api/"):
                resp = self._dispatch_api(request)
            else:
                resp = self._dispatch_static(request)
        except HttpError as e:
            resp = WireResponse(e.status, e.message.encode(), "text/plain")
        cap = self._config.max_response_bytes
        if len(resp.content) > cap:
            return WireResponse(500, b"response too large", "text/plain")
        return resp

    # -- api -------------------------------------------------------------

    def _dispatch_api(self, request: Request) -> WireResponse:
        name = request.path[len("/api/") :].strip("/")
        if not name or "/" in name or name.startswith("_"):
            raise HttpError(404, f"no such endpoint: {request.path}")
        handler_path = f"{API_ROOT}/{name}.py"
        fs = self._ws.fs
        if not fs.exists(handler_path):
            raise HttpError(404, f"no such endpoint: {request.path}")

        verb = request.method.lower()
        if verb not in _VERBS:
            raise HttpError(405, f"unsupported method: {request.method}")
        source = fs.read(handler_path).decode("utf-8")
        # Cheap verb check before spending a sandbox execution.
        if not re.search(rf"^[ \t]*def[ \t]+{verb}[ \t]*\(", source, re.M):
            raise HttpError(405, f"{request.method} not supported by {name}")

        readonly = verb == "get"
        provider = self._ws._provider
        atomic = (
            not readonly
            and provider.caps.staging
            and not provider.dirty
        )

        # GET: read-only cache view (when cache is enabled at all).
        from ..workspace import _UNSET

        cache_override: Any = _UNSET  # default behavior
        if readonly and self._ws.cache_enabled:
            cache_override = _ReadOnlyCache(self._ws.cache)

        result = self._ws._exec_python(
            source + _TRAILER.format(verb=verb),
            inputs={"nt__req": request},
            sandbox=self._ro_sandbox if readonly else self._rw_sandbox,
            cache_override=cache_override,
        )

        if result.stdout:
            self._log(f"[{name}:{verb}] stdout:\n{result.stdout}")
        if result.error is not None:
            if atomic:
                provider.discard()
            self._log(f"[{name}:{verb}] ERROR:\n{result.error}")
            return WireResponse(500, b"internal error (see /app/logs/api.log)",
                                "text/plain")

        http = result.namespace.get("nt__http")
        if http is not None:
            status, message = http
            return WireResponse(int(status), str(message).encode(), "text/plain")

        try:
            return normalize(result.namespace.get("nt__resp"))
        except TypeError as e:
            if atomic:
                provider.discard()
            self._log(f"[{name}:{verb}] BAD RETURN: {e}")
            return WireResponse(500, str(e).encode(), "text/plain")

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

    # -- logging -------------------------------------------------------------

    def _log(self, message: str) -> None:
        try:
            fs = self._ws.fs
            fs.makedirs(f"{APP_ROOT}/logs", exist_ok=True)
            stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
            fs.write(LOG_PATH, f"[{stamp}] {message.rstrip()}\n".encode(), mode="a")
        except Exception:
            pass  # logging must never break dispatch


def enable_apps(ws: Workspace, config: AppsConfig | None = None) -> AppRuntime:
    """Wire the apps runtime into a workspace: builds the AppRuntime
    and registers the ``curl`` terminal builtin. Returns the runtime
    (also the M3 router's dispatch source)."""
    runtime = AppRuntime(ws, config)
    from .curl import make_curl_command

    ws.register_command("curl", make_curl_command(runtime))
    return runtime


def request(method: str, url: str, **kwargs: Any) -> Request:
    """Convenience re-export of :func:`make_request`."""
    return make_request(method, url, **kwargs)
