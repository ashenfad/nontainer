# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **The workspace root contract.** Agent-visible files now live under
  one configurable absolute path — `/workspace` by default, set with
  `workspace(..., root=)` and readable as `ws.root`. One value per
  session, inherited by forks. The point is cross-executor agreement:
  a dud VM mounts its guest workspace at the same path, so
  `/workspace/data/in.csv` names the same file whether agent code runs
  in the local sandbox or on a real machine. Previously the VM rooted
  the workspace somewhere else entirely, and agents burned turns
  discovering the split.
- **`[dud]` extra documented**, with an Executors section in the README
  covering the second seam — `WorkspaceProvider` decides where state
  lives, `Executor` decides where code runs, and the two are
  independent.

### Changed
- **BREAKING — agent-visible paths moved under the root.** Skills are
  at `<root>/skills` (was `/skills`), app handlers at `<root>/app`
  (was `/app`), UI artifacts at `<root>/ui` (was `/ui`), and the
  handler log at `<root>/app/logs/api.log`. Sandbox module imports
  resolve from the root too (`Policy.module_root`, requires
  sandtrap >= 0.2.12). Anything holding those paths literally —
  prompts, seeded files, stored sessions — needs repathing.
- **BREAKING — `DudExecutor()` now defaults to a real VM**
  (`backend="vm"`, resolved per platform) instead of the unsandboxed
  `"subprocess"` rung. The old default gave real bash and real files
  with *zero* containment, running as the host user with open egress —
  strictly weaker than the `LocalExecutor` a caller had just left, and
  it was what you got by reaching for a real machine and passing
  nothing. A host without a hypervisor now fails closed
  (`IsolationUnavailable`, missing piece named) rather than silently
  running unsandboxed. `backend="subprocess"` remains available as an
  explicit opt-in: it buys fidelity, not isolation, and is the only
  backend needing no hypervisor.
- **Dependency floors**: `sandtrap >= 0.2.12` (for `Policy.module_root`)
  and `dud >= 0.2.1` (for the guest workspace mounting at the
  configured root).

### Fixed
- **`DudExecutor` reaches dud's backends through `dud.session()`**
  instead of importing `dud.backends.*` directly. It had drifted a
  release behind: `backend="firecracker"` raised
  `ValueError("unknown dud backend")`, making dud's Linux/KVM rung
  unreachable from nontainer at all, and `backend="vm"` was hardcoded
  to vfkit, so on Linux it would try to boot a macOS hypervisor rather
  than resolving to firecracker. Routing through the façade fixes both
  and means a new dud rung needs no change here.
- **Absolute writes inside the guest land in the diff.** With the
  workspace mounted at the root, a write to `/workspace/x` from VM
  guest code is harvested like any other workspace write; it used to
  land beside the staging internals, invisible to diffs and lost on
  reset.

## [0.1.2] - 2026-07-19

### Added
- **Tracebacks in error results and `/app/logs/api.log`.** Runtime
  errors now render the full traceback — frames, line numbers, the
  raise site — instead of a bare message (under process isolation the
  traceback used to be lost crossing the worker pipe; requires
  sandtrap >= 0.2.10). Sandbox machinery frames (sandtrap/monkeyfs
  plumbing) are dropped, host install prefixes are stripped from
  library frames (`pandas/core/generic.py`, not the absolute venv
  path), and pathological depth is middle-elided.
- **Request context in api.log tags.** Handler log entries read
  `[dashboard:get ?source=filtered&makes=Tesla]` — the query string is
  what lets an agent correlate errors with requests instead of reading
  identical bare lines as a stale log.
- **More intent hints** (`error_hint`, superseding `blocked_import_hint`
  as the entry point, wired into both run_python observations and
  api.log): `shutil` → terminal cp/mv or open(); `__import__` → plain
  import statements work here; plotly's kaleido dead end → `ui = {...}`
  or matplotlib; the tick limit → vectorize, native calls don't tick.
- **Wider `os.path` grant**: `getsize` + `abspath` (monkeyfs-patched,
  VFS-routed) and `split`/`normpath`/`relpath` (pure string math).
  `getmtime`/`getatime`/`getctime` stay out — monkeyfs doesn't patch
  them; `os.stat(p).st_mtime` is the granted route.

### Added (notebook echo)
- **Bare final expressions display in `run_python`** (sandtrap's
  REPL echo, `PythonConfig.echo = "last"` by default): a trailing
  `df.head()` shows its repr, no `print()` needed — and the tool
  description teaches it. Echoed values ride the snapshot-prints
  stream, so a bare expression over a huge object gets reprobate's
  bounded structural render, not a megabyte of repr. Script surfaces
  are exempt by per-exec override (sandtrap >= 0.2.11): the terminal
  `python` builtin keeps `python -c` semantics for pipelines, and app
  handlers never echo into api.log.

### Fixed
- **`dataframes()` pins a fork-safe arrow allocator**
  (`ARROW_DEFAULT_MEMORY_POOL=system`, via `setdefault` before the
  first pandas import). Arrow's default mimalloc pool keeps per-thread
  heaps that don't survive `fork()` — a sandbox worker forked from a
  threaded host segfaulted in `libarrow`'s `mi_thread_init` on its
  first arrow allocation (parquet reads, pandas-3 arrow-backed
  strings), and every respawn re-forked the same hostile parent: a
  permanent "Worker process died during initialisation" loop.
  Embedders that import pandas before building configs should set the
  variable themselves, earlier.

