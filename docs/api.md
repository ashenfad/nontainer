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
`kvgit` deletes the named branches from `store/kvgit` — and with each
branch the session-scoped tags it owns, leaving store-scoped ones
alone, since that scope exists so a publication outlives its session —
`dir` removes the `store/<session>/` trees, `agentfs` unlinks the
`store/<session>.db` files. Plural because a caller often owns more than one branch/dir/db
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
    ui_problems: tuple[str, ...] = ()   # why a `ui` value did not render
                                        # as intended (the 8MB cap, with
                                        # the remediation) -- actionable
                                        # text meant to reach the agent
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

Checkpoints cover workspace-owned files and cache. Host-object calls
and mounts are external effects: their data is not checkpointed,
restored, or copied by a fork. A fork does inherit the mount *points*,
and sees the same live directories behind them.

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
ws.head_tree: str | None # content hash of the head — the identity of
                         # WHAT the files and cache are, where `head`
                         # identifies the point in history
```

Unversioned providers raise `NotSupportedError`; `autocheckpoint` is
forced off for them. With autocheckpoint on, each successful mutating
tool call commits with `info={"tool": ...}`; read-only calls never
commit. `info` dicts must be JSON-serializable.

Equal trees mean identical content, whatever the metadata or ancestry
around it. The converse does not hold: kvgit stamps each write with
when it happened, so rewriting a file with the same bytes still moves
the tree. `CheckpointInfo.tree` carries the same hash per history
entry (`None` on a provider that keeps no such hash).

#### Tags (`ws.caps.tags`)

A tag is a name for a checkpoint that outlives the call that made it —
immutable, and a garbage-collection root: the named checkpoint and its
ancestry stay reachable for as long as the name does.

```python
ws.tag(name, *, info=None, scope="session") -> str    # checkpoint id
ws.tags(*, scope="session") -> dict[str, str]         # name -> checkpoint id
ws.tag_info(name, *, scope="session") -> TagInfo | None
ws.delete_tag(name, *, scope="session") -> None
ws.at_tag(name, *, scope="session") -> Workspace      # frozen; see below
ws.diff(a, b) -> WorkspaceDiff                        # two checkpoint ids
ws.changed_since(ref, *, scope="session") -> WorkspaceDiff  # tag name or id
```

**Two scopes, and nontainer decides what they mean** rather than
handing embedders one flat namespace to partition themselves:

| scope | belongs to | survives `delete_workspace` | for |
|---|---|---|---|
| `"session"` (default) | this session | ❌ — it goes with the branch | checkpoints worth naming: "before the refactor", "the state the report was built from" |
| `"store"` | no session | ✅ | publications: the snapshot an app serves, the state a link points at |

`ws.tags()` lists only the scope you ask for, so two sessions can each
hold a `v1` and neither sees the other's. Names come back the way you
passed them; the scope prefix kvgit stores them under (`<session>/`,
`store/`) never surfaces, and a name that starts with one is rejected
so a scope can't be spoofed. Otherwise the rule is kvgit's: any
non-empty name without `%`, `/` included. Tags never move — an existing
name raises rather than being repointed. `ws.tag()` checkpoints staged
work first (`info={"tool": "tag", "name": ...}`, like `fork`), so the
name means what the caller saw.

`TagInfo` carries `name`, `scope`, `id` (the checkpoint), `tree`,
`time`, the `info` dict, and `dangling` (the checkpoint is not in the
store — damage, not an ordinary state).

**Frozen workspaces.** `ws.at_tag(name)` returns a `Workspace` over the
tagged state that can be read but never written: `ws.frozen` is True,
`autocheckpoint` is forced off, `file_write` / `file_edit` / `put` /
`checkpoint` / `fork` / `tag` / `rollback` raise `NotSupportedError`,
and the executor holds a read-only filesystem, so a shell redirect or
`open(..., "w")` from agent code fails where it happens with a message
naming the tag. Reads, `run_python` that only reads, and apps dispatch
all work; `discard()` and `close()` work. It inherits the parent's
construction settings the way `fork` does — python config **including
its live host objects**, mounts, root, executor factory, commands — so
an app served from a snapshot still talks to the session's live db.
The files are frozen; the host's world is not.

**`changed_since`** takes a tag name or a checkpoint id (the tag is
tried first, in `scope`) and compares it with the current head.
`WorkspaceDiff` holds `added` / `removed` / `modified` as absolute
workspace paths — `/workspace/data/in.csv`, the way agent code and
`ws.fs` name files. Framework keys (cache, cwd, the stored
conversation, the filesystem's own bookkeeping) are not files and never
appear. A file that was rewritten counts as modified even if its bytes
did not change, and staged-but-uncommitted work is not in the diff at
all (check `ws.dirty`).

### Introspection

```python
ws.session: str
ws.caps: Capabilities
ws.cache_enabled: bool
ws.python_config: PythonConfig
ws.root: str                  # the workspace root (see the factory)
ws.frozen: bool               # a read-only snapshot at a tag (at_tag)
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
    warm_view_workers: int = 1                   # resident app-handler workers
    preload_grants: bool = False            # share granted modules via the broker
    policy: sandtrap.Policy | None = None   # bypass the sugar entirely
