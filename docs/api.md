# API Reference

Everything importable from `nontainer`, `nontainer.providers`,
`nontainer.adapters.*`, and `nontainer.apps`.

## `nontainer.workspace(...)` — the factory

```python
workspace(
    session: str,
    *,
    store: str | Path | None = None,      # default ~/.nontainer
    backend: "kvgit" | "dir" | "agentfs" = "kvgit",
    provider: WorkspaceProvider | None = None,   # overrides backend/store
    python: PythonConfig | None = None,
    mounts: dict[str, Mount] | None = None,
    commands: dict[str, CommandFunc] | None = None,
    cache: bool = True,
    autocheckpoint: bool = True,
    max_observation: int = 32_000,
) -> Workspace
```

Session resolution: `kvgit` → branch per session in one shared store;
`dir` → `store/<session>/`; `agentfs` → `store/<session>.db`. Session
ids are validated (`SESSION_ID_RE`) on every path — they often flow
from untrusted input.

## `Workspace`

One instance == one session's world. Not thread-safe: one workspace,
one thread at a time (adapters enforce this with a lock). Context
manager (`with ... as ws:` closes on exit).

### The two tools

```python
ws.terminal(command: str) -> TerminalResult
ws.run_python(code: str, *, inputs: dict | None = None) -> PythonResult

# async host facades — run the sync execution in a thread so an
# event-loop host (FastAPI, etc.) stays responsive. Same results,
# same semantics; the agent's code is unchanged (still sync).
await ws.aterminal(command) -> TerminalResult
await ws.arun_python(code, *, inputs=None) -> PythonResult
```

Neither raises for agent-code failure — check truthiness. `inputs`
must be picklable data (per-call counterpart to the construction-time
`host_objects`, which are live resources).

Use the `a*` variants when embedding in an async server — they're
just `run_in_executor` wrappers, so CPU-bound sandbox work never
blocks your loop. A workspace is single-writer and enforces it:
mutating calls hold an internal lock, so parallel calls to one
workspace serialize safely (each atomic + checkpointed) instead of
corrupting staged state. Read-only accessors don't take the lock —
and neither do the host-side escape hatches (`ws.fs` writes, `ws.cache`
mutation), so a host thread using those while agent calls run
serializes itself. `run_in_threadpool(ws.run_python, code)` from
Starlette works too if you'd rather not use the facade.

`terminal` executes pipes, redirects (`> >> <`), `&&`/`||`/`;`,
quoting, ~33 builtins (via termish) plus injected commands. `cd`
persists across calls (and rolls back with checkpoints on kvgit).
A reserved `python` builtin bridges into `run_python` with script
semantics: `python -c 'code'`, `python file.py`, or piped stdin;
stdout flows to the pipeline, errors → exit 1, the namespace is
dropped.

`run_python` scope: whitelisted `modules`, injected `host_objects`,
`cache` (when enabled), stdlib `open()`/`os` routed to the workspace
fs (monkeyfs), imports from `helpers/` on the fs. Script model:
top-level bindings do NOT persist between calls — they are *reported*
via `result.namespace`.

### Results

```python
@dataclass(frozen=True)
class TerminalResult:
    stdout: str; exit_code: int; stderr: str = ""; truncated: bool = False
    checkpoint: str | None = None       # commit this call created
    # truthy iff exit_code == 0

@dataclass(frozen=True)
class PythonResult:
    stdout: str; stderr: str = ""       # stderr chatter ≠ failure
    error: str | None = None            # rendered traceback, or None
    ticks: int = 0; duration: float = 0.0; truncated: bool = False
    namespace: Mapping[str, Any] = {}   # for the HOST; adapters never
                                        # inline it into observations
    checkpoint: str | None = None       # commit this call created
    # truthy iff error is None

@dataclass(frozen=True)
class WriteOutcome:                     # from write_file / put
    path: str; size: int; created: bool
    checkpoint: str | None = None
```

