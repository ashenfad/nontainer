"""The ``ws-vitest`` harness: JavaScript unit tests in a headless
browser, against the files the workspace holds.

Runs on the same :class:`~nontainer.apps.driver.AppDriver` ``test_app``
uses, with a spec of its own — so the default rung needs the ``[apps]``
extra (playwright) plus ``playwright install chromium``, and shares
that one browser.

Two things make this a spec of its own rather than a ``test_app``
flag.

**The synthetic root has two trees under it.** ``app/`` and ``tests/``
are served as siblings::

    https://nontainer.test/apps/t-unit/            the harness page
    https://nontainer.test/apps/t-unit/__nt/harness.js
    https://nontainer.test/apps/t-unit/app/util.js
    https://nontainer.test/apps/t-unit/tests/util.test.js

so ``import { add } from '../app/util.js'`` resolves from a test file —
the import an agent writes anyway. test_app's base is the *app* root,
where ``tests/`` has no URL at all.

**The run is hermetic.** No api routes come up, nothing outside the
synthetic origin is reachable, and the policy on the wire is stricter
than the app's own: ``connect-src 'self'`` rather than ``'self'
https:``. A unit test that silently reaches the network passes for the
wrong reason and fails in production, so a forgotten stub fails on the
fetch — which is the outcome that teaches. The policy is written here
because ``AppsConfig.csp_extend`` adds sources and never removes them,
so no configuration path tightens the derived one.

The browser itself is not this module's business. A run is one
:class:`~nontainer.apps.driver.DriveSpec` — the harness page and the
files under test as a serve callable, one ``eval`` action awaiting the
run's promise — handed to the same driver ``test_app`` uses. No port to
bind, no server to reap. The result comes back through the page's own
evaluation, which goes over the browser's control channel and is not
subject to the page's policy.

The browser lives on the HOST on every rung. On a VM rung the guest
never sees the test file; the workspace filesystem is read here.
"""

from __future__ import annotations

import json
import posixpath
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, unquote

from ..wspytest import TestFrame, TestOutcome
from .contract import WireResponse
from .dispatch import _content_type
from .driver import ActionOutcome, DriveReport, DriveSpec, Refusal, pick_driver
from .testapp import parse_frames

_HOST = "nontainer.test"
_TOKEN = "t-unit"
_PREFIX = f"/apps/{_TOKEN}"
BASE_URL = f"https://{_HOST}{_PREFIX}/"

#: Where the harness module is served from, under the synthetic root.
HARNESS_PATH = "__nt/harness.js"

#: The trees served under the synthetic root, in workspace coordinates.
SERVED_DIRS = ("app", "tests")

#: The subtree no static path may reach — ``app/api/`` is backend source
#: and the app's own static dispatch refuses it too.
API_DIR = "app/api"

#: The policy the harness page is served under. Stricter than the app's
#: on purpose: ``connect-src 'self'`` is what makes a forgotten fetch
#: stub fail here instead of quietly reaching a real host, and
#: ``'unsafe-inline'`` is what the inline import map and the inline
#: module entry need. ``AppsConfig.csp_extend`` appends sources and
#: never removes them, so the derived app policy cannot be narrowed to
#: this; the unit tier writes its own.
UNIT_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "connect-src 'self'; "
    "img-src 'self' data:; "
    "base-uri 'none'; "
    "form-action 'none'"
)

#: A request the driver refuses, as JSON — a test that calls
#: ``res.json()`` without checking ``.ok`` reads the reason instead of a
#: second, misleading parse error.
_HERMETIC_BODY = json.dumps(
    {
        "error": (
            "ws-vitest: no api routes are up. A unit test reaches the files "
            "under test and nothing else, so a forgotten stub fails here "
            "rather than passing for the wrong reason. Stub the boundary: "
            "vi.stubFetch({'api/scores': {scores: []}})."
        )
    }
).encode()

_NOT_FOUND_BODY = json.dumps(
    {
        "error": (
            "ws-vitest: not found. The harness serves app/ and tests/ as "
            "siblings under one root, and nothing else."
        )
    }
).encode()

#: Cap on a quoted source line, and on the file a line is quoted from.
_MAX_QUOTED_LINE = 200
_MAX_ANNOTATED_BYTES = 512_000


# ---------------------------------------------------------------------------
# the harness: vitest's surface, as page JavaScript
#
# Two package data files, not two Python strings — JavaScript and HTML
# read as themselves, lint as themselves, and are diffed as themselves.
# ---------------------------------------------------------------------------

