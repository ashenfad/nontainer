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
```

Neither raises for agent-code failure — check truthiness. `inputs`
must be picklable data (per-call counterpart to the construction-time
`host_objects`, which are live resources).

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
    # truthy iff exit_code == 0

@dataclass(frozen=True)
class PythonResult:
    stdout: str; stderr: str = ""       # stderr chatter ≠ failure
    error: str | None = None            # rendered traceback, or None
    ticks: int = 0; duration: float = 0.0; truncated: bool = False
    namespace: Mapping[str, Any] = {}   # for the HOST; adapters never
                                        # inline it into observations
    # truthy iff error is None
```

### Host-side access

```python
ws.fs                 # termish-protocol filesystem (seed/harvest directly)
ws.cache              # MutableMapping; raises NotSupportedError if disabled
ws.put(src, dest=None) -> str          # host file → workspace (checkpointed)
ws.get(src, dest=None) -> bytes        # workspace → host (never checkpoints)
ws.register_command(name, fn)          # add a termish command post-construction
```

Cache key rules: str keys, no `__` prefix, no `/`; values validated
picklable at write (`CacheError` otherwise). Cache holds **data**;
reusable code belongs in `helpers/` files.

### Versioning (gated by `ws.caps`)

```python
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
    modules: Sequence[ModuleType | ModuleGrant] = ()
    host_objects: Mapping[str, Any] = {}
    network: bool = False
    isolation: "none" | "process" | "kernel" = "none"
    timeout: float = 30.0
    tick_limit: int = 1_000_000
    memory_limit_mb: int | None = None
    policy: sandtrap.Policy | None = None   # bypass the sugar entirely
```

- `ModuleGrant(module, network=False, host_fs=False)` — per-module
  passthroughs. `host_fs` lets a library's own code manage real-fs
  state (download caches, temp files); it is NOT how you share data
  with the agent (that's `Mount`).
- Kernel caveat: with `isolation="kernel"`, ANY network/host-fs grant
  disables that kernel restriction for the whole worker (seccomp/
  Landlock are monotonic). nontainer emits a `RuntimeWarning` at
  construction when this happens.
- `Mount(path, readonly=True)` — a real directory in the workspace
  tree, visible to both tools, NOT versioned/forked.

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
    **toolkit_kwargs,
)
```

`"auto"`: plain python env → one `terminal` tool; cache or host
objects → split `terminal` + `run_python`. All tool calls hold a
per-workspace lock (agno `arun()` runs sync tools concurrently on
threads; parallel calls serialize safely). With `apps=`, `test_app`
returns `ToolResult(content=..., images=[...])` — screenshots as real
images for vision models.

### MCP (`nontainer.adapters.mcp`, `[mcp]` extra)

```python
build_server(workspace, *, tools="auto", apps=None, name="nontainer") -> FastMCP
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

Serving:

```python
build_router(
    resolve: Callable[[str], Workspace | None],   # token → workspace
    *,
    config: AppsConfig | None = None,
    queue_depth: int = 8,             # per-token; overflow → 429
    rate_limit_per_min: int = 120,    # per-token; overflow → 429
    quiesce_seconds: float = 5.0,     # lazy checkpoint window
    csp: str | None = <default>,      # CSP header on served HTML
) -> Router                            # ASGI; app.mount("/apps", router)

mint_token(nbytes: int = 32) -> str    # capability-grade token
```

The router calls `resolve` once per unseen token and caches the
workspace + runtime. Requests never mint commits; the router
checkpoints lazily on quiesce with `info={"source": "api"}`.
