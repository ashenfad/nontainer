# API Reference

Everything importable from `nontainer`, `nontainer.providers`,
`nontainer.adapters.*`, and `nontainer.apps`.

## `nontainer.Store` — where sessions live

A `Workspace` is one session's world. A `Store` is the place those
sessions live in, and it owns the verbs about the *set* of them.

```python
Store(
    path: str | Path | None = None,       # default ~/.nontainer
    *,
    backend: "kvgit" | "dir" | "agentfs" = "kvgit",
    provider_factory: Callable[[str], WorkspaceProvider] | None = None,
)
nontainer.store(...)                      # the same, as sugar

store.open(session, **workspace kwargs) -> Workspace
store.sessions() -> list[str]             # session ids on the store
store.exists(session) -> bool
store.delete(sessions, *, min_age=3600) -> None
store.resolve(ref, *, root=None, **settings) -> Workspace   # frozen, at session@commit
store.clean(*, min_age=3600) -> int       # sweep unreachable commits
store.tags -> StoreTags                   # store-scoped tags (below)
store.close() -> None                     # also a context manager
```

Store layout is the backend's: kvgit keeps one shared store at
`<path>/kvgit` with a branch per session, `dir` keeps
`<path>/<session>/`, `agentfs` keeps `<path>/<session>.db`.
`provider_factory` replaces all of that for `open` (bring your own
substrate); the store-level verbs then refuse, because the layout is
the factory's and guessing it would be worse than saying so.

`sessions()` lists session ids only: kvgit's `refs/tags/` tag refs,
the `@` store namespace (where the publication branches and the store's
own read anchor live), and the legacy `__void__` branch are reserved
names, not sessions.

**`store.delete(sessions, *, min_age=3600)`** drops a session's entire
stored state — sessions only: every name must be a session id, checked
before any of them reaches the backend, so the `@store/` branches (a
publication's, the read anchor) cannot be named for teardown; a
publication goes with `unpublish`. It dispatches by backend to the
layout `open` built —
`kvgit` deletes the named branches from `<path>/kvgit`, and with each
branch the session-scoped tags it owns, leaving store-scoped ones
alone, since that scope exists so a publication outlives its session;
`dir` removes the `<path>/<session>/` trees; `agentfs` unlinks the
`<path>/<session>.db` files. Plural because a caller often owns more
than one branch/dir/db per logical session (an app publishing snapshot
branches, a batch cleanup); `sessions` may be a single id or any
iterable. Idempotent — a name that doesn't exist and a store that was
never created are both no-ops. Store-level, not live-session: close
any open `Workspace` on these sessions first (a kvgit store handle
pins its branch). It cleans only the workspace store, never
bookkeeping a caller keeps *beside* it. `min_age` is the orphan
sweep's grace period in seconds (kvgit only), so a concurrent writer
mid-commit is never swept out from under.

**`store.resolve(ref)`** opens a frozen `Workspace` at the exact
commit a ref names. A `Ref` is `session@commit`, optionally
`session@commit:/path` (the path is carried, not yet interpreted);
`Ref.parse(str)` and `str(ref)` are the two directions. Resolving into
a session the store has never held raises rather than creating the
branch.

**`store.tags`** — the store-scoped half of tags, the names that
deliberately outlive the session that made them:

```python
store.tags.add(ws_or_ref, name, *, info=None) -> str   # commit id
store.tags.list() -> dict[str, str]                    # name -> commit id
store.tags.info(name) -> TagInfo | None
store.tags.list_info() -> dict[str, TagInfo]           # every tag, one open
store.tags.delete(name) -> None
store.tags.at(name, **settings) -> Workspace           # frozen snapshot
```

`add` takes the workspace whose current state to name (staged changes
are committed first, so the name means what the caller saw) or a ref
naming an exact commit. A workspace tags through its own provider, so
it must be one this store opened — `Store.open`, or a fork/snapshot of
one; a workspace from elsewhere is refused rather than silently tagging
*its* store. Session-scoped tags stay on the workspace (`ws.tags`). Because a store-scoped tag belongs
to no session, `store.tags.at(name).session` names whichever branch the
read was anchored on — the tag is the identity there, not the session.
kvgit has no handle without a branch, so the read borrows one where it
can and mints one where it cannot: a session if the store has any,
otherwise a publication's own `@store/pub/...` branch, otherwise the
store's own `@store/anchor`, created on that first read and holding a
single empty commit. **A store-scoped tag therefore opens for as long
as it exists**, with every session deleted and nothing published. The
anchor is not a session, `sessions()` never lists it, `delete` refuses
a name that is not a session id and so cannot reach it, and `clean()`
keeps it: a branch head is a GC root, and the commit it holds owns
nothing to sweep.

**Frozen opens take execution settings.** `store.tags.at(name,
**settings)`, `store.resolve(ref, *, root=None, **settings)` and
`Publication.open(version=None, **settings)` accept `Store.open`'s
construction keywords — `python`, `mounts`, `commands`, `cache`,
`max_observation`, `executor_factory`, `root` — applied to the frozen
workspace they return. `autocommit` is not among them: a frozen
provider commits nothing, so the flag has nothing to switch. Nor are
`provider` and `executor`, for the reason `Store.open` refuses them —
the store builds the provider, and an executor instance is bound to
one session, so a caller hands over the `executor_factory` instead. An
unrecognized keyword raises `TypeError` naming the set.

The rule is that a commit holds the tree and nothing else: a live
sqlite handle is not a file, which is why `host_objects` are injected
rather than committed. So the store, which opens a tree rather than a
session, has no settings to inherit, and the embedder supplies them at
the call — `pub.open(python=PythonConfig(host_objects={"db": db}))` is
how a published handler reaches a live database. `Publication.open`
takes no `root`: a version records the workspace root its files were
published under, and reading them at another one finds an empty tree.
A session's own `ws.tags.at(name)` is the other shape — it inherits
the settings of the session it came from.

A **writable mount is refused** (`ValueError` naming the point). A
frozen workspace accepts no writes from anyone, and a mount is the one
part of its filesystem that is a real host directory rather than a
state in the store — `ws.files.fs` hands out the composed filesystem,
so a mount left writable would carry a write through to that
directory, permanently and outside the versioning plane. The flag is
surfaced rather than coerced: silently flipping it would hand back a
workspace whose mounts do not do what the caller asked. Pass
`Mount(..., readonly=True)` and the bytes read as they do anywhere
else.

**`store.publications`** — the app, published: an immutable version of
part of a session's tree, with a name and a pointer saying which
version is current.

```python
store.publish(ws, name, *, paths=("app/",), version=None, current=True,
              info=None) -> Publication
store.publications() -> dict[str, Publication]         # by name
store.publication(name) -> Publication | None
store.set_current(name, version) -> Publication
store.unpublish(name, version, *, min_age=3600) -> None

Publication: .name, .versions -> tuple[Version, ...], .current
             .version(name) -> Version | None
             .current_version -> Version
             .open(version=None, **settings) -> Workspace   # frozen, at that version

Version:     .name, .version, .tag, .ref, .published_from, .created
```

What lands is **the subtree, not the session**. `publish` derives a new
commit holding the files under `paths` and the filesystem rows that
describe them — no cache, no working directory, no ws-git blob, no
conversation record, no file from outside `paths`. Provenance is a soft
reference in the commit's info (`published_from`, a `Ref`), not a parent
pointer, so a version is self-contained: it reads alone, it exports
alone, and it pins none of the session's history against `store.clean`.
Deleting the session leaves every version of it exactly as it was.

`paths` are workspace paths: relative entries are taken under `ws.root`
(`"app/"` means `<root>/app`), absolute ones as given, trailing slash
optional. Publishing paths that hold no files is refused rather than
producing an empty version. `ws` must be one this store opened and must
be clean — publish names a commit, so land staged changes with
`ws.commit()` or drop them with `ws.discard()` first. `version` defaults
to `v<N>`, one past the highest `v`-number the lineage holds. Only
names of that exact shape are counted, so a lineage holding `v1`, `v2`
and `release-1` gets `v3`: the default is a series of its own, and a
version the caller named stands outside it. An embedder that wants
every version numbered passes `version=` itself. An explicit name must
be unused, because versions never move.

`info` may not set `tool`, `name`, `version`, `published_from` or
`paths`: those are what publish writes into the commit, and the commit is where a
reader checks where a version came from. A clash raises `ValueError`
naming the keys, rather than being silently overridden — a false
provenance in an immutable commit outlives every chance to notice it.

The publication verbs split their refusals by whose mistake it is. A
**`ValueError`** is the call's: a name that is not session-id shaped, an
`info` key publish writes itself, a version name the lineage already
holds, `paths` matching no file, and a version name `set_current`,
`unpublish` or `Publication.open` cannot find. An embedder mapping
errors to HTTP answers 400 to all of them. A **`WorkspaceError`** is the
store's state: a session with staged changes, a leftover tag or branch,
a version unpublished since the `Publication` was fetched. A
**`NotSupportedError`** (a `WorkspaceError`) says this store cannot
publish at all — no branches and tags under it, or a `provider_factory`
layout it does not own.

One publish writes three things:

- a reserved branch `@store/pub/<name>/<version>` holding the derived
  commit, started from the store's empty root. It is what lets a
  version be opened without borrowing a live session, and a
  store-scoped read anchors on it before the store mints an anchor of
  its own. It is not a session — `store.sessions()` never lists it,
  `@` being a character no session id may start with — but it is a
  legal `Ref` target, so `store.resolve(str(version.ref))` opens it.
- a store-scoped tag `<name>/<version>` naming that commit.
- a record in the **publication registry**, `publications.json` under
  the store path, written atomically (write-then-rename). A store with
  no directory of its own (`provider_factory`) keeps it in memory for
  the life of the `Store`. Its shape:

  ```json
  {
    "scoreboard": {
      "versions": {
        "v1": {
          "tag": "scoreboard/v1",
          "ref": "@store/pub/scoreboard/v1@<commit>",
          "published_from": "author@<commit>",
          "created": 1788897774.69,
          "root": "/workspace"
        }
      },
      "current": "v1"
    }
  }
  ```

  The registry is **generic**: no token, no route, no database. An
  embedder serving a publication keeps those in its own table keyed by
  name — see `docs/apps.md`.

  The record is the last of the three to land, so a process that dies
  mid-publish leaves a branch, and usually its tag, that no record
  names. Publishing the same thing again adopts it: a recordless
  branch whose head commit names the same source commit and the same
  `paths` was written by that attempt and no other, so its commit
  becomes this publish's and the record is written over it — one
  version, one branch, no duplicate. A recordless branch of some other
  attempt is refused by name, and `unpublish` clears it.

  Every mutation reads, decides and writes under one lock — a
  `flock` on `publications.lock` beside the registry for other
  processes, and a per-path `threading.Lock` for this one's threads —
  so two publishers cannot pick the same version number or drop each
  other's record. On a platform without `fcntl` the in-process lock is
  the whole guarantee, and two processes publishing to one store can
  still lose a record.

Blobs are copied into the derived commit rather than pointed at. That
is fine at app sizes, and content addressing under kvgit would make the
copy free.

A `Publication` is a **snapshot of the registry**, not a capability:
`open()` re-reads it and refuses a version that has since been
unpublished, and so does `store.resolve` on a publication's ref. The
same goes for the branch — a reserved `@store/` branch is never created
by being opened, so a stale ref cannot resurrect what an unpublish
deleted (which would then block republishing that version).

`set_current` is the mutable half: it moves the pointer, and moves it
back as easily. That is true for **code**. Data a version's handlers
wrote lives outside the workspace and does not roll back with it.

`current=False` records the version and leaves the pointer where it
is, so a caller can land a tree, check it at `pub.open(version)` and
switch with `set_current` after. The version that opens a lineage takes
the pointer whatever the flag says, because a publication must point
somewhere.

`unpublish` removes a version's tag, its branch and its record. The
current version is refused while others remain, so undoing a publish
that went wrong means moving the pointer back with `set_current`
first — or publishing with `current=False`, which never takes it. The
last version of a publication may go however it is pointed at, and
takes the publication's record with it. A version with no record but a
branch or a tag of its name is cleared the same way, so an operator can
free the name a publish that died mid-way left reserved — but only what
publish itself wrote: a store tag may hold a slash, so `release/prod`
answers to `unpublish("release", "prod")` by name alone, and a tag or
branch whose info does not carry publish's provenance (`tool`, `name`,
`version`, `published_from`, `paths`) is left where it is and the call
raises `ValueError`.

