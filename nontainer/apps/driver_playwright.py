"""The Playwright driver: a headless Chromium on the host.

Requires the ``[apps]`` extra (playwright) plus ``playwright install
chromium``.

The workspace IS the origin: a fresh browser context intercepts every
request via ``page.route`` — anything under the spec's base URL is
answered by ``DriveSpec.serve``, which is the same dispatch the curl
builtin and the published router use. External hosts are denied except
the script-host allowlist and what the served policy permits. No port,
no server.

One Chromium is shared across all runs, on a dedicated async loop-thread
(see ``browser.py``); each run gets a fresh context, bounded by a
concurrency semaphore. ``serve`` is synchronous and CPU-bound, so it is
hopped off the browser loop into a thread — a page's parallel fetches
then serialize wherever the serve callable serializes them, rather than
inside this loop.

What this module decides is how a browser is driven. What the results
MEAN — which stack frame is the agent's, how a refusal reads, where a
screenshot is written — belongs to the caller, and none of it is here.
"""

from __future__ import annotations

import asyncio
import re
import time
from concurrent.futures import Future
from typing import Any
from urllib.parse import urlsplit

from .csp import csp_permits
from .driver import ActionOutcome, DriveReport, DriveSpec, PageError, Refusal

_ASSERT_POLL_MS = 50

#: Collects the browser's own policy refusals, which arrive as events
#: rather than as failed requests: a refused script does not throw, so
#: nothing else in the page would name it.
_CSP_INIT_SCRIPT = (
    "document.addEventListener('securitypolicyviolation', e => {"
    "  (window.__nt_csp = window.__nt_csp || []).push("
    "    [e.effectiveDirective || e.violatedDirective,"
    "     e.blockedURI || '(inline)']);"
    "});"
)

_SELECTOR_ACTIONS = ("click", "type", "select", "read")

#: Frames one grid holds. A grid is one image in the model's context
#: however many frames it tiles, so the bound is on legibility: past a
#: dozen, each frame is too small to read a layout from.
_GRID_MAX_TILES = 12
_GRID_CAPTION_PX = 30
_GRID_GAP_PX = 4


def _grid_slug(name: Any) -> str:
    """A grid name as it can appear in a file name."""
    slug = re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-")
    return slug or "grid"


def _grid_columns(n: int) -> int:
    return 1 if n == 1 else 2 if n <= 4 else 3 if n <= 9 else 4


_SCREENSHOT_RETRY_WAITS = (0.1, 0.4)
"""Seconds before each retry of a screenshot Chromium refused with
"Unable to capture screenshot"."""


async def _screenshot(page: Any, **kwargs: Any) -> bytes:
    """``page.screenshot``, with Chromium's transient refusal retried.

    Headless Chromium now and then answers a capture with "Protocol
    error (Page.captureScreenshot): Unable to capture screenshot", most
    often on a loaded machine and a fresh page: the frame is not ready
    to be read yet, and a moment later it is. Unretried, it failed an
    agent's screenshot, a grid's tiling, and CI runs at random. Only
    that error is retried, briefly; anything else is the page's or the
    caller's, and raises at once."""
    for wait in (*_SCREENSHOT_RETRY_WAITS, None):
        try:
            return await page.screenshot(**kwargs)
        except Exception as e:
            if wait is None or "Unable to capture screenshot" not in str(e):
                raise
            await asyncio.sleep(wait)
    raise AssertionError("unreachable")  # pragma: no cover


