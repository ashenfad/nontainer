"""The served Content-Security-Policy, mirrored for verification.

A driver that intercepts requests has to agree with the policy the app
is actually served under: aborting what the policy permits is a false
red, and letting through what it refuses is a false green that shows up
only once the app is published. These are pure functions over the
policy string — what it permits, and how one refusal reads to the agent
that has to repair it.

Conservative by design. A source this cannot honor exactly (a wildcard,
a quoted keyword) is read as permitting nothing, because the cost of
guessing wide is an app that verifies green and breaks on delivery.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit


def blocked_script_note(url: str, script_hosts: tuple[str, ...]) -> str:
    """The harness's message for a script from a host that isn't allowed.
    Shared, because the block can now come from either side: request
    interception, or the CSP that reaches the browser first."""
    return (
        f"{url} -> blocked: scripts may only load "
        f"from the CDN allowlist ({', '.join(script_hosts) or 'none'})"
    )


def blocks_code(directive: str) -> bool:
    """Does this violated directive mean CODE DID NOT RUN?

    Those are failures, not warnings: the app is not doing what the agent
    thinks it is. A refused image or font is a blemish on a page that
    otherwise works, and shouldn't turn a run red on its own."""
    return directive.startswith("script") or directive in ("worker-src", "child-src")


def _csp_directives(csp: str) -> dict[str, list[str]]:
    """``{directive: [source, ...]}`` for one policy string."""
    out: dict[str, list[str]] = {}
    for part in (csp or "").split(";"):
        tokens = part.split()
        if tokens:
            out[tokens[0].lower()] = tokens[1:]
    return out


# A source naming a scheme and nothing else: `https:`, `data:`, `blob:`.
# Distinguished from a bare host with a port (`scripts.internal:8443`),
# which names an origin and must keep its port.
_SCHEME_ONLY = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:$")


def _csp_source_netloc(src: str) -> str | None:
    """The ``host[:port]`` a source names, or ``None`` when it names no
    host — a quoted keyword (``'self'``) or a scheme-only source."""
    if src.startswith("'") or _SCHEME_ONLY.match(src):
        return None
    rest = src.split("://", 1)[1] if "://" in src else src
    return rest.split("/", 1)[0].lower() or None


def csp_script_origins(csp: str) -> tuple[str, ...]:
    """Host origins a policy permits scripts from.

    Interception has to agree with the policy actually being enforced: a
    custom ``AppsConfig.csp`` that allows a host ``script_hosts`` doesn't
    list would be served happily and aborted here, which is a false RED
    and a divergence in the other direction.

    Conservative by design — quoted keywords (``'self'``), scheme-only
    sources (``https:``, ``data:``) and wildcards are skipped. Those
    cannot be honored by a hostname check, so list them in
    ``script_hosts`` explicitly rather than having this guess. An
    explicit port is KEPT (``scripts.internal:8443``): that is the
    origin the browser connects to, and it is what a request's netloc
    is compared against."""
    directives = _csp_directives(csp)
    sources = directives.get("script-src") or directives.get("default-src")
    out: list[str] = []
    for src in sources or []:
        if "*" in src:
            continue
        netloc = _csp_source_netloc(src)
        if netloc:
            out.append(netloc)
    return tuple(out)


# Which directive governs each Playwright resource type, in the order a
# browser consults them (the specific directive, then its fallback).
# A top-level document never reaches this table — it is the app's own
# origin — so ``document`` here means a SUB-FRAME, governed by
# frame-src. Anything unlisted falls through to default-src, which is
# also every listed directive's fallback when the policy omits it.
_RESOURCE_DIRECTIVES: dict[str, tuple[str, ...]] = {
    "image": ("img-src",),
    "xhr": ("connect-src",),
    "fetch": ("connect-src",),
    "eventsource": ("connect-src",),
    "websocket": ("connect-src",),
    "stylesheet": ("style-src",),
    "font": ("font-src",),
    "media": ("media-src",),
    "document": ("frame-src", "child-src"),
    "worker": ("worker-src", "child-src"),
    "manifest": ("manifest-src",),
}