**Not yet:** `store.shared(name)` raises `NotImplementedError`; it is a
later stage of the API plan.

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
    autocommit: bool = True,
    max_observation: int = 32_000,
    executor_factory: Callable[[], Executor] | None = None,
    root: str = "/workspace",
) -> Workspace
```

Sugar for `Store(store, backend=backend).open(session, ...)` — the
shortest way in when a caller has one session in mind. Session
resolution: `kvgit` → branch per session in one shared store; `dir` →
`store/<session>/`; `agentfs` → `store/<session>.db`. `provider`
overrides `backend`/`store` entirely, the same substitution
`Store(provider_factory=...)` makes, for one session. Session ids are
validated (`SESSION_ID_RE`) on every path — they often flow from
untrusted input.

`root` is the **workspace root**: the absolute path agent-visible
files live under, and the one path contract shared across executors.
cwd starts there, VFS module imports resolve from it, skills install
to `<root>/skills`, the app tree is `<root>/app` — and a VM executor
(dud) mounts its guest workspace at this exact path, so an absolute
path in agent code names the same file on every executor. Forks
inherit it. `root="/"` selects the flat pre-0.2 layout (no VM path
parity — a guest can't mount at the fs root).

## `Workspace`

One instance == one session's world. Not thread-safe: one workspace,
one thread at a time (adapters enforce this with a lock). Context
manager (`with ... as ws:` closes on exit).

The session's own state — the two tools, the commit flow, the lock —
is on the object; everything else is reached through a namespace that
says which seam it belongs to:

| | holds | |
|---|---|---|
| `ws.files` | the file surface | read / write / edit / put / get / list / exists / read_artifact / attach / detach / attachments / export / fs |
| `ws.index` | the agent's own git | stage / unstage / commit / status / discard / log / head / checkout |
| `ws.tags` | this session's tags | add / list / info / delete / at |
| `ws.runtime` | how code runs | the executor, commands, shell variables, the raw calls |
| `store.tags` | names that outlive the session | add / list / info / delete / at |

The namespaces are views: they hold the workspace and no state, so
`ws.files` is the same object every time you ask and costs nothing to
reach.

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
workspace serialize safely (each atomic + committed) instead of
corrupting staged state. Read-only accessors don't take the lock —
and neither do the host-side escape hatches (`ws.files.fs` writes, `ws.cache`
mutation), so a host thread using those while agent calls run holds
`ws.lock` itself (see the extension surface below).
`run_in_threadpool(ws.run_python, code)` from Starlette works too if
you'd rather not use the facade.

`terminal` executes pipes, redirects (`> >> <`), `&&`/`||`/`;`,
quoting, ~33 builtins (via termish) plus injected commands. `cd`
persists across calls (and is committed with them on kvgit, so a
checkout restores it).
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
    commit: str | None = None       # commit this call created
    # truthy iff exit_code == 0

@dataclass(frozen=True)
class PythonResult:
    stdout: str; stderr: str = ""       # stderr chatter ≠ failure
    error: str | None = None            # rendered traceback, or None
    ticks: int = 0; duration: float = 0.0; truncated: bool = False
    namespace: Mapping[str, Any] = {}   # for the HOST; adapters never
                                        # inline it into observations
    commit: str | None = None       # commit this call created
    ui_problems: tuple[str, ...] = ()   # why a `ui` value did not render
                                        # as intended (the 8MB cap, with
                                        # the remediation) -- actionable
                                        # text meant to reach the agent
    # truthy iff error is None

@dataclass(frozen=True)
class WriteOutcome:                     # from files.write / files.put
    path: str; size: int; created: bool
    commit: str | None = None
```