#: The harness module, served to the page at ``__nt/harness.js``. It is
#: a real ``.js`` file beside this one — shipped WITH the package, which
#: is the only sense in which it is vendored: a browser loads it from
#: the workspace's own origin and never from a CDN. Keeping it in a
#: Python string would cost it its syntax highlighting, its linter and
#: any chance of being opened as what it is.
HARNESS_FILE = "harness.js"

#: The harness page. One inline import map (so ``from 'vitest'`` and
#: ``from '@jest/globals'`` both resolve to the harness) and one inline
#: module entry that imports the test file and parks the run's promise
#: on ``window``. Those two stay INLINE — that is what ``'unsafe-inline'``
#: in the unit policy is for, and what the measured zero-violation load
#: depends on. The harness itself is a file, so ``'self'`` covers it.
PAGE_FILE = "harness.html"

_ASSETS: dict[str, bytes] = {}


def asset(name: str) -> bytes:
    """One of this package's data files, read once.

    ``importlib.resources`` rather than ``__file__`` arithmetic, so the
    bytes are found the same way whether the package is a directory on
    disk, an installed wheel or a zip import.
    """
    if name not in _ASSETS:
        from importlib import resources

        _ASSETS[name] = resources.files("nontainer.apps").joinpath(name).read_bytes()
    return _ASSETS[name]


def harness_js() -> bytes:
    """The harness module as it goes on the wire."""
    return asset(HARNESS_FILE)


def page_html(rel: str, options: dict[str, Any]) -> bytes:
    """The harness page for one test file: the import map, and the entry
    that imports it. ``rel`` is workspace-relative (``tests/x.test.js``),
    which is also its path under the synthetic root."""
    spec = "/".join(quote(part, safe="") for part in rel.split("/"))
    return (
        asset(PAGE_FILE)
        .replace(b"__SPEC__", spec.encode())
        .replace(b"__OPTS__", json.dumps(options).encode())
    )


# ---------------------------------------------------------------------------
# results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FileResult:
    """One test file's run. ``collection_error`` is a file that threw
    before any test could run — an import that failed, a bad specifier,
    a ``vi.mock`` at module scope — which is the JavaScript spelling of
    the thing pytest calls a collection error."""

    file: str
    outcomes: tuple[TestOutcome, ...] = ()
    duration: float = 0.0
    collected: int = 0
    """Tests the file defined, before ``-t`` narrowed them."""
    console: tuple[str, ...] = ()
    violations: tuple[str, ...] = ()
    """What the page was refused: a Content-Security-Policy violation,
    or a request the driver aborted."""
    collection_error: str | None = None
    load_error: str | None = None
    """The harness page itself did not come up — a browser problem, not
    the agent's code."""
    frames: tuple[TestFrame, ...] = ()
    """The collection error's own frames, where it had any."""

    @property
    def failed(self) -> bool:
        return (
            self.collection_error is not None
            or self.load_error is not None
            or any(o.status != "passed" for o in self.outcomes)
        )


# ---------------------------------------------------------------------------
# stack frames, in workspace coordinates
# ---------------------------------------------------------------------------


class _Sources:
    """Test and module files, read once each, for the lines a report
    shows. Resolved against the WORKSPACE root: the harness serves
    ``app/`` and ``tests/`` as siblings, so a frame in a test file has no
    meaning under the app root."""

    def __init__(self, ws: Any):
        self._ws = ws
        self._lines: dict[str, list[str]] = {}

    def lines(self, rel: str) -> list[str]:
        if rel not in self._lines:
            text = ""
            try:
                path = _abs(self._ws, rel)
                fs = self._ws.files.fs
                if fs.exists(path) and fs.isfile(path):
                    data = fs.read(path)
                    if len(data) <= _MAX_ANNOTATED_BYTES:
                        text = data.decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001 — a diagnostic never breaks a run
                text = ""
            self._lines[rel] = text.splitlines()
        return self._lines[rel]

    def line(self, rel: str, no: int) -> str:
        lines = self.lines(rel)
        if not 0 < no <= len(lines):
            return ""
        text = lines[no - 1]
        return text[:_MAX_QUOTED_LINE] if len(text) > _MAX_QUOTED_LINE else text


