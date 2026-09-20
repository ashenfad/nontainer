"""The seam an app is verified across: spec in, report out.

Verification has two halves. One is neutral — what the actions mean,
which frame of a stack is the agent's, how a refusal reads, where a
screenshot is written. The other drives a browser. This module names
the line between them so a second driver can be written without moving
the first half anywhere.

::

    class AppDriver(Protocol):
        def run(self, spec: DriveSpec) -> DriveReport: ...

The invariant that makes the seam worth having: **the driver's
dispatcher is the dispatcher the publication will use.** A driver is
handed ``DriveSpec.serve``, and everything the page asks for comes back
through it, so what verifies green is served by the same code that will
serve the visitor.

Two rules keep drivers interchangeable:

- **A batch, not a conversation.** A driver receives the whole action
  list and returns one result per action. There is no per-primitive
  interface to implement, because a driver on the far side of a socket
  or a postMessage bridge could not satisfy one.
- **One coordinate system.** A report names files, requests and stack
  frames in ``DriveSpec.base_url`` terms, whatever origin the driver
  actually served from, so the neutral half can attribute a frame to a
  workspace file without knowing which driver ran.

The report comes back RAW: unparsed stacks, unphrased refusals, console
lines with their repeat counts, screenshot bytes. Interpretation — and
every word the agent reads — belongs to the caller.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol, runtime_checkable

Serve = Callable[[str, str, bytes, "Mapping[str, str]"], Any]
"""How the app is served: ``(method, url, body, headers)`` answered with
a :class:`~nontainer.apps.contract.WireResponse`.

``url`` is relative to :attr:`DriveSpec.base_url` and keeps its leading
slash (``/api/scores?limit=3``); a request that leaves the base never
reaches here (see :attr:`DriveSpec.off_base_body`). The call is
synchronous and may be CPU-bound — a driver runs it off whatever loop
it drives the page from."""


@dataclass(frozen=True)
class DriveSpec:
    """One run, as the neutral half has already decided it."""

    serve: Serve
    base_url: str
    """Where the app is served, as URLs in the report will spell it."""

    actions: tuple[dict[str, Any], ...] = ()
    """The coerced action list, in order. A driver stops at the first
    failure: later actions depend on earlier ones."""

    width: int | None = None
    height: int | None = None
    """Viewport, already resolved from any preset. ``None`` leaves the
    driver's own default."""

    csp: str = ""
    """The policy served on the app's HTML, and the policy request
    interception must agree with. Empty means no policy at all."""

    script_hosts: tuple[str, ...] = ()
    """Hosts executable code may load from. Everything else is refused,
    whatever the policy says about other resource types."""

    open_https_types: tuple[str, ...] = ()
    """Resource types allowed from ANY https host on top of what the
    policy permits — the data an app legitimately pulls from third
    parties (map tiles, imagery, an API). Empty is a hermetic run: only
    the app's own origin and what the policy names."""

    init_scripts: tuple[str, ...] = ()
    """JavaScript evaluated in every document before the page's own
    code runs."""

    load_timeout_ms: int = 10_000
    action_timeout_ms: int = 5_000
    assert_timeout_ms: int = 2_000
    """Budget for one ``assert``, which is retried until it passes."""

    eval_timeout_ms: int | None = None
    """Ceiling on one ``eval``, whose expression may await. ``None``
    leaves it unbounded."""

    settle_gap_ms: int = 300
    settle_cap_ms: int = 5_000
    """Network quiet a driver waits for after an action, and how long it
    waits before giving up and saying so. ``0`` disables settling: a run
    whose page fetches nothing pays nothing for it."""

    max_screenshots: int = 0
    screenshot_dir: str = ""
    """Where captured pages are NAMED (the caller writes the bytes):
    a screenshot comes back keyed ``<screenshot_dir>/<label>-<n>.png``,
    which is also the path an action result quotes."""

    console_limit: int = 100
    """Cap on DISTINCT console lines. Repeats are free — they collapse
    into a count."""

    refusal_limit: int = 20
    page_error_limit: int = 20

    off_base_status: int = 404
    off_base_body: str = ""
    off_base_content_type: str = "application/json"
    """The answer to a request that leaves ``base_url`` while staying on
    its origin — an app that hardcoded an absolute path. It is answered
    rather than aborted so the app's own error path can display it, and
    reported as a refusal for the caller to phrase."""


