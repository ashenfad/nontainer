"""test_app: headless verification of the agent's app. Requires the
``[apps]`` extra (playwright) plus ``playwright install chromium``.

The workspace IS the origin: a fresh browser context intercepts every
request via ``page.route`` — static paths and ``/api/*`` are answered
by the same ``dispatch`` the curl builtin uses; external hosts are
denied except the script-host allowlist (``AppsConfig.script_hosts``,
default esm.sh and friends for the no-build frontend tiers — the same
declaration the served CSP derives from). No port, no server.

Relocatability is enforced here by construction (docs/apps.md): the
app is served under a synthetic prefix (``/apps/t-test/``), so a
frontend that hardcodes absolute URLs (``fetch('/api/x')``) gets an
instructive 404 during verification instead of breaking at delivery.

Screenshots are written to ``<root>/app/screenshots/`` in the workspace and
returned as paths — bytes never ride in model-facing observations,
and the screenshots version/fork/roll back with the session.

Execution: one Chromium is shared across all test_app calls, on a
dedicated async loop-thread (see ``browser.py``); each call runs on its
own fresh context, bounded by a concurrency semaphore. The synchronous
route dispatch is CPU-bound, so it's hopped off the browser loop into a
thread; ``AppRuntime.dispatch`` serializes it under the workspace's
own single-writer lock (``ws.lock``) — a page's parallel fetches don't
reenter the sandbox, and dispatch/screenshot writes can't race
ordinary tool calls on the same workspace.
"""

from __future__ import annotations

import asyncio
import posixpath
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from .contract import filter_headers, make_request

if TYPE_CHECKING:
    from .dispatch import AppRuntime

_HOST = "nontainer.test"
_TOKEN = "t-test"
_PREFIX = f"/apps/{_TOKEN}"
_BASE_URL = f"https://{_HOST}{_PREFIX}/"

VIEWPORTS = {
    "desktop": {"width": 1280, "height": 800},
    "tablet": {"width": 768, "height": 1024},
    "mobile": {"width": 390, "height": 844},
}

# JSON (not plain text): the app's own res.json() error path can then
# actually read and display it
_ABSOLUTE_PATH_HINT = (
    b'{"error": "nontainer: absolute path -- apps are served under a '
    b"prefix and must use RELATIVE urls (fetch('api/x'), not "
    b"fetch('/api/x'))\"}"
)


def coerce_actions(actions: Any) -> list[dict[str, Any]]:
    """Normalize loosely-typed model arguments: JSON strings decode,
    a bare dict becomes a one-action list. Raises ValueError with an
    agent-actionable message otherwise."""
    import json

    if isinstance(actions, str):
        try:
            actions = json.loads(actions)
        except ValueError as e:
            raise ValueError(
                f"actions must be a JSON list of action objects ({e})"
            ) from e
    if isinstance(actions, dict):
        actions = [actions]
    if actions is None:
        return []
    if not isinstance(actions, list) or not all(isinstance(a, dict) for a in actions):
        raise ValueError(
            'actions must be a list of objects like {"click": "#sel"} — '
            f"got {type(actions).__name__}"
        )
    return actions


# ---------------------------------------------------------------------------
# page errors: which frame is the agent's, and what is on that line
# ---------------------------------------------------------------------------
#
# A stack is mostly other people's code. With a component library in
# play the top frame is thirty deep in a vendored bundle, and the one
# line the agent can act on is below it — so reporting the FIRST frame
# reports the least useful one. And a line number alone still costs a
# call to go look, on the file the agent just wrote.
#
# These are pure functions over the stack text (the fs read is injected)
# so they can be tested without a browser.

_LOCATION_RE = re.compile(r"^(?P<url>.+):(?P<line>\d+):(?P<col>\d+)$")

_MAX_QUOTED_LINE = 200
"""Cap on the quoted source line. The file-size guard is not enough on
its own: one minified or generated line can be most of a file, and
page errors are not truncated downstream — so a single error could eat
the whole observation budget and push out the diagnostics around it."""

_MAX_ANNOTATED_BYTES = 512_000
"""Don't slurp a bundle to quote one line; agent-authored files are small
and anything this size is not what the agent is debugging."""


