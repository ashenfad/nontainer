# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Pre-1.0; the API is still moving. Notable changes since the initial cut:

### Added
- **Safe stdlib by default** — `PythonConfig(stdlib=True)` grants a
  curated stdlib set (see `nontainer.presets.STDLIB`), so a plain
  workspace's Python can `import math`/`json`/`csv`/... out of the box.
- **Module-grant presets** — `nontainer.presets.dataframes()` (numpy +
  pandas) and `plotting()` (matplotlib Agg-pinned + font cache warmed;
  plotly optional). `ModuleGrant` gains `include`/`exclude`/`recursive`/
  `name`; `PythonConfig.modules` flattens preset lists one level.
- **Results pin their commit** — `TerminalResult`/`PythonResult`/
  `EditOutcome` carry `checkpoint` (the commit the call produced, or
  `None`); `write_file`/`put` return a `WriteOutcome`; `ws.head` /
  `ws.dirty` pin the state a read-only call observed.
- **Async host facades** — `ws.aterminal` / `ws.arun_python` run the
  sync execution in a thread so event-loop hosts (FastAPI, etc.) stay
  responsive; the agent surface is unchanged.
- **Shared browser for `test_app`** — one Chromium across all calls
  (async Playwright on a dedicated loop-thread), a context per
  concurrent test bounded by a semaphore (`configure_browser`), plus
  `arun_test_app` and `shutdown_browser`. Memory scales with
  concurrency, not sessions.
- **`py.typed`** — the package now ships its PEP 561 marker.
- **Tool primers** — `WorkspaceTools`/`build_server` accept
  `terminal_primer` / `python_primer`: embedder guidance appended to the
  respective tool's description (e.g. "`db` is a SQLite store — use it,
  not `cache`, for shared state"). Strict 1-to-1 with the exposed tools;
  a `python_primer` in terminal-only mode lands in the terminal tool's
  `python` section (with a warning).

- **Faithful `sys` in terminal `python`** — piped input reaches the code
  as `sys.stdin` (`cat data | python script.py`), and `sys.argv` /
  `input()` work, via sandtrap's synthetic safe `sys`. No `import`
  quoting workarounds; dangerous `sys` internals stay unreachable.

### Changed
- **Workspace enforces its single-writer invariant internally.**
  Mutating public calls (`terminal`, `run_python`, `write_file`,
  `edit_file`, `put`, `checkpoint`, `restore`, `rollback`, `discard`,
  `fork`, `close`) hold an internal `RLock`, so a harness that threads
  parallel tool calls onto one session serializes safely — each call
  atomic + checkpointed — instead of corrupting staged state. Custom
  harnesses no longer need to supply their own lock (the adapters keep
  theirs as a fence for adapter-level work). Read-only accessors stay
  lock-free; host-side escape hatches (`ws.fs` writes, `ws.cache`
  mutation) bypass the lock and remain the caller's concurrency
  problem. RLock so a `host_object` that calls back into the public
  API serializes instead of deadlocking.
- **stderr capture is per-execution, not a process-global redirect.**
  `run_python` stderr now comes from sandtrap 0.2.4's ContextVar-routed
  capture (`ExecResult.stderr`): concurrent executions — other sessions
  in the same process, frozen app serving — no longer cross-contaminate
  stderr or risk leaving `sys.stderr` pointing at a dead buffer. The
  internal `capture_stderr` escape hatch is gone; served (frozen) app
  handlers get stderr capture back. Sandboxed `sys.stderr` writes in
  the terminal `python` builtin now surface as stderr instead of
  leaking into pipeline stdout.
- **Live app serving is now frozen (read-only) snapshots.** `build_router`
  serves a Workspace pinned to a published commit: handlers read the VFS
  and call `host_objects` but can't mutate it (write → 500). This makes
  serving **concurrent** (fresh read-only sandbox per request, no
  per-session lock, no staged buffer, no checkpointing) and lossless to
  evict. Mutable app state belongs in an external store via
  `host_objects`. Removed: per-session serialization, quiesce
  checkpointing, `queue_depth`/`quiesce_seconds`. Added: `max_snapshots`,
  `on_log` (handler logs route off the read-only VFS; default: the
  `nontainer.apps` logger). `AppRuntime(..., frozen=True, log_sink=...)`.
  The router is **stateless** — `resolve → dispatch`, no snapshot cache,
  no residency/lifecycle (cache inside `resolve` if it's expensive; the
  router doesn't close its result). Rate limiting is an edge concern;
  `rate_limit_per_min`/`max_snapshots`/`queue_depth` are gone.

### Fixed
- **App static serving path traversal** — `.`/`..` segments can no
  longer escape `/app/`, and backend source under `/app/api/` is never
  served as a static file.

### Changed
- Requires **sandtrap ≥ 0.2.4** (per-execution stderr capture;
  recursive-registration filter propagation, dotted patterns,
  synthetic `sys`/stdin) and **monkeyfs ≥ 0.1.5**
  (`VirtualFS.invalidate()`).
