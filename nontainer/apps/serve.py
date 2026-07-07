"""Live serving of a FROZEN app snapshot — a mountable ASGI router.

Embedder interface (all of it)::

    from nontainer.apps import build_router, mint_token

    router = build_router(resolve)        # (token) -> read-only Workspace | None
    app.mount("/apps", router)            # FastAPI or Starlette alike

The router serves a **published, read-only snapshot**: ``resolve`` returns
a Workspace pinned to a commit (the shared artifact), and the frontend
plus handlers are served against it. Handlers may READ the workspace and
call injected ``host_objects`` (e.g. a read-only telemetry client), but
they cannot mutate the VFS — a write attempt is a 500. Mutable app state
belongs in an external store reached through ``host_objects``, not the
served VFS.

Because nothing mutates, serving is simple: requests to one snapshot run
**concurrently** (each on a fresh read-only sandbox — no per-session lock,
no staged buffer, no checkpointing), snapshots are a benign LRU cache
(lossless to evict), and there is no durability surface. ``{token}`` is a
capability — mint with :func:`mint_token`, map to snapshots in YOUR
storage.

Threat framing (docs/apps.md): anonymous HTTP triggers agent-authored
code under your sandbox policy. The default posture keeps it boring: no
network unless the workspace's PythonConfig granted it, read-only VFS,
per-request budgets, a per-token rate limit, and a strict-ish default CSP
on served HTML. Handler stdout/errors go to ``on_log`` (default: the
``nontainer.apps`` logger) since the VFS is read-only.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from collections import OrderedDict, deque
from typing import Any, Callable

from ..workspace import Workspace
from .contract import filter_headers, make_request
from .dispatch import AppRuntime, AppsConfig

_logger = logging.getLogger("nontainer.apps")

_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://esm.sh https://unpkg.com "
    "https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline'; "
    "connect-src 'self' https://esm.sh https://unpkg.com "
    "https://cdn.jsdelivr.net; "
    "img-src 'self' data:"
)


def mint_token(nbytes: int = 32) -> str:
    """A capability-grade token (~43 url-safe chars for the default).
    Distinct from session ids by design — session ids may be guessable;
    tokens must not be. The token→snapshot map is the embedder's."""
    return secrets.token_urlsafe(nbytes)


class _RateLimit:
    """Sliding-window per-token rate limit. Frozen serving needs no
    per-session lock (read-only + fresh sandbox per request → safe
    concurrency), only overload protection."""

    def __init__(self, per_min: int) -> None:
        self._per_min = per_min
        self._stamps: deque[float] = deque()
        self._lock = threading.Lock()

    def allow(self) -> bool:
        now = time.monotonic()
        with self._lock:
            while self._stamps and now - self._stamps[0] > 60.0:
                self._stamps.popleft()
            if len(self._stamps) >= self._per_min:
                return False
            self._stamps.append(now)
            return True


class _Snapshot:
    """A cached read-only snapshot. Reference-counted so eviction never
    closes a workspace mid-request: eviction marks it closed, and the
    last in-flight request to finish does the actual close."""

    def __init__(self, ws: Workspace, runtime: AppRuntime, rate: _RateLimit) -> None:
        self.ws = ws
        self.runtime = runtime
        self.rate = rate
        self._active = 0
        self._evicted = False
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            self._active += 1

    def release(self) -> None:
        with self._lock:
            self._active -= 1
            close = self._evicted and self._active == 0
        if close:
            self._close()

    def evict(self) -> None:
        """Called under the cache lock; closes now iff idle, else defers
        the close to the last in-flight request."""
        with self._lock:
            self._evicted = True
            close = self._active == 0
        if close:
            self._close()

    def _close(self) -> None:
        try:
            self.ws.close()
        except Exception:
            pass


def build_router(
    resolve: Callable[[str], Workspace | None],
    *,
    config: AppsConfig | None = None,
    rate_limit_per_min: int = 120,
    max_snapshots: int = 64,
    csp: str | None = _CSP,
    on_log: Callable[[str], None] | None = None,
) -> Any:
    """Build the mountable ASGI router serving frozen snapshots.

    ``resolve(token)`` returns a read-only ``Workspace`` pinned to the
    published commit (or ``None`` → 404). Requests are served
    concurrently; ``on_log`` receives handler stdout/errors (default: the
    ``nontainer.apps`` logger).
    """
    try:
        from starlette.responses import Response as HttpResponse
        from starlette.routing import Route, Router
    except ImportError as e:
        raise ImportError(
            "build_router requires starlette: pip install nontainer[apps]"
        ) from e

    import anyio

    cfg = config or AppsConfig()
    log_sink = on_log or (lambda m: _logger.warning("app: %s", m))
    snapshots: OrderedDict[str, _Snapshot] = OrderedDict()
    snap_lock = threading.Lock()

    def _snapshot_for(token: str) -> _Snapshot | None:
        """Return a snapshot with an acquired reference (caller must
        release). None if the token is unknown."""
        with snap_lock:
            snap = snapshots.get(token)
            if snap is not None:
                snapshots.move_to_end(token)
                snap.acquire()
                return snap
        ws = resolve(token)
        if ws is None:
            return None
        fresh = _Snapshot(
            ws,
            AppRuntime(ws, cfg, frozen=True, log_sink=log_sink),
            _RateLimit(rate_limit_per_min),
        )
        with snap_lock:
            snap = snapshots.setdefault(token, fresh)  # lost race → existing
            if snap is not fresh:
                fresh._close()  # our resolved ws lost the race — don't leak it
            snapshots.move_to_end(token)
            snap.acquire()
            # Benign cache: read-only snapshots are lossless to drop; evict
            # (deferred close) to release store handles once idle.
            while len(snapshots) > max_snapshots:
                _, evicted = snapshots.popitem(last=False)
                evicted.evict()
        return snap

    def _handle_sync(
        snap: _Snapshot, method: str, url: str, body: bytes, headers: dict
    ) -> Any:
        # No lock: frozen dispatch builds a fresh read-only sandbox per
        # request, so concurrent requests to one snapshot are safe.
        return snap.runtime.dispatch(
            make_request(method, url, body=body, headers=headers)
        )

    async def endpoint(request: Any) -> Any:
        token = request.path_params["token"]
        path = "/" + request.path_params.get("path", "")
        snap = _snapshot_for(token)  # acquires a reference (release below)
        if snap is None:
            return HttpResponse("unknown token", status_code=404)
        try:
            if not snap.rate.allow():
                return HttpResponse("rate limit exceeded", status_code=429)
            body = await request.body()
            url = path + (f"?{request.url.query}" if request.url.query else "")
            wire = await anyio.to_thread.run_sync(
                _handle_sync,
                snap,
                request.method,
                url,
                body,
                filter_headers(request.headers),
            )
        finally:
            snap.release()
        headers = dict(wire.headers)
        if csp and wire.content_type.startswith("text/html"):
            headers.setdefault("content-security-policy", csp)
        return HttpResponse(
            wire.content,
            status_code=wire.status,
            media_type=wire.content_type,
            headers=headers,
        )

    methods = ["GET", "POST", "PUT", "DELETE", "PATCH"]
    return Router(
        routes=[
            Route("/{token}", endpoint, methods=methods),
            Route("/{token}/{path:path}", endpoint, methods=methods),
        ]
    )