def csp_directive_for(resource_type: str) -> str:
    """The directive a request of this type is judged by — what an
    abort note must name for the fix to be actionable."""
    return _RESOURCE_DIRECTIVES.get(resource_type, ("default-src",))[0]


def _source_matches(src: str, parts: Any) -> bool:
    """Does one CSP source cover this request's origin?

    Modest on purpose: scheme-only sources match by scheme, host
    sources by scheme (when given) plus host, and a leading ``*.``
    matches subdomains but not the bare domain. A source WITHOUT a port
    matches any port, which is looser than a browser — the cost of
    guessing wrong here is aborting a request the served policy allows,
    a false red, and that is the failure this matching exists to
    prevent. Keywords and ``data:``/``blob:`` never match: they name the
    document's own origin or bytes it already holds, not a network
    fetch this handler could let through."""
    if src == "*":
        return True
    if src.startswith("'") or src in ("data:", "blob:", "filesystem:", "mediastream:"):
        return False
    if _SCHEME_ONLY.match(src):
        return parts.scheme == src[:-1].lower()
    scheme = src.split("://", 1)[0].lower() if "://" in src else ""
    host = _csp_source_netloc(src)
    if not host or (scheme and parts.scheme != scheme):
        return False
    netloc = parts.netloc.lower()
    if ":" in host:  # an explicit port is part of the origin
        return netloc == host
    hostname = netloc.split(":", 1)[0]
    if host.startswith("*."):
        return hostname.endswith(host[1:])  # a subdomain, not the bare domain
    return hostname == host


def csp_permits(csp: str, resource_type: str, url: str) -> bool:
    """Would the SERVED policy let this non-script request through?

    Interception aborts what the policy would refuse, so it must also
    let through what the policy allows — otherwise an embedder who
    widened one directive (``csp_extend={"connect-src":
    ("http://api.internal",)}`` for an intranet API the browser is the
    only path to) gets an app that serves fine and fails verification.
    An empty policy means no header at all: nothing here to permit
    anything, so the caller's own rule decides."""
    parts = urlsplit(url)
    if not parts.netloc:
        return False
    directives = _csp_directives(csp)
    for name in (*_RESOURCE_DIRECTIVES.get(resource_type, ()), "default-src"):
        if name in directives:
            return any(_source_matches(src, parts) for src in directives[name])
    return False


def csp_note(directive: str, blocked: str, script_hosts: tuple[str, ...]) -> str:
    """Phrase one CSP violation as the fix.

    An external script the allowlist doesn't cover gets the SAME message
    the route handler would have given, because now it never reaches the
    route handler — the browser refuses it first. Reporting the generic
    policy text there would be a worse diagnostic than before the CSP
    was enforced, which is the trap this whole change is meant to avoid.
    """
    if directive.startswith("script") and blocked.startswith(("http://", "https://")):
        if urlsplit(blocked).netloc not in script_hosts:
            return blocked_script_note(blocked, script_hosts)
    served = (
        f"blocked by the app's Content-Security-Policy ({directive}). This is "
        "the policy a PUBLISHED app is served under, so it would otherwise "
        "have failed only after publishing"
    )
    if blocks_code(directive):
        # Code that never ran, and never announced it: a refused script
        # does not throw, so without this nothing would name it at all.
        return (
            f"{blocked} -> {served} — and a refused script does not throw, so "
            "nothing else would name it. blob:/data: urls, eval, and new "
            "Function are not permitted; load code from the app's own files "
            "instead."
        )
    # An image, font, stylesheet or fetch. Telling THIS one to stop using
    # eval would send the repair somewhere there is nothing to repair.
    return (
        f"{blocked} -> {served}. Serve it from the app's own files "
        f"(a relative url) or an https host the {directive} directive allows."
    )