Every mutating call's result pins the commit its autocheckpoint
created — `ws.restore(result.checkpoint)` is compensation by identity,
no step counting. `checkpoint` is `None` when nothing was committed:
read-only call, no-op edit, turn-mode checkpointing (the id comes from
`end_turn()` instead), or an unversioned provider. Host-facing like
`namespace` — adapters never render it into the model's observation.

Oversized stdout from `print()` is re-rendered **budget-aware** via
[reprobate](https://github.com/ashenfad/reprobate): structural elision
(`[0, 1, 2, ...996 more]`) instead of a mid-token cut. Small output
stays byte-exact; non-print writes fall back to a head-cut.

### Host-side access

```python
ws.fs                 # termish-protocol filesystem (seed/harvest directly)
ws.cache              # MutableMapping; raises NotSupportedError if disabled
ws.write_file(path, content) -> WriteOutcome   # parents created; checkpointed
ws.edit_file(path, old, new, replace_all=False) -> EditOutcome
    # exact-string replacement with agent-tolerant fallbacks (the agex
    # strategy set, ported): exact → trailing-ws-flexible →
    # indent-flexible (replacement re-indented to the file's baseline);
    # replacement-already-present → no-op (count=0, "already_applied").
    # Unique-match-or-replace_all; WorkspaceError with a "did you mean
    # these lines?" snippet otherwise. Carries `checkpoint` when the
    # edit committed.
ws.put(src, dest=None) -> WriteOutcome # host file → workspace (checkpointed)
ws.get(src, dest=None) -> bytes        # workspace → host (never checkpoints)
ws.register_command(name, fn)          # add a termish command post-construction
```

Cache key rules: str keys, no `__` prefix, no `/`; values validated
picklable at write (`CacheError` otherwise). Cache holds **data**;
reusable code belongs in `helpers/` files.

### Versioning (gated by `ws.caps`)

```python
ws.head: str | None      # current checkpoint id; None if unversioned.
                         # Pins read-only observations (reads don't move
                         # it) — exact iff not ws.dirty
ws.dirty: bool           # staged-but-uncommitted changes exist
ws.checkpoint(info: dict | None = None) -> str   # atomic: files + cache + cwd
ws.restore(checkpoint_id: str) -> None
ws.rollback(steps: int = 1) -> str
ws.history(limit: int | None = None) -> Iterable[CheckpointInfo]
ws.fork(name: str) -> Workspace                  # cost varies by backend
ws.discard() -> None                             # drop staged writes
```

Unversioned providers raise `NotSupportedError`; `autocheckpoint` is
forced off for them. With autocheckpoint on, each successful mutating
tool call commits with `info={"tool": ...}`; read-only calls never
commit. `info` dicts must be JSON-serializable.

### Introspection

```python
ws.session: str
ws.caps: Capabilities
ws.cache_enabled: bool
ws.python_config: PythonConfig
```

## `PythonConfig`

```python
@dataclass(frozen=True)
class PythonConfig:
    modules: Sequence[ModuleType | ModuleGrant | Sequence[...]] = ()
    stdlib: bool = True                     # curated safe-stdlib set
    host_objects: Mapping[str, Any] = {}
    network: bool = False
    isolation: "none" | "process" | "kernel" = "none"
    timeout: float = 30.0
    tick_limit: int = 1_000_000
    memory_limit_mb: int | None = None
    policy: sandtrap.Policy | None = None   # bypass the sugar entirely
```

- `stdlib=True` (default) grants the curated safe-stdlib set
  (`nontainer.presets.STDLIB`): math/statistics/decimal/fractions,
  random (minus global seed/state), collections/itertools,
  datetime/time/calendar/zoneinfo, re/string/textwrap,
  json/csv/pickle/base64/uuid/hashlib, traceback formatters, typing,
  io, VFS-routed os/os.path/pathlib/glob/fnmatch, and
  gzip/zipfile/tarfile. `stdlib=False` for a truly bare cell.
- `modules` extends the stdlib set and flattens one level of nesting,
  so preset grant lists splice in directly:
  `modules=[dataframes(), plotting(), my_module]`. Explicit grants
  for a stdlib module override its stdlib-set registration.
- `ModuleGrant(module, network=False, host_fs=False, include="*",
  exclude=("_*", "*._*"), recursive=False, name=None)` — per-module
  passthroughs and member patterns (sandtrap semantics). `host_fs`
  lets a library's own code manage real-fs state (download caches,
  temp files); it is NOT how you share data with the agent (that's
  `Mount`). `name` is for submodules reached as attributes
  (`ModuleGrant(os.path, name="os.path")`). Filters propagate through
  `recursive=True` to submodules, and dotted patterns match qualified
  names (`"DataFrame.eval"`, `"pandas.core*"`) — sandtrap ≥ 0.2.2
  semantics.