```

- `stdlib=True` (default) grants the curated safe-stdlib set
  (`nontainer.presets.STDLIB`): math/statistics/decimal/fractions,
  random (minus global seed/state), collections/itertools,
  heapq/bisect, narrow functools (`partial`, `reduce`, `lru_cache`,
  `cache`),
  datetime/time/calendar/zoneinfo, re/string/textwrap,
  difflib, narrow shlex (`quote`, `join`),
  json/csv/struct/base64/binascii/uuid/hashlib, pprint/traceback
  formatters, typing,
  io, VFS-routed os/os.path/pathlib/glob/fnmatch, and
  gzip/zipfile/tarfile. `stdlib=False` for a truly bare cell.
- `pickle` is intentionally excluded: deserialization executes
  attacker-selected reducers outside the sandbox's normal call gating,
  and serialization can invoke object reduction hooks. Trusted
  embedders can still opt in explicitly with
  `modules=[ModuleGrant(pickle)]`; do not do so for agent-controlled
  data.
- `numbers` and `collections.abc` are intentionally excluded:
  `ABCMeta.register` mutates process-global type registries, and module
  member filters do not currently constrain attributes reached through
  an ABC class returned by the module.
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
- `warm_view_workers` (process/kernel only) caps the resident workers kept
  for `exec_python(view=...)` calls — in practice, apps' handler
  dispatch (the live preview, `test_app`, published-app requests).
  `run_python` and plain `exec_python` are unaffected: they run in the
  session sandbox, whose worker is created once at workspace
  construction and held for its life — already warm.

  It is a **latency optimization, not a safety mechanism**. It used to
  be both, when a view sandbox was forked per request from a live ASGI
  server; sandtrap >= 0.3 creates workers from a forkserver broker, so
  that hazard is gone at its source. What remains is worker start —
  which forkserver made *more* expensive, since a worker re-imports the
  granted stack rather than inheriting it: ~18ms and ~23MB on a stdlib
  policy, ~235ms and ~113MB with pandas/numpy/plotly granted.

  The default of `1` keeps the app-iteration loop warm (edit,
  `test_app`, preview — essentially sequential) while holding one
  worker. Raise it for genuinely concurrent serving. Past the cap,
  requests fall back to a per-call sandbox rather than queueing, so
  too-low costs latency while too-high costs memory.

  Two numbers, worth not conflating: **peak** workers during a burst is
  set by concurrency, not by this cap — N concurrent calls means N
  workers alive at once either way. **Resident** workers afterwards is
  `min(N, warm_view_workers)`, and only ever rises toward the cap, because
  transients are reaped when their call ends while pooled ones are kept
  and nothing expires an idle one. So the cap is a floor you fill and
  keep paying for (per distinct view, per workspace), not a ceiling you
  retreat from.

  `0` gives every call a pristine worker, and is the only setting with
  clean process-state semantics: any pool >0 means `sys.modules` and
  module globals outlive the request that touched them, shared between
  handlers of one app.
- `preload_grants` (process/kernel only) imports your granted modules once
  into sandtrap's forkserver broker, so every worker inherits them
  copy-on-write instead of importing its own copy. It is the big lever on
  worker cost and moves both numbers at once — with `dataframes()` granted,
  a worker goes from ~176ms and ~77MB to ~14ms and ~33MB here. It applies to
  **every** worker including the session worker each workspace holds for its
  life, so across many open workspaces it moves more memory than
  `warm_view_workers` does.

  Off by default because preloading runs your grants' *import-time code in
  the broker*: a module that starts a background thread on import leaves the
  broker multi-threaded, and a worker forked from it can inherit a lock held
  by that thread — the exact hang the forkserver default prevents. Your
  grants are yours to vouch for; the stdlib and data-stack presets are fine.

  **It is process-wide, not per-workspace.** The preload list is read once,
  when the broker starts, so the first workspace to start a worker decides
  for the process. Later workspaces still work — their modules are imported
  per worker — and sandtrap emits a `RuntimeWarning` naming what won't be
  inherited. Set it uniformly across the workspaces you build.
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
fork/discard`, `tag/tags/tag_info/delete_tag/at_tag/diff`, `mount`,
`close`.

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

