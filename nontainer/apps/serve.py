"""M3: live serving — a mountable router over the same dispatch.

Embedder interface (all of it)::

    from nontainer.apps import build_router, mint_token, AppsConfig

    router = build_router(resolve)        # (token) -> Workspace | None
    app.mount("/apps", router)            # FastAPI or Starlette alike

The router is an ASGI app (Starlette ``Router``) — nontainer never
owns a port or a process. ``{token}`` is a capability: mint with
:func:`mint_token`, map to sessions in YOUR storage; the router calls
``resolve`` once per unseen token and caches the workspace + its
AppRuntime (build once, reuse — handler sandboxes are not per-request
objects).

Threat framing (docs/apps.md): enabling this means anonymous HTTP can
trigger agent-authored code under your sandbox policy. The default
posture keeps that boring: no network in handlers unless the
workspace's PythonConfig granted it, workspace-only fs, per-request
budgets, per-token serialization + bounded queue (429 overflow) +
rate limit, and a strict-ish default CSP on served HTML (inline
scripts allowed — agents write them — plus the CDN allowlist).

Checkpointing: requests never mint commits. The router checkpoints
lazily on quiesce — before handling a request, if the provider is
dirty and ``quiesce_seconds`` have passed since the last mutating
request, it commits with ``info={"source": "api"}``.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections import deque
from typing import Any, Callable

from ..workspace import Workspace
from .contract import filter_headers, make_request
from .dispatch import AppRuntime, AppsConfig

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
    tokens must not be. The token→session map is the embedder's."""
    return secrets.token_urlsafe(nbytes)


class _Gate:
    """Per-token serialization + bounded queue + sliding-window rate
    limit. The lock serializes ROUTER traffic; if the same workspace is
    also driven by agent tools concurrently, share discipline above."""

    def __init__(self, queue_depth: int, rate_per_min: int) -> None:
        self.lock = threading.Lock()
        self._pending = 0
        self._pending_lock = threading.Lock()
        self._queue_depth = queue_depth
        self._rate_per_min = rate_per_min
        self._stamps: deque[float] = deque()
        self.last_mutation = 0.0

    def try_enter(self) -> str | None:
        """Returns a refusal reason or None (admitted)."""
        now = time.monotonic()
        with self._pending_lock:
            while self._stamps and now - self._stamps[0] > 60.0:
                self._stamps.popleft()
            if len(self._stamps) >= self._rate_per_min:
                return "rate limit exceeded"
            if self._pending >= self._queue_depth:
                return "queue full"
            self._stamps.append(now)
            self._pending += 1
        return None

    def leave(self) -> None:
        with self._pending_lock:
            self._pending -= 1


class _Entry:
    def __init__(self, ws: Workspace, runtime: AppRuntime, gate: _Gate) -> None:
        self.ws = ws
        self.runtime = runtime
        self.gate = gate


def build_router(
    resolve: Callable[[str], Workspace | None],
    *,
    config: AppsConfig | None = None,
    queue_depth: int = 8,
    rate_limit_per_min: int = 120,
    quiesce_seconds: float = 5.0,
    max_sessions: int = 64,
    csp: str | None = _CSP,
) -> Any:
    """Build the mountable ASGI router. See module docstring."""
    try:
        from starlette.responses import Response as HttpResponse
        from starlette.routing import Route, Router
    except ImportError as e:
        raise ImportError(
            "build_router requires starlette: pip install nontainer[apps]"
        ) from e

    import anyio

    from collections import OrderedDict

    cfg = config or AppsConfig()
    entries: OrderedDict[str, _Entry] = OrderedDict()
    entries_lock = threading.Lock()

    def _evict_lru() -> None:
        """Bounded cache (PR#1 review): workspaces hold real resources
        (db handles, AgentFS loop threads). Evict least-recently-used
        idle entries; busy ones (lock held) are skipped this round."""
        for token in list(entries):
            if len(entries) < max_sessions:
                return
            candidate = entries[token]
            if candidate.gate.lock.locked():
                continue
            del entries[token]
            try:
                candidate.ws.close()
            except Exception:
                pass

    def _entry_for(token: str) -> _Entry | None:
        with entries_lock:
            entry = entries.get(token)
            if entry is not None:
                entries.move_to_end(token)
                return entry
        ws = resolve(token)
        if ws is None:
            return None
        new = _Entry(ws, AppRuntime(ws, cfg), _Gate(queue_depth, rate_limit_per_min))
        with entries_lock:
            entry = entries.setdefault(token, new)  # lost race → existing
            entries.move_to_end(token)
            if len(entries) > max_sessions:
                _evict_lru()
        return entry

    def _handle_sync(
        entry: _Entry, method: str, url: str, body: bytes, headers: dict
    ) -> Any:
        with entry.gate.lock:
            # Lazy quiesce checkpoint (requests never mint commits).
            provider = entry.ws._provider
            if (
                provider.caps.versioned
                and provider.dirty
                and entry.gate.last_mutation
                and time.monotonic() - entry.gate.last_mutation > quiesce_seconds
            ):
                try:
                    provider.checkpoint(info={"source": "api"})
                except Exception:
                    pass
            wire = entry.runtime.dispatch(
                make_request(method, url, body=body, headers=headers)
            )
            if method.upper() != "GET":
                entry.gate.last_mutation = time.monotonic()
            return wire

    async def endpoint(request: Any) -> Any:
        token = request.path_params["token"]
        path = "/" + request.path_params.get("path", "")
        entry = _entry_for(token)
        if entry is None:
            return HttpResponse("unknown token", status_code=404)

        refusal = entry.gate.try_enter()
        if refusal is not None:
            return HttpResponse(refusal, status_code=429)
        try:
            body = await request.body()
            url = path + (
                f"?{request.url.query}" if request.url.query else ""
            )
            wire = await anyio.to_thread.run_sync(
                _handle_sync,
                entry,
                request.method,
                url,
                body,
                filter_headers(request.headers),
            )
        finally:
            entry.gate.leave()

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