- Kernel caveat: with `isolation="kernel"`, ANY network/host-fs grant
  disables that kernel restriction for the whole worker (seccomp/
  Landlock are monotonic). nontainer emits a `RuntimeWarning` at
  construction when this happens.
- `Mount(path, readonly=True)` — a real directory in the workspace
  tree, visible to both tools, NOT versioned/forked.

## Presets (`nontainer.presets`)

Curated grant lists for the heavy libraries, with agex's accumulated
exclude lists (global RNG state, memory-mapped host files, display
calls). Presets run at config-construction time — host level — which
is when their environment side effects must happen.

```python
from nontainer.presets import dataframes, plotting

PythonConfig(modules=[dataframes(), plotting()])

STDLIB                    # the stdlib=True grant tuple, reusable
dataframes()              # numpy + pandas (ImportError if missing)
plotting(plotly=None)     # matplotlib: Agg-pinned + font cache warmed
                          # plotly: None=if installed, True=required, False=skip
```

## Providers (`nontainer.providers`)

All satisfy the `WorkspaceProvider` protocol (`nontainer.protocol`):
`session`, `caps`, `fs`, `kv`, `dirty`, `checkpoint/restore/history/
fork/discard`, `mount`, `close`.

```python
KvgitProvider.open(path=None, *, session, codecs=None)  # None → memory store
KvgitProvider(staged, *, session)                        # bring your own Staged
    .staged            # the kvgit Staged (host-side power tool)

DirProvider(root, *, session)
    .root              # the real directory

AgentFSProvider(db_path, *, session)                     # [agentfs] extra
    .db_path           # the SQLite artifact
```

Capabilities at a glance:

| | versioned | staging | cheap_fork | merge | sql_audit |
|---|---|---|---|---|---|
| Kvgit | ✅ | ✅ | ✅ | ✅ | ❌ |
| Dir | ❌ | ❌ | ❌ | ❌ | ❌ |
| AgentFS | ❌ (spike) | ❌ | ❌ | ❌ | ✅ |

`codecs="scientific"` on kvgit enables numpy/pandas chunk dedup
(requires `kvgit[scientific]`).

## Errors (`nontainer`)

`WorkspaceError` (base) · `NotSupportedError` (capability missing) ·
`SessionIdError` · `CheckpointNotFoundError` · `CacheError`.

## Adapters

### agno (`nontainer.adapters.agno`, `[agno]` extra)

```python
WorkspaceTools(
    workspace: Workspace,
    *,
    tools: "auto" | "terminal" | "split" = "auto",
    apps: AppRuntime | None = None,     # adds the test_app tool
    checkpoint: "call" | "turn" = "call",
    terminal_primer: str | None = None, # host guidance → terminal tool
    python_primer: str | None = None,   # host guidance → run_python tool
    **toolkit_kwargs,
)
# checkpoint="turn": one commit per agent turn (the agex model) — wire
# tk.end_turn into Agent(post_hooks=[...]). Crash mid-turn can lose
# the turn's staged work; "call" trades chattier history for max
# durability. Workspace.autocheckpoint is also publicly settable.
```