@dataclass(frozen=True)
class Frame:
    """One parsed stack frame. ``rel`` is the app-relative path when the
    frame names a file this workspace serves, else ``None``."""

    raw: str
    fn: str | None
    url: str
    line: int
    col: int
    rel: str | None
    kind: str
    """``"agent"`` (a file the agent authored), ``"vendor"`` (a declared
    static asset or a third-party host), or ``"opaque"`` (blob:/data:,
    eval'd code — nothing the agent can open)."""


def parse_frames(stack: str) -> list[Frame]:
    """Parse a V8 stack into frames, unclassified (``kind="opaque"``,
    ``rel=None``). Lines that are not frames are skipped.

    The location is taken from the LAST parenthesised group, not the
    first: a function name can itself contain parentheses (``at weird
    (name) (app.js:1:2)``, and V8's nested-eval frames), and splitting
    on the first one puts half the name into the url — which then reads
    as third-party code and loses the agent its own frame."""
    out: list[Frame] = []
    for raw in (stack or "").splitlines():
        line = raw.strip()
        if not line.startswith("at "):
            continue
        body, fn = line[3:].strip(), None
        if body.endswith(")") and "(" in body:
            cut = body.rfind("(")
            fn = body[:cut].strip() or None
            body = body[cut + 1 : -1]
        m = _LOCATION_RE.match(body)
        if not m:
            continue
        out.append(
            Frame(
                raw=line,
                fn=fn,
                url=m.group("url"),
                line=int(m.group("line")),
                col=int(m.group("col")),
                rel=None,
                kind="opaque",
            )
        )
    return out


def classify_frame(frame: Frame, asset_prefixes: tuple[str, ...] = ()) -> Frame:
    """Locate a frame against the app being served.

    Two URL shapes name a served file: the synthetic test_app origin
    (``https://nontainer.test/apps/t-test/app.js``), and a bare name with
    no scheme — which is how a ``//# sourceURL=app.jsx`` comment surfaces,
    the convention a browser-side transpiler uses to keep its output
    attributable. Everything else is somebody else's code.
    """
    url = frame.url
    rel: str | None = None
    if any(c in url for c in " \t()"):
        # Not a URL we can attribute — V8's nested-eval frames
        # ("eval at fn (app.js:1:2), <anonymous>:1:1") survive the
        # parse but name no single file. Better opaque than pointed at
        # a path that doesn't exist.
        return Frame(**{**vars(frame), "rel": None, "kind": "opaque"})
    if url.startswith(_BASE_URL):
        rel = url[len(_BASE_URL) :]
    elif url.rstrip("/") == _BASE_URL.rstrip("/"):
        rel = ""
    elif "://" not in url and not url.startswith(("blob:", "data:")):
        rel = url.lstrip("/")
    if rel is not None:
        rel = rel.split("?", 1)[0].split("#", 1)[0]
        # An error thrown from an inline <script> reports the DOCUMENT
        # url, which is the app root — and its line numbers are
        # index.html's. Resolving it the same way _dispatch_static does
        # is what keeps the most common case (a script in the page the
        # agent just wrote) attributable at all.
        rel = posixpath.normpath(rel or "index.html")
        if rel in (".", "") or rel.startswith(".."):
            rel = None
    if rel is None:
        # A third-party host is vendor code; blob:/data:/eval is opaque
        # (there is no file to open, so naming a line would mislead).
        kind = "opaque" if url.startswith(("blob:", "data:")) else "vendor"
        if "://" not in url:
            kind = "opaque"
        return Frame(**{**vars(frame), "rel": None, "kind": kind})
    if any(rel == p or rel.startswith(p + "/") for p in asset_prefixes):
        return Frame(**{**vars(frame), "rel": rel, "kind": "vendor"})
    return Frame(**{**vars(frame), "rel": rel, "kind": "agent"})