def _abs(ws: Any, rel: str) -> str:
    root = "" if ws.root == "/" else ws.root.rstrip("/")
    return f"{root}/{rel.lstrip('/')}"


def workspace_frames(stack: str, sources: "_Sources") -> list[TestFrame]:
    """A V8 stack as frames in workspace coordinates.

    A frame is kept when it names a file the agent wrote — something
    under ``app/`` or ``tests/`` on the synthetic origin. The harness
    module and the page's own inline entry are plumbing, and a report
    that shows them teaches the agent to read past it.
    """
    out: list[TestFrame] = []
    for frame in parse_frames(stack or ""):
        url = frame.url
        if not url.startswith(BASE_URL):
            continue
        rel = unquote(url[len(BASE_URL) :].split("?", 1)[0].split("#", 1)[0])
        if not any(rel == d or rel.startswith(d + "/") for d in SERVED_DIRS):
            continue
        out.append(
            TestFrame(
                path=rel,
                line=frame.line,
                function=frame.fn or "",
                source=((frame.line, sources.line(rel, frame.line)),),
                column=frame.col,
            )
        )
    return out


def render_frames(frames: list[TestFrame] | tuple[TestFrame, ...]) -> str:
    """A failure's frames in vitest's stack shape: the innermost first,
    each naming ``file:line:column`` with the line it happened on."""
    out: list[str] = []
    for frame in frames:
        where = f"{frame.path}:{frame.line}:{frame.column}"
        out.append(f" ❯ {where}" + (f" {frame.function}" if frame.function else ""))
        text = frame.source[-1][1] if frame.source else ""
        if text.strip():
            out.append(f"   {frame.line}| {text.rstrip()}")
    return "\n".join(out)


def _message(error: dict) -> str:
    """One page-side error as a report message, in workspace
    coordinates: the synthetic origin is an implementation detail of the
    harness, and a message naming it sends the agent looking for a host
    that does not exist.

    ``also`` carries the errors a failure stood in front of — the
    teardown hooks that ran after the one that failed and threw too.
    They are named under it rather than dropped, since each is its own
    repair."""
    name = error.get("name") or "Error"
    text = error.get("message")
    text = "" if text is None else str(text)
    head = f"{name}: {text}".rstrip(": ").replace(BASE_URL, "")
    return "\n".join([head, *(f"also: {_message(e)}" for e in error.get("also") or ())])


def _unhandled(rel: str, error: dict, sources: "_Sources") -> TestOutcome:
    """An error no test was running to be charged with. It is the
    file's, not a test's — naming it after whichever test happened to be
    next would send the repair to the wrong place."""
    frames = workspace_frames(error.get("stack") or "", sources)
    message = _message(error)
    return TestOutcome(
        name=f"{rel} > (unhandled error)",
        file=rel,
        line=frames[0].line if frames else None,
        status="error",
        message=message,
        traceback=render_frames(frames) or message,
        frames=tuple(frames),
    )


def _outcome(rel: str, record: dict, sources: "_Sources") -> TestOutcome:
    """One page-side result as a report outcome."""
    name = f"{rel} > {record['name']}"
    duration = float(record.get("ms") or 0.0) / 1000.0
    error = record.get("error")
    if not error:
        return TestOutcome(
            name=name, file=rel, line=None, status="passed", duration=duration
        )
    frames = workspace_frames(error.get("stack") or "", sources)
    message = _message(error)
    return TestOutcome(
        name=name,
        file=rel,
        line=frames[0].line if frames else None,
        status="failed",
        duration=duration,
        message=message,
        traceback=render_frames(frames) or None,
        frames=tuple(frames),
    )


# ---------------------------------------------------------------------------
# the driver
# ---------------------------------------------------------------------------


def _served(ws: Any, rel: str) -> tuple[bytes, str] | None:
    """The bytes for a path under the synthetic root, or ``None`` when
    nothing there is served. ``app/api/`` is refused with everything
    else: it is backend source, and the app's own static dispatch
    refuses that subtree too.

    Reads the workspace WITHOUT taking its lock, and must: this runs on
    a worker thread of the browser's loop, and the run already holds the
    lock on the thread that started it (``run_js_tests``). Acquiring an
    RLock held by a different thread would simply hang."""
    clean = posixpath.normpath(rel)
    if clean in (".", "") or clean.startswith(".."):
        return None
    if clean == API_DIR or clean.startswith(API_DIR + "/"):
        return None
    if not any(clean == d or clean.startswith(d + "/") for d in SERVED_DIRS):
        return None
    path = _abs(ws, clean)
    fs = ws.files.fs
    if not fs.exists(path) or not fs.isfile(path):
        return None
    return fs.read(path), _content_type(clean)


