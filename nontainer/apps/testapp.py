"""test_app: headless verification of the agent's app. Requires the
``[apps]`` extra (playwright) plus ``playwright install chromium``.

The workspace IS the origin: a fresh browser context intercepts every
request via ``page.route`` — static paths and ``/api/*`` are answered
by the same ``dispatch`` the curl builtin uses; external hosts are
denied except a small CDN allowlist (esm.sh and friends, for the
no-build frontend tiers). No port, no server.

Relocatability is enforced here by construction (docs/apps.md): the
app is served under a synthetic prefix (``/apps/t-test/``), so a
frontend that hardcodes absolute URLs (``fetch('/api/x')``) gets an
instructive 404 during verification instead of breaking at delivery.

Screenshots are written to ``/app/screenshots/`` in the workspace and
returned as paths — bytes never ride in model-facing observations,
and the screenshots version/fork/roll back with the session.

Implementation note: Playwright's sync API is thread-bound, so M2
launches a browser per call (~300ms) rather than sharing one across
calls. Fine at verification cadence; a persistent browser thread is a
recorded optimization, not a design change.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from .contract import make_request

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

_DEFAULT_CDN_ALLOWLIST = ("esm.sh", "unpkg.com", "cdn.jsdelivr.net")

_ABSOLUTE_PATH_HINT = (
    b"nontainer: absolute path -- apps are served under a prefix and must "
    b"use RELATIVE urls (fetch('api/x'), not fetch('/api/x'))"
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

    load_error: str | None = None

    def __bool__(self) -> bool:
        return self.ok


def run_test_app(
    runtime: "AppRuntime",
    actions: list[dict[str, Any]] | None = None,
    *,
    viewport: str | dict[str, int] = "desktop",
    cdn_allowlist: tuple[str, ...] = _DEFAULT_CDN_ALLOWLIST,
    max_screenshots: int = 5,
    load_timeout_ms: int = 10_000,
    assert_timeout_ms: int = 2_000,
) -> TestAppResult:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise ImportError(
            "test_app requires the apps extra: pip install nontainer[apps] "
            "&& playwright install chromium"
        ) from e

    ws = runtime._ws
    vp = VIEWPORTS.get(viewport, VIEWPORTS["desktop"]) if isinstance(
        viewport, str
    ) else {"width": int(viewport.get("width", 1280)),
            "height": int(viewport.get("height", 800))}

    console: list[str] = []
    page_errors: list[str] = []
    results: list[ActionResult] = []
    screenshots: list[str] = []
    shot_counter = 0

    def route_handler(route: Any, request: Any) -> None:
        parts = urlsplit(request.url)
        if parts.netloc == _HOST:
            if not parts.path.startswith(_PREFIX + "/") and parts.path != _PREFIX:
                route.fulfill(status=404, body=_ABSOLUTE_PATH_HINT,
                              content_type="text/plain")
                return
            rel = parts.path[len(_PREFIX):] or "/"
            url = rel + (f"?{parts.query}" if parts.query else "")
            wire = runtime.dispatch(
                make_request(
                    request.method,
                    url,
                    body=request.post_data_buffer or b"",
                    headers={},
                )
            )
            route.fulfill(status=wire.status, body=wire.content,
                          content_type=wire.content_type)
        elif parts.netloc in cdn_allowlist:
            route.continue_()
        else:
            route.abort()

    # Idle-gap settling (ported from agex-studio's app-control design).
    # Playwright's networkidle is STICKY — once reached after navigation
    # it resolves immediately and never waits for click-triggered
    # fetches. So we track in-flight requests ourselves and wait for a
    # quiet gap measured from settle() entry: a click's async handler
    # gets `gap` ms to fire its first fetch; request chains keep
    # extending the window; the cap bounds slow apps (use {"wait": ms}
    # for known-slow paths).
    import time as _time

    net = {"inflight": 0, "last": 0.0}

    def _track_start(_req: Any) -> None:
        net["inflight"] += 1
        net["last"] = _time.monotonic()

    def _track_end(_req: Any) -> None:
        net["inflight"] = max(0, net["inflight"] - 1)
        net["last"] = _time.monotonic()

    def settle(page: Any, gap: float = 0.3, cap: float = 5.0) -> None:
        start = _time.monotonic()
        while _time.monotonic() - start < cap:
            quiet_since = max(net["last"], start)
            if net["inflight"] == 0 and _time.monotonic() - quiet_since >= gap:
                return
            page.wait_for_timeout(25)

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            context = browser.new_context(viewport=vp)
            page = context.new_page()
            page.on("console", lambda m: console.append(f"[{m.type}] {m.text}"))
            page.on("pageerror", lambda e: page_errors.append(str(e)))
            page.on("request", _track_start)
            page.on("requestfinished", _track_end)
            page.on("requestfailed", _track_end)
            context.route("**/*", route_handler)

            try:
                page.goto(_BASE_URL, timeout=load_timeout_ms)
                settle(page)
            except Exception as e:
                return TestAppResult(
                    ok=False,
                    console=tuple(console[:100]),
                    page_errors=tuple(page_errors[:20]),
                    load_error=str(e),
                )

            ok = True
            for i, action in enumerate(actions or []):
                try:
                    value: str | None = None
                    if "click" in action:
                        page.click(action["click"], timeout=5_000)
                        settle(page)
                    elif "type" in action:
                        sel, text = action["type"]
                        page.fill(sel, text, timeout=5_000)
                        settle(page)
                    elif "read" in action:
                        value = page.text_content(
                            action["read"], timeout=5_000
                        )
                    elif "eval" in action:
                        value = repr(page.evaluate(action["eval"]))
                    elif "assert" in action:
                        # Web-first assertion (THE Playwright idiom):
                        # retry the predicate until truthy or timeout —
                        # click→assert is robust independent of settle.
                        try:
                            page.wait_for_function(
                                action["assert"], timeout=assert_timeout_ms
                            )
                            passed, why = True, None
                        except Exception:
                            passed = False
                            try:
                                page.evaluate(action["assert"])
                                why = "assertion is falsy"
                            except Exception as inner:
                                why = f"assertion errored: {inner}"
                        results.append(ActionResult(
                            i, action, ok=passed,
                            value=str(passed),
                            error=why,
                        ))
                        ok = ok and passed
                        continue
                    elif "screenshot" in action:
                        if shot_counter >= max_screenshots:
                            raise RuntimeError(
                                f"screenshot cap ({max_screenshots}) reached"
                            )
                        shot_counter += 1
                        png = page.screenshot()
                        ws.fs.makedirs("/app/screenshots", exist_ok=True)
                        path = f"/app/screenshots/shot-{shot_counter}.png"
                        ws.fs.write(path, png)
                        screenshots.append(path)
                        value = path
                    elif "wait" in action:
                        page.wait_for_timeout(int(action["wait"]))
                    else:
                        raise ValueError(f"unknown action: {action!r}")
                    results.append(ActionResult(i, action, ok=True, value=value))
                except Exception as e:
                    results.append(ActionResult(i, action, ok=False, error=str(e)))
                    ok = False
                    break  # later actions depend on earlier ones

            return TestAppResult(
                ok=ok and not page_errors,
                results=tuple(results),
                console=tuple(console[:100]),
                page_errors=tuple(page_errors[:20]),
                screenshots=tuple(screenshots),
            )
        finally:
            browser.close()


def render_test_app(result: TestAppResult) -> str:
    """Observation rendering for adapters (paths, never bytes)."""
    parts: list[str] = [f"test_app: {'PASS' if result.ok else 'FAIL'}"]
    if result.load_error:
        parts.append(f"[load error] {result.load_error}")
    for r in result.results:
        desc = next(iter(r.action.items()))
        line = f"  {r.index}. {desc[0]}({desc[1]!r}): {'ok' if r.ok else 'FAILED'}"
        if r.value not in (None, "None"):
            line += f" -> {r.value}"
        if r.error:
            line += f" [{r.error}]"
        parts.append(line)
    if result.screenshots:
        parts.append(f"screenshots: {', '.join(result.screenshots)}")
    if result.page_errors:
        parts.append("[page errors]\n" + "\n".join(result.page_errors))
    if result.console:
        tail = result.console[-10:]
        parts.append("[console tail]\n" + "\n".join(tail))
    return "\n".join(parts)