def describe_page_error(
    name: str,
    message: str,
    stack: str,
    *,
    asset_prefixes: tuple[str, ...] = (),
    read_line: Any = None,
) -> str:
    """Render one page error the way an agent can act on it.

    Picks the first frame in the agent's OWN files, says how many frames
    were skipped to reach it, and quotes the offending source line
    (``read_line(rel, lineno) -> str | None``). When no frame names a
    file the agent wrote, it says so rather than printing a location
    from a bundle — the same rule as the parse-error branch below: a
    misleading diagnostic is worse than an absent one.
    """
    text = f"{name or 'Error'}: {message}"
    frames = [classify_frame(f, asset_prefixes) for f in parse_frames(stack)]
    if not frames:
        if name == "SyntaxError":
            # Parse errors carry NOTHING through pageerror.
            return text + (
                " (parse error: the browser reports no line — bisect the "
                "<script> blocks to find it)"
            )
        return text

    agent = next((f for f in frames if f.kind == "agent"), None)
    if agent is None:
        elsewhere = _describe_elsewhere(frames)
        return f"{text} (no frame in your own files — {elsewhere})"

    skipped = frames.index(agent)
    where = (
        f"at {agent.fn} ({agent.rel}:{agent.line}:{agent.col})"
        if agent.fn
        else (f"at {agent.rel}:{agent.line}:{agent.col}")
    )
    if skipped:
        where += f", +{_frames(skipped)} above it in library code"
    out = f"{text} ({where})"
    try:
        source = read_line(agent.rel, agent.line) if read_line else None
    except Exception:
        source = None  # a diagnostic must never break the run
    if source:
        out += f"\n     {agent.line} | {_clip(source, agent.col)}"
    return out


