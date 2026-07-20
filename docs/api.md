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
    executor_factory: Callable[[], Executor] | None = None,
    root: str = "/workspace",
) -> Workspace
```

Session resolution: `kvgit` → branch per session in one shared store;
`dir` → `store/<session>/`; `agentfs` → `store/<session>.db`. Session
ids are validated (`SESSION_ID_RE`) on every path — they often flow
from untrusted input.

`root` is the **workspace root**: the absolute path agent-visible
files live under, and the one path contract shared across executors.
cwd starts there, VFS module imports resolve from it, skills install
to `<root>/skills`, the app tree is `<root>/app` — and a VM executor
(dud) mounts its guest workspace at this exact path, so an absolute
path in agent code names the same file on every executor. Forks
inherit it. `root="/"` selects the flat pre-0.2 layout (no VM path
parity — a guest can't mount at the fs root).

## `nontainer.delete_workspace(...)` — teardown

```python
delete_workspace(
    sessions: str | Iterable[str],
    *,
    store: str | Path | None = None,      # default ~/.nontainer
    backend: "kvgit" | "dir" | "agentfs" = "kvgit",
) -> None
```

The counterpart to `workspace(...)`: drops a session's entire stored
state, dispatching by `backend` to the same layout the factory built —
`kvgit` deletes the named branches from `store/kvgit`, `dir` removes
the `store/<session>/` trees, `agentfs` unlinks the `store/<session>.db`
files. Plural because a caller often owns more than one branch/dir/db
per logical session (an app publishing snapshot branches, a batch
cleanup); `sessions` may be a single id or any iterable. Idempotent —
a name that doesn't exist and a store that was never created are both
no-ops. Store-level, not live-session: close any open `Workspace` on
these sessions first (a kvgit store handle pins its branch). It cleans
only the workspace store, never bookkeeping a caller keeps *beside* it.

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
mutation), so a host thread using those while agent calls run holds
`ws.lock` itself (see the extension surface below).
`run_in_threadpool(ws.run_python, code)` from Starlette works too if
you'd rather not use the facade.

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
ws.root: str                  # the workspace root (see the factory)
ws.supports_commands: bool    # executor capability, below
```

**`ws.supports_commands`** — whether injected terminal commands
(`commands=`, `ws.register_command`) actually reach the shell. It's an
`Executor` capability, in the same declare-the-difference spirit as
`ws.caps` for providers:

| Executor | `supports_commands` | why |
|---|---|---|
| `LocalExecutor` | `True` | termish receives the mapping, so an injected command is a real command |
| `DudExecutor` | `False` | a guest runs actual bash; there's no hook to inject into |

Tool descriptions gate on it — the apps primer teaches `curl` only
where it exists, since promising an agent a command that answers
`command not found` costs it turns. An executor that predates the flag
reads as `True`, keeping its historical behavior.

On an executor without it, `test_app` is the verification path. Note
that importing a handler module and calling its verb by hand is *not*
an equivalent substitute: it skips routing and runs GET without its
read-only filesystem, so it can pass on code the real request path
rejects.

### Extension surface

For embedders composing execution features *on top of* the workspace —
the apps extra is the reference consumer. Most callers never need
these; they are a documented, kept-stable contract so extensions don't
reach into internals (and stay portable across providers):

```python
ws.exec_python(code, *, inputs=None, sandbox=None, cache=None,
               stdin=None, argv=None) -> PythonResult
    # the raw execution path: no checkpoint, no lock. `sandbox`
    # overrides the default sandbox (from build_sandbox); `cache`
    # overrides the agent-visible cache mapping (None = workspace
    # default); stdin/argv expose sandtrap's synthetic `sys`. Safe to
    # call concurrently with distinct sandboxes (frozen app serving
    # does); callers whose work mutates the workspace hold ws.lock.
ws.build_sandbox(*, timeout=None, tick_limit=None,
                 extra_classes=(), filesystem=None) -> Sandbox
    # a sandbox sharing the frozen PythonConfig's registrations, with
    # per-purpose overrides: budgets, extra registered classes (e.g. a
    # request/response contract), a filesystem view (e.g. ReadOnlyFS).
    # The built Policy is memoized per parameter set, so minting a
    # fresh sandbox per request is cheap.
ws.lock: threading.RLock
    # the single-writer lock the mutating public methods hold. Hold it
    # for host-side/extension work that mutates the workspace (ws.fs
    # writes, ws.cache mutation, read-modify-write) and must serialize
    # with tool calls. RLock: safe to hold around locked public calls.
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
    tick_limit: int = 50_000_000
    memory_limit_mb: int | None = None
    echo: "none" | "last" | "all" = "last"  # bare-final-expr display in run_python
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
KvgitProvider.delete(path, sessions)   # drop branches (path = the store dir)

DirProvider(root, *, session)
    .root              # the real directory
DirProvider.delete(path, sessions)     # rmtree dirs (path = the store base)

AgentFSProvider(db_path, *, session)                     # [agentfs] extra
    .db_path           # the SQLite artifact
AgentFSProvider.delete(path, sessions) # unlink dbs (path = the store base)
```

Each `delete(path, sessions)` is the store-level teardown primitive
`delete_workspace` dispatches to — plural, idempotent, and validating
session ids first where a bad name could escape the store root (dir,
agentfs). Kvgit's runs deletions from a hidden `__void__` anchor branch
(created on first delete, never listed): kvgit can't delete the branch
a store handle is anchored on, so the sole-branch case has nothing else
to sit on. `path` is the store directory — the same `store/kvgit` that
`open` takes for kvgit; the parent store base for dir/agentfs (they
resolve `<session>/` and `<session>.db` under it).

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

