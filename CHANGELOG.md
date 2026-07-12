# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Pre-1.0; the API is still moving. Notable changes since the initial cut:

### Added
- **`python3` terminal alias.** The reserved `python` bridge now also
  answers to `python3` — the reflex spelling agents type first. Both
  names are reserved against user command injection.
- **`warnings` in the STDLIB preset.** `warn`, `filterwarnings`,
  `simplefilter`, and `catch_warnings` are granted — agents reach for
  `warnings.filterwarnings("ignore")` the moment pandas/sklearn start
  emitting deprecation noise, and the module was imported by the
  presets but never granted.
- **Artifact channels: binary in, images and files out.** Three
  pieces close the "artifacts are stranded in the workspace" gap:
  a `view_image` tool in both adapters (the agent views a saved
  plot/chart — returned as real image content for vision models;
  png/jpeg/gif/webp, 10MB cap); MCP **resources** exposing every
  workspace file as `workspace://{path}` (text as text, binary as
  blob) with a `workspace://-/tree` index — the client-side window
  for extracting what the agent produced; and a `--mount
  POINT=DIR[:rw]` flag on the MCP CLI (read-only by default) — the
  inbound channel for seeding real host files without base64 games.
  `file_write` results additionally carry a ground-truth
  `ResourceLink` to the written file (the link exists because the
  write succeeded), and the MCP tool descriptions coach the agent to
  mention `workspace://` URIs when it produces artifacts.
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

### Added
- **Workspace extension surface: `exec_python` / `build_sandbox` /
  `lock`.** A small, documented contract for embedders composing
  execution features on top of the workspace: `exec_python(code, *,
  inputs, sandbox, cache, stdin, argv)` is the raw execution path (no
  checkpoint, no lock; `cache=` overrides the agent-visible cache —
  the old private `_UNSET` sentinel is gone); `build_sandbox(*,
  timeout, tick_limit, extra_classes, filesystem)` mints per-purpose
  sandboxes sharing the frozen config, memoizing the built `Policy`
  per parameter set so a fresh sandbox per request is cheap; `lock`
  exposes the single-writer RLock for host/extension work that must
  serialize with tool calls. The apps extra now talks exclusively to
  this surface (no private attribute access — enforced by a test), so
  it runs unchanged on any `WorkspaceProvider`; frozen serving's
  per-request policy rebuild (a latency + DoS-amplification papercut
  on the anonymous path) is fixed by the memo; mutable (authoring)
  dispatch now serializes under the workspace's own lock, so test_app
  route callbacks and screenshot writes can't race ordinary tool
  calls.

### Added
- **`--apps` flag on the MCP CLI.** `python -m nontainer.adapters.mcp
  --apps` enables the apps loop without writing an embed script: the
  `curl` terminal builtin plus a `test_app` tool whose screenshots
  return as MCP image content. Previously test_app over MCP required
  calling `build_server(ws, apps=...)` from Python.

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
- **Agent-set response headers are matched case-insensitively.**
  `normalize()` lowercases `Response.headers` keys on the way to the
  wire, so the idiomatic `"Content-Type": "text/csv"` overrides the
  inferred content type instead of being silently ignored, and an
  agent-set `Content-Security-Policy` makes the served router defer
  its default instead of emitting a duplicate header (browsers apply
  the intersection). `WireResponse.headers` keys are now canonical
  lowercase.
- **`Request.require()` coerces symmetrically across sources.** JSON
  has one number type, so `require("x", float)` accepts JSON `5` and
  `require("n", int)` accepts `2.0` (non-integral floats still 400);
  bools are never numbers (JSON `true` no longer passes an `int`
  check); JSON strings coerce like query params; and query-param bools
  parse `true/1/false/0` instead of Python's `bool("false") is True`.
- **Screenshot cap no longer aborts the test.** A `test_app` action
  hitting `max_screenshots` is a noted soft skip (`ok`, with a
  "skipped: screenshot cap reached" note) instead of a hard failure
  that discarded every later action — asserts after the cap now run
  and count.
- **Handler-log failures warn instead of going silently blind.**
  `_log` still never breaks dispatch, but a broken/full fs (or a
  raising `on_log` sink) now emits one `RuntimeWarning` per runtime —
  previously every handler diagnostic vanished while the agent's
  documented repair loop ("tail `/app/logs/api.log`") debugged blind.
- **`test_app` false-PASS window closed (as far as heuristics can).**
  `read` now settles before observing, so a fetch that *starts* after
  the previous action's settle returned (debounce, `setTimeout`) is
  waited for instead of read as stale DOM. And a settle that exits via
  its cap (`settle_cap`, default 5s — now a `test_app` parameter)
  attaches a stale-risk note to the action's result instead of
  silently passing, pointing the agent at `{"assert": ...}` — the
  retrying form no heuristic can replace, since nothing can wait for a
  fetch that hasn't started yet.
- **Browser shutdown no longer stalls interpreter exit.** The shared
  test_app browser's atexit teardown deadlines dropped from 10s+5s to
  3s+2s — a healthy Chromium closes in milliseconds, and a wedged one
  isn't worth holding process exit for. `configure_browser` now
  documents its process-global, first-caller-wins contract.
- **App static serving path traversal** — `.`/`..` segments can no
  longer escape `/app/`, and backend source under `/app/api/` is never
  served as a static file.

### Changed
- Requires **sandtrap ≥ 0.2.4** (per-execution stderr capture;
  recursive-registration filter propagation, dotted patterns,
  synthetic `sys`/stdin) and **monkeyfs ≥ 0.1.5**
  (`VirtualFS.invalidate()`).
