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
from collections.abc import Mapping, Sequence
from typing import Any, Callable

from ..workspace import Workspace
from .contract import filter_headers, filter_response_headers, make_request
from .dispatch import AppRuntime, AppsConfig

_logger = logging.getLogger("nontainer.apps")


# The CSP's job here is SCRIPT supply-chain pinning: executable code
# only from ``AppsConfig.script_hosts`` — the same declaration
# test_app's interception enforces, so apps can't verify green and
# break published. Images/fetches/styles/fonts are open to any https
# host: data apps legitimately need map tiles, remote imagery, and
# third-party APIs, and there is no proxy path inside the walls
# (handlers have no network). The cost is reopening beacon-style
# exfiltration channels — an embedder serving untrusted audiences
# tightens by declaring a whole policy in ``AppsConfig.csp``, and
# loosens one directive with ``AppsConfig.csp_extend``.
#
# ``blob:`` is allowed on ``img-src`` and ``media-src`` and refused on
# ``script-src``, which is not an inconsistency. A blob URL names bytes
# the page already holds, in this document and on this origin: it
# reaches no other origin's data, and as an image or a video it is
# DISPLAYED, never executed — the same risk class as ``data:``, which
# these directives already allow. Charting libraries need it (plotly
# rasterizes by drawing a Blob-backed <img> onto a canvas, which is how
# "download as png" and ``Plotly.toImage()`` work), and a client-side
# audio/video preview built from a Blob needs the same on media. A
# blob-loaded SCRIPT or worker is the opposite case: it is CODE, and
# code from a blob is code from nowhere the allowlist can name, so
# ``script-src`` (and the absent ``worker-src``/``child-src``, which
# fall through to ``default-src 'self'``) keep refusing it.
def build_csp(
    script_hosts: tuple[str, ...],
    *,
    extend: Mapping[str, Sequence[str]] | None = None,
) -> str:
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
    on this line.

    ``extend`` (``AppsConfig.csp_extend``) is EXTEND-ONLY: each
    ``{directive: sources}`` entry appends those sources to the derived
    directive — de-duplicated, derived sources first, directive names
    matched case-insensitively — or adds the directive when the derived
    policy has none. It cannot remove a source or a directive; a policy
    that must be TIGHTER is declared whole in ``AppsConfig.csp``."""
    directives: list[tuple[str, list[str]]] = [
        ("default-src", ["'self'"]),
        (
            "script-src",
            [
                "'self'",
                "'unsafe-inline'",
                "'wasm-unsafe-eval'",
                *(f"https://{h}" for h in script_hosts),
            ],
        ),
        ("style-src", ["'self'", "'unsafe-inline'", "https:"]),
        ("connect-src", ["'self'", "https:"]),
        ("font-src", ["'self'", "https:", "data:"]),
        ("img-src", ["'self'", "https:", "data:", "blob:"]),
        ("media-src", ["'self'", "https:", "data:", "blob:"]),
    ]
    for raw_name, sources in (extend or {}).items():
        name = raw_name.lower()
        for existing, current in directives:
            if existing == name:
                current.extend(s for s in sources if s not in current)
                break
        else:
            directives.append((name, list(dict.fromkeys(sources))))
    return "; ".join(
        f"{name} {' '.join(sources)}" for name, sources in directives if sources
    )


def resolve_csp(config: "AppsConfig") -> str:
    """The policy this config means: ``csp`` verbatim when set, else one
    derived from ``script_hosts`` and extended by ``csp_extend``.

    One function, both halves — the router sends this on served HTML and
    ``test_app`` sends it during verification, so an app cannot pass
    under one policy and be served another."""
    declared = getattr(config, "csp", None)
    if declared is not None:
        return declared
    return build_csp(
        config.script_hosts, extend=getattr(config, "csp_extend", None) or None
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
    (``None``) takes ``config.csp``, which itself defaults to a policy
    derived from ``config.script_hosts`` via ``build_csp`` and extended
    per-directive by ``config.csp_extend``; pass a full policy string to
    override, or ``""`` to disable.

    Whatever it resolves to is what served HTML carries — a handler
    cannot substitute a policy of its own. Handler response headers are
    allowlisted the way request headers are (representation and caching
    metadata plus ``x-*``); an embedder needing more wraps this router
    in their own middleware.

    Prefer declaring it on the config: ``test_app`` enforces
    ``config.csp`` during verification, so a policy passed only here is
    one verification never sees.
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
        csp = resolve_csp(cfg)
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
        # The app gets representation and caching metadata; the
        # embedder keeps the headers that grant privileges on the
        # serving origin (cookies, cross-origin reads, framing).
        headers = filter_response_headers(wire.headers)
        # The served policy is the CONFIGURED policy. Contained code
        # does not get to choose its own containment, so this assigns
        # rather than defers -- an app that names a policy of its own
        # has already lost it to the filter above, and an empty csp
        # means no header at all.
        if csp and wire.content_type.startswith("text/html"):
            headers["content-security-policy"] = csp
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
