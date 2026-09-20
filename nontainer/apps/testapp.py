"""test_app: headless verification of the agent's app.

Two halves meet here. This one is neutral: what an action MEANS, which
frame of a stack is the agent's, how a refusal reads, where a
screenshot is written, what makes a run PASS. The other drives a
browser, behind :class:`~nontainer.apps.driver.AppDriver` — one
``run(spec) -> report`` call, with the default driver a headless
Chromium on the host (``driver_playwright.py``, the ``[apps]`` extra
plus ``playwright install chromium``).

The workspace IS the origin: the driver answers every request from the
app's own ``dispatch`` — the same one the curl builtin and the
published router use — and denies external hosts except the script-host
allowlist (``AppsConfig.script_hosts``, default esm.sh and friends for
the no-build frontend tiers, the same declaration the served CSP
derives from). No port, no server.

Relocatability is enforced here by construction (docs/apps.md): the
app is served under a synthetic prefix (``/apps/t-test/``), so a
frontend that hardcodes absolute URLs (``fetch('/api/x')``) gets an
instructive 404 during verification instead of breaking at delivery.

Screenshots come back as bytes and are written to
``<root>/app/screenshots/`` once the run is over, returned as paths —
bytes never ride in model-facing observations, and the screenshots
version, fork and check out with the session.

Locking: nothing here holds ``ws.lock`` across a run. Each dispatched
request takes it inside ``AppRuntime.dispatch`` (so a page's parallel
fetches don't reenter the sandbox, and can't race ordinary tool calls),
and the reads and writes that follow the run — the source line behind a
stack frame, the screenshot files — take it one at a time.
"""

from __future__ import annotations

import asyncio
import posixpath
import re
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from .contract import filter_headers, filter_response_headers, make_request
from .csp import (
    blocked_script_note,
    blocks_code,
    csp_directive_for,
    csp_note,
    csp_script_origins,
)
from .driver import (
    ActionOutcome,
    DriveReport,
    DriveSpec,
    PageError,
    Refusal,
    adrive,
    pick_driver,
)

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
    '{"error": "nontainer: absolute path -- apps are served under a '
    "prefix and must use RELATIVE urls (fetch('api/x'), not "
    "fetch('/api/x'))\"}"
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