### Changed
- **Tick limits raised**: `PythonConfig.tick_limit` 1M → 50M,
  `AppsConfig.request_tick_limit` 200k → 10M. The same sandbox
  checkpoint enforces the timeout, so that's the real runaway guard;
  the tick limit is a determinism backstop and must never fire on an
  honest loop over a few-hundred-k-row frame.
- sandtrap floor raised to 0.2.11 (worker-rendered tracebacks,
  per-exec echo override).

## [0.1.1] - 2026-07-15

### Added
- **The nontainer a2ui catalog** (`docs/a2ui/catalog.json`, exported as
  `nontainer.adapters.a2ui.NONTAINER_CATALOG`): the idiomatic home for
  extension semantics — re-exports the basic-catalog components the
  egress adapter emits and declares `Stat {label, value, sublabel?}`,
  `Callout {title?, body?, tone}`, and `Chart {spec}`. Passing it as
  `turn_to_a2ui(catalog_id=...)` opts the surface into flat
  one-component-per-item cards that say what they mean, instead of
  Card/Column/Text trees with role-suffixed ids; any other catalog id
  (including a consumer's own) keeps the basic approximation, since we
  can't know what a foreign catalog declares. `Chart` stays
  unconditional — a plotly figure has no basic approximation worth
  shipping.

### Fixed
- **a2ui cards rendered as empty boxes on basic-catalog consumers**
  (#16). The v0.9 basic-catalog `Card` takes a singular required
  `child` id (`unevaluatedProperties: false` — a `children` array
  isn't ignored, it's invalid), so every stat/callout Card shipped
  content the reference renderer never saw. Card content now rides an
  intermediate `Column` behind `child`; the callout's `tone` stays a
  passthrough prop on the basic shape as a documented deviation
  (strictly validating consumers should use `NONTAINER_CATALOG`, where
  `tone` is declared).
- **Card-builder hardening for direct `/ui` writes**, which bypass
  `materialize_ui`'s normalization: an unknown callout `tone` clamps
  to `info` (the catalog declares a closed enum), and explicit nulls
  in stat items read as absent — empty label/value, omitted sublabel —
  never as the literal text `"None"`.

## [0.1.0] - 2026-07-15

### Added
- **`AppsConfig.script_hosts` + `apps_primer`: the script allowlist is
  one declaration.** The hosts browser scripts may load from used to
  live in four hand-synced places — test_app's interception, the served
  CSP, the agent-facing notes, curl's error message — kept honest only
  by a test. All four now derive from `AppsConfig.script_hosts`
  (default unchanged: `DEFAULT_SCRIPT_HOSTS`), so an embedder adding a
  private registry host (e.g. a self-hosted esm.sh over an internal
  npm registry) changes one tuple and the walls, the verifier, and the
  agent's instructions stay in agreement. `apps_primer` appends
  embedder guidance to the apps notes — the place to teach a private
  component lib's known-good import block. `build_router(csp=...)`
  now defaults to deriving from the config (`build_csp`); pass a string
  to override or `""` to disable. Removed: `test_app`'s per-call
  `cdn_allowlist` parameter (set it on the config instead). Agents predictably
  write into `/ui` themselves (`fig.write_json('/ui/x.json')`,
  savefig) instead of assigning objects to `ui = {...}` — and those
  files displayed nowhere. `run_python` now diffs the `/ui` listing
  around the call and appends files the code created to the
  `[ui artifacts: ...]` note (deduped against materialized values),
  extending the existing path-pointer near-miss forgiveness.
- **The walls label their doors.** Three predictable agent collisions
  now redirect instead of dead-ending:
  a 404 on `/api/<name>.py` says endpoints are module names without
  the extension (and suggests the real path when it exists) — agents
  reliably mirror the filename into `fetch()` and then debug the
  backend; blocked imports of `subprocess`/`requests`/`urllib.request`/
  `httpx`/`socket` get a `[hint: ...]` in both run_python observations
  and api.log pointing at the terminal's curl; and `urllib.parse` is
  granted in the STDLIB preset (pure string functions only — `quote`,
  `urlencode`, `parse_qs`, `urlparse`, ... — the network side of
  urllib stays out). The apps primer also states the no-`.py`-in-URL
  rule explicitly.
- **The 8MB `ui` artifact cap explains itself.** An oversize value used
  to silently degrade to a truncated `repr` `.txt` — a 280k-point
  plotly map showed up as a wall of text with no hint why. Now the
  tool result carries a `[ui note: ...]` diagnosis (size vs cap, and
  for plotly the actual usual culprit: per-point customdata/hover
  strings — coordinates are cheap, WebGL traces render 100k+ points
  fine) so the agent self-corrects, and the `.txt` artifact shows the
  same message to the human where the figure would have been.
  `materialize_ui` now returns `(artifacts, problems)`. The tool
  description also teaches the cap + lean-spec guidance up front.
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
- **`test_app` accepts a stringified actions list.** Models routinely
  send the nested list as a JSON string; the pydantic layer agno wraps
  entrypoints in rejected it on the annotation before the existing
  `coerce_actions` tolerance could run. The annotation is loosened so
  coercion gets its chance.
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