@dataclass(frozen=True)
class ActionOutcome:
    """One action's raw result. ``value`` is whatever the action
    produced, undescribed: text for a read, the evaluated value for an
    eval, a path for a screenshot.

    ``error`` on a successful action is a NOTE — the page never went
    quiet, a cap was reached — not a failure."""

    index: int
    ok: bool
    value: Any = None
    error: str | None = None
    timed_out: bool = False
    """The action hit a ceiling the spec set rather than answering.
    Distinct from any other failure, and worth saying: a caller phrases
    "nothing came back in time" differently from "it came back wrong"."""


@dataclass(frozen=True)
class PageError:
    """An uncaught page-side error, stack unparsed: which frame matters
    is decided against the workspace, which the driver cannot read."""

    name: str
    message: str
    stack: str


@dataclass(frozen=True)
class Refusal:
    """Something the page asked for and did not get, unphrased.

    ``kind`` is ``"off-base"`` (an absolute path that left the app's
    prefix), ``"script"`` (code from a host outside the allowlist),
    ``"resource"`` (anything else the interception refused) or
    ``"policy"`` (the browser's own Content-Security-Policy refusal,
    which names a ``directive``)."""

    kind: str
    url: str
    resource_type: str = ""
    directive: str = ""


@dataclass(frozen=True)
class DriveReport:
    """What one run saw."""

    loaded: bool = False
    load_error: str | None = None
    """The page never came up; the actions never ran."""

    actions: tuple[ActionOutcome, ...] = ()
    console: tuple[tuple[str, int], ...] = ()
    """``(line, repeats)`` in first-seen order."""

    page_errors: tuple[PageError, ...] = ()
    refusals: tuple[Refusal, ...] = ()
    """In the order they happened, de-duplicated."""

    screenshots: Mapping[str, bytes] = field(default_factory=dict)
    """PNG bytes keyed by the path :attr:`DriveSpec.screenshot_dir`
    implies, in capture order. The caller writes them."""

    duration: float = 0.0


@runtime_checkable
class AppDriver(Protocol):
    """Runs one :class:`DriveSpec` and reports what happened.

    ``run`` is synchronous to its caller. A driver that is natively
    asynchronous may also offer ``async def arun(spec)``; :func:`adrive`
    uses it when it exists and otherwise keeps a loop free by running
    ``run`` in a thread.

    A driver reports app problems — a page that would not load, an
    action that failed — in the report. It raises only when it cannot
    run at all.
    """

    def run(self, spec: DriveSpec) -> DriveReport: ...


async def adrive(driver: Any, spec: DriveSpec) -> DriveReport:
    """Drive without blocking the calling event loop."""
    arun = getattr(driver, "arun", None)
    if arun is not None:
        return await arun(spec)
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, driver.run, spec)


def pick_driver(config: Any = None, workspace: Any = None) -> Any:
    """The driver a run uses: the config's, else the executor's, else
    Playwright on the host.

    An executor offers one through ``Runtime.app_driver`` — the rung
    that runs the app is the rung that can verify it the way a visitor
    will see it, and a rung that cannot says so by offering nothing.
    """
    driver = getattr(config, "driver", None)
    if driver is None and workspace is not None:
        driver = getattr(workspace.runtime, "app_driver", None)
    if driver is None:
        from .driver_playwright import PlaywrightDriver

        driver = PlaywrightDriver()
    return driver