| | versioned | staging | cheap_fork | merge | tags | sql_audit |
|---|---|---|---|---|---|---|
| Kvgit | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ |
| Dir | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| AgentFS | ❌ (spike) | ❌ | ❌ | ❌ | ❌ | ✅ |

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
    session_db: KvgitSessionDb | KvgitStoreDb | None = None,  # conversation in the branch
    terminal_primer: str | None = None, # host guidance → terminal tool
    python_primer: str | None = None,   # host guidance → run_python tool
    **toolkit_kwargs,
)
# checkpoint="turn": one commit per agent turn (the agex model) — wire
# tk.end_turn into Agent(post_hooks=[...]). Crash mid-turn can lose
# the turn's staged work; "call" trades chattier history for max
# durability. Workspace.autocheckpoint is also publicly settable.
# session_db: the db over this same workspace (see below). Naming it
# makes end_turn a no-op — the db commits the turn instead.
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

**`ArtifactPath` (`nontainer.ArtifactPath`).** Values that cannot cross
as data — a plotly figure, a DataFrame, a matplotlib figure, a PIL
image — are written to `<root>/ui/<name>.<ext>` by `run_python` itself,
and the binding becomes an `ArtifactPath`:

```python
r.namespace["ui"]["chart"]        # ArtifactPath('/workspace/ui/chart.table.json',
                                  #              kind='table')
r.namespace["ui"]["note"]         # 'top three'   -- plain data untouched
```

A `str` subclass, so knowing the type is optional: an embedder that has
never heard of it still gets a working absolute path (it compares,
joins and serializes as one). One that cares uses
`isinstance(v, ArtifactPath)`, which a bare path string could not
answer — agents put ordinary strings in `ui` too. `.kind` is **derived**
from the suffix via `artifact_kind`, never stored, so it cannot
disagree with the path.

`ws.read_artifact(path) -> bytes | None` fetches one. It returns
`None` rather than raising when the file is unreadable, which is
exactly the `read_bytes` contract `turn_to_a2ui` documents — the
obvious `lambda p: ws.fs.read(p)` raises `FileNotFoundError` and breaks
the envelope's never-raises guarantee mid-stream:

```python
turn_to_a2ui(prose, artifacts, ws.read_artifact, file_url, surface_id=sid)
```

Bytes, not a parsed payload: every consumer here parses for itself
(a2ui degrades on malformed JSON rather than raising), and a typed
loader would invite reading an artifact as the original object — which
a `head(200)` table cannot honour. Use `ArtifactPath.kind` to decide
how to interpret them.

Because this lives in `run_python` rather than an adapter, it is the
same on every executor: a VM guest serializes the object where it lives
and sends a claim home, the in-process path serializes it here, and
both produce the same binding, the same file, and the same
`ui_problems` when the 8MB cap is hit.

### agno sessions (`nontainer.adapters.agno_db`, `[agno]` extra)

The agent's conversation stored in the workspace branch, so one commit
holds the turn's files, `cache`, cwd **and** memory — `ws.restore()`
rewinds all four, `fork_session()` branches all four.