**The artifacts note (`run_python`).** The `ui = {...}` convention
materializes namespace values into `/ui/` files (spec formats > pixels >
html > data), then appends a single model-facing line to the tool result:

```
[ui artifacts: name -> /ui/name.plotly.json, other -> /ui/other.png]
```

The agent reads it to embed `![name](/ui/...)` in its reply; unreferenced
artifacts display after the prose. This line is a **public, round-trippable
contract** — harnesses parse tool results with `parse_artifacts_note`, never
a private regex:

```python
from nontainer.adapters.render import (
    artifact_kind, artifacts_note, parse_artifacts_note,
)
parse_artifacts_note(tool_result)  # -> [(name, path), ...], [] if no note
artifact_kind("/ui/x.plotly.json")  # -> "plotly"
```

Grammar: `name " -> " path`, segments joined by `", "`, wrapped in
`[ui artifacts: ...]`. Names are **sanitized** (`[\w.-]+`, matching the
filename rule) so `", "`/`" -> "` never occur inside a name — that keeps
the parse unambiguous even when the note rides mid-string (it is appended
after the render output and before any `[ui note: ...]` problem lines).
`artifact_kind(path)` maps a suffix to its render kind
(`plotly`/`table`/`cards`/`image`/`html`/`json`/`text`/`binary`) — the
single source of truth mirroring studio's `Artifact.svelte` dispatch;
compound spec suffixes win over the bare `.json` floor.

### MCP (`nontainer.adapters.mcp`, `[mcp]` extra)

```python
build_server(workspace, *, tools="auto", apps=None, name="nontainer",
             terminal_primer=None, python_primer=None) -> FastMCP
```

CLI: `python -m nontainer.adapters.mcp --session S [--store DIR]
[--backend kvgit|dir] [--tools auto|terminal|split] [--no-cache]
[--module NAME ...] [--apps] [--mount POINT=DIR[:rw] ...]` (stdio
transport). `--apps` enables the apps loop — a `test_app` tool
(screenshots return as MCP image content; needs the `[apps]` extra +
`playwright install chromium`, checked lazily at first `test_app`),
plus the `curl` terminal builtin on executors that support injected
commands (see `ws.supports_commands` under Introspection). `--mount /data=~/datasets`
exposes a host directory inside the workspace (read-only unless
`:rw`) — the inbound channel for real files, no base64 games.
`build_server` for anything the flags don't cover (module grants with
network/host-fs, host objects, primers).

**Artifact channels.** Every server also registers:

- a `view_image` tool (both adapters): the agent views a workspace
  image — a saved plot, a chart — returned as real image content for
  vision models (png/jpeg/gif/webp, 10MB cap).
- MCP **resources** (MCP adapter): any workspace file is readable as
  `workspace://{path}` — text files as text, binary as blob — and
  `workspace://-/tree` lists all paths. Tools are the agent's hands;
  resources are the client's window into the artifacts it produced
  (datasets out, plots out, zips out). `file_write` results carry a
  ground-truth `ResourceLink` to the written file, and the tool
  descriptions coach the agent to mention `workspace://` URIs when it
  produces an artifact for the user.

## Apps (`nontainer.apps`, serving/test_app need the `[apps]` extra)

Design doc: [apps.md](apps.md).

```python
enable_apps(ws, config: AppsConfig | None = None) -> AppRuntime
    # builds handler sandboxes + registers the `curl` terminal builtin
    # (which only reaches the shell where ws.supports_commands)

AppsConfig(request_timeout=5.0, request_tick_limit=10_000_000,
           max_response_bytes=2_000_000,
           script_hosts=DEFAULT_SCRIPT_HOSTS,  # where browser scripts may
           #   load from — drives test_app interception, the served CSP,
           #   and the agent-facing allowlist sentence (one declaration)
           apps_primer=None)  # embedder guidance appended to the apps
           #   notes (private component libs, house conventions)

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

Handler contract (agent-authored files under `/workspace/app/api/`):

```python
Request(method, path, params, headers, body, json)
    .require(name, typ=str)     # HttpError(400) if missing/mistyped.
    # Liberal-in, symmetric across JSON body and query params: strings
    # coerce through typ (bool: true/1/false/0); JSON's single number
    # type means int passes for float and integral float for int;
    # bools are never numbers.
Response(status=200, body=None, headers={})
    # header keys may be any casing; normalized (lowercased) on the
    # wire — an agent-set Content-Type wins over the inferred type,
    # and an agent-set Content-Security-Policy defers the served
    # default instead of duplicating it.
HttpError(status, message)
```

Liberal returns: dict/list → JSON · str → text · bytes → blob ·
`Response` → as specified · None → 204. GET handlers run against a
read-only filesystem AND a read-only cache view. Failed mutating
handlers discard their staged writes when the provider was clean at
dispatch. Logs: `/workspace/app/logs/api.log`.

`test_app` actions: `{"click": sel}` · `{"type": [sel, text]}` ·
`{"read": sel}` · `{"eval": js}` · `{"assert": js}` (retries ~2s) ·
`{"screenshot": true}` (→ `/workspace/app/screenshots/`) · `{"wait": ms}`.
Viewports: `"desktop"`/`"tablet"`/`"mobile"` or `{width, height}`.

Serving (frozen snapshots — read-only, concurrent):

```python
build_router(
    resolve: Callable[[str], Workspace | None],   # token → read-only ws @ commit
    *,
    config: AppsConfig | None = None,
    csp: str | None = None,  # None → derived from config.script_hosts
    #   (build_csp); a string overrides wholesale; "" disables
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