async def _tile_grid(
    browser: Any, tiles: list[tuple[str, bytes]], width: int, height: int
) -> bytes:
    """One image of ``tiles`` (``(caption, png)`` in capture order), laid
    out as many columns wide as their count needs and as wide as the
    viewport they were taken in, each captioned.

    Tiled by the browser rather than an imaging library, so the driver
    needs nothing it does not already have: the frames go onto a page as
    images and the page is captured once. A fresh context, not the run's:
    the run's routes answer every request from the app and its init
    scripts are the embedder's, and neither belongs on this page."""
    import base64
    import html

    cols = _grid_columns(len(tiles))
    tile_w = (width - _GRID_GAP_PX * (cols - 1)) // cols
    tile_h = round(tile_w * height / width)
    cells = "".join(
        '<figure><img src="data:image/png;base64,'
        + base64.b64encode(png).decode()
        + '"><figcaption>'
        + html.escape(caption)
        + "</figcaption></figure>"
        for caption, png in tiles
    )
    page_html = (
        "<!doctype html><html><head><style>"
        f"html,body{{margin:0;background:#1f2328;width:{width}px}}"
        f"body{{display:grid;grid-template-columns:repeat({cols},{tile_w}px);"
        f"gap:{_GRID_GAP_PX}px}}"
        "figure{margin:0}"
        f"img{{display:block;width:{tile_w}px;height:{tile_h}px}}"
        f"figcaption{{height:{_GRID_CAPTION_PX}px;line-height:{_GRID_CAPTION_PX}px;"
        "padding:0 8px;color:#f0f3f6;font:15px/30px ui-monospace,Menlo,monospace;"
        "white-space:nowrap;overflow:hidden;text-overflow:ellipsis}"
        "</style></head><body>" + cells + "</body></html>"
    )
    rows = -(-len(tiles) // cols)
    context = await browser.new_context(
        viewport={
            "width": width,
            "height": rows * (tile_h + _GRID_CAPTION_PX) + _GRID_GAP_PX * (rows - 1),
        },
        device_scale_factor=1,
    )
    try:
        page = await context.new_page()
        await page.set_content(page_html, wait_until="load")
        return await _screenshot(page, full_page=True)
    finally:
        await context.close()


def require_playwright() -> None:
    try:
        import playwright.async_api  # noqa: F401
    except ImportError as e:
        raise ImportError(
            "the Playwright driver requires the apps extra: pip install "
            "nontainer[apps] && playwright install chromium"
        ) from e


class PlaywrightDriver:
    """Drives a spec against the shared host Chromium."""

    def run(self, spec: DriveSpec) -> DriveReport:
        """Blocking entry: submit to the shared browser and wait. A
        browser/launch failure comes back as ``load_error`` (a driver
        reports app and browser problems rather than raising); a missing
        package raises ImportError."""
        require_playwright()
        try:
            return self._submit(spec).result()
        except Exception as e:  # launch/worker failure → a report, not a raise
            return DriveReport(load_error=f"Playwright/Chromium unavailable: {e}")

    async def arun(self, spec: DriveSpec) -> DriveReport:
        """Async entry for event-loop hosts (MCP): awaits the
        browser-loop Future without burning a waiting thread."""
        require_playwright()
        try:
            return await asyncio.wrap_future(self._submit(spec))
        except Exception as e:
            return DriveReport(load_error=f"Playwright/Chromium unavailable: {e}")

    def _submit(self, spec: DriveSpec) -> "Future[DriveReport]":
        from .browser import submit_job

        return submit_job(lambda browser, sema: _drive(browser, sema, spec))


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
    clock again — a hang with no output.
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


async def _drive(
    browser: Any, sema: "asyncio.Semaphore", spec: DriveSpec
) -> DriveReport:
    """Run one spec against a fresh context on the shared browser.
    Runs on the browser loop-thread; bounded by ``sema``."""
    started = time.perf_counter()
    base = spec.base_url
    origin = urlsplit(base)
    host, prefix = origin.netloc, origin.path.rstrip("/")
    settle_cap = spec.settle_cap_ms / 1000
    settle_gap = spec.settle_gap_ms / 1000

    # Repeated console lines are near-pure context tax: one audited
    # session spent 39% of all test_app result bytes (7,922 of 20,279)
    # on 32 copies of the same CDN warning, against a model working in
    # ~30k of context. Collapse by text, keep first-seen order, and
    # carry the count — a genuinely repeating log (a retry storm, a
    # render loop) still reads as repeating.
    console: dict[str, int] = {}
    page_errors: list[PageError] = []
    outcomes: list[ActionOutcome] = []
    screenshots: dict[str, bytes] = {}
    refusals: dict[Refusal, None] = {}  # ordered de-dupe
    shot_counter = 0
    # Frames bound for a grid, by grid name, and the path each grid's
    # tiled image will be written to. A grid takes its screenshot slot
    # when its first frame is taken, which is where its path sits in
    # capture order; the image itself is made once the run is over.
    grids: dict[str, list[tuple[str, bytes]]] = {}
    grid_paths: dict[str, str] = {}
    loop = asyncio.get_running_loop()

    def _refuse(refusal: Refusal) -> None:
        if len(refusals) < spec.refusal_limit:
            refusals.setdefault(refusal)

    def _console(message: Any) -> None:
        line = f"[{message.type}] {message.text}"
        if line in console:
            console[line] += 1
        elif len(console) < spec.console_limit:  # cap DISTINCT lines
            console[line] = 1

    def _report(loaded: bool, load_error: str | None = None) -> DriveReport:
        return DriveReport(
            loaded=loaded,
            load_error=load_error,
            actions=tuple(outcomes),
            console=tuple(console.items()),
            page_errors=tuple(page_errors),
            refusals=tuple(refusals),
            screenshots=screenshots,
            duration=time.perf_counter() - started,
        )

    async def _collect_csp(page: Any) -> None:
        """Fold the browser's own policy refusals into the report.

        These would otherwise be invisible: the block never throws into
        the page, so nothing in the page errors names it either."""
        try:
            hits = await page.evaluate("window.__nt_csp || []")
        except Exception:
            return  # page closed / navigated away: a diagnostic, not the run
        for directive, blocked in hits or ():
            _refuse(Refusal(kind="policy", url=blocked, directive=directive))

    async def route_handler(route: Any, request: Any) -> None:
        parts = urlsplit(request.url)
        if parts.netloc == host:
            if not parts.path.startswith(prefix + "/") and parts.path != prefix:
                # Answered rather than aborted, and with a body: an app
                # that calls .json() without checking .ok then reads the
                # reason instead of a second, misleading parse error.
                _refuse(Refusal(kind="off-base", url=parts.path))
                await route.fulfill(
                    status=spec.off_base_status,
                    body=spec.off_base_body.encode(),
                    content_type=spec.off_base_content_type,
                )
                return
            rel = parts.path[len(prefix) :] or "/"
            url = rel + (f"?{parts.query}" if parts.query else "")
            # sync + CPU-bound: run off the browser loop, where it can
            # serialize with the rest of the host's work
            wire = await loop.run_in_executor(
                None,
                spec.serve,
                request.method,
                url,
                request.post_data_buffer or b"",
                request.headers,
            )
            await route.fulfill(
                status=wire.status,
                body=wire.content,
                content_type=wire.content_type,
                headers=dict(wire.headers),
            )
        elif parts.netloc in spec.script_hosts:
            await route.continue_()
        elif request.resource_type != "script" and (
            (request.resource_type in spec.open_https_types and parts.scheme == "https")
            or csp_permits(spec.csp, request.resource_type, request.url)
        ):
            # Mirror the serving policy: an origin it permits (an
            # intranet http API in connect-src, a framed host in
            # frame-src) is served, so aborting it here would be a false
            # red. The open types are the second half of that — data an
            # app pulls from third parties, where the policy itself is
            # written wide.
            await route.continue_()
        else:
            kind = "script" if request.resource_type == "script" else "resource"
            _refuse(
                Refusal(kind=kind, url=request.url, resource_type=request.resource_type)
            )
            await route.abort()

    # Idle-gap settling: Playwright's networkidle is STICKY — once
    # reached after navigation it resolves immediately and never waits
    # for click-triggered fetches. So we track in-flight requests and
    # wait for a quiet gap measured from settle() entry.
    net = {"inflight": 0, "last": 0.0}

    def _track_start(_req: Any) -> None:
        net["inflight"] += 1
        net["last"] = time.monotonic()

    def _track_end(_req: Any) -> None:
        net["inflight"] = max(0, net["inflight"] - 1)
        net["last"] = time.monotonic()

    async def settle(page: Any) -> str | None:
        """Wait for network quiet; returns None when settled, or a
        stale-risk note when the cap expired first. A cap exit means
        the page was still busy — the one case where a following
        read/screenshot is genuinely untrustworthy, so it's surfaced
        on the action result instead of silently swallowed."""
        if settle_cap <= 0:
            return None
        start = time.monotonic()
        while time.monotonic() - start < settle_cap:
            quiet_since = max(net["last"], start)
            if net["inflight"] == 0 and time.monotonic() - quiet_since >= settle_gap:
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

    async def _selectors_present(page: Any) -> str:
        """What the page actually offers, for a selector that missed.

        Playwright says only what it waited for, so an agent re-guesses
        blind. A miss is the most common action failure there is."""
        try:
            found = await page.evaluate(
                "() => [...new Set(["
                "  ...[...document.querySelectorAll('[id]')].map(e => '#' + e.id),"
                "  ...[...document.querySelectorAll('[data-key]')]"
                '      .map(e => `[data-key="${e.dataset.key}"]`),'
                "])].slice(0, 25)"
            )
        except Exception:
            return ""  # a diagnostic must never replace the real error
        if not found:
            return " — the page has no element with an id or data-key"
        return " — selectors on the page: " + ", ".join(found)

    async def _capture(page: Any, label: str) -> str | None:
        """Screenshot, named the way the caller will write it. ``None``
        means the CAP was reached -- a benign skip. A genuine failure
        RAISES, because collapsing the two let an explicitly requested
        screenshot fail and be reported as "cap reached", ok=True."""
        nonlocal shot_counter
        if shot_counter >= spec.max_screenshots:
            return None
        png = await _screenshot(page)
        shot_counter += 1
        path = f"{spec.screenshot_dir}/{label}-{shot_counter}.png"
        screenshots[path] = png
        return path

    async def _capture_tile(page: Any, grid: Any, label: Any) -> tuple[str | None, str]:
        """A frame for a grid. Returns the grid's path, or ``None`` and
        why it was skipped -- the cap was reached before the grid began,
        or the grid is full. Both are soft skips, as a plain screenshot
        past the cap is."""
        nonlocal shot_counter
        name = str(grid)
        if name not in grids:
            if shot_counter >= spec.max_screenshots:
                return None, f"skipped: screenshot cap ({spec.max_screenshots}) reached"
            shot_counter += 1
            path = f"{spec.screenshot_dir}/grid-{_grid_slug(name)}-{shot_counter}.png"
            grids[name] = []
            grid_paths[name] = path
            screenshots[path] = b""  # holds its place in capture order
        tiles = grids[name]
        if len(tiles) >= _GRID_MAX_TILES:
            return None, (
                f"skipped: grid {name!r} already holds {_GRID_MAX_TILES} frames "
                "(start another grid for more)"
            )
        png = await _screenshot(page)
        tiles.append((str(label) if label is not None else str(len(tiles) + 1), png))
        return grid_paths[name], ""

    async def _compose_grids() -> None:
        """Each grid's frames as one image, at the path its frames were
        promised. A grid that cannot be tiled fails the actions that fed
        it, rather than leaving them pointing at an image that is not
        there."""
        width = spec.width or 1280
        height = spec.height or 800
        for name, tiles in grids.items():
            path = grid_paths[name]
            try:
                screenshots[path] = await _tile_grid(browser, tiles, width, height)
            except Exception as e:
                screenshots.pop(path, None)
                for j, outcome in enumerate(outcomes):
                    if outcome.value == path:
                        outcomes[j] = ActionOutcome(
                            outcome.index,
                            ok=False,
                            error=f"grid {name!r} could not be assembled: {e}",
                        )

    async with sema:
        context = await browser.new_context(
            **(
                {"viewport": {"width": spec.width, "height": spec.height}}
                if spec.width and spec.height
                else {}
            )
        )
        try:

            def _page_error(e: Any) -> None:
                # Collected raw: describing an error well means reading
                # the agent's source file, and this callback runs ON the
                # browser loop-thread, where host I/O would stall every
                # other run sharing it.
                if len(page_errors) < spec.page_error_limit:
                    page_errors.append(
                        PageError(
                            name=getattr(e, "name", "") or "Error",
                            message=getattr(e, "message", None) or str(e),
                            stack=getattr(e, "stack", "") or "",
                        )
                    )

            for script in (_CSP_INIT_SCRIPT, *spec.init_scripts):
                await context.add_init_script(script)
            page = await context.new_page()
            page.on("console", _console)
            page.on("pageerror", _page_error)
            page.on("request", _track_start)
            page.on("requestfinished", _track_end)
            page.on("requestfailed", _track_end)
            await context.route("**/*", route_handler)

            try:
                opened = await page.goto(
                    base + spec.start, timeout=spec.load_timeout_ms
                )
                # A run opened at its first goto's page owes that goto
                # the warning a busy page gets, as an ordinary goto
                # would have attached it.
                opened_note = await settle(page)
            except Exception as e:
                await _collect_csp(page)
                return _report(False, load_error=str(e))

            for i, action in enumerate(spec.actions):
                try:
                    value: Any = None
                    note: str | None = None
                    if "click" in action:
                        await page.click(
                            action["click"], timeout=spec.action_timeout_ms
                        )
                        note = await settle(page)
                    elif "type" in action:
                        sel, text = action["type"]
                        try:
                            await page.fill(sel, text, timeout=spec.action_timeout_ms)
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
                            await page.select_option(
                                sel, val, timeout=spec.action_timeout_ms
                            )
                        except Exception:
                            # Agents pass whichever of value/label the
                            # DOM showed them; falling back to the
                            # visible label keeps that from becoming
                            # another guess-and-retry cycle.
                            try:
                                await page.select_option(
                                    sel, label=val, timeout=spec.action_timeout_ms
                                )
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
                        value = await page.text_content(
                            action["read"], timeout=spec.action_timeout_ms
                        )
                    elif "eval" in action:
                        # Settle first, for the reason `read` does: an
                        # expression evaluated against a page still
                        # fetching reads stale DOM and reports it as
                        # fact. `eval` is where the questions `read`
                        # cannot express get asked -- counts,
                        # attributes, computed styles -- so it needs the
                        # guarantee more, not less.
                        note = await settle(page)
                        pending = page.evaluate(action["eval"])
                        if spec.eval_timeout_ms is None:
                            value = await pending
                        else:
                            # The deadline bounds the WHOLE await: an
                            # expression that never settles would
                            # otherwise hang with no output.
                            value = await asyncio.wait_for(
                                pending, spec.eval_timeout_ms / 1000
                            )
                    elif "assert" in action:
                        # Web-first assertion: retry the predicate until
                        # truthy or timeout. Polled from HERE rather than
                        # with page.wait_for_function, which installs its
                        # poller into the page and needs `unsafe-eval` —
                        # blocked by the policy this harness sends, so
                        # every retry died and an app that merely settles
                        # asynchronously failed. page.evaluate goes over
                        # CDP instead and is not subject to page policy.
                        passed, why = await _poll_assert(
                            page, action["assert"], spec.assert_timeout_ms
                        )
                        outcomes.append(
                            ActionOutcome(i, ok=passed, value=passed, error=why)
                        )
                        continue
                    elif "screenshot" in action and action.get("grid") is not None:
                        # One frame of a grid: every shot sharing the name
                        # comes back as ONE tiled image, so a run can look
                        # at a dozen states for the context cost of one.
                        value, skipped = await _capture_tile(
                            page, action["grid"], action.get("label")
                        )
                        if value is None:
                            outcomes.append(ActionOutcome(i, ok=True, error=skipped))
                            continue
                    elif "screenshot" in action:
                        shot = await _capture(page, "shot")
                        if shot is None:
                            # Soft skip, not a failure: hitting the cap
                            # must not abort the run — later actions
                            # (especially asserts) still run and count.
                            outcomes.append(
                                ActionOutcome(
                                    i,
                                    ok=True,
                                    error=(
                                        "skipped: screenshot cap "
                                        f"({spec.max_screenshots}) reached"
                                    ),
                                )
                            )
                            continue
                        value = shot
                    elif (
                        "goto" in action
                        and i == 0
                        and spec.start
                        and str(action["goto"]).lstrip("/") == spec.start
                    ):
                        # The run opened here (DriveSpec.start): this
                        # goto is done, and going again would load the
                        # page, and run its scripts, twice. Its status is
                        # the opening load's, held to what any goto is.
                        if opened is not None and not opened.ok:
                            raise ValueError(
                                f"goto {spec.start!r} -> HTTP {opened.status} "
                                "(the page was not served; check the filename "
                                "and that it is under the app root)"
                            )
                        value = spec.start
                        note = opened_note
                    elif "goto" in action:
                        # Policy refusals live on `window`, so harvest
                        # them before the navigation discards the page.
                        await _collect_csp(page)
                        target = str(action["goto"]).lstrip("/")
                        response = await page.goto(
                            base + target, timeout=spec.load_timeout_ms
                        )
                        # goto RESOLVES on 4xx/5xx -- it only raises for
                        # transport failures -- so without this a
                        # navigation to a missing page is a passing
                        # action on a 404 document. `response` is None
                        # for a same-document navigation, which did not
                        # fetch anything to be wrong about.
                        if response is not None and not response.ok:
                            raise ValueError(
                                f"goto {target!r} -> HTTP {response.status} "
                                "(the page was not served; check the filename "
                                "and that it is under the app root)"
                            )
                        note = await settle(page)
                        value = target
                    elif "press" in action:
                        # Real key events, through the browser's own
                        # keyboard: they go to the focused element and
                        # bubble to document and window, so a page hears
                        # them wherever it listens. A KeyboardEvent
                        # dispatched from eval reaches only the target it
                        # is dispatched on, and agents kept dispatching
                        # on document for games listening on window.
                        keys = action["press"]
                        keys = [keys] if isinstance(keys, str) else list(keys)
                        if not keys or not all(isinstance(k, str) and k for k in keys):
                            raise ValueError(
                                'press takes a key or a list of keys, like "ArrowLeft", '
                                '" " or ["ArrowUp", "Enter"] (Playwright key names)'
                            )
                        hold = int(action.get("hold", 0) or 0)
                        if hold < 0 or hold > spec.action_timeout_ms:
                            raise ValueError(
                                f"hold is milliseconds, 0 to {spec.action_timeout_ms}"
                            )
                        if action.get("on"):
                            await page.focus(
                                action["on"], timeout=spec.action_timeout_ms
                            )
                        for key in keys:
                            if hold:
                                await page.keyboard.down(key)
                                await page.wait_for_timeout(hold)
                                await page.keyboard.up(key)
                            else:
                                await page.keyboard.press(key)
                        note = await settle(page)
                        value = " ".join(keys)
                    elif "wait" in action:
                        await page.wait_for_timeout(int(action["wait"]))
                    else:
                        raise ValueError(f"unknown action: {action!r}")
                    outcomes.append(ActionOutcome(i, ok=True, value=value, error=note))
                except Exception as e:
                    # asyncio's timeout carries no message, and only the
                    # spec's own ceilings raise it here.
                    timed_out = isinstance(e, (TimeoutError, asyncio.TimeoutError))
                    detail = (
                        f"the expression did not settle within {spec.eval_timeout_ms}ms"
                        if timed_out and spec.eval_timeout_ms is not None
                        else str(e)
                    )
                    selector = next(
                        (action[k] for k in _SELECTOR_ACTIONS if k in action), None
                    )
                    if isinstance(selector, (list, tuple)):
                        selector = selector[0]
                    if selector and "waiting for locator" in detail:
                        detail += await _selectors_present(page)
                    # The run stops here (later actions depend on this
                    # one), so this is the last look at the page there
                    # will be. Without it the agent re-runs the whole
                    # test just to add a screenshot and see the state.
                    try:
                        shot = await _capture(page, "failure")
                    except Exception:
                        shot = None  # a diagnostic must never replace the error
                    if shot:
                        detail += f"\n  page at failure: {shot}"
                    outcomes.append(
                        ActionOutcome(i, ok=False, error=detail, timed_out=timed_out)
                    )
                    break  # later actions depend on earlier ones

            await _collect_csp(page)
        finally:
            await context.close()
        # After a failure too: the frames taken before it are what the
        # agent will want to look at. The app's context is closed first,
        # so tiling takes this run's slot of the semaphore rather than a
        # second context on top of it: the concurrency limit is a bound
        # on open contexts, which is what costs the browser memory.
        await _compose_grids()
        return _report(True)