def _line_reader(runtime: "AppRuntime") -> Any:
    """``(rel, lineno) -> source line | None``, read from the workspace.

    Called off the browser loop-thread (see the call site): it takes the
    workspace lock, which the route dispatch also holds."""

    def read_line(rel: str, lineno: int) -> str | None:
        ws = runtime.workspace
        path = f"{runtime._app_root}/{rel}"
        try:
            with ws.lock:
                if not ws.files.fs.exists(path) or not ws.files.fs.isfile(path):
                    return None
                data = ws.files.fs.read(path)
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
    runtime: "AppRuntime", records: tuple[PageError, ...]
) -> tuple[str, ...]:
    """Render the page errors a run collected, reading the agent's own
    source for the offending line. Done once the run is over: a driver
    reports a stack unparsed because deciding which frame matters means
    reading the workspace, which is the host's to do and not the
    browser's."""
    prefixes = _asset_prefixes(runtime)
    read_line = _line_reader(runtime)
    return tuple(
        describe_page_error(
            error.name,
            error.message,
            error.stack,
            asset_prefixes=prefixes,
            read_line=read_line,
        )
        for error in records[:20]
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
    """Workspace paths under ``<root>/app/screenshots/``."""

    rejected: tuple[str, ...] = ()
    """Requests the harness refused (absolute paths, blocked scripts),
    each with WHY and the fix — the browser console only shows the
    symptom (a truncated JSON parse error, an anonymous ERR_FAILED)."""

    load_error: str | None = None

    def __bool__(self) -> bool:
        return self.ok


def _save_screenshot(runtime: "AppRuntime", path: str, png: bytes) -> None:
    """Write a screenshot to the workspace fs — off the browser loop and
    under the workspace's single-writer lock, since ``ws.files.fs`` is shared
    with the executor-hopped route dispatch (which serializes under the
    same lock inside ``AppRuntime.dispatch``)."""
    ws = runtime.workspace
    with ws.lock:
        ws.files.fs.makedirs(f"{runtime._app_root}/screenshots", exist_ok=True)
        ws.files.fs.write(path, png)


# ---------------------------------------------------------------------------
# the drive: a spec out, a report back
# ---------------------------------------------------------------------------

#: Resource types a run accepts from ANY https host, over and above what
#: the served policy names. Data apps legitimately pull map tiles,
#: remote imagery and third-party APIs, and the derived policy is
#: written wide for exactly those — so verification has to be too, or an
#: app verifies red and serves fine.
_OPEN_HTTPS_TYPES = ("image", "xhr", "fetch", "stylesheet", "font")


def _serve(runtime: "AppRuntime", csp: str) -> Any:
    """How the app is served for a run: the same ``dispatch`` the curl
    builtin and the published router use. This is the invariant the
    driver seam is for — what verifies green is served by the code that
    will serve the visitor."""

    def serve(method: str, url: str, body: bytes, headers: Any) -> Any:
        wire = runtime.dispatch(
            make_request(method, url, body=body, headers=filter_headers(headers))
        )
        # Verification must see the wire the router would produce, so
        # the same response-header allowlist applies: a handler setting
        # Set-Cookie or Access-Control-Allow-Origin has to fail HERE
        # rather than depend on a header it will never be served.
        out = filter_response_headers(wire.headers)
        # Send the SERVED policy on HTML. Interception reproduces a
        # policy's ORIGIN rules, but a policy also governs BEHAVIOUR —
        # eval, new Function, blob workers, blob module scripts — and
        # none of those involve a request to intercept. Without the real
        # header those pass here and fail only once published, silently:
        # a blocked blob script never throws into the page, so a
        # try/catch around it sees nothing. It is the CONFIGURED policy,
        # assigned rather than deferred, because that is what the app
        # will be served.
        if csp and wire.content_type.startswith("text/html"):
            out["content-security-policy"] = csp
        return replace(wire, headers=out)

    return serve


def build_spec(
    runtime: "AppRuntime",
    actions: list[dict[str, Any]] | None,
    *,
    viewport: str | dict[str, int] = "desktop",
    max_screenshots: int = 5,
    load_timeout_ms: int = 10_000,
    assert_timeout_ms: int = 2_000,
    settle_cap: float = 5.0,
) -> DriveSpec:
    """Everything one run has been decided to be, before a browser is
    involved."""
    vp = (
        VIEWPORTS.get(viewport, VIEWPORTS["desktop"])
        if isinstance(viewport, str)
        else {
            "width": int(viewport.get("width", 1280)),
            "height": int(viewport.get("height", 800)),
        }
    )
    # One declaration (AppsConfig.script_hosts) drives interception AND
    # the served CSP: what verifies headlessly matches what serves.
    from .serve import resolve_csp

    csp = resolve_csp(runtime.config)
    # Interception must agree with the policy actually enforced. With
    # the derived policy these are the same list; with a custom one, its
    # script origins join in, so a host the served policy allows is not
    # aborted (a false red, and the same divergence pointed the other
    # way).
    script_hosts = tuple(
        dict.fromkeys((*runtime.config.script_hosts, *csp_script_origins(csp)))
    )
    return DriveSpec(
        serve=_serve(runtime, csp),
        base_url=_BASE_URL,
        actions=tuple(actions or ()),
        width=vp["width"],
        height=vp["height"],
        csp=csp,
        script_hosts=script_hosts,
        open_https_types=_OPEN_HTTPS_TYPES,
        load_timeout_ms=load_timeout_ms,
        assert_timeout_ms=assert_timeout_ms,
        settle_cap_ms=int(settle_cap * 1000),
        max_screenshots=max_screenshots,
        screenshot_dir=f"{runtime._app_root}/screenshots",
        off_base_body=_ABSOLUTE_PATH_HINT,
    )


def _refusal_note(refusal: Refusal, spec: DriveSpec) -> str:
    """One thing the page did not get, phrased as the fix. The browser
    console only shows the symptom (a truncated JSON parse error, an
    anonymous ERR_FAILED)."""
    if refusal.kind == "off-base":
        return (
            f"{refusal.url} -> 404: absolute path (apps serve under "
            "a prefix — use relative URLs: fetch('api/x'), not "
            "fetch('/api/x'))"
        )
    if refusal.kind == "policy":
        return csp_note(refusal.directive, refusal.url, spec.script_hosts)
    if refusal.kind == "script":
        return blocked_script_note(refusal.url, spec.script_hosts)
    if spec.csp:
        # Name the directive that would have to allow it: the fix is a
        # source on that directive, and a note saying only "blocked"
        # sends the repair looking at the app.
        return (
            f"{refusal.url} -> blocked ({refusal.resource_type}: not "
            "permitted by the served policy's "
            f"{csp_directive_for(refusal.resource_type)}; https hosts "
            "are allowed, anything else needs AppsConfig.csp_extend "
            "or csp)"
        )
    return f"{refusal.url} -> blocked ({refusal.resource_type}; https-only environment)"


def _action_value(action: dict[str, Any], outcome: ActionOutcome) -> str | None:
    """The action's result as the agent reads it. ``eval`` is repr'd
    because it answers with a VALUE — a count, an attribute, a computed
    style — and ``'2'`` and ``2`` are different answers."""
    if "assert" in action:
        return str(outcome.value)
    if "eval" in action:
        return repr(outcome.value)
    value = outcome.value
    return value if value is None or isinstance(value, str) else str(value)


def _write_screenshots(runtime: "AppRuntime", report: DriveReport) -> tuple[str, ...]:
    """Persist the report's captures under ``<root>/app/screenshots/``,
    in capture order. Bytes never ride in model-facing observations, and
    the files version, fork and check out with the session."""
    for path, png in report.screenshots.items():
        _save_screenshot(runtime, path, png)
    return tuple(report.screenshots)


def build_result(
    runtime: "AppRuntime", spec: DriveSpec, report: DriveReport
) -> TestAppResult:
    """One report, read against the workspace it ran on: stacks
    annotated with the agent's own source, refusals phrased as fixes,
    screenshots written, and the one verdict the agent acts on."""
    rejected: dict[str, None] = {}  # ordered de-dupe
    blocked_code = False  # a policy refusal that stopped code running
    relocatable = True  # an absolute url is always an app bug
    for refusal in report.refusals:
        if refusal.kind == "off-base":
            relocatable = False
        elif refusal.kind == "policy" and blocks_code(refusal.directive):
            blocked_code = True
        rejected.setdefault(_refusal_note(refusal, spec))
    screenshots = _write_screenshots(runtime, report)
    page_errors = _annotate_page_errors(runtime, report.page_errors)
    console = tuple(line if n == 1 else f"{line} (x{n})" for line, n in report.console)
    if report.load_error is not None:
        return TestAppResult(
            ok=False,
            console=console,
            page_errors=page_errors,
            rejected=tuple(rejected),
            load_error=report.load_error,
        )
    results = tuple(
        ActionResult(
            outcome.index,
            spec.actions[outcome.index],
            ok=outcome.ok,
            value=_action_value(spec.actions[outcome.index], outcome),
            error=outcome.error,
        )
        for outcome in report.actions
    )
    return TestAppResult(
        # A run whose code was refused is not a PASS: reporting a
        # refused script while still printing PASS would leave the false
        # green intact one layer up.
        ok=all(r.ok for r in results)
        and not page_errors
        and not blocked_code
        and relocatable,
        results=results,
        console=console,
        page_errors=page_errors,
        screenshots=screenshots,
        rejected=tuple(rejected),
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


def _driver_failed(driver: Any, error: Exception) -> DriveReport:
    """A driver that could not run at all is a load error, not a raise:
    test_app answers with a result for app problems and for browser
    problems alike."""
    return DriveReport(load_error=f"{type(driver).__name__} failed: {error}")


def run_test_app(
    runtime: "AppRuntime",
    actions: list[dict[str, Any]] | None = None,
    **kwargs: Any,
) -> TestAppResult:
    """Blocking entry: drive the app and read the report against the
    workspace. A browser/launch failure comes back as ``load_error``
    (test_app never raises for app problems); a driver whose dependency
    is missing raises ImportError."""
    driver = pick_driver(runtime.config, runtime.workspace)
    spec = build_spec(runtime, actions, **kwargs)
    try:
        try:
            report = driver.run(spec)
        except ImportError:
            raise
        except Exception as e:
            report = _driver_failed(driver, e)
        return build_result(runtime, spec, report)
    finally:
        _flush_log(runtime)


async def arun_test_app(
    runtime: "AppRuntime",
    actions: list[dict[str, Any]] | None = None,
    **kwargs: Any,
) -> TestAppResult:
    """Async entry for event-loop hosts (MCP): awaits the drive without
    burning a waiting thread, and reads the report in a thread —
    assembly touches the workspace under its lock, which is not the
    caller's loop's business to block on."""
    driver = pick_driver(runtime.config, runtime.workspace)
    spec = build_spec(runtime, actions, **kwargs)
    try:
        try:
            report = await adrive(driver, spec)
        except ImportError:
            raise
        except Exception as e:
            report = _driver_failed(driver, e)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, build_result, runtime, spec, report)
    finally:
        _flush_log(runtime)


def _console_excerpt(console: tuple[str, ...], tail: int = 10) -> list[str]:
    """The last few lines, plus any earlier error/warning they buried.

    A chatty page pushes the one line that explains the failure out of a
    plain tail — and console noise is exactly what a chatty page has a
    lot of. Errors keep their chronological position; they are simply
    not allowed to fall off the end."""
    kept = list(console[-tail:])
    earlier = [
        line
        for line in console[:-tail]
        if line.startswith(("[error]", "[warning]")) and line not in kept
    ]
    if not earlier:
        return kept
    promoted = earlier[-5:]
    # Count what is actually missing from the output, not what fell out
    # of the tail: the promoted lines are on screen. A diagnostic that
    # overstates its own elision is one an agent cannot reason from.
    omitted = len(console) - len(kept) - len(promoted)
    middle = [f"… {omitted} more lines …"] if omitted > 0 else []
    return [*promoted, *middle, *kept]


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
        parts.append("[console tail]\n" + "\n".join(_console_excerpt(result.console)))
    return "\n".join(parts)
