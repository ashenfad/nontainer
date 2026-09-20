# Extending nontainer

For implementers of the three seams: a new substrate, a new place code
runs, or the agent loop behind a delegation. If you are embedding
nontainer rather than extending it, the [API reference](api.md) is the
page you want; why the seams are drawn here is in the
[design notes](design.md#three-seams-and-where-a-session-ends).

The three contracts live in one module, `nontainer.protocol`, so an
implementer of any of them reads one file:

| seam | what it decides | the workspace-side face |
|---|---|---|
| `WorkspaceProvider` | where state lives | `Workspace` |
| `Executor` | where code runs against it | `Runtime` (`ws.runtime`) |
| `SessionRunner` | how an agent turn runs | `nontainer.sessions.Sessions` |

## `WorkspaceProvider` — the substrate

One provider instance is one session's world. Session resolution
("kvgit branch per session id") happens in the factory that builds the
provider, not in the provider. Providers are not thread-safe and do not
need to be: `Workspace` owns the single-writer invariant.

```
session · caps · fs · kv · dirty · head · frozen · frozen_at
commit / checkout / history / fork / discard / merge / apply / commit_at
commit_keys / files_at / working_files / key_at
branch_head / expand_commit
tag / check_tag / tags / tag_info / delete_tag / at_tag / diff
mount · close
```

Every one of those is called without a guard, so a provider that omits
one is not a provider a workspace can drive; a provider without the
capability a member needs raises `NotSupportedError` there rather than
leaving the attribute off. One member is optional and probed for:
`refresh()` — re-read the branch head, discarding uncommitted writes —
which delegation calls where it exists, to re-apply work against a head
that won a race.

Three surfaces carry the state:

- **`fs`** — a filesystem satisfying termish's `FileSystem` protocol
  (16 methods). termish executes shell commands against it; monkeyfs
  routes sandboxed `open()` / `os.*` through it.
- **`kv`** — a `MutableMapping[str, Any]` for small values. nontainer
  builds the agent-facing `cache` on top (prefix-scoped, key rules,
  picklability checks). Values must round-trip pickle, or the
  provider's own encoding.
- **`dirty`** — staged-but-uncommitted writes exist. Always False
  without `caps.staging`.

**Two commit primitives.** `commit(info)` takes everything
uncommitted, always — it is the framework's durability verb, and the
code around a workspace can only rely on it if its scope never depends
on what the agent has staged. `commit_keys(info, keys=...)` takes
exactly the keys it is given and returns `None` when none of them had
anything pending. `keys` are provider keys as stored
(`__agno__/session`) or absolute workspace paths, which the provider
resolves to its own keys; whatever bookkeeping a commit of those keys
needs to be readable rides along.

**Two read views.** `files_at(commit)` and `working_files()` give this
session's files by workspace path — at a commit, and live with
uncommitted writes included and uncommitted deletions excluded. Both
are read views rather than copies: values are read on demand, so
comparing two trees costs the blobs it actually reads.

**A provider holds no index.** The agent's git is metadata built over
`commit_keys` and those two views, and `ws.index` owns it. That one
keyed-commit primitive is the whole substrate requirement for the
fiction, which is why it is provider-shaped rather than kvgit-shaped.

`checkout` **appends**: the state is restored by writing it, so the
result is a new commit whose keyset equals the target's, and every
commit made since the target is still in `history()`. It converges on
the target — a write another handle lands while it is in flight is
superseded rather than carried into the result — and when the working
state already equals the target, nothing is committed and the current
head comes back.

`apply(base, theirs, info=)` is the three-way of a merge with the base
NAMED rather than found, which is why one primitive spells two verbs: a
revert names the commit as `base` and its parent as `theirs`, a
cherry-pick names them the other way about. `None` on either side names
the tree before anything. Either id may name a commit on another
session — a change is content, and reading it takes no place in anyone's
graph — and both are recorded as soft references, never as parents.

`at_tag` returns a **frozen** provider: reads see the tagged state,
`commit` / `checkout` / `fork` / `tag` / `delete_tag` raise
`NotSupportedError`, and writes may stage (so `dirty` can become True)
but have nowhere to land. Such a provider reports `frozen` True and
`frozen_at` the name it was opened at; every other handle answers False
and `None`, including one on a substrate with no tags to freeze at — a
provider with nothing to snapshot is not frozen, it is ordinary.

**Reading across sessions.** `branch_head(session)` is another
session's current commit, for the verbs that are about a branch that is
not this one — merging it, cherry-picking from it, attaching its tree,
resolving a bare session name as a ref. `key_at(commit, key)` is the
non-file half of `files_at`: it reads the framework's own planes, the
ws-git blob and the seed a session was given, out of a commit without
materializing a tree. `expand_commit(commit, session=)` turns the seven
characters every ws-git line prints back into a whole id, and hands
back anything it cannot expand unchanged — a prefix nothing matches
included — so the read that follows refuses it in its own words.

### What each capability gates

`Capabilities` is flags rather than promises: declare the difference
instead of pretending equivalence. `versioned` is the master switch —
with it False, `commit` / `checkout` / `history` / `fork` all raise
`NotSupportedError` and the rest are meaningless.

| flag | what it turns on |
|---|---|
| `versioned` | `commit`, `checkout`, `history`, `fork` |
| `staging` | writes accumulate until `commit()`; `discard()` drops them. False means writes are durable immediately and `discard()` raises |
| `cheap_fork` | fork is O(1) with shared storage. False but versioned still forks — just expensively, by copy |
| `merge` | `merge`, the `apply` primitive behind `ws.revert` / `ws.cherry_pick`, and the `commit_at` lookup that names the commits an apply resolves between — three verbs, not one |
| `tags` | `tag` / `tags` / `tag_info` / `delete_tag` / `at_tag`. Not `diff`, which is versioning's: a name is one way to reach a commit, not what makes two comparable |
| `index` | `commit_keys` and the read views, so the agent-facing git fiction is available |
| `sql_audit` | an operation-level audit log queryable with SQL |
| `fuse_mount` | `mount()` exposes the workspace at a real path for subprocesses and C extensions |

### The bundled providers

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
agentfs). Kvgit's goes through `kvgit.delete_branches`, which is
anchor-free: it works on the store rather than through a branch handle,
so even the last session on a store deletes cleanly. `path` is the
store directory — the same `store/kvgit` that `open` takes for kvgit;
the parent store base for dir/agentfs (they resolve `<session>/` and
`<session>.db` under it).

