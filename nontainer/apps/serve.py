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
belongs in an external store reached through ``host_objects`` (a
sqlite/postgres client), not the served VFS.

Because a frozen snapshot is immutable, serving is **stateless**: each
request calls ``resolve`` and dispatches on a read-only sandbox of its
own — concurrent, no per-session lock, no session cache, no lifecycle to
manage. Under process/kernel isolation that sandbox is checked out of
the executor's worker pool rather than forked per request (a fork from a
live, multi-threaded server is its own hazard); the exclusivity that
makes concurrent dispatch safe is the checkout, not the fork. ``resolve`` is called per request and its result is NOT closed
by the router; if it is expensive, cache the read-only Workspace inside
``resolve`` (safe — it's immutable). Rate limiting and quotas are edge
concerns; put them at your gateway.

Threat framing (docs/apps.md): anonymous HTTP triggers agent-authored
code under your sandbox policy. The default posture keeps it boring:
read-only VFS, no network unless the workspace's PythonConfig granted
it, per-request budgets, and a strict-ish default CSP on served HTML.
Handler stdout/errors go to ``on_log`` (default: the ``nontainer.apps``
logger) since the VFS is read-only.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any, Callable

from ..workspace import Workspace
from .contract import filter_headers, make_request
from .dispatch import AppRuntime, AppsConfig

_logger = logging.getLogger("nontainer.apps")


# The CSP's job here is SCRIPT supply-chain pinning: executable code
# only from ``AppsConfig.script_hosts`` — the same declaration
# test_app's interception enforces, so apps can't verify green and
# break published. Images/fetches/styles/fonts are open to any https
# host: data apps legitimately need map tiles, remote imagery, and
# third-party APIs, and there is no proxy path inside the walls
# (handlers have no network). The cost is reopening beacon-style
# exfiltration channels — embedders serving untrusted audiences
# should tighten via build_router(csp=...).
def build_csp(script_hosts: tuple[str, ...]) -> str:
    """The default served-HTML Content-Security-Policy for a given
    script-host allowlist.

    ``'wasm-unsafe-eval'`` is present because browsers gate WebAssembly
    compilation on ``script-src``: without it, a vendored library with a
    wasm core (duckdb-wasm, sql.js, pyodide) verifies green under
    test_app — which enforces the allowlist by intercepting requests, not
    by sending this header — and then dies only once published. That is
    the verify-green/publish-broken split this whole declaration exists
    to close. It permits wasm compilation ONLY; it does not enable
    ``eval``, and it is far narrower than the ``'unsafe-inline'`` already
    on this line."""
    hosts = " ".join(f"https://{h}" for h in script_hosts)
    return (
        "default-src 'self'; "
        f"script-src 'self' 'unsafe-inline' 'wasm-unsafe-eval' {hosts}; "
        "style-src 'self' 'unsafe-inline' https:; "
        "connect-src 'self' https:; "
        "font-src 'self' https: data:; "
        "img-src 'self' https: data:"
    )


def mint_token(nbytes: int = 32) -> str:
    """A capability-grade token (~43 url-safe chars for the default).
    Distinct from session ids by design — session ids may be guessable;
    tokens must not be. The token→snapshot map is the embedder's."""
    return secrets.token_urlsafe(nbytes)


def build_router(
    resolve: Callable[[str], Workspace | None],
    *,
    config: AppsConfig | None = None,
    csp: str | None = None,
    on_log: Callable[[str], None] | None = None,
) -> Any:
    """Build the mountable ASGI router serving frozen snapshots.

    ``resolve(token)`` returns a read-only ``Workspace`` pinned to the
    published commit (or ``None`` → 404) — called **per request**, and
    its result is not closed by the router (cache inside ``resolve`` if
    resolving is expensive). Requests are served concurrently; ``on_log``
    receives handler stdout/errors (default: the ``nontainer.apps``
    logger).

    ``csp``: the Content-Security-Policy set on served HTML. Default
    (``None``) derives from ``config.script_hosts`` via ``build_csp``;
    pass a full policy string to override, or ``""`` to disable.
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
    if csp is None:
        csp = build_csp(cfg.script_hosts)
    log_sink = on_log or (lambda m: _logger.warning("app: %s", m))

    def _handle_sync(
        ws: Workspace, method: str, url: str, body: bytes, headers: dict
    ) -> Any:
        # Frozen dispatch gives each request a read-only sandbox of its
        # own, so concurrent requests (even to one snapshot) are safe with
        # no lock. Cheap when `resolve` caches its Workspace: the policy is
        # memoized per workspace and, under process/kernel isolation, the
        # worker is resident — so a request costs neither registration nor
        # a fork.
        runtime = AppRuntime(ws, cfg, frozen=True, log_sink=log_sink)
        return runtime.dispatch(make_request(method, url, body=body, headers=headers))

    async def endpoint(request: Any) -> Any:
        token = request.path_params["token"]
        path = "/" + request.path_params.get("path", "")
        ws = resolve(token)
        if ws is None:
            return HttpResponse("unknown token", status_code=404)

        body = await request.body()
        url = path + (f"?{request.url.query}" if request.url.query else "")
        wire = await anyio.to_thread.run_sync(
            _handle_sync,
            ws,
            request.method,
            url,
            body,
            filter_headers(request.headers),
        )
        # wire.headers keys are lowercased by normalize(), so this
        # setdefault correctly defers to an agent-set CSP (any casing)
        # instead of emitting a duplicate header.
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