Every mutating call's result pins the commit its autocommit
created — `ws.checkout(result.commit)` is compensation by identity,
no step counting. `commit` is `None` when nothing was committed:
read-only call, no-op edit, turn-mode committing (the id comes from
`end_turn()` instead), or an unversioned provider. Host-facing like
`namespace` — adapters never render it into the model's observation.

Oversized stdout from `print()` is re-rendered **budget-aware** via
[reprobate](https://github.com/ashenfad/reprobate): structural elision
(`[0, 1, 2, ...996 more]`) instead of a mid-token cut. Small output
stays byte-exact; non-print writes fall back to a head-cut.

### Files (`ws.files`)

```python
ws.files.read(path) -> bytes                 # raises if absent
ws.files.exists(path) -> bool
ws.files.list(path=".", recursive=False) -> list[str]
    # files and directories, sorted, spelled the way `path` was —
    # absolute in, absolute out, so an entry goes straight back into
    # `read`. One level down by default; `recursive` takes everything
    # beneath, walking with the filesystem's own recursion (which does
    # not descend into symlinked directories, so a link to an ancestor
    # is listed, not followed).
ws.files.fs                 # termish-protocol filesystem (seed/harvest directly)
ws.cache              # MutableMapping; raises NotSupportedError if disabled
ws.files.write(path, content) -> WriteOutcome   # parents created; committed
ws.files.edit(path, old, new, replace_all=False) -> EditOutcome
    # exact-string replacement with agent-tolerant fallbacks (the agex
    # strategy set, ported): exact → trailing-ws-flexible →
    # indent-flexible (replacement re-indented to the file's baseline);
    # replacement-already-present → no-op (count=0, "already_applied").
    # Unique-match-or-replace_all; WorkspaceError with a "did you mean
    # these lines?" snippet otherwise. Carries `commit` when the
    # edit committed.
ws.files.put(src, dest=None) -> WriteOutcome # host file → workspace (committed)
ws.files.get(src, dest=None) -> bytes        # workspace → host (never commits)
ws.files.read_artifact(path) -> bytes | None # unreadable IS the answer
ws.files.export() -> ContextManager[Path]    # the workspace at a real
                                             # path (FUSE providers only)
ws.runtime.register_command(name, fn)          # add a termish command post-construction
```

Writes through `ws.files` are the same operation the file tools
perform: the single-writer lock is held for the call and the commit
flow runs when it lands. Reads take no lock and never commit.
`ws.files.fs` is the escape hatch below all of that — documented and
supported, but it writes straight to the provider, so a host thread
using it while agent calls run holds `ws.lock` itself.

Cache key rules: str keys, no `__` prefix, no `/`; values validated
picklable at write (`CacheError` otherwise). Cache holds **data**;
reusable code belongs in `helpers/` files.

### Versioning (gated by `ws.caps`)

Commits cover workspace-owned files and cache. Host-object calls
and mounts are external effects: their data is not committed,
restored, or copied by a fork. A fork does inherit the mount *points*,
and sees the same live directories behind them.

```python
ws.head: str | None      # current commit id; None if unversioned.
                         # Pins read-only observations (reads don't move
                         # it) — exact iff not ws.uncommitted
ws.uncommitted: bool     # the store's buffer holds writes no commit took
                         # (the framework's question; "does the AGENT have
                         # uncommitted work" is ws.index.status())
ws.ref: Ref              # this session at its current commit
ws.commit(info: dict | None = None) -> str   # everything: files+cache+cwd
ws.checkout(ref) -> str                  # restore a commit of THIS session
                                         # (appends; returns the new commit)
ws.checkout(ref, paths=[...]) -> str     # TAKE those paths from any ref
                                         # (a named directory is mirrored)
ws.log(limit=None, kind="work"|"agent"|"all") -> Iterable[CommitInfo]
ws.fork(name, *, at=None, inherit="full"|"fresh", paths=None) -> Workspace
ws.merge(source: str) -> MergeOutcome            # needs caps.merge
ws.discard() -> None                             # drop staged writes
ws.autocommit: bool                              # settable; see below

ws.index.stage(paths) -> tuple[str, ...]         # needs caps.index
ws.index.unstage(paths) -> tuple[str, ...]
ws.index.commit(message=None, *, info=None) -> str   # the staged set
ws.index.status() -> WorkspaceStatus             # pure read
ws.index.discard() -> None                       # abandon the composition
ws.index.log(limit=None) -> list[CommitInfo]     # the agent's own commits
ws.index.head -> str | None                      # the agent's last commit
ws.index.checkout(commit) -> str                 # restore the tree to one
                                                 # of the AGENT's commits
```

**`ws.index` is the agent's own git**, and a fiction over this
session's history: the index and the agent's commit graph are metadata
in a reserved key, `status` measures against the agent's last commit
rather than the store's head, and `log` walks the agent's commits and
not the framework's. `ws-git` in the terminal is the same
implementation with the agent's spelling, so host and agent see one
index — registered by `nontainer.wsgit.register_wsgit(ws)`, which the
embedder calls and no adapter calls for it, where `ws.index` is on
every workspace unconditionally. See [design.md](design.md) for the
model.

`WorkspaceStatus` carries `branch` (the session), `staged` and
`unstaged` (workspace paths, both measured against the agent's last
commit rather than the store's head), and the live merge context:
`merge_source`, the session an outstanding merge came from or `None`,
and `merge_unresolved`, the paths that merge left still carrying
markers.

`ws.index.checkout(commit)` takes one of the AGENT's commits — what
`ws.index.log()` lists — and refuses any other commit in the session's
history, because a commit with no place in the agent's graph would
strand its whole log behind it. Moving the session to an arbitrary
store commit is `ws.checkout(commit)`, the host's verb.

**Two commit verbs, and they are for two different callers.**
`ws.commit()` takes everything uncommitted, always — it is the
framework's durability verb, and the code around a workspace (a turn
hook, a session db, a skill installer) can only rely on it if its
scope never depends on what the agent has staged. It is invisible to
the agent: a framework commit leaves the staged set staged and the
work in progress modified, because both are measured against the
agent's own last commit. `ws.index.commit(message)` is the agent's
verb: the staged set (everything modified when nothing is staged),
committed as a point in the agent's graph, with the work in progress
left in the tree and out of the commit. The terminal's `ws-git commit`
is the same verb — it reads its context, because a person at a
terminal can see the status line and has no `-a` to ask with.

The content hash of the head — the identity of *what* the files and
cache are, where `head` identifies the point in history — is
`next(iter(ws.log(limit=1))).tree`.

**`ws.checkout(commit)`** makes this session what it was at one of its
own commits — files, cache, cwd, the ws-git blob, the stored
conversation, everything it holds — and returns the id of the commit
that lands. Uncommitted writes are replaced by the restored state
(`ws.discard()` is the verb for dropping them on their own). It never
switches sessions: a session IS a branch, so anything that is not a
commit here is refused with a message naming `ws.fork(name)` and
`store.open(name)` rather than guessed at.

**It appends.** The restored state is written and committed, so the
returned id is a *new* commit and everything committed since the target
is still in `ws.log()`. Nothing leaves the branch, `store.clean()` has
nothing to collect, and an undo is redo-able. A checkout onto state the
workspace already holds writes nothing and returns the current head.
The agent's git rewinds with the tree — its head and graph live in a
key the checkout restores like any other — so after a checkout to a
commit made when `ws.index.head` was X, it is X again.

Going back is by identity, not by position: name the commit from
`ws.log()`. That also makes the redo obvious — a checkout appends, so
the commit it stepped off is still one entry back in the log, and
checking it out returns you to it.

**`ws.fork(name, *, at=None, inherit="full", paths=None)`** branches
this session into a new one and opens it.

**A fork point is always a commit.** Uncommitted writes here are landed
first, under `{"tool": "fork", "child": name}`, and that commit is the
child's base and the merge base for the way back. Forking a copy of the
staging buffer would give the child a state that never existed in
history and a merge base predating this session's own edits. With `at`
nothing is committed: the buffer belongs to this session's present, not
to the past the fork branches from.

`inherit` decides whether the stored conversation comes along, and
nothing else. `"full"` (default) keeps it — the continue-where-I-am
fork. `"fresh"` drops every `__agno__/*` key on the child's first
commit, for a delegate that starts a chat of its own over these files;
it touches no file. A brief, a summary, a distilled context is content
the caller supplies with the child's task — nothing here can write one,
since nontainer stores the conversation and does not interpret it.

**A fork starts with a fresh ws-git state**, under either inheritance:
no `ws.index.head`, nothing staged, no inherited merge context. A
branch carries the workspace; an index is a composition in progress,
and a delegate does not start halfway through somebody else's. So the
child's `ws.index.log()` is its own (`ws-git log <parent>` reaches the
parent's), and — the reason it matters — a child that never uses ws-git
merges and is taken from at its branch head, the rule the fiction
already had for a session with no agent commit. The reset lands as a
`ws-git.fork` bookkeeping commit, so it is at the child's store head
for any reader; it is hidden from `ws.log()` and shown by `kind="all"`,
and a parent that never used ws-git leaves nothing to reset and costs
the fork no commit. Before its first commit a child reads the way a
repo does before its first: everything it can see is modified.

`paths` narrows the child's **view**, not its tree. Its branch holds
everything this one had; its filesystem lists and reads only the seeded
paths (directories or files, absolute or relative to the root). So
"push this content to the delegate" costs no prune commit and the merge
back stays an ordinary three-way with no rule to special-case. This
session sees the child's whole branch regardless — `ws.diff`,
`ws.checkout(ref, paths=)`, `ws.files.attach`.

**One write rule, better than git's sparse checkout.** The child may
CREATE a new path anywhere (it merges as an addition, and notes and
scratch are ordinary work); a created path joins its view, so it can
read back what it made. Modifying or deleting a path that exists
outside the view is refused with `PermissionError` naming the view: you
cannot overwrite what you cannot see. The view is drawn around the
FILE, not the name — a link is a second name for one, so a link whose
target the view hides cannot be made, and an existing link out of the
view reads as absent rather than becoming a way through. Locally the rule is enforced in
the filesystem the sandbox holds; on a guest rung only the seeded
subtree is materialized and the same rule is applied to the write
harvest, so a call that tried lands nothing and reads as errored.

The view is recorded on the child's branch under a reserved key, so a
reopened delegate is narrowed exactly as it was, and it merges
`MergeChoice.OURS` — merging a narrowed delegate never narrows the
caller. A fork given the whole tree records nothing, so a child of a
narrowed session does not inherit its blinkers.

**`ws.checkout(ref, paths=[...])`** is the second form of one verb, and
git's `restore --source=<ref> -- <paths>`: it makes those paths match
any ref and leaves everything else alone. `ref` is a `session@commit`,
a session name (that session's last *agent* commit — the same reading
`merge` takes, so the two cannot disagree about what a delegate said),
or a commit of this session. A path that names a *directory* mirrors
that subtree — a file it holds here and the ref does not is removed,
so taking a delegate's `pkg/` cannot leave behind the `pkg/old.py` the
delegate deleted — while a path that names a file moves that file and
removes nothing. They land as ordinary writes and removals, so the
view rule and the agent's own status see them as work in the tree, and
the commit records `{"tool": "checkout", "taken_from": "<ref>",
"paths": [...], "removed": [...]}` (`removed` only where the mirror
dropped something) — a soft reference, not ancestry. Only file keys
move, so the merge-policy question never arises.

**`ws.files.attach(ref, at, *, readonly=True, root=None)`** mounts
another session's tree, frozen at a commit, inside this one at `at`;
`detach(at)` removes it and `attachments()` lists `{point: ref}`. For
reading someone else's work in place — a delegate's branch while
deciding whether to merge it — without copying it in. Explicit and
never automatic. Like a `Mount`: NOT versioned, not captured by
commits, not carried by a fork, gone when the session closes;
`readonly=False` is refused, since a frozen state has nothing to offer
it. What lands at `at` is the source's workspace ROOT, so a file the
delegate calls `auth.py` reads as `<at>/auth.py` — a lineage shares one
root, so that root is this session's, and `root=` names a different one
for a session from elsewhere. What lands is the source's **whole
branch**, not what its own session can see: a delegate given a narrow
view is exactly the one worth attaching. A point inside this session's root
reaches every rung; outside it, an executor running elsewhere never
sees it — the contract a `Mount` outside the root already has. And as
with a mount, while anything is attached the working directory belongs
to the composition, so a session committed in that state reopens at its
root rather than where its agent was standing.

**`store.fork(src, dst, *, at=None, inherit=, paths=)`** is the same
verb for host code with no workspace open, and takes `store.open`'s
keywords for the workspace it returns. `root=` opens the source at that
root too: a lineage shares one, and a view normalized against a
different root would name paths the child cannot see.

**`store.resolve(ref, *, root=None, **settings)`** reads a commit under
a given workspace root. A commit holds its files at whatever root the
session that made them used, so resolving at the wrong one reads an
empty tree. `settings` are the rest of `Store.open`'s construction
keywords, applied to the frozen workspace it returns (see *Frozen opens
take execution settings* under `store.tags`).

**`ws.merge(source)`** merges another session into this one
(`caps.merge`). **A merge takes only what has been committed, on both
sides, and refuses a source that has more — and with `caps.index`
that means committed by the agent:**

- This session must have nothing modified against its own last ws-git
  commit. Autocommit keeps the store's buffer clean while an agent is
  composing, so the buffer is not the question; the refusal names the
  two fixes (`ws-git commit -m ...` to land the work, `ws-git checkout
  <your last commit>` to drop it). A session that has never made a
  ws-git commit has no such baseline, so only an open index refuses
  there.
- The source is merged at ITS last agent commit, whose tree is exactly
  what that agent committed — not its store head, which also holds
  whatever the framework committed for it since. Symmetrically, a
  source with anything newer than that commit is REFUSED rather than
  merged at a state its agent has moved past; the refusal names the
  same two fixes, in that session's terms. A source that never used
  ws-git has no such baseline and is merged at its store head, as
  before.

File conflicts land as conflict markers IN the merge commit and are
reported in `MergeOutcome.conflicts` rather than blocking it: resolve
them with ordinary edits and commit — the paths the merge reports are
exactly the ones `ws-git status` then shows as `UU`, and each stops
being unresolved when its markers are gone, not when it is committed.
A file is reported once, by path, however many keys it occupies: its
bytes and the metadata beside them are two keys describing one file.
Non-file contested state, which no merge function can resolve, aborts
the merge untouched (`merged=False`, `commit=None`); so does a path
that is a file on one side and a directory on the other, which is
reported by path like any other file conflict.

`MergeOutcome.commit` is the merge commit; `ws.head` afterwards is the
bookkeeping commit that records it as the agent's, which is what
leaves a clean workspace behind a merge. `MergeOutcome.auto_merged` is
the other half of `conflicts`: the paths the merge changed and settled
on its own, so the two together are everything the merge touched.

Unversioned providers raise `NotSupportedError`; `autocommit` is
forced off for them. With autocommit on, each successful mutating
tool call commits with `info={"tool": ...}`; read-only calls never
commit. `info` dicts must be JSON-serializable.

Equal trees mean identical content, whatever the metadata or ancestry
around it. The converse does not hold: kvgit stamps each write with
when it happened, so rewriting a file with the same bytes still moves
the tree — ask `changed_since` when the question is content.
`CommitInfo.tree` carries the same hash per history entry (`None`
on a provider that keeps no such hash).

**`ws.log(kind=)`** says whose commits, and the default hides the ones
nobody performed:

| kind | what it yields |
|---|---|
| `"work"` (default) | every commit except the ws-git fiction's bookkeeping (the working-tree restore after a partial commit, the tree a checkout writes, the blob a merge records) — the framework's commits and the agent's |
| `"agent"` | the agent's own commits alone: the same set `ws.index.log()` walks, read off the store's history rather than the agent's graph |
| `"all"` | the store's history exactly as the provider keeps it, bookkeeping included |

`limit` counts what comes back, not what was read: `log(limit=5)` is
five commits of the kind asked for, however many stand between them. A
limit that asks for nothing gets nothing, whatever the kind —
`limit=0` is an empty log, and so is a negative one, matching the
provider's own history. A provider with no index makes no bookkeeping
commits, so `"work"` and `"all"` are the same history there. An unknown
kind raises `ValueError`. `ws.index.log()` is the agent's own fiction
and takes no `kind`.

`CommitInfo.parents` carries the ids a commit descends from: one for an
ordinary commit, two for a merge, empty for a root commit and for a
provider whose history is a list rather than a graph. A merge's first
parent is the side it was made from, so `ws.diff(merge.parents[0],
merge.id)` is the merge's own work.

#### Tags (`ws.caps.tags`)

A tag is a name for a commit that outlives the call that made it —
immutable, and a garbage-collection root: the named commit and its
ancestry stay reachable for as long as the name does.

```python
ws.tags.add(name, *, at=None, info=None) -> str       # commit id
ws.tags.list() -> dict[str, str]                      # name -> commit id
ws.tags.info(name) -> TagInfo | None
ws.tags.delete(name) -> None
ws.tags.at(name) -> Workspace                         # frozen; see below
ws.diff(a, b) -> WorkspaceDiff                        # two commit ids
ws.changed_since(ref) -> WorkspaceDiff       # tag name, commit id or Ref

store.tags.add(ws_or_ref, name, *, info=None) -> str  # the store scope
store.tags.list() / .info(name) / .delete(name) / .at(name)
store.tags.list_info() -> dict[str, TagInfo]          # every tag, one open
```

**Two scopes, and the object you reach through is the scope** — no
argument to pass, and no flat namespace for embedders to partition
themselves:

| scope | belongs to | survives `Store.delete` | for |
|---|---|---|---|
| `"session"` (default) | this session | ❌ — it goes with the branch | commits worth naming: "before the refactor", "the state the report was built from" |
| `"store"` | no session | ✅ | publications: the snapshot an app serves, the state a link points at |

`ws.tags.list()` lists this session's own, so two sessions can each
hold a `v1` and neither sees the other's; `store.tags.list()` is the
one listing every session can read. Names come back the way you
passed them; the scope prefix kvgit stores them under (`<session>/`,
`@store/`) never surfaces, and a name that starts with one is rejected
so a scope can't be spoofed. The store prefix leads with `@`, which a
session id can never start with, so the two scopes cannot collide
however a session is named. Otherwise the rule is kvgit's: any
non-empty name without `%`, `/` included. Tags never move — an existing
name raises rather than being repointed. `ws.tags.add()` commits staged
work first (`info={"tool": "tag", "name": ...}`, like `fork`), so the
name means what the caller saw.

`store.tags.list_info()` is the bulk read: every store-scoped tag
described on one backend open, where `info(name)` opens the store for
each call — a store keeps no handle of its own, and holding a branch
open by name would create that branch. `list()` stays the cheaper
answer when only the commit ids are wanted. A session's tags are read
through the workspace's own live handle, so `ws.tags.info()` opens
nothing and needs no bulk form.

`TagInfo` carries `name`, `scope`, `id` (the commit), `tree`,
`time`, the `info` dict, and `dangling` (the commit is not in the
store — damage, not an ordinary state).

**Frozen workspaces.** `ws.tags.at(name)` returns a `Workspace` over the
tagged state that can be read but never written: `ws.frozen` is True,
`autocommit` is forced off, `file_write` / `file_edit` / `put` /
`commit` / `fork` / `tag` / `checkout` raise `NotSupportedError`,
and the executor holds a read-only filesystem **and a read-only
cache**, so a shell redirect, `open(..., "w")` or `cache["x"] = 1` from
agent code fails where it happens with a message naming the tag
(`ws.cache["x"] = 1` from the host raises `PermissionError` the same
way). On an executor with its own substrate — a dud guest — the write
lands in that substrate before anything here can stop it, so the
workspace refuses the harvest instead: nothing is absorbed, the guest
is re-synced from the frozen tree, and the call comes back with a
non-zero `exit_code` / an `error` carrying the same refusal.
`ExecutionContext.frozen` tells an executor it may refuse earlier;
nothing depends on it doing so. Reads, `run_python` that only reads,
and apps dispatch all work; `discard()` and `close()` work. It inherits the parent's
construction settings the way `fork` does — python config **including
its live host objects**, mounts, root, executor factory, commands — so
an app served from a snapshot still talks to the session's live db.
The files are frozen; the host's world is not. A store-level frozen
open (`store.tags.at`, `store.resolve`, `Publication.open`) opens a
tree rather than a session, so it has no settings to inherit and takes
them as keywords at the call instead.

**`changed_since`** takes a tag name, a commit id or a `Ref`, and
compares it with the current head. A name is looked up as this
session's tag first and the store's second, so `ws.changed_since("v1")`
is the everyday spelling and a published store tag needs no extra
argument.
`WorkspaceDiff` holds `added` / `removed` / `modified` as absolute
workspace paths — `/workspace/data/in.csv`, the way agent code and
`ws.files.fs` name files. Framework keys (cache, cwd, the stored
conversation, the filesystem's own bookkeeping) are not files and never
appear, and staged-but-uncommitted work is not in the diff at all
(check `ws.uncommitted`).

It also carries `seed`: the paths the session the diff *ends at* was
seeded with, when it was narrowed by `fork(paths=)`, and empty
otherwise. `diff.in_seed` and `diff.elsewhere` split `diff.paths` by
it, which is how a caller reading a delegate's diff tells the work it
sent the delegate to do from everything else it touched. The merge
takes both — the grouping is what stops the second from going
unnoticed. It groups by the *seed*, not the delegate's current view: a
file the delegate created is in its view because it made it, and that
is exactly a change the caller has not seen before. `ws-git diff
<session>` prints the same split under `# N path(s) in <session>'s
seed` / `# N path(s) elsewhere`.

`modified` is the content question: a file re-saved with the bytes it
already had is not a change, even though it is a new write. kvgit
compares blob pointers, so the provider reads the bytes at both
commits for the paths that diff flagged — one read per candidate per
side — and keeps only the ones that really differ. `tree` behaves the
other way and is meant to: it moves on any write, because kvgit stamps
each entry with when it happened. `tree` identifies this exact write;
`diff` identifies the content.

### Introspection

```python
ws.session: str
ws.caps: Capabilities         # what the PROVIDER can do
ws.root: str                  # the workspace root (see the factory)
ws.frozen: bool               # a read-only snapshot at a tag (tags.at)
ws.runtime: Runtime           # how code runs against this state (below)
ws.runtime.supports_commands: bool    # executor capability, below
ws.runtime.supports_ws_verbs: bool
ws.runtime.cache_enabled: bool
ws.runtime.python_config: PythonConfig
```

`ws.caps` describes the substrate: versioning, staging, cheap forks,
merge, tags, the index. Execution capabilities are the runtime's,
because they belong to the executor rather than to where state lives —
the same workspace answers differently under a different
`executor_factory`.

**`ws.runtime.supports_commands`** — whether injected terminal commands
(`commands=`, `ws.runtime.register_command`) actually reach the shell. It's an
`Executor` capability, in the same declare-the-difference spirit as
`ws.caps` for providers:

| Executor | `supports_commands` | why |
|---|---|---|
| `LocalExecutor` | `True` | termish receives the mapping, so an injected command is a real command |
| `DudExecutor` | `False` | a guest runs actual bash, so injected commands don't reach it — but `ws-*` verbs (ws-git, ws-curl) ferry in over hostcall |

Tool descriptions gate on it — the apps primer teaches `ws-curl` only
where it exists (injected commands, or the `ws-*` ferry on guests), since promising an agent a command that answers
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
ws.runtime.exec_python(code, *, inputs=None, stdin=None, argv=None,
               echo=None, view=None) -> PythonResult
    # the raw execution path: no commit, no lock. `view` (a
    # ViewSpec) requests a restricted, budgeted execution — a
    # read-only fs/cache view, a tighter timeout/tick budget, contract
    # classes in scope — and is executor-neutral: no sandbox object
    # crosses the seam. `echo` overrides expression echo for the call;
    # stdin/argv expose sandtrap's synthetic `sys`. Safe to call
    # concurrently with a `view` (frozen app serving does); callers
    # whose work mutates the workspace hold ws.lock.
ws.lock: threading.RLock
    # the single-writer lock the mutating public methods hold. Hold it
    # for host-side/extension work that mutates the workspace (ws.files.fs
    # writes, ws.cache mutation, read-modify-write) and must serialize
    # with tool calls. RLock: safe to hold around locked public calls.
```

## `Runtime` (`ws.runtime`)

The executor half of a session. `Workspace` holds the state — the
provider, the lock, the commit flow, cwd, the cache key rules;
`Runtime` holds everything about *running code* against it.

```python
Runtime(ws, *, executor=None, python=None, mounts=None,
        commands=None, max_observation=32_000)

rt.executor -> Executor            # the bound executor
rt.python_config -> PythonConfig
rt.supports_commands / rt.supports_ws_verbs -> bool
rt.exec_python(code, ...) -> PythonResult   # raw: no lock, no commit
rt.exec_shell(script) -> TerminalResult     # raw: no lock, no commit
rt.register_command(name, fn, *, rebind=None) -> None
rt.env -> MutableMapping[str, str]  # the shell environment, live
                                    # (rt.env["X"] = "1"; del rt.env["X"];
                                    #  dict(rt.env); a fork replays it)
rt.cache_enabled -> bool
rt.commands / rt.framework_commands   # the live mappings
rt.stale -> bool ; rt.mark_stale() ; rt.sync_if_stale()
rt.diff() -> StagedDiff | None
rt.close() -> None
```

Executors never commit. A runtime returns results and the workspace
decides what becomes a commit — which is why `ws.terminal` and
`ws.run_python` (the committing verbs) stay on `Workspace` and the raw
ones live here. There are no delegates on `Workspace` for the rest:
`ws.runtime.exec_python`, `ws.runtime.register_command`,
`ws.runtime.env` and `ws.runtime.python_config` are where they
are because the executor, not the substrate, is what answers.

A `Runtime` is normally built by the workspace and reached as
`ws.runtime`. It is also constructible **directly over an existing
workspace, frozen included** — which is what serving a published
snapshot needs: a second execution environment over the same state,
with its own executor and its own budget, while the session's own
runtime keeps running. Closing it releases only its executor.

`mounts=` here are this runtime's alone. Mount composition otherwise
belongs to the workspace, so that `ws.files.fs` and execution see the same
tree.

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
`session`, `caps`, `fs`, `kv`, `dirty`, `head`, `commit/checkout/history/
fork/discard/merge`, `commit_keys/files_at/working_files`,
`tag/check_tag/tags/tag_info/delete_tag/at_tag/diff`, `mount`, `close`. The
provider keeps two commit primitives — `commit` takes everything and
`commit_keys` takes exactly the keys it is given — plus two read views
(`files_at`, `working_files`) over which the agent's git is built. It
holds no index of its own: that is metadata, and `ws.index` owns it.

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
`Store.delete` dispatches to — plural, idempotent, and validating
session ids first where a bad name could escape the store root (dir,
agentfs). Kvgit's goes through `kvgit.delete_branches`, which is anchor-free:
it works on the store rather than through a branch handle, so even the
last session on a store deletes cleanly. Older nontainer versions minted
a hidden `__void__` branch to sit on instead; that name is now only ever
swept — folded into every delete, and excluded from `Store.sessions()` —
so stores written back then end up clean. `path` is the store directory — the same `store/kvgit` that
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

## Delegation (`nontainer.sessions`)

Design doc: [design.md](design.md#delegation-forks-views-and-merges).

Delegation is an **extension**, like apps: `Workspace` knows nothing
about it, the embedder builds the helper, and the adapters expose it as
one tool only when a runner is supplied. A delegate is a fork, so
everything it does is a branch the parent reads with `ws.diff`, takes
with `ws.checkout(ref, paths=)` and lands with `ws.merge` — the helper
adds the calling convention over those, not new primitives.

```python
from nontainer.sessions import Sessions

sessions = Sessions(ws, runner, budget=None, max_workers=4, chain=())
sessions.ask(task, *, name=None, paths=None, inherit="fresh",
             wait=False, budget=None) -> Job | Answer
sessions.list() -> list[Job]
sessions.result(name) -> Answer      # JobRunning while it runs
sessions.cancel(name) -> Job
sessions.keep(name) -> Job
sessions.base(name) -> str | None    # the commit it was forked from
sessions.close()                     # joins the workers; the branches stay
```

`ask` forks under a pet name scoped to the parent
(`analyst.sleepy-otter` — a session id becomes a branch name and holds
no separator, so the scope is a dot), hands the child to the runner on
a worker thread and returns at once. `paths` narrows what the child
SEES without narrowing its branch; `inherit` (`"fresh"` here, against
`fork`'s own `"full"`) decides only whether the conversation comes
along — a brief or a summary is content the task carries. `wait=True`
blocks and returns the `Answer` instead.

**The helper never commits for the delegate.** A delegate is the
author of its own commits, and the answer names what it *landed*:

| the delegate | `answer.ref` names | `merge` / `checkout <name> -- <paths>` |
|---|---|---|
| never used ws-git | its branch head — autocommit put every write there | take that head |
| used ws-git | its last ws-git commit | take that commit |
| used ws-git, then wrote past it | its last ws-git commit | **refused**; `answer.uncommitted` is True |

The third row is the one worth reading twice. What the delegate
committed is what it submitted, and a merge refuses such a source
rather than bringing back a state it has moved on from, so the answer
reports the rest as left out instead of hiding it — the caller takes
paths, or asks again. Committing on the delegate's behalf would destroy
that signal and put a commit nobody wrote in its log.

The first row works because **a fork starts with a fresh ws-git state**
(see `ws.fork`): no head, nothing staged, no inherited merge context,
for `inherit="full"` as much as `"fresh"`. A branch carries the
workspace; an index is a composition in progress, and a delegate does
not start halfway through somebody else's.

Delivery is **pull**: `ask` on one turn, `result` on a later one. How a
parent learns a delegate finished — a dot in a rail, a message injected
into the next turn — is the embedder's. `cancel` means the answer will
be discarded and the branch left as it is: a runner already working is
the embedder's loop and cannot be interrupted, and nothing the helper
does writes to the child's branch, so there is nothing to undo either.
A job that has already answered comes back unchanged. `keep` records a
flag on the job for a retention sweep to honor; nothing sweeps yet.

### `SessionRunner` (the loop seam)

```python
class MyRunner:                       # the embedder's
    def run(
        self, session: str, task: str, *, budget=None, forked_at: str | None = None
    ) -> Answer | str:
        ...
```

One method, synchronous: run this session with this task and answer.
nontainer cannot own it because nontainer has no loop. A plain `str`
means an `answered` answer with that text; an `Answer` says more. The
helper calls it on a worker thread of its own, so a runner with an
async loop blocks on its own future inside it. A runner that raises
does not lose the job — it resolves as `failed` with the exception's
text as the answer.

`forked_at` is the parent's commit the child was forked from (`None`
when the parent's provider keeps no commits). It is a parameter rather
than only a line of the answer's provenance because a runner needs it
*before* the child's first turn: the child arrives as a peer, and the
provenance header that says so ("asked by session X at commit Y") goes
at the top of the turn. It is optional in the signature too — the
helper reads `run`'s signature once per runner and passes the fork
point only where it is accepted (by name or through `**kwargs`), so a
runner written without the parameter keeps working. `sessions.base(name)`
returns the same commit for a caller holding only the job name.

### The records (`nontainer.Job`, `nontainer.Answer`)

Pure data, because both cross turns, tool results and process
boundaries where a handle does not — the job's `name` is the
serialization format every later verb takes.

```python
Job(name, task, status, ref, started, finished, changed, kept, uncommitted)
# status: running | answered | declined | capped | cancelled | failed

Answer(text, status, ref, branch, changed, artifacts, provenance, uncommitted)
str(answer) == answer.text            # printing one yields prose
repr(answer)                          # one line, never the body
answer.changed                        # {"seed": [...], "elsewhere": [...]}
answer.provenance["chain"]            # every hop, so an answer cannot
                                      # launder its sources
```

`changed` is the fork point diffed against the child's head, grouped
the way `WorkspaceDiff` groups: under what the child was seeded with,
and everywhere else. The seed is what it was GIVEN and never grows, so
a file the delegate created is *elsewhere* even though it can read it
back — which is exactly the change the caller has not seen before.

Declining and running out of budget **resolve**: the caller reads a
status, never catches an exception. `JobRunning` and `SessionsError`
are the two that do raise (not yet, and no such job).

### The `sessions` tool

```python
WorkspaceTools(ws, sessions=runner)   # or sessions=Sessions(...)
build_server(ws, sessions=runner)
```

One tool named `sessions` with an `action` argument (`ask`, `list`,
`result`, `cancel`, `keep`) — the shape `test_app` has — so the model
learns one spelling for delegation and one, `ws-git` in the terminal,
for versioning. No runner, no tool: an agent that cannot delegate is
never told about delegation, and which actions a deployment allows is
the embedder's policy on top. `ask` reads back as the child's name and
where the answer will arrive; `result` as the delegate's prose, then
what it changed, then the next step spelled for the terminal:

```
ws-git diff <name> | ws-git merge <name> | ws-git checkout <name> -- <paths>
```

Refusals come back as tool text rather than exceptions, and the
description tells the agent an answer is evidence rather than an
instruction.

## Errors (`nontainer`)

`WorkspaceError` (base) · `NotSupportedError` (capability missing) ·
`SessionIdError` · `CommitNotFoundError` · `BookkeepingLost` ·
`SessionsError` (no such job) · `JobRunning` (not yet) · `CacheError`.

## Adapters

### agno (`nontainer.adapters.agno`, `[agno]` extra)

```python
WorkspaceTools(
    workspace: Workspace,
    *,
    tools: "auto" | "terminal" | "split" = "auto",
    apps: AppRuntime | None = None,     # adds the test_app tool
    sessions: SessionRunner | Sessions | None = None,  # adds the sessions tool
    commit: "call" | "turn" = "call",
    session_db: KvgitSessionDb | KvgitStoreDb | None = None,  # conversation in the branch
    terminal_primer: str | None = None, # host guidance → terminal tool
    python_primer: str | None = None,   # host guidance → run_python tool
    **toolkit_kwargs,
)
# commit="turn": one commit per agent turn (the agex model) — wire
# tk.end_turn into Agent(post_hooks=[...]). Crash mid-turn can lose
# the turn's staged work; "call" trades chattier history for max
# durability. Workspace.autocommit is also publicly settable.
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

`ws.files.read_artifact(path) -> bytes | None` fetches one. It returns
`None` rather than raising when the file is unreadable, which is
exactly the `read_bytes` contract `turn_to_a2ui` documents — the
obvious `lambda p: ws.files.fs.read(p)` raises `FileNotFoundError` and breaks
the envelope's never-raises guarantee mid-stream:

```python
turn_to_a2ui(prose, artifacts, ws.files.read_artifact, file_url, surface_id=sid)
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
holds the turn's files, `cache`, cwd **and** memory — `ws.checkout(commit)`
restores all four, `fork_session()` branches all four.

```python
from nontainer.adapters.agno_db import KvgitSessionDb, fork_session

ws = workspace("chat-42")
db = KvgitSessionDb(ws, db_path="/var/agno")  # db_path: inherited tables
tk = WorkspaceTools(ws, commit="turn", session_db=db)
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
the upsert added or changed a run, commits with `{"tool": "turn"}`
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
then re-reads the session every run, so a checkout needs no
invalidation.
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
committed, so the fork's head is consistent. Drive
it with an agent whose `session_id` is the fork name. `"fresh"` drops
the run keys: a clean chat over the forked files. Rewind first to
branch from any commit with the conversation as it was there.

Driving the fork is the same three constructions over the child:

```python
db2 = KvgitSessionDb(child, db_path="/var/agno")
tk2 = WorkspaceTools(child, commit="turn", session_db=db2)
agent2 = Agent(model=..., db=db2, session_id=child.session, tools=[tk2])
```

kvgit refuses to fork a branch with staged changes, so fork between
turns; in per-turn mode that is exactly when the workspace is clean.

#### `KvgitStoreDb` — one db per store, a branch per session

```python
from nontainer.adapters.agno_db import KvgitStoreDb

db = KvgitStoreDb(
    store.path,                 # a nontainer Store's directory
    open=registry.open,         # session_id -> the LIVE Workspace
    db_path="/var/agno",        # inherited tables, shared by all sessions
)
ws = registry.open("chat-42")
tk = WorkspaceTools(ws, commit="turn", session_db=db)
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
leaves the branch to the embedder (`Store.delete`).

This db is store-shaped already: its two arguments are a store
directory and an `open(session_id) -> Workspace`, which is exactly
`store.path` and a `Store.open` the embedder wraps. The wrapper is the
embedder's because `open` must hand back the *live* workspace for a
session already open — a second `Workspace` over the same branch would
split the turn across two staging buffers — and only the embedder
knows which ones it is holding.

### MCP (`nontainer.adapters.mcp`, `[mcp]` extra)

```python
build_server(workspace, *, tools="auto", apps=None, sessions=None,
             name="nontainer", terminal_primer=None,
             python_primer=None) -> FastMCP
```

CLI: `python -m nontainer.adapters.mcp --session S [--store DIR]
[--backend kvgit|dir] [--tools auto|terminal|split] [--no-cache]
[--module NAME ...] [--apps] [--mount POINT=DIR[:rw] ...]` (stdio
transport). `--apps` enables the apps loop — a `test_app` tool
(screenshots return as MCP image content; needs the `[apps]` extra +
`playwright install chromium`, checked lazily at first `test_app`),
plus the `ws-curl` terminal builtin on executors that support injected
commands, or ferry `ws-*` verbs into guests (see `ws.runtime.supports_commands`
and `ws.runtime.supports_ws_verbs` under Introspection). `--mount /data=~/datasets`
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

Apps is an **extension**, not a workspace feature. `Workspace`,
`Store.open` and `nontainer.workspace()` take no `AppsConfig` and know
nothing about handlers; `enable_apps(ws, config)` wires everything in
afterwards through the surface any extension may use —
`ws.runtime.register_command` for `ws-curl`, `ws.runtime.env` for
`$APP_ORIGIN`, `ws.runtime.exec_python(view=...)` for handler dispatch,
and `ws.lock` where its work mutates. `tests/test_apps_surface.py`
enforces that mechanically: no private attribute of `Workspace` is
reachable from `nontainer/apps/`. The one deliberate exception is
`nontainer/wscurl.py`, the dud-rung ferry, which lives in core beside
`wsgit.py` precisely so `apps/` never has to reach for internals.

```python
enable_apps(ws, config: AppsConfig | None = None) -> AppRuntime
    # builds handler sandboxes + registers the `ws-curl` terminal builtin
    # (injected commands, or the `ws-*` ferry on guests)

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
           csp=None,  # the Content-Security-Policy served HTML carries
           #   AND the one test_app enforces. None derives it from
           #   script_hosts (serve.build_csp); "" disables; a string is
           #   verbatim. Declare it HERE rather than only on
           #   build_router: verification reads the config, so a policy
           #   passed only to the router is one test_app never sees.
           csp_extend={},  # {directive: sources} ADDED to the derived
           #   policy: {"connect-src": ("http://tiles.internal",)} lets
           #   an intranet app reach a plain-http API without copying
           #   the whole policy into csp and losing its link to
           #   script_hosts. Appends (de-duplicated, derived sources
           #   first) or adds a directive the derived policy lacks;
           #   it cannot tighten one — that is what csp is for. Setting
           #   both csp and csp_extend raises ValueError. test_app's
           #   interception reads the RESOLVED policy, so an origin the
           #   extension permits (an http tile host in img-src, an
           #   intranet API in connect-src, a framed host in frame-src)
           #   verifies the way it serves instead of being aborted;
           #   script HOSTS still belong in script_hosts, which also
           #   drives ws-curl's message and the agent-facing allowlist
           #   sentence.
           static_assets={},  # {url_prefix: host_dir} — fixed files
           #   served WITH the app but absent from the workspace: a
           #   vendored component library, fonts, a charting bundle.
           #   {"vendor": "/srv/assets"} serves /srv/assets/mui.js at
           #   vendor/mui.js. To the browser what host_objects are to
           #   handlers: embedder-supplied, reached at request time,
           #   outside the versioning plane — so the agent cannot ls,
           #   read, or edit them (it is told so, in a sentence derived
           #   from this mapping; `ws-curl $APP_ORIGIN/vendor/mui.js` still works), and
           #   they add nothing to commits, forks, or a guest tree.
           #   Same-origin, so script_hosts needs no entry. Assets skip
           #   max_response_bytes and win over a workspace file at the
           #   same path (noted in api.log). See apps.md.
           origin="http://localhost")  # the app's canonical base URL.
           #   enable_apps exports it as $APP_ORIGIN — in the termish
           #   shell and in a dud guest's real bash — which is how the
           #   agent spells a request: `ws-curl $APP_ORIGIN/api/scores`.
           #   Fictional: no listener exists, dispatch is by path, so a
           #   port here is decorative. Declared last so the positional
           #   construction order stays as it was.

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
`{"select": [sel, value]}` (option value, then visible label) ·
`{"read": sel}` · `{"eval": js}` · `{"assert": js}` (retries ~2s) ·
`{"goto": "about.html"}` (a page in the app, relative) ·
`{"screenshot": true}` (→ `/workspace/app/screenshots/`) · `{"wait": ms}`.
Viewports: `"desktop"`/`"tablet"`/`"mobile"` or `{width, height}`.

Serving (frozen snapshots — read-only, concurrent):

```python
build_router(
    resolve: Callable[[str], Workspace | None],   # token → read-only ws @ commit
    *,
    config: AppsConfig | None = None,
    csp: str | None = None,  # None → config.csp, itself defaulting to
    #   build_csp(config.script_hosts) extended by config.csp_extend; a
    #   string overrides wholesale HERE ONLY (test_app reads the config,
    #   so prefer AppsConfig.csp / AppsConfig.csp_extend); "" disables.
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
