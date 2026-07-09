"""Shared-browser worker for test_app.

Sync Playwright pins a browser to the thread that created it, so
concurrency would cost one browser per thread. Async Playwright drives
many contexts concurrently from a single event loop — so we run async
Playwright on one dedicated background thread and marshal jobs onto it.
Result: one Chromium process, one context per *concurrent* test
(semaphore-bounded), memory scaling with concurrency rather than with
the number of sessions.

Public surface:
- ``submit_job(make_coro) -> Future`` — run a job on the browser loop;
  ``make_coro(browser, semaphore)`` returns the awaitable that does the
  work. Sync callers ``.result()``; async callers ``asyncio.wrap_future``.
- ``configure_browser(max_concurrent=...)`` — before first use.
- ``shutdown_browser()`` — tears the browser + loop down (also atexit).
"""

from __future__ import annotations

import asyncio
import atexit
import threading
from concurrent.futures import Future
from typing import Any, Awaitable, Callable

_DEFAULT_MAX_CONCURRENT = 8

# The job factory: given the live browser and the concurrency semaphore,
# return the coroutine that runs one test against a fresh context.
JobFactory = Callable[[Any, "asyncio.Semaphore"], Awaitable[Any]]


class _BrowserWorker:
    """One Chromium on one dedicated asyncio loop-thread."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._browser: Any = None
        self._playwright: Any = None
        self._pw_ctx: Any = None
        self._sema: asyncio.Semaphore | None = None
        self._launch_lock: asyncio.Lock | None = None
        self._max_concurrent = _DEFAULT_MAX_CONCURRENT
        self._start_lock = threading.Lock()
        self.launches = 0  # observable for tests (relaunch counting)

    # -- configuration -------------------------------------------------

    def configure(self, *, max_concurrent: int) -> None:
        if self._loop is not None:
            raise RuntimeError(
                "configure_browser() must be called before the first test_app"
            )
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be >= 1")
        self._max_concurrent = max_concurrent

    # -- lifecycle -----------------------------------------------------

    def _ensure_started(self) -> asyncio.AbstractEventLoop:
        if self._loop is not None:
            return self._loop
        with self._start_lock:
            if self._loop is not None:
                return self._loop
            loop = asyncio.new_event_loop()
            ready = threading.Event()

            def run() -> None:
                asyncio.set_event_loop(loop)
                self._sema = asyncio.Semaphore(self._max_concurrent)
                self._launch_lock = asyncio.Lock()
                ready.set()
                try:
                    loop.run_forever()
                finally:
                    loop.close()  # release selector/fds; shutdown discards it

            t = threading.Thread(
                target=run, name="nontainer-browser", daemon=True
            )
            t.start()
            ready.wait()
            self._loop = loop
            self._thread = t
            atexit.register(self.shutdown)
            return loop

    async def _ensure_browser(self) -> Any:
        """(Re)launch the browser if needed. Runs on the loop thread.
        The launch lock keeps concurrent jobs from racing two launches;
        a crashed browser is transparently replaced."""
        assert self._launch_lock is not None
        async with self._launch_lock:
            if self._browser is not None and self._browser.is_connected():
                return self._browser
            if self._playwright is None:
                from playwright.async_api import async_playwright

                self._pw_ctx = async_playwright()
                self._playwright = await self._pw_ctx.__aenter__()
            self._browser = await self._playwright.chromium.launch()
            self.launches += 1
            return self._browser

    def submit(self, make_coro: JobFactory) -> Future:
        loop = self._ensure_started()

        async def job() -> Any:
            browser = await self._ensure_browser()
            return await make_coro(browser, self._sema)

        return asyncio.run_coroutine_threadsafe(job(), loop)

    def shutdown(self) -> None:
        loop = self._loop
        if loop is None:
            return

        async def _close() -> None:
            if self._browser is not None:
                try:
                    await self._browser.close()
                except Exception:
                    pass
            if self._pw_ctx is not None:
                try:
                    await self._pw_ctx.__aexit__(None, None, None)
                except Exception:
                    pass

        # Short deadlines: a healthy browser closes in milliseconds, and
        # when it's wedged, waiting longer helps nobody — this runs at
        # interpreter exit (atexit) and must not stall shutdown.
        try:
            asyncio.run_coroutine_threadsafe(_close(), loop).result(timeout=3)
        except Exception:
            pass
        loop.call_soon_threadsafe(loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._loop = self._thread = None
        self._browser = self._playwright = self._pw_ctx = None
        self._sema = self._launch_lock = None


_worker = _BrowserWorker()


def configure_browser(*, max_concurrent: int = _DEFAULT_MAX_CONCURRENT) -> None:
    """Set the ceiling on concurrent test_app contexts (default 8).
    Must be called before the first test_app runs.

    PROCESS-GLOBAL, first-caller-wins: there is one shared Chromium
    (and one config) per process — the right shape for the standard
    one-embedder deployment. Concurrent *sessions* are fine (each
    test_app gets its own isolated browser context); what can't
    coexist is two independent embedders in one process wanting
    different configs. Calling this after the browser started raises
    rather than silently ignoring the setting."""
    _worker.configure(max_concurrent=max_concurrent)


def submit_job(make_coro: JobFactory) -> Future:
    """Run a job on the shared browser loop; returns a Future."""
    return _worker.submit(make_coro)


def shutdown_browser() -> None:
    """Close the browser and stop the loop-thread (idempotent; also
    runs at interpreter exit)."""
    _worker.shutdown()