```python
from nontainer.adapters.agno_db import KvgitSessionDb, fork_session

ws = workspace("chat-42")
db = KvgitSessionDb(ws, db_path="/var/agno")  # db_path: inherited tables
tk = WorkspaceTools(ws, checkpoint="turn", session_db=db)
agent = Agent(model=..., db=db, session_id=ws.session, tools=[tk],
              post_hooks=[tk.end_turn])
```

`KvgitSessionDb` is an agno `JsonDb` whose **sessions table** lives in
the branch:

| key | value |
|---|---|
| `__agno__/session` | session dict minus runs, plus ordered `run_ids` |
| `__agno__/runs/<run_id>` | one run dict each |

One key per run so kvgit shares every earlier run by hash; values are
the JSON-shaped dicts agno hands over, never pickled agno objects.
Every other `BaseDb` table (memories, metrics, traces, evals,
knowledge) is inherited unchanged and writes to `db_path`: those are
cross-session and must not version with one branch.

**The commit trigger.** `upsert_session` writes its keys and then, when
the upsert added or changed a run, checkpoints with `{"tool": "turn"}`
— so the commit happens at the moment agno persists the run. This is
why `session_db=` exists: agno runs post hooks *before* it persists the
session, so `tk.end_turn` would commit the files without the
conversation. With a session db wired, `end_turn` is a no-op and stays
harmless to leave in `post_hooks`.

**One session per workspace.** `get_session` answers only for the
branch's session id, `get_sessions` returns at most one, and an upsert
with any other id raises `NotSupportedError` naming `fork_session` —
which is what stops agno's own `Agent.fork_session` over a per-branch
db (it copies runs into a *new* session id through the *same* db). Note
that agno logs and swallows exceptions from `upsert_session`, so what
the caller sees is that nothing was written. Team and workflow sessions
raise too. For agno's cross-session features, and for agno's own fork,
use `KvgitStoreDb` below.

**Import.** `db.seed(session)` writes a whole `AgentSession` into a
branch that holds no runs yet and commits it — the path for moving an
existing conversation out of another agno db. The session and its runs
land under the branch's own id, whatever id they carried at the source,
so an agent opened on `session_id=ws.session` finds them. It refuses a
branch that already holds runs, which is what keeps the rewind guard
meaningful everywhere else.

**Rewind.** Leave `Agent.cache_session` at its default (`False`): agno
then re-reads the session every run, so a restore needs no invalidation.
With it on, an upsert whose prior runs are not a tail of the branch's
`run_ids` raises (naming `cache_session`) and writes nothing. A tail,
not the whole list, because agno 3.x reads with a run limit and writes
back only the most recent runs; the branch keeps its full list.

```python
child = fork_session(ws, "what-if", conversation="inherit")  # or "fresh"
```

Returns the forked `Workspace`. The fork's session key is rewritten
with `session_id = name` and `session_data["forked_from_session_id"]
= <parent>` (where agno keeps fork lineage), and that rewrite is
checkpointed, so the fork's head is consistent. Drive
it with an agent whose `session_id` is the fork name. `"fresh"` drops
the run keys: a clean chat over the forked files. Rewind first to
branch from any checkpoint with the conversation as it was there.

Driving the fork is the same three constructions over the child:

```python
db2 = KvgitSessionDb(child, db_path="/var/agno")
tk2 = WorkspaceTools(child, checkpoint="turn", session_db=db2)
agent2 = Agent(model=..., db=db2, session_id=child.session, tools=[tk2])
```

kvgit refuses to fork a branch with staged changes, so fork between
turns; in per-turn mode that is exactly when the workspace is clean.

#### `KvgitStoreDb` — one db per store, a branch per session

```python
from nontainer.adapters.agno_db import KvgitStoreDb

db = KvgitStoreDb(
    store,                      # the same store= you pass to workspace()
    open=registry.open,         # session_id -> the LIVE Workspace
    db_path="/var/agno",        # inherited tables, shared by all sessions
)
ws = registry.open("chat-42")
tk = WorkspaceTools(ws, checkpoint="turn", session_db=db)
agent = Agent(model=..., db=db, session_id="chat-42", tools=[tk])
```