def _serve(ws: Any, rel: str, name: str | None, misses: list[str]) -> Any:
    """How the harness page and the files under test reach the browser.

    The trees are served as they stand — no api routes come up, and a
    path that names nothing is remembered as well as answered: a module
    import that 404s surfaces in the page as "Failed to fetch
    dynamically imported module" and names the IMPORTER, never the file
    that was missing.
    """

    def serve(method: str, url: str, body: bytes, headers: Any) -> WireResponse:
        # Percent-DECODED before anything looks it up: a space, a `#`
        # or a non-ASCII character rides the wire encoded, and matching
        # the encoded spelling against a filename reports a file that is
        # right there as missing. `_served` normalizes and confines what
        # comes out, so a decoded separator cannot escape the two trees.
        rest = unquote(url.split("?", 1)[0].split("#", 1)[0].lstrip("/"))
        if rest in ("", "index.html"):
            return WireResponse(
                200,
                page_html(rel, {"name": name} if name else {}),
                "text/html; charset=utf-8",
                {"content-security-policy": UNIT_CSP},
            )
        if rest == HARNESS_PATH:
            return WireResponse(200, harness_js(), "text/javascript; charset=utf-8")
        if rest == "api" or rest.startswith("api/"):
            return WireResponse(404, _HERMETIC_BODY, "application/json")
        found = _served(ws, rest)
        if found is None:
            if rest not in misses and len(misses) < 20:
                misses.append(rest)
            return WireResponse(404, _NOT_FOUND_BODY, "application/json")
        content, content_type = found
        return WireResponse(200, content, content_type)

    return serve


def _spec(
    ws: Any,
    rel: str,
    misses: list[str],
    *,
    name: str | None,
    load_timeout_ms: int,
    run_timeout_ms: int,
) -> DriveSpec:
    """One test file's run, as a drive: load the harness page, then
    await the run's promise.

    Hermetic by construction — no script hosts, no open resource types,
    and a policy of ``'self'`` — so a forgotten stub fails on the fetch
    rather than passing for the wrong reason. Nothing settles either:
    the page makes its own requests and the run is over when the
    harness says so, not when the network goes quiet.
    """
    return DriveSpec(
        serve=_serve(ws, rel, name, misses),
        base_url=BASE_URL,
        actions=({"eval": _AWAIT_RUN},),
        csp=UNIT_CSP,
        eval_timeout_ms=run_timeout_ms,
        load_timeout_ms=load_timeout_ms,
        settle_cap_ms=0,
        console_limit=50,
        off_base_status=404,
        off_base_body=_HERMETIC_BODY.decode(),
    )


def _payload(outcome: ActionOutcome | None, run_timeout_ms: int) -> dict:
    """The harness's own report, or an error standing in for it."""

    def failed(message: str) -> dict:
        return {
            "results": [],
            "collected": 0,
            "error": {"name": "Error", "message": message, "stack": ""},
        }

    if outcome is None:
        return failed("the run produced no result")
    if outcome.timed_out:
        return failed(
            f"the run did not finish within {run_timeout_ms}ms — a test "
            "returned a promise that never settled"
        )
    if not outcome.ok:
        return failed(outcome.error or "the run did not produce a result")
    if not isinstance(outcome.value, dict):
        return failed(f"the harness answered with {outcome.value!r}")
    return outcome.value


def _refusal_note(refusal: Refusal) -> str:
    """One refusal in the unit tier's own words: everything here points
    at the same repair, which is to stub the boundary."""
    if refusal.kind == "policy":
        return (
            f"{refusal.url} -> blocked by the ws-vitest policy "
            f"({refusal.directive}). A unit run reaches the files under "
            "test and nothing else."
        )
    return (
        f"{refusal.url} -> blocked ({refusal.resource_type}): a "
        "ws-vitest run reaches the files under test and nothing "
        "else. Stub the boundary with vi.stubFetch."
    )