## `Executor` — where code runs

`WorkspaceProvider` says where state *lives*; `Executor` says how code
*runs* against it. The contract has two capability flags, one optional
capability object, two lifecycle methods, two execution methods, two
staging methods and one path mapper. Choosing and configuring one of the two that ship is the
embedder's side, in the [API reference](api.md#executors).

```python
supports_commands: bool
supports_ws_verbs: bool
app_driver: AppDriver | None   # optional

def open(self, context: ExecutionContext) -> None
def close(self) -> None
def exec_python(code, *, inputs=None, stdin=None, argv=None,
                echo=None, view=None) -> PythonResult
def exec_shell(script: str) -> TerminalResult
def diff(self) -> StagedDiff | None
def sync(self) -> None
def guest_to_host(self, guest_path: str) -> str | None
```

**Executors never commit.** Both execution methods return a result
whose `commit` is `None`; the workspace holds the single-writer lock,
absorbs whatever the executor staged, and decides what becomes a
commit. That direction is why swapping the executor for a real machine
never touches the versioning semantics. Agent-code failure is a result
(`PythonResult.error`, a non-zero `exit_code`), never an exception.

**`supports_commands`** — whether `ExecutionContext.commands` reach the
shell. `LocalExecutor` runs termish and hands it the mapping, so an
injected builtin is a real command; `DudExecutor` runs actual bash in a
guest, which has no such hook, and declares `False`. Tool descriptions
are built against the flag. An executor predating the flag reads as
`True`.

**`supports_ws_verbs`** — whether the `ws-*` verbs reach that shell
some other way. They are the agent's own tools and must work wherever
the agent's code runs, so an executor with no command registry
declares this True and carries them across itself; `DudExecutor` does,
`LocalExecutor` declares `False` because its commands are already
reachable. The framework registers and advertises a verb where either
flag holds. Default `False`, so an executor that declares neither is
read as in-process — except that one written before these members were
named is recognized by the private `_guest_to_host` it would carry.

**`guest_to_host(path)`** — a guest-absolute path as the host spells
it, or `None` where nothing maps. A traceback frame, the shell's idea
of cwd and a verb's argv all arrive in the guest's spelling, and naming
a workspace file means mapping one back; `None` is the honest answer
for an in-process executor and for a guest path outside the workspace,
which a caller passes through unchanged rather than guessing at.