The agno-shaped face over the per-branch view: every session call is
routed to a `KvgitSessionDb` over the workspace `open` returns, so the
commit trigger and the guards are the same. `open` must return the
live workspace for an open session (the one its toolkit writes through)
and resume or create the branch otherwise; `WorkspaceTools` checks that
through `db.owns(workspace)`.

What the store adds: `get_sessions` lists every branch (committed heads,
read without opening; filters, `sort_by` `created_at`/`updated_at`,
`limit`/`page`; runs loaded for the returned page only), so
`search_past_sessions` and the AgentOS routers see the store's
sessions. `Agent.fork_session` works: a new id carrying
`session_data["forked_from_session_id"]` forks the parent's branch and
seeds agno's re-keyed copy of the runs — files shared by hash, the
conversation copy not. `get_session` for an unknown id returns `None`
and creates nothing. `delete_session` clears the conversation and
leaves the branch to the embedder.

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
           apps_primer=None,  # embedder guidance APPENDED to the apps
           #   notes (available endpoints, house conventions)
           frontend_notes=None,  # which frontend approach to reach for,
           #   which libraries exist, and where they come from — the
           #   part of the notes only the embedder can know. None = the
           #   built-in block ("plain DOM is the most reliable choice",
           #   Preact/htm from esm.sh, plotly from jsdelivr); "" omits
           #   it; a string REPLACES it — including the plain-DOM
           #   recommendation, which is the point for an embedder
           #   vendoring a design system. Import
           #   render.DEFAULT_FRONTEND_NOTES to extend rather than
           #   discard. Replaces (not appends) because the built-in
           #   block says "copy this exactly" and names a CDN — a
           #   correction underneath it would lose. Air-gapped
           #   deployments set this alongside static_assets.
           csp=None)  # the Content-Security-Policy served HTML carries
           #   AND the one test_app enforces. None derives it from
           #   script_hosts (serve.build_csp); "" disables; a string is
           #   verbatim. Declare it HERE rather than only on
           #   build_router: verification reads the config, so a policy
           #   passed only to the router is one test_app never sees.
           static_assets={})  # {url_prefix: host_dir} — fixed files
           #   served WITH the app but absent from the workspace: a
           #   vendored component library, fonts, a charting bundle.
           #   {"vendor": "/srv/assets"} serves /srv/assets/mui.js at
           #   vendor/mui.js. To the browser what host_objects are to
           #   handlers: embedder-supplied, reached at request time,
           #   outside the versioning plane — so the agent cannot ls,
           #   read, or edit them (it is told so, in a sentence derived
           #   from this mapping; `curl vendor/mui.js` still works), and
           #   they add nothing to commits, forks, or a guest tree.
           #   Same-origin, so script_hosts needs no entry. Assets skip
           #   max_response_bytes and win over a workspace file at the
           #   same path (noted in api.log). See apps.md.

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
    # wire, where an agent-set Content-Type wins over the inferred
    # type. Serving allowlists the rest: content-type, cache-control,
    # vary, etag, last-modified, content-disposition, location, and any
    # x-* custom header. Everything else is dropped — set-cookie,
    # access-control-allow-*, content-security-policy, x-frame-options
    # are the embedder's to set, not the app's, and the proxy commands
    # x-accel-* / x-sendfile / x-lighttpd-send-file are refused out of
    # the x-* namespace so a handler cannot reach an internal location
    # through a server in front.
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
    csp: str | None = None,  # None → config.csp, itself defaulting to
    #   build_csp(config.script_hosts); a string overrides wholesale
    #   HERE ONLY (test_app reads the config, so prefer AppsConfig.csp);
    #   "" disables.
    #   Whatever it resolves to is what served HTML carries: a handler
    #   returning its own Content-Security-Policy has it dropped.
    #   Carries 'wasm-unsafe-eval': browsers gate WebAssembly on
    #   script-src, and test_app enforces the allowlist by intercepting
    #   requests rather than sending this header — so without it a
    #   wasm-backed bundle verifies green and dies published.
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
