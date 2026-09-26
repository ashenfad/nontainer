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
    HANDLER_CONTRACT,
    WIRE_NOT_ACCEPTABLE,
    WIRE_NOTED_RESPONSE,
    WIRE_REFUSED,
    WIRE_RESPONSE,
    HttpError,
    Request,
    WireResponse,
    error_body,
    make_request,
    nt__Encoder,
    size_refusal,
)

# Fixed names under the workspace root (ws.root, default /workspace):
# the app tree is <root>/app, handlers <root>/app/api, the handler log
# <root>/app/logs/api.log, test_app's captures <root>/app/screenshots.
# AppRuntime derives the absolute paths.
APP_DIR = "app"
API_DIR = "api"
LOG_DIR = "logs"
LOG_FILE = "api.log"
SCREENSHOT_DIR = "screenshots"

# What the authoring loop writes into the app tree for the agent to
# read back through the filesystem: handler tracebacks and prints (which
# can carry secrets from exception messages) and page captures. They are
# the author's, never a visitor's, so a publication leaves them out.
AUTHORING_DIRS = (LOG_DIR, SCREENSHOT_DIR)

# Top-level directories of the app tree that static serving refuses, in
# the live preview and in a publication alike: backend source, and the
# authoring artifacts above. The refusal is by directory name, so it
# also covers a publication made before those were left out of one.
UNSERVED_DIRS = (API_DIR, *AUTHORING_DIRS)

# The same, as workspace paths relative to the root, in the spelling
# ``Store.publish(exclude=...)`` takes.
PUBLISH_EXCLUDE = tuple(f"{APP_DIR}/{name}/" for name in AUTHORING_DIRS)


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
    """An error response in the one shape every error takes, whether
    dispatch or a handler's ``HttpError`` raised it: a JSON body (see
    :func:`~nontainer.apps.contract.error_body`)."""
    return WireResponse(int(status), error_body(message, **extra), "application/json")


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