**`app_driver`** *(optional)* — the
[`AppDriver`](apps.md#the-driver-seam) `test_app` should verify an app
with, or nothing. The rung that will SERVE an app is the rung that can
verify it the way a visitor sees it, so an executor able to offer that
runtime says so by defining the name; `Runtime.app_driver` reads it and
answers `None` for everything else, which is every executor that ships
today. An embedder overrides the choice with `AppsConfig.driver`.

**`open(context)`** binds to one session's state and starts any
resident machinery — `LocalExecutor` builds the default sandbox and
forks the isolation worker; `DudExecutor` boots or resumes a guest and
materializes the tree. `Runtime.__init__` calls it once, as the last
step of its own construction, so no construction failure after that
point can orphan a worker. A workspace builds exactly one runtime for
itself, and a frozen open or a fork builds another for the workspace it
returns. It is not re-entrant. `close()` must be best-effort and
idempotent and **must not raise**: the workspace closes its provider
next regardless.

### Concurrency

**`exec_python(view=)` may be called concurrently, and the rest may
not.** `terminal`, `run_python` and every mutating workspace verb run
under the workspace's single-writer `RLock`, so an executor sees them
one at a time whatever the host does with threads. Serving an app from
a frozen snapshot takes no such lock — there is nothing to write, and
one request must not queue behind another — so view calls arrive in
parallel and **a view exec must be reentrant**. Each executor answers
that its own way: `LocalExecutor` draws a resident worker per call from
a pool, `DudExecutor` has one guest channel and serializes internally.
Authoring dispatch (a mutable workspace) is an ordinary mutating call
and takes the lock like one.

**`diff()` and `sync()` are called around execs, never during one.**
The workspace calls `diff` after every mutating exec, holding the lock,
before its commit flow; it marks the executor stale whenever it moves
provider state behind its back and calls `sync` once, lazily, before
the next execution rather than at the moment of the change. So an
executor is never asked to harvest a call that is still running, and
never asked to re-materialize a tree it is mid-exec against.

**`exec_python(view=)`** is where an executor is asked for a
restricted, budgeted execution — the apps extra's handler dispatch is
the only consumer. A `ViewSpec` declares the *intent*
(`readonly_fs`, `readonly_cache`, `timeout`, `tick_limit`,
`extra_classes`) and each executor realizes it its own way:
`LocalExecutor` builds a sandtrap sandbox with a memoized policy and
registers the extra classes; `DudExecutor` ships those classes' module
*source* so a guest with no nontainer installed can synthesize them,
and rejects a non-empty write-diff (rung 1) or mounts read-only (VM
rungs). No sandbox object crosses the seam, so nothing sandtrap-shaped
rides on the protocol. `tick_limit` is LocalExecutor's alone — a remote
executor has no tick machinery, and wall-clock is its guard.

**`diff()` and `sync()`** exist for executors whose writes do not land
in the provider directly. The workspace calls `diff` after every
mutating exec, before its commit flow, and `sync` whenever it moves
provider state behind the executor's back — checkout, discard, the
host-side write helpers, direct `ws.files.fs` writes. Both are free
no-ops for `LocalExecutor`: its writes land in the provider the moment
they happen, so there is nothing to harvest and no second copy to
refresh. `DudExecutor` harvests the guest's writes as a `StagedDiff`
(whole-file payloads, not patches, translated to fs-root-relative
paths, with the harvest rebased so each call yields only its own
changes) and re-materializes the tree wholesale on `sync`. `sync` is
called lazily, once, before the next execution, so N host writes cost
one sync rather than N.

A remote executor that loses its guest **between a successful exec and
the harvest** raises `HarvestLost` from `diff()` rather than returning
an empty diff. The call is torn, not cleanly absent: cache write-backs
land inside the successful exec while fs writes cross only in the
follow-up harvest, so an empty diff would report success for a call
whose file effects silently vanished. The workspace surfaces an errored
result and unwinds the staged half.

### `ExecutionContext`

What `open` binds to. For `LocalExecutor` these are live references and
execution works directly against the provider-backed objects; a remote
executor treats the context as the two ends of its transport. Three
fields are held **by reference on purpose** and must not be copied at
open:

- `commands` — the workspace mutates it after construction
  (`register_command`, which is how `enable_apps` installs `ws-curl`).
- `shell_env` — the runtime publishes into it after construction
  (`$APP_ORIGIN`), so an executor that copied it would run every later
  call without those variables. Executors snapshot it *per call*
  instead, so a command mutating the mapping termish is handed does not
  leak into the next call. It belongs to the runtime, not the
  workspace: one workspace can carry several runtimes, each with its
  own variables.
- `workspace` — the live workspace, for framework callbacks that must
  act *as* it. `None` when the context is hand-built, and then those
  callbacks are simply unoffered.

`head` is a callable rather than a value: it returns the commit id the
workspace fs currently *equals*, and `None` whenever staging is dirty
or the provider has no commit identity. An executor with a reusable
substrate uses it as a content-addressed state tag — a parked machine
tagged with the same id can resume with no tree push — so a tag must
never name a state the tree does not exactly hold, which is why it is
read at tag time rather than frozen at open.

`frozen` says this workspace is a read-only snapshot. Honouring it is
an **optimization, never the guarantee**: `LocalExecutor` needs no code
for it, since the `fs` and `kv` bound here are already read-only views
that raise where the write happens, while an executor running against
its own substrate may write freely and report the harvest — the
workspace refuses to absorb it and tells the caller the call was
refused.

`root` is the workspace root, the one absolute-path contract shared
across executors: `LocalExecutor` points sandtrap's module imports
there, and a VM executor mounts its guest workspace at that exact path,
so an absolute path in agent code means the same file everywhere.

### Reaching the host from a guest

Two mechanisms carry host-side behaviour into a guest that runs real
bash and real Python, and an executor with its own substrate needs
both.

**The `ws-*` verb ferry.** A `ws-` name operates on the workspace
through nontainer, so it is implemented once on the host and relayed
from the guest. `nontainer.wsverb` is that relay: it emits one guest
shell function per registered verb, prepended per exec so a
registration made after the executor opened takes effect with no
session rebuild, and each function calls home over `dud-hostcall` to a
single host object named `ws_verb` (`wsverb.DUD_OBJECT`). A user host
object under that name is refused at executor open rather than
shadowing either way. The handler invokes the LIVE command from the
registry of the runtime whose executor ferried it, absorbs the guest's
writes first so every verb dispatches against fresh state, and answers
with a JSON triple (`stdout`, `stderr`, `exit_code`, plus base64
`files` for captures) that the guest function splits back onto the real
streams. What a verb supplies is a `FerrySpec` saying which of its
flags carry filesystem paths, which carry free text, and whether its
bare arguments are paths at all — the one thing the relay cannot guess,
since a ws-curl URL and a ws-git pathspec both start with `/` and mean
opposite things. Only commands tagged as framework verbs are fronted,
so a custom command under a framework name stays a local-rung creature.

**The host prelude.** `PythonConfig.host_objects` arrive in a guest as
plain data inside the execution's payload, so the dud executor prepends
a prelude that synthesizes the `host` module around them: `from host
import db` resolves in a guest exactly as it does in-process, and the
module refuses attribute sets there for the same reason it does here.
The prelude shifts the submitted code's line numbers, and the executor
renumbers guest tracebacks back into the coordinates of the code the
caller sent.

Both are declared on the executor and reported by `Runtime` for the
callers that gate on them: `ws.runtime.supports_ws_verbs` is True for a
guest-bridging executor even though `supports_commands` is False there,
and `ws.runtime.guest_to_host(path)` maps a guest path back for a
caller that has no business knowing which rung it is on.

## `SessionRunner` — the agent loop

Running a nested agent turn is neither provider-shaped nor
executor-shaped: it needs a model, a tool loop and a budget, none of
which nontainer owns. So the protocol names the seam and the embedder
implements it.

```python
class MyRunner:
    def run(
        self, session: str, task: str, *, budget=None, forked_at: str | None = None
    ) -> Answer | str:
        ...
```

`session` names the workspace the turn runs against, `task` is the
instruction, `budget` caps the work and its interpretation is the
runner's (turns, tokens, seconds), and `forked_at` is the parent's
commit the child was forked from — optional in the signature as well as
in value. Whatever a runner returns, the caller fills in what only the
host knows — the child's ref, its branch, the paths it changed — so a
runner that returns them is not required to get them right. How the
helper calls it, what an answer names, what retention does and what
refuses are all in [sessions.md](sessions.md).

**`HostObjectFactory`** is a declared seam that nothing calls yet.
It names the shape for deciding which host objects a child session's
code may reach — asked once per child with `(parent_session,
child_session, kind)`, `kind` saying why the child exists — because
host objects are live resources and a per-user db handle belongs to
the user whose turn opened it. Today a fork replays the parent's
`PythonConfig`, host objects included, and a delegate's runner may
open the child with a config of its own; an implementation of this
protocol is not asked by anything, so write one only when a release
says the helper takes it.

## Conformance

This is the page where naming tests is the point. An implementation is
expected to pass:

| suite | what it holds |
|---|---|
| `tests/test_kvgit_provider.py`, `tests/test_dir_provider.py`, `tests/test_agentfs_provider.py` | each bundled provider against the protocol, capability flags included |
| `tests/test_protocol.py` | the shapes the seams speak |
| `tests/test_wsgit_conformance.py` | the same ws-git scripts read identically on the local and dud rungs |
| `tests/test_wscurl_conformance.py` | the same for `ws-curl` |
| `tests/test_wspytest_conformance.py`, `tests/test_wsvitest_conformance.py` | the same for the two test verbs |

The cross-rung suites are the discriminating ones for a new executor:
a same-script write-then-verb flow passes trivially where the command
and the files share one substrate and passes on a guest rung only if
the verb ferry absorbs writes first. They restrict themselves to shell
features both shells offer, so what they compare is the verbs'
contract rather than a shell's dialect.