`"auto"`: plain python env → one `terminal` tool; cache or host
objects → split `terminal` + `run_python`. Parallel tool calls
serialize safely (agno `arun()` runs sync tools concurrently on
threads; the workspace's internal lock enforces single-writer, and
the adapter's own lock fences its surrounding work). With `apps=`, `test_app`
returns `ToolResult(content=..., images=[...])` — screenshots as real
images for vision models.

**Primers** append embedder guidance to a tool's description — the
place to tell the agent about conventions the core can't infer (e.g.
"`db` is a SQLite store injected via host_objects — use it, not
`cache`, for shared state"). Strict 1-to-1 with the exposed tools:
`terminal_primer` → the `terminal` tool, `python_primer` → the
`run_python` tool. In terminal-only mode there is no `run_python`
tool, so a `python_primer` lands in the `terminal` tool's `python`
section (and warns). Same params on `build_server`.

### MCP (`nontainer.adapters.mcp`, `[mcp]` extra)

```python
build_server(workspace, *, tools="auto", apps=None, name="nontainer",
             terminal_primer=None, python_primer=None) -> FastMCP
```

CLI: `python -m nontainer.adapters.mcp --session S [--store DIR]
[--backend kvgit|dir] [--tools auto|terminal|split] [--no-cache]
[--module NAME ...]` (stdio transport). `build_server` for anything
the flags don't cover.

## Apps (`nontainer.apps`, serving/test_app need the `[apps]` extra)

Design doc: [apps.md](apps.md).

```python
enable_apps(ws, config: AppsConfig | None = None) -> AppRuntime
    # builds handler sandboxes + registers the `curl` terminal builtin

AppsConfig(request_timeout=5.0, request_tick_limit=200_000,
           max_response_bytes=2_000_000)

AppRuntime.dispatch(request: Request) -> WireResponse
AppRuntime.test_app(actions, *, viewport="desktop", ...) -> TestAppResult

request(method, url, *, body=b"", headers=None) -> Request  # convenience

# test_app shares one Chromium across all calls (async Playwright on a
# dedicated loop-thread); concurrent tests get their own contexts,
# bounded by a semaphore. Tune before the first test_app:
configure_browser(max_concurrent=8)
await arun_test_app(runtime, actions, ...)   # async entry (no waiting thread)
shutdown_browser()                           # close browser + loop (also atexit)
```

Handler contract (agent-authored files under `/app/api/`):

```python
Request(method, path, params, headers, body, json)
    .require(name, typ=str)     # HttpError(400) if missing/mistyped
Response(status=200, body=None, headers={})
HttpError(status, message)
```

Liberal returns: dict/list → JSON · str → text · bytes → blob ·
`Response` → as specified · None → 204. GET handlers run against a
read-only filesystem AND a read-only cache view. Failed mutating
handlers discard their staged writes when the provider was clean at
dispatch. Logs: `/app/logs/api.log`.

`test_app` actions: `{"click": sel}` · `{"type": [sel, text]}` ·
`{"read": sel}` · `{"eval": js}` · `{"assert": js}` (retries ~2s) ·
`{"screenshot": true}` (→ `/app/screenshots/`) · `{"wait": ms}`.
Viewports: `"desktop"`/`"tablet"`/`"mobile"` or `{width, height}`.

Serving (frozen snapshots — read-only, concurrent):

```python
build_router(
    resolve: Callable[[str], Workspace | None],   # token → read-only ws @ commit
    *,
    config: AppsConfig | None = None,
    csp: str | None = <default>,      # CSP header on served HTML
    on_log: Callable[[str], None] | None = None,  # default: nontainer.apps logger
) -> Router                            # ASGI; app.mount("/apps", router)

mint_token(nbytes: int = 32) -> str    # capability-grade token
```

Serving is **stateless and read-only**: `resolve` is called per request
(cache inside it if expensive; the router does not close its result),
and handlers may read the workspace + call `host_objects` but cannot
mutate the VFS (a write → 500). Requests run **concurrently** (fresh
read-only sandbox each — no cache, no lock, no lifecycle). Mutable app
state goes to an external store via `host_objects`. Rate limiting is an
edge concern.