def _unserved_top(rel: str) -> str | None:
    """The entry of ``UNSERVED_DIRS`` that a canonical path relative to
    the app root sits in (or names), else ``None``.

    Compared without case: a workspace over a case-insensitive host
    filesystem opens ``LOGS/api.log`` as ``logs/api.log``, so an
    exact-case check would refuse one spelling and serve the other."""
    top = rel.split("/", 1)[0].casefold()
    for name in UNSERVED_DIRS:
        if top == name.casefold():
            return name
    return None


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
        top = _unserved_top(prefix)
        if top == API_DIR:
            # /api/ routes to handlers before static ever runs, so an
            # asset there would be silently unreachable.
            raise ValueError(
                f"static_assets prefix {raw!r} is unreachable: /api/ routes to handlers"
            )
        if top is not None:
            # Static serving refuses these directories before it looks
            # for an asset, so an asset there would never be served.
            raise ValueError(
                f"static_assets prefix {raw!r} is unreachable: {top}/ holds the "
                "app's authoring artifacts, which are never served"
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


# Trailer appended to handler source. The return is encoded here, in
# the sandbox, into the wire tuple the host reads back (see
# contract.nt__Encoder); an HttpError becomes a response the same way,
# so intentional errors come back structured rather than as tracebacks.
# The return value itself is never bound, so nothing but the tuple is
# left in the namespace for the executor to carry back.
#
# The size limits are literals in the trailer's source rather than
# inputs, so no binding the handler can reach changes them. That keeps
# an honest handler's oversized body from leaving the sandbox; one that
# replaces the encoder is still held to the same limits by the host.
_WIRE = "nt__wire"
_TRAILER = """

try:
    nt__wire = nt__Encoder.respond(
        {verb}(nt__req),
        nt__req.headers.get("accept"),
        text_limit={text_limit!r},
        binary_limit={binary_limit!r},
        carry_limit={carry_limit!r},
    )
except HttpError as nt__e:
    nt__wire = nt__Encoder.error(
        nt__e.status,
        nt__e.message,
        text_limit={text_limit!r},
        binary_limit={binary_limit!r},
        carry_limit={carry_limit!r},
    )
"""


def _trailer(
    verb: str,
    text_limit: int | None = None,
    binary_limit: int | None = None,
    carry_limit: int | None = None,
) -> str:
    """The trailer calling ``verb``, with the size limits written in."""
    return _TRAILER.format(
        verb=verb,
        text_limit=None if text_limit is None else int(text_limit),
        binary_limit=None if binary_limit is None else int(binary_limit),
        carry_limit=None if carry_limit is None else int(carry_limit),
    )


class _Refused(Exception):
    """The wire carried a refusal: the handler returned something the
    liberal-return rules reject. The message says what."""


class _Unacceptable(Exception):
    """The wire said the request asked for a format the handler's
    sandbox cannot produce (Arrow without pyarrow). The message says
    what, and how to fix it."""


class _Malformed(Exception):
    """The wire value is not one of the shapes the encoder produces."""


def _read_wire(value: Any) -> tuple[WireResponse, str | None]:
    """The response one handler execution handed back, and the note
    for the handler log a noted response carries (``None`` for a plain
    one).

    The value came out of the sandbox, so nothing about it is trusted:
    each element's EXACT type is checked (a subclass of ``str`` or
    ``dict`` would carry methods of the handler's making into the code
    that reads it), and the status must be one HTTP can carry. Raises
    :class:`_Refused` for the encoder's refusal marker,
    :class:`_Unacceptable` for its not-acceptable marker, and
    :class:`_Malformed` for anything else that is not a response."""
    if type(value) is not tuple or not value or type(value[0]) is not str:
        raise _Malformed(f"expected a wire tuple, got {type(value).__name__}")
    tag = value[0]
    if tag == WIRE_REFUSED:
        if len(value) != 2 or type(value[1]) is not str:
            raise _Malformed("a refusal must be (tag, message: str)")
        raise _Refused(value[1])
    if tag == WIRE_NOT_ACCEPTABLE:
        if len(value) != 2 or type(value[1]) is not str:
            raise _Malformed("a not-acceptable must be (tag, message: str)")
        raise _Unacceptable(value[1])
    note = None
    if tag == WIRE_NOTED_RESPONSE:
        if len(value) != 6:
            raise _Malformed(f"a noted response has 6 elements, got {len(value)}")
        note = value[5]
        if type(note) is not str:
            raise _Malformed("a response's note must be a str")
    elif tag != WIRE_RESPONSE:
        raise _Malformed(f"unknown wire tag {tag[:40]!r}")
    elif len(value) != 5:
        raise _Malformed(f"a response has 5 elements, got {len(value)}")
    _, status, content_type, headers, body = value[:5]
    if type(status) is not int or not 100 <= status <= 599:
        raise _Malformed("status must be an int from 100 to 599")
    if type(content_type) is not str:
        raise _Malformed("content type must be a str")
    if type(headers) is not dict or not all(
        type(k) is str and type(v) is str for k, v in headers.items()
    ):
        raise _Malformed("headers must be a dict of str to str")
    if type(body) is not bytes:
        raise _Malformed("body must be bytes")
    return WireResponse(status, body, content_type, dict(headers)), note


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


def _is_csp_token(text: str) -> bool:
    """A CSP directive name or source with nothing in it that could
    splice the policy: non-empty, no whitespace, no ``;``."""
    return bool(text) and ";" not in text and not any(c.isspace() for c in text)


@dataclass(frozen=True)
class AppsConfig:
    request_timeout: float = 5.0
    # request_timeout is the real per-request guard (same sandbox
    # commit checks both); the tick limit only backstops it and
    # must not fire on an honest handler looping over a big frame.
    request_tick_limit: int = 10_000_000
    max_response_bytes: int = 10_000_000
    """The largest TEXT body a handler may answer with: JSON, ``text/*``,
    XML and JavaScript types (the rule ``ws-curl`` uses to decide what is
    text). Any other body is held to ``max_binary_response_bytes``.

    Enforced where the handler ran, right after its return is encoded,
    so an oversized body never crosses the sandbox boundary; the host
    checks again. Over it, the request answers 500 with a message, also
    in ``api.log``, that says what to do instead. An executor that can
    carry less back from one execution lowers it (see
    ``Runtime.view_result_limit``). Declared ``static_assets`` are
    exempt; a static file from the workspace is not. Only the embedder
    sets it: nothing in handler code can raise it."""
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
    from handler rules are deliberate: no response-size cap
    (the embedder chose these bytes; the caps exist to catch runaway
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

    Setting this AND a non-empty ``csp_extend`` is a configuration
    error: a verbatim policy has nothing to extend."""
    csp_extend: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    """Sources to ADD to individual directives of the DERIVED policy::

        AppsConfig(csp_extend={
            "img-src": ("blob:",),
            "connect-src": ("http://tiles.internal", "wss:"),
        })

    The case this exists for is an intranet deployment where the only
    network path an app has is the browser's: a tile server or API on
    plain ``http://`` is refused by ``connect-src 'self' https:`` and
    ``img-src 'self' https:``, and the alternative — copying the whole
    policy into ``csp`` — silently drops its link to ``script_hosts``
    and ``'wasm-unsafe-eval'``, so anything later added to the derived
    policy is missing from the copy with nothing to say so.

    EXTEND-ONLY, deliberately. Each entry appends its sources to that
    directive of the derived policy (de-duplicated, derived sources
    first, directive names matched case-insensitively), or adds the
    directive when the derived policy has none (``frame-src``,
    ``worker-src``). Removing a source, or making a directive stricter,
    is not expressible here — declare the whole policy in ``csp`` for
    that.

    Extending ``script-src`` is allowed (an embedder may need
    ``'unsafe-eval'``), but ``script_hosts`` remains the declaration for
    script HOSTS: it also drives curl's external-URL message and the
    allowlist sentence the agent reads, neither of which a policy
    string reaches.

    Verification follows the extension either way: test_app reads the
    RESOLVED policy, taking script origins from ``script-src`` and, for
    every other resource, asking whether the policy permits the request
    before aborting it. So an intranet host added to ``img-src`` or
    ``connect-src`` verifies the way it serves rather than being
    aborted as a false red.

    Declared last: see ``frontend_notes``."""
    origin: str = "http://localhost"
    """Canonical base URL of the app, taught to agents as ``$APP_ORIGIN``.

    Fictional — no listener exists — so the port, if any, is decorative
    and any localhost port dispatches by path. The origin form is what
    reads identically on every rung.

    Appended after ``csp_extend`` so the 0.3.3 positional order still
    binds (see ``test_positional_construction_matches_0_3_3``).
    """
    handler_example: str | None = None
    """The example handler the apps notes show — the code an agent
    copies when it writes its first endpoint.

    ``None`` keeps the default block (a ``get``/``post`` pair keeping
    state in ``cache``). ``""`` omits it. A string REPLACES it::

        AppsConfig(handler_example=(
            "Handlers export verb functions; example "
            "__WS__/app/api/notes.py:\\n\\n"
            "    from host import db\\n\\n"
            "    def get(req):\\n"
            "        return {'notes': db.list()}\\n"
        ))

    ``__WS__`` in it is substituted with the workspace root, as
    elsewhere in the notes, so the path reads the way the agent would
    type it.

    Replacing rather than appending is the point: an embedder whose
    handlers must keep state somewhere else (a database it injects)
    would otherwise have its rule sitting in ``apps_primer`` UNDER an
    example that shows the other store, and the example is what gets
    copied. One declaration, one store.

    What stays regardless, because it is nontainer's own contract: only
    verb functions are routed, the return shapes, ``HttpError``, and the
    read-only filesystem and cache a GET handler runs under.

    Declared last: see ``frontend_notes``."""
    driver: Any = None
    """The :class:`~nontainer.apps.driver.AppDriver` ``test_app`` runs
    through, or ``None`` to let the run choose.

    The choice, in order: this field, then the executor's own
    (``Runtime.app_driver``), then a headless Chromium on the host. An
    embedder sets this to verify somewhere other than the host — the
    invariant a driver keeps is that the dispatcher it serves the page
    from is the dispatcher the publication will use, so the driver that
    matches how an app will be SERVED is the one that can catch what
    would otherwise fail only after publishing.

    Declared last: see ``frontend_notes``."""
    max_binary_response_bytes: int = 32_000_000
    """The largest body of any type ``max_response_bytes`` does not
    cover: images, ``application/octet-stream``, an Arrow stream a
    table return answers with. Enforced the same way.

    Larger than the text cap because a binary body is already compact:
    the same rows as an Arrow stream are a fraction of their size as
    JSON, which is why the text refusal suggests asking for Arrow.

    Declared last: see ``frontend_notes``."""

    def __post_init__(self) -> None:
        """Validate ``csp_extend`` at construction, where the traceback
        still points at the embedder's own call, rather than shipping a
        malformed directive into a served header (a stray ``;`` or
        space would splice a new directive into the policy)."""
        if not self.csp_extend:
            return
        if self.csp is not None:
            raise ValueError(
                "csp_extend cannot be combined with csp: a verbatim policy has "
                "nothing to extend. Put the added sources in the csp string, or "
                "drop csp and let the policy derive from script_hosts."
            )
        for name, sources in self.csp_extend.items():
            if (
                not isinstance(name, str)
                or not _is_csp_token(name)
                or name.lower() != name
            ):
                raise ValueError(
                    f"csp_extend directive must be a lowercase name with no "
                    f"whitespace or ';': {name!r}"
                )
            if isinstance(sources, str) or not isinstance(sources, (tuple, list)):
                raise ValueError(
                    f"csp_extend[{name!r}] must be a tuple or list of source "
                    f"strings, not {sources!r}"
                )
            for src in sources:
                if not isinstance(src, str) or not _is_csp_token(src):
                    raise ValueError(
                        f"csp_extend[{name!r}] source must be a non-empty string "
                        f"with no whitespace or ';': {src!r}"
                    )


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
        # The names a handler may use, plus the encoder its trailer
        # calls (bound the same way, so every executor that carries the
        # contract classes carries the encoder too).
        self._contract = (*HANDLER_CONTRACT, nt__Encoder)
        # Path layout, derived once from the workspace root.
        self._app_root = app_root(ws)
        self._api_root = f"{self._app_root}/{API_DIR}"
        self._log_dir = f"{self._app_root}/{LOG_DIR}"
        self._log_path = f"{self._log_dir}/{LOG_FILE}"
        self._screenshot_dir = f"{self._app_root}/{SCREENSHOT_DIR}"

    @property
    def config(self) -> AppsConfig:
        """The runtime's config — adapters read ``script_hosts`` /
        ``apps_primer`` from here to build tool descriptions."""
        return self._config

    @property
    def workspace(self) -> Workspace:
        """The workspace this runtime serves handlers from. What
        ``test_app`` reads sources and writes screenshots through: the
        same session, under the same lock."""
        return self._ws

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
        # A handler's response was held to the caps where it was encoded
        # and again in _dispatch_api. A static file from the workspace is
        # held to them here. Declared assets skip them: the caps exist
        # to catch runaway output, and an asset's size is a decision the
        # embedder already made.
        if not api and not asset:
            refusal = size_refusal(
                len(resp.content),
                resp.content_type,
                self._config.max_response_bytes,
                self._config.max_binary_response_bytes,
                advise=False,
            )
            if refusal is not None:
                resp = _error_response(500, refusal)
        # Only /api requests: static assets are high-volume and
        # low-signal, and would bury the tracebacks the log exists for.
        if api:
            self._pending.append(self._request_line(request, resp.status))
            self._flush_if_free()
        return resp

    # -- api -------------------------------------------------------------

    def _dispatch_api(self, request: Request) -> WireResponse:
        # A workspace with no executor is one served as files: its tree
        # held no handler when it was opened, so no /api path here has
        # an endpoint behind it and the lookup below has nothing to
        # find. Answered first so that serving a files-only tree never
        # reaches for an executor that was deliberately not built.
        if not self._ws.runtime.executes:
            raise HttpError(404, f"no such endpoint: {request.path}")
        name = request.path[len("/api/") :].strip("/")
        if not name or "/" in name or name.startswith("_"):
            raise HttpError(404, f"no such endpoint: {request.path}")
        handler_path = f"{self._api_root}/{name}.py"
        fs = self._ws.files.fs
        # A handler is a file. A directory carrying the name is not an
        # endpoint, and answering 404 here is what keeps it from being
        # read as source below.
        if not fs.isfile(handler_path):
            # agents mirror the FILENAME into the url
            # (fetch('api/explorer.py')) and then debug the backend for
            # an hour — label the door
            if name.endswith(".py"):
                bare = name[:-3]
                if bare and fs.isfile(f"{self._api_root}/{bare}.py"):
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
        atomic = not readonly and ws.caps.staging and not ws.uncommitted

        # The view declares the intent; the executor realizes it (a
        # restricted sandbox held exclusively for this call — so a
        # read-only GET can't mutate, contract classes are in scope,
        # and the per-request budget applies, whichever executor runs
        # it). No sandbox object crosses back here.
        #
        # The size caps travel into the sandbox with the trailer, and the
        # executor is told the largest body it must carry back: one with
        # a transport limit sizes it to fit, up to its own ceiling, which
        # then lowers both caps.
        text_limit = self._config.max_response_bytes
        binary_limit = self._config.max_binary_response_bytes
        carry_limit = ws.runtime.view_result_limit
        largest = max(text_limit, binary_limit)
        if carry_limit is not None:
            largest = min(largest, carry_limit)
        view = ViewSpec(
            readonly_fs=readonly,
            readonly_cache=readonly,
            timeout=self._config.request_timeout,
            tick_limit=self._config.request_tick_limit,
            extra_classes=self._contract,
            result_bytes=largest,
        )
        result = ws.runtime.exec_python(
            source + _trailer(verb, text_limit, binary_limit, carry_limit),
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

        try:
            resp, note = _read_wire(result.namespace.get(_WIRE))
        except _Refused as e:
            if atomic:
                ws.discard()
            self._log(f"[{where}] BAD RETURN: {e}")
            return _error_response(500, str(e))
        except _Unacceptable as e:
            # The response depended on the request's Accept, so it says
            # so: a cache must not answer a JSON request with this.
            if atomic:
                ws.discard()
            self._log(f"[{where}] NOT ACCEPTABLE: {e}")
            return WireResponse(
                406, error_body(str(e)), "application/json", {"vary": "Accept"}
            )
        except _Malformed as e:
            # Not something a handler's return produces: the encoder
            # always hands back a well-formed tuple or a refusal. So the
            # tuple was replaced, or never arrived (an executor that
            # drops a value it cannot carry, such as one over its size
            # limit, drops it silently).
            if atomic:
                ws.discard()
            reason = (
                "no response came back from the handler's execution (an "
                "executor leaves out a value it cannot carry, such as a "
                "response body over its size limit)"
                if _WIRE not in result.namespace
                else f"the handler's response came back malformed: {e}"
            )
            self._log(f"[{where}] ERROR: {reason}")
            return _error_response(500, "internal error", log=self._log_path)

        if note is not None:
            self._log(f"[{where}] NOTE: {note}")
        # The encoder refused an oversized body before it crossed; this
        # holds a handler that replaced the encoder to the same caps.
        refusal = size_refusal(
            len(resp.content), resp.content_type, text_limit, binary_limit, carry_limit
        )
        if refusal is not None:
            if atomic:
                ws.discard()
            self._log(f"[{where}] BAD RETURN: {refusal}")
            return _error_response(500, refusal)
        return resp

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
        the response-size caps (see :meth:`_dispatch`)."""
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
        # Backend source and the authoring artifacts are never served as
        # static, and the check runs on the canonical path. The /api/ URL
        # prefix routes to handlers, but a static request that normalizes
        # INTO api/ (e.g. `/./api/h.py`, `/x/../api/_shared.py`) would
        # otherwise serve raw handler source — the frontend/backend
        # boundary — and one that reaches logs/ or screenshots/ would
        # serve the author's tracebacks and captures to any visitor.
        # Refused before the asset lookup, so no declared prefix can
        # reopen these paths whatever the config holds.
        if _unserved_top(rel) is not None:
            raise HttpError(404, f"not found: {request.path}")
        asset = self._asset_response(rel, request)
        if asset is not None:
            return asset, True
        fs = self._ws.files.fs
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
        gates per-request atomicity on ``not ws.uncommitted`` — so the note
        would silently cost the next mutating handler its rollback. Same
        reasoning as the request-line buffer; see ``_flush_if_free``."""
        if rel in self._shadow_notes:
            return
        path = posixpath.normpath(f"{self._app_root}/{rel}")
        if not self._ws.files.fs.exists(path):
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
        ``not ws.uncommitted``, so a diagnostic write here would silently
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
        if self._log_sink is not None or not ws.caps.staging or ws.uncommitted:
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
            fs = self._ws.files.fs
            fs.makedirs(self._log_dir, exist_ok=True)
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


def app_runtime(ws: Workspace) -> "AppRuntime | None":
    """The app runtime this workspace is wired with, or ``None``.

    Read off the ``ws-curl`` command registered on the workspace's
    runtime, which is where the association lives: the command closure
    holds the runtime, the workspace holds its commands, and nothing
    else holds either. So a workspace dropped by its embedder takes its
    runtime with it, where a registry keyed on workspaces would keep
    both alive through the runtime's own reference back. A fork
    rebuilds the loop bound to itself, so a child has one without
    anybody having called ``enable_apps`` on it.
    """
    command = ws.runtime.commands.get("ws-curl")
    return getattr(command, "app_runtime", None)


def enable_apps(ws: Workspace, config: AppsConfig | None = None) -> AppRuntime:
    """Wire the apps runtime into a workspace: builds the AppRuntime
    and registers the ``ws-curl`` fetch and the ``ws-pytest`` /
    ``ws-vitest`` unit-test terminal builtins. Returns the runtime (also
    the live router's dispatch source).

    Idempotent, and a fork counts as already wired: the child of a
    workspace with an app rebuilds the loop bound to itself as part of
    the fork, so this returns that runtime rather than building a
    second one over a ``ws-curl`` already registered. Which means the
    ``config`` of the first wiring is the one that stands — a workspace
    already carrying an app is not reconfigured by asking for one
    again.
    """
    from ..wspytest import register_wspytest
    from ..wsvitest import register_wsvitest
    from .wscurl import make_curl_command

    # Framework-owned: a fork/snapshot rebuilds its own runtime bound
    # to itself instead of inheriting the parent-bound closure.
    def _register(target: Workspace) -> AppRuntime:
        existing = app_runtime(target)
        if existing is not None:
            return existing
        target_runtime = AppRuntime(target, config)
        curl = make_curl_command(target_runtime)
        curl.app_runtime = target_runtime
        target.runtime.register_command("ws-curl", curl, rebind=_register)
        # The canonical origin form needs the value in the shell, on
        # every rung: termish expands it, dud guests get it exported.
        origin = config.origin if config is not None else AppsConfig.origin
        target.runtime.env["APP_ORIGIN"] = origin
        # The unit tier, beside the wire tier: a workspace with an app
        # gets all three verbs. Both register themselves on a workspace
        # without an app too, which is why this is a no-op when already
        # there.
        register_wspytest(target)
        register_wsvitest(target)
        return target_runtime

    return _register(ws)


def request(method: str, url: str, **kwargs: Any) -> Request:
    """Convenience re-export of :func:`make_request`."""
    return make_request(method, url, **kwargs)
