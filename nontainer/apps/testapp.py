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

_FRAME_RE = re.compile(
    r"^\s*at\s+(?:(?P<fn>.*?)\s+\()?(?P<url>.+?):(?P<line>\d+):(?P<col>\d+)\)?\s*$"
)

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
    ``rel=None``). Lines that are not frames are skipped."""
    out: list[Frame] = []
    for raw in (stack or "").splitlines():
        m = _FRAME_RE.match(raw)
        if not m:
            continue
        out.append(
            Frame(
                raw=raw.strip(),
                fn=m.group("fn"),
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
        out += f"\n     {agent.line} | {source}"
    return out


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
    """Load succeeded, no action errored, and no assert was falsy."""

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
    script_hosts = runtime.config.script_hosts

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
            await route.fulfill(
                status=wire.status, body=wire.content, content_type=wire.content_type
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
                _reject(
                    f"{request.url} -> blocked: scripts may only load "
                    f"from the CDN allowlist ({', '.join(script_hosts)})"
                )
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
                        # Web-first assertion (THE Playwright idiom):
                        # retry the predicate until truthy or timeout.
                        try:
                            await page.wait_for_function(
                                action["assert"], timeout=assert_timeout_ms
                            )
                            passed, why = True, None
                        except Exception:
                            passed = False
                            try:
                                await page.evaluate(action["assert"])
                                why = "assertion is falsy"
                            except Exception as inner:
                                why = f"assertion errored: {inner}"
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

            return TestAppResult(
                ok=ok and not page_error_records,
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