def _clip(source: str, col: int, limit: int = _MAX_QUOTED_LINE) -> str:
    """Keep the quoted line to a glanceable size, windowed on the error
    column. A generated or minified line can be most of a file, and the
    first ``limit`` characters of one say nothing about a fault 200k
    columns in — so slide the window to where the error actually is."""
    if len(source) <= limit:
        return source
    if col <= limit:
        return source[:limit] + " …"
    start = max(0, col - limit // 2)
    return "… " + source[start : start + limit] + " …"


def _frames(n: int) -> str:
    return f"{n} frame" + ("" if n == 1 else "s")


def _describe_elsewhere(frames: list[Frame]) -> str:
    """Where the error DID come from, when none of it is the agent's."""
    vendor = sum(1 for f in frames if f.kind == "vendor")
    opaque = len(frames) - vendor
    generated = "generated code (blob:/eval), which has no file to open"
    if vendor and not opaque:
        return (
            "its only frame is in library code"
            if vendor == 1
            else f"all {vendor} frames are in library code"
        )
    if opaque and not vendor:
        return (
            f"its only frame is in {generated}"
            if opaque == 1
            else f"all {opaque} frames are in {generated}"
        )
    return f"{_frames(vendor)} in library code, {opaque} in {generated}"


_ASSERT_POLL_MS = 50


async def _poll_assert(page: Any, expression: str, timeout_ms: int) -> tuple[bool, Any]:
    """Retry ``expression`` until truthy or the budget runs out.

    Returns ``(passed, why)``. An expression that RAISES is retried too:
    a predicate reaching into a node the app has not rendered yet throws
    on the first pass and succeeds on the third, which is the ordinary
    shape of asserting against an app that fetches. Only the last error
    is reported, so the message describes the state the run ended in
    rather than the state it started in.

    The deadline bounds each EVALUATION, not just the gaps between them:
    ``page.evaluate`` awaits a promise the expression returns, so one
    that never settles would block before the loop could look at the
    clock again — a hang with no output, where the old
    ``wait_for_function(timeout=…)`` had bounded the whole thing.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_ms / 1000
    why: Any = "assertion is falsy"
    # Whether ANY evaluation ever came back. It separates "this
    # expression never settles" from "the deadline simply expired":
    # the last poll before the deadline gets a sliver of budget and
    # times out even on an instant expression, which would otherwise
    # report a plainly falsy assert as a hung promise.
    settled = False

    def expired() -> tuple[bool, Any]:
        if settled:
            return False, why
        # Distinct from falsy, and worth saying: an assert that awaits
        # something that never arrives is a broken assertion, not a
        # broken app.
        return False, (
            f"assertion did not settle within {timeout_ms}ms — it returned a "
            "promise that never resolved (await the value in the app and "
            "assert on the DOM instead)"
        )

    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            return expired()
        try:
            value = await asyncio.wait_for(page.evaluate(expression), remaining)
            settled = True
            if value:
                return True, None
            why = "assertion is falsy"
        except (TimeoutError, asyncio.TimeoutError):
            return expired()
        except Exception as e:
            settled = True
            why = f"assertion errored: {e}"
        await asyncio.sleep(
            min(_ASSERT_POLL_MS / 1000, max(0.0, deadline - loop.time()))
        )


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


def _csp_script_origins(csp: str) -> tuple[str, ...]:
    """Host origins a policy permits scripts from.

    Interception has to agree with the policy actually being enforced: a
    custom ``AppsConfig.csp`` that allows a host ``script_hosts`` doesn't
    list would be served happily and aborted here, which is a false RED
    and a divergence in the other direction.

    Conservative by design — quoted keywords (``'self'``), scheme-only
    sources (``https:``) and wildcards are skipped. Those cannot be
    honored by a hostname check, so list them in ``script_hosts``
    explicitly rather than having this guess."""
    directives: dict[str, list[str]] = {}
    for part in (csp or "").split(";"):
        tokens = part.split()
        if tokens:
            directives[tokens[0].lower()] = tokens[1:]
    sources = directives.get("script-src") or directives.get("default-src") or []
    out: list[str] = []
    for src in sources:
        if src.startswith("'") or "*" in src:
            continue
        host = src.split("://", 1)[-1].split("/", 1)[0]
        if host and ":" not in host:  # drops `https:` / `data:` scheme sources
            out.append(host)
    return tuple(out)


def _csp_note(directive: str, blocked: str, script_hosts: tuple[str, ...]) -> str:
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


def _line_reader(runtime: "AppRuntime") -> Any:
    """``(rel, lineno) -> source line | None``, read from the workspace.

    Called off the browser loop-thread (see the call site): it takes the
    workspace lock, which the route dispatch also holds."""

    def read_line(rel: str, lineno: int) -> str | None:
        ws = runtime._ws
        path = f"{runtime._app_root}/{rel}"
        try:
            with ws.lock:
                if not ws.fs.exists(path) or not ws.fs.isfile(path):
                    return None
                data = ws.fs.read(path)
            if len(data) > _MAX_ANNOTATED_BYTES:
                return None
            lines = data.decode("utf-8", errors="replace").splitlines()
            if not 1 <= lineno <= len(lines):
                return None
            return lines[lineno - 1].strip() or None
        except Exception:
            return None  # a diagnostic must never break the run

    return read_line


def _asset_prefixes(runtime: "AppRuntime") -> tuple[str, ...]:
    return tuple(
        str(p).strip("/") for p in getattr(runtime.config, "static_assets", {})
    )


def _annotate_page_errors(
    runtime: "AppRuntime", records: list[tuple[str, str, str]]
) -> tuple[str, ...]:
    """Render collected (name, message, stack) records. Runs off the
    browser loop-thread — it reads the workspace fs under ``ws.lock``,
    which route dispatch holds too, and blocking the loop would stall
    every other test_app sharing it."""
    prefixes = _asset_prefixes(runtime)
    read_line = _line_reader(runtime)
    return tuple(
        describe_page_error(
            name, message, stack, asset_prefixes=prefixes, read_line=read_line
        )
        for name, message, stack in records[:20]
    )


@dataclass(frozen=True)
class ActionResult:
    index: int
    action: dict[str, Any]
    ok: bool
    value: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class TestAppResult:
    ok: bool
    """Load succeeded, no action errored, no assert was falsy, and no
    Content-Security-Policy violation stopped code running.

    That last clause is not bookkeeping: reporting a refused script while
    still printing PASS would leave the false green intact one layer up.
    A refused image or font stays a warning — a blemish on a page that
    otherwise works."""

    results: tuple[ActionResult, ...] = ()
    console: tuple[str, ...] = ()
    page_errors: tuple[str, ...] = ()
    screenshots: tuple[str, ...] = ()
    """Workspace paths under /app/screenshots/."""

    rejected: tuple[str, ...] = ()
    """Requests the harness refused (absolute paths, blocked scripts),
    each with WHY and the fix — the browser console only shows the
    symptom (a truncated JSON parse error, an anonymous ERR_FAILED)."""

    load_error: str | None = None

    def __bool__(self) -> bool:
        return self.ok


def _save_screenshot(runtime: "AppRuntime", path: str, png: bytes) -> None:
    """Write a screenshot to the workspace fs — off the browser loop and
    under the workspace's single-writer lock, since ``ws.fs`` is shared
    with the executor-hopped route dispatch (which serializes under the
    same lock inside ``AppRuntime.dispatch``)."""
    ws = runtime._ws
    with ws.lock:
        ws.fs.makedirs(f"{runtime._app_root}/screenshots", exist_ok=True)
        ws.fs.write(path, png)


async def _run_actions(
    browser: Any,
    sema: "asyncio.Semaphore",
    runtime: "AppRuntime",
    actions: list[dict[str, Any]] | None,
    *,
    viewport: str | dict[str, int] = "desktop",
    max_screenshots: int = 5,
    load_timeout_ms: int = 10_000,
    assert_timeout_ms: int = 2_000,
    settle_cap: float = 5.0,
) -> TestAppResult:
    """Run one test against a fresh context on the shared browser.
    Runs on the browser loop-thread; bounded by ``sema``."""
    vp = (
        VIEWPORTS.get(viewport, VIEWPORTS["desktop"])
        if isinstance(viewport, str)
        else {
            "width": int(viewport.get("width", 1280)),
            "height": int(viewport.get("height", 800)),
        }
    )

    # One declaration (AppsConfig.script_hosts) drives interception here
    # AND the served CSP: what verifies headlessly matches what serves.
    from .serve import resolve_csp

    csp = resolve_csp(runtime.config)
    # Interception must agree with the policy actually enforced. With the
    # derived policy these are the same list; with a custom one, its
    # script origins join in, so a host the served policy allows is not
    # aborted here (a false red, and the same divergence pointed the
    # other way).
    script_hosts = tuple(
        dict.fromkeys((*runtime.config.script_hosts, *_csp_script_origins(csp)))
    )

    # Repeated console lines are near-pure context tax: one audited
    # session spent 39% of all test_app result bytes (7,922 of 20,279)
    # on 32 copies of the same Tailwind CDN warning, against a model
    # working in ~30k of context. Collapse by text, keep first-seen
    # order, and carry the count — a genuinely repeating log (a retry
    # storm, a render loop) still reads as repeating.
    console: dict[str, int] = {}
    page_error_records: list[tuple[str, str, str]] = []
    results: list[ActionResult] = []
    screenshots: list[str] = []
    rejected: dict[str, None] = {}  # ordered de-dupe
    csp_blocked_code: list[str] = []  # violations that stopped code running
    shot_counter = 0
    loop = asyncio.get_running_loop()

    def _reject(note: str) -> None:
        if len(rejected) < 20:
            rejected.setdefault(note)

    def _console(message: Any) -> None:
        line = f"[{message.type}] {message.text}"
        if line in console:
            console[line] += 1
        elif len(console) < 100:  # cap DISTINCT lines; repeats stay free
            console[line] = 1

    def _console_lines() -> tuple[str, ...]:
        return tuple(
            line if n == 1 else f"{line} (x{n})" for line, n in console.items()
        )

    async def _collect_csp(page: Any) -> None:
        """Fold any CSP violations into `rejected`, phrased as the fix.

        These used to be invisible: test_app sent no policy, so an app
        using a blob module script or eval verified green and broke only
        once published — and the block never throws into the page, so
        nothing in page_errors named it either."""
        try:
            hits = await page.evaluate("window.__nt_csp || []")
        except Exception:
            return  # page closed / navigated away: a diagnostic, not the run
        for directive, blocked in hits or ():
            _reject(_csp_note(directive, blocked, script_hosts))
            if blocks_code(directive):
                # A run whose code was refused is not a PASS. Without
                # this the whole change only makes the failure VISIBLE,
                # while `ok` keeps saying the app works — the false
                # green it exists to remove, one layer up.
                csp_blocked_code.append(blocked)

    async def _rendered_errors() -> tuple[str, ...]:
        """Render collected page errors, reading the agent's source for
        the offending line. Hopped off the browser loop for the same
        reason route dispatch is: it takes ``ws.lock``, and this loop is
        shared by every concurrent test_app."""
        if not page_error_records:
            return ()
        return await loop.run_in_executor(
            None, _annotate_page_errors, runtime, page_error_records
        )

    async def route_handler(route: Any, request: Any) -> None:
        parts = urlsplit(request.url)
        if parts.netloc == _HOST:
            if not parts.path.startswith(_PREFIX + "/") and parts.path != _PREFIX:
                _reject(
                    f"{parts.path} -> 404: absolute path (apps serve under "
                    "a prefix — use relative URLs: fetch('api/x'), not "
                    "fetch('/api/x'))"
                )
                await route.fulfill(
                    status=404,
                    body=_ABSOLUTE_PATH_HINT,
                    content_type="application/json",
                )
                return
            rel = parts.path[len(_PREFIX) :] or "/"
            url = rel + (f"?{parts.query}" if parts.query else "")
            req = make_request(
                request.method,
                url,
                body=request.post_data_buffer or b"",
                headers=filter_headers(request.headers),
            )
            # sync + CPU-bound: run off the browser loop; dispatch
            # serializes under ws.lock so parallel fetches don't
            # reenter the sandbox (or race tool calls)
            wire = await loop.run_in_executor(None, runtime.dispatch, req)
            headers = dict(wire.headers)
            # Send the SERVED policy on HTML. Interception reproduces a
            # CSP's origin rules, but a CSP also governs BEHAVIOUR —
            # eval, new Function, blob workers, blob module scripts —
            # and none of those involve a request to intercept. Without
            # the real header those pass here and fail only once
            # published, silently: a blocked blob script never throws
            # into the page, so a try/catch around it sees nothing.
            # setdefault, like serve.py: an agent-set policy wins.
            if csp and wire.content_type.startswith("text/html"):
                headers.setdefault("content-security-policy", csp)
            await route.fulfill(
                status=wire.status,
                body=wire.content,
                content_type=wire.content_type,
                headers=headers,
            )
        elif parts.netloc in script_hosts:
            await route.continue_()
        elif parts.scheme == "https" and request.resource_type in (
            "image",
            "xhr",
            "fetch",
            "stylesheet",
            "font",
        ):
            # Mirror the serving CSP: scripts only from the allowlist,
            # but data/imagery (map tiles!) from any https host — so
            # what verifies here matches what serves published.
            await route.continue_()
        else:
            if request.resource_type == "script":
                _reject(blocked_script_note(request.url, script_hosts))
            else:
                _reject(
                    f"{request.url} -> blocked "
                    f"({request.resource_type}; https-only environment)"
                )
            await route.abort()

    # Idle-gap settling: Playwright's networkidle is STICKY — once
    # reached after navigation it resolves immediately and never waits
    # for click-triggered fetches. So we track in-flight requests and
    # wait for a quiet gap measured from settle() entry.
    import time as _time

    net = {"inflight": 0, "last": 0.0}

    def _track_start(_req: Any) -> None:
        net["inflight"] += 1
        net["last"] = _time.monotonic()

    def _track_end(_req: Any) -> None:
        net["inflight"] = max(0, net["inflight"] - 1)
        net["last"] = _time.monotonic()

    async def settle(page: Any, gap: float = 0.3) -> str | None:
        """Wait for network quiet; returns None when settled, or a
        stale-risk note when the cap expired first. A cap exit means
        the page was still busy — the one case where a following
        read/screenshot is genuinely untrustworthy, so it's surfaced
        on the action result instead of silently swallowed."""
        start = _time.monotonic()
        while _time.monotonic() - start < settle_cap:
            quiet_since = max(net["last"], start)
            if net["inflight"] == 0 and _time.monotonic() - quiet_since >= gap:
                return None
            await page.wait_for_timeout(25)
        n = net["inflight"]
        detail = (
            f"{n} request(s) still in flight"
            if n
            else "network activity never went quiet"
        )
        return (
            f"page did not settle within {settle_cap:.1f}s ({detail}); "
            'results may be stale -- prefer {"assert": ...} (it retries)'
        )

    async with sema:
        context = await browser.new_context(viewport=vp)
        try:

            def _page_error(e: Any) -> None:
                # Collect raw here and render later: describing an error
                # well means reading the agent's source file, and this
                # callback runs ON the browser loop-thread, where taking
                # ws.lock would stall every other test_app sharing it.
                page_error_records.append(
                    (
                        getattr(e, "name", "") or "Error",
                        getattr(e, "message", None) or str(e),
                        getattr(e, "stack", "") or "",
                    )
                )

            # A CSP violation surfaces in the console as browser prose,
            # which is the wrong shape for the agent's repair loop and
            # easy to lose in a chatty tail. Capture the structured
            # event instead and render it beside the harness's own
            # refusals, where the agent already looks.
            await context.add_init_script(
                "document.addEventListener('securitypolicyviolation', e => {"
                "  (window.__nt_csp = window.__nt_csp || []).push("
                "    [e.effectiveDirective || e.violatedDirective,"
                "     e.blockedURI || '(inline)']);"
                "});"
            )
            page = await context.new_page()
            page.on("console", _console)
            page.on("pageerror", _page_error)
            page.on("request", _track_start)
            page.on("requestfinished", _track_end)
            page.on("requestfailed", _track_end)
            await context.route("**/*", route_handler)

            try:
                await page.goto(_BASE_URL, timeout=load_timeout_ms)
                await settle(page)
            except Exception as e:
                await _collect_csp(page)
                return TestAppResult(
                    ok=False,
                    console=_console_lines(),
                    page_errors=await _rendered_errors(),
                    rejected=tuple(rejected),
                    load_error=str(e),
                )

            ok = True
            for i, action in enumerate(actions or []):
                try:
                    value: str | None = None
                    note: str | None = None
                    if "click" in action:
                        await page.click(action["click"], timeout=5_000)
                        note = await settle(page)
                    elif "type" in action:
                        sel, text = action["type"]
                        try:
                            await page.fill(sel, text, timeout=5_000)
                        except Exception as e:
                            # Playwright's own message ("Element is not
                            # an <input>...") names the problem but not
                            # the fix, and agents rediscover the
                            # dispatchEvent('change') workaround instead
                            # of reaching for the action below.
                            if "not an <input>" in str(e) or "<select>" in str(e):
                                raise ValueError(
                                    f"{sel} is a <select> — use "
                                    f'{{"select": [{sel!r}, value]}}, not "type"'
                                ) from e
                            raise
                        note = await settle(page)
                    elif "select" in action:
                        sel, val = action["select"]
                        try:
                            await page.select_option(sel, val, timeout=5_000)
                        except Exception:
                            # Agents pass whichever of value/label the
                            # DOM showed them; falling back to the
                            # visible label keeps that from becoming
                            # another guess-and-retry cycle.
                            try:
                                await page.select_option(sel, label=val, timeout=5_000)
                            except Exception as e:
                                raise ValueError(
                                    f"{sel}: no option matching {val!r} by "
                                    f"value or by visible label ({e})"
                                ) from e
                        note = await settle(page)
                    elif "read" in action:
                        # Settle first: a fetch that STARTED after the
                        # previous action's settle returned (debounce,
                        # setTimeout) would otherwise be read as stale
                        # DOM — the false-green an agent can't catch.
                        note = await settle(page)
                        value = await page.text_content(action["read"], timeout=5_000)
                    elif "eval" in action:
                        value = repr(await page.evaluate(action["eval"]))
                    elif "assert" in action:
                        # Web-first assertion: retry the predicate until
                        # truthy or timeout. Polled from HERE rather than
                        # with page.wait_for_function, which installs its
                        # poller into the page and needs `unsafe-eval` —
                        # blocked by the CSP this harness now sends, so
                        # every retry died and an app that merely settles
                        # asynchronously failed. page.evaluate goes over
                        # CDP instead and is not subject to page policy.
                        passed, why = await _poll_assert(
                            page, action["assert"], assert_timeout_ms
                        )
                        results.append(
                            ActionResult(
                                i, action, ok=passed, value=str(passed), error=why
                            )
                        )
                        ok = ok and passed
                        continue
                    elif "screenshot" in action:
                        if shot_counter >= max_screenshots:
                            # Soft skip, not a failure: hitting the cap
                            # must not abort the test — later actions
                            # (especially asserts) still run and count.
                            results.append(
                                ActionResult(
                                    i,
                                    action,
                                    ok=True,
                                    error=(
                                        "skipped: screenshot cap "
                                        f"({max_screenshots}) reached"
                                    ),
                                )
                            )
                            continue
                        shot_counter += 1
                        png = await page.screenshot()
                        path = (
                            f"{runtime._app_root}/screenshots/shot-{shot_counter}.png"
                        )
                        # off the loop AND under the dispatch lock (ws.fs
                        # is shared with executor-hopped dispatch)
                        await loop.run_in_executor(
                            None, _save_screenshot, runtime, path, png
                        )
                        screenshots.append(path)
                        value = path
                    elif "wait" in action:
                        await page.wait_for_timeout(int(action["wait"]))
                    else:
                        raise ValueError(f"unknown action: {action!r}")
                    results.append(
                        ActionResult(i, action, ok=True, value=value, error=note)
                    )
                except Exception as e:
                    results.append(ActionResult(i, action, ok=False, error=str(e)))
                    ok = False
                    break  # later actions depend on earlier ones

            await _collect_csp(page)
            return TestAppResult(
                ok=ok and not page_error_records and not csp_blocked_code,
                results=tuple(results),
                console=_console_lines(),
                page_errors=await _rendered_errors(),
                screenshots=tuple(screenshots),
                rejected=tuple(rejected),
            )
        finally:
            await context.close()


def _require_playwright() -> None:
    try:
        import playwright.async_api  # noqa: F401
    except ImportError as e:
        raise ImportError(
            "test_app requires the apps extra: pip install nontainer[apps] "
            "&& playwright install chromium"
        ) from e


def _submit(runtime: "AppRuntime", actions, kwargs):
    from .browser import submit_job

    return submit_job(
        lambda browser, sema: _run_actions(browser, sema, runtime, actions, **kwargs)
    )


def _flush_log(runtime: "AppRuntime") -> None:
    """A run's request lines buffer while the workspace is clean, so a
    read-only GET can't cost the next mutating request its rollback
    (see ``AppRuntime._flush_if_free``). The end of a run is the safe
    moment to write them: the agent's next move is to read the log."""
    try:
        runtime.flush_log()
    except Exception:  # diagnostics must never fail a verification run
        pass


def run_test_app(
    runtime: "AppRuntime",
    actions: list[dict[str, Any]] | None = None,
    **kwargs: Any,
) -> TestAppResult:
    """Blocking entry: submit to the shared browser and wait. A
    browser/launch failure comes back as ``load_error`` (test_app never
    raises for app problems); a missing package raises ImportError."""
    _require_playwright()
    try:
        return _submit(runtime, actions, kwargs).result()
    except Exception as e:  # launch/worker failure → a result, not a raise
        return TestAppResult(
            ok=False, load_error=f"Playwright/Chromium unavailable: {e}"
        )
    finally:
        _flush_log(runtime)


async def arun_test_app(
    runtime: "AppRuntime",
    actions: list[dict[str, Any]] | None = None,
    **kwargs: Any,
) -> TestAppResult:
    """Async entry for event-loop hosts (MCP): awaits the browser-loop
    Future without burning a waiting thread."""
    _require_playwright()
    try:
        return await asyncio.wrap_future(_submit(runtime, actions, kwargs))
    except Exception as e:
        return TestAppResult(
            ok=False, load_error=f"Playwright/Chromium unavailable: {e}"
        )
    finally:
        _flush_log(runtime)


def render_test_app(result: TestAppResult) -> str:
    """Observation rendering for adapters (paths, never bytes)."""
    parts: list[str] = [f"test_app: {'PASS' if result.ok else 'FAIL'}"]
    if result.load_error:
        parts.append(f"[load error] {result.load_error}")
    for r in result.results:
        if isinstance(r.action, dict) and r.action:
            key, val = next(iter(r.action.items()))
            desc = f"{key}({val!r})"
        else:
            desc = repr(r.action)
        line = f"  {r.index}. {desc}: {'ok' if r.ok else 'FAILED'}"
        if r.value not in (None, "None"):
            line += f" -> {r.value}"
        if r.error:
            line += f" [{r.error}]"
        parts.append(line)
    if result.screenshots:
        parts.append(f"screenshots: {', '.join(result.screenshots)}")
    if result.rejected:
        parts.append("[rejected requests]\n" + "\n".join(result.rejected))
    if result.page_errors:
        parts.append("[page errors]\n" + "\n".join(result.page_errors))
    if result.console:
        tail = result.console[-10:]
        parts.append("[console tail]\n" + "\n".join(tail))
    return "\n".join(parts)