def _result(
    ws: Any,
    rel: str,
    report: DriveReport,
    misses: list[str],
    run_timeout_ms: int,
) -> FileResult:
    """One report as this verb's report contract."""
    console = _lines(dict(report.console))
    violations = tuple(
        dict.fromkeys(_refusal_note(r) for r in report.refusals if r.kind != "off-base")
    )
    if report.load_error is not None:
        return FileResult(
            file=rel,
            duration=report.duration,
            console=console,
            violations=violations,
            load_error=report.load_error,
        )
    sources = _Sources(ws)
    payload = _payload(report.actions[0] if report.actions else None, run_timeout_ms)
    error = payload.get("error")
    if error:
        frames = workspace_frames(error.get("stack") or "", sources)
        message = _message(error)
        if misses and "dynamically imported module" in message:
            message += " — nothing is served at " + ", ".join(misses)
        return FileResult(
            file=rel,
            duration=report.duration,
            console=console,
            violations=violations,
            collection_error=message,
            frames=tuple(frames),
        )
    outcomes = [_outcome(rel, record, sources) for record in payload["results"]]
    # What no test was running to be charged with: a module-scope
    # rejection, a timer that outlived the test that set it. The
    # driver's own page errors are the backstop for anything the page's
    # listeners could not attribute, and are used only when the harness
    # accounted for nothing — otherwise the same failure would be
    # reported twice.
    unattributed = list(payload.get("stray") or ())
    if not unattributed and not any(o.status != "passed" for o in outcomes):
        unattributed = [
            {"name": e.name, "message": e.message, "stack": e.stack}
            for e in report.page_errors
        ]
    outcomes.extend(_unhandled(rel, record, sources) for record in unattributed)
    return FileResult(
        file=rel,
        outcomes=tuple(outcomes),
        duration=report.duration,
        collected=int(payload.get("collected") or 0),
        console=console,
        violations=violations,
    )


#: Await the harness's promise, giving the module entry a moment to park
#: it. A page whose harness never evaluated (a served file that would not
#: parse) has no promise at all, and says so rather than hanging.
_AWAIT_RUN = """async () => {
  for (let i = 0; i < 200 && !window.__nt_done; i++) {
    await new Promise((r) => setTimeout(r, 10));
  }
  if (!window.__nt_done) {
    return {results: [], collected: 0, error: {
      name: 'Error',
      message: 'the harness page did not start (nothing evaluated the entry module)',
      stack: '',
    }};
  }
  return await window.__nt_done;
}"""


def _lines(console: dict[str, int]) -> tuple[str, ...]:
    return tuple(line if n == 1 else f"{line} (x{n})" for line, n in console.items())


def require_playwright() -> None:
    try:
        import playwright.async_api  # noqa: F401
    except ImportError as e:
        raise ImportError(
            "ws-vitest requires the apps extra: pip install nontainer[apps] "
            "&& playwright install chromium"
        ) from e


def run_js_tests(
    ws: Any,
    files: Any,
    *,
    name: str | None = None,
    bail: int = 0,
    load_timeout_ms: int = 15_000,
    run_timeout_ms: int = 30_000,
) -> list[FileResult]:
    """Run each test file in the headless browser and report what
    happened, one :class:`FileResult` per file.

    ``name`` is the ``-t`` filter, applied in the page where the test
    names are. ``bail`` stops once that many files have failed — the
    files already run are returned, and the rest are not started; 0 is
    no limit.
    """
    require_playwright()
    driver = pick_driver(workspace=ws)
    out: list[FileResult] = []
    failed = 0
    # The workspace's single-writer lock, held for the whole run on THIS
    # thread: the page is served from the tree as it stands, so nothing
    # may rewrite a module between the import that loads it and the test
    # that asserts on it. Held here rather than taken per read inside the
    # serve callable, which the driver calls on a worker thread — an
    # RLock is re-entrant for its owner and a deadlock for anyone else,
    # and the verb is typed inside an already-locked terminal call.
    with ws.lock:
        for rel in files:
            misses: list[str] = []
            spec = _spec(
                ws,
                rel,
                misses,
                name=name,
                load_timeout_ms=load_timeout_ms,
                run_timeout_ms=run_timeout_ms,
            )
            try:
                report = driver.run(spec)
            except Exception as e:  # noqa: BLE001 — a browser failure is a result
                report = DriveReport(load_error=f"{type(driver).__name__} failed: {e}")
            result = _result(ws, rel, report, misses, run_timeout_ms)
            out.append(result)
            if result.failed:
                failed += 1
                if bail and failed >= bail:
                    break
    return out
