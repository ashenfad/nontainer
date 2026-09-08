# Design notes

Why nontainer is shaped the way it is. This is the rationale doc — for
*using* nontainer, see [quick-start](quick-start.md) and the
[API reference](api.md); for the apps extra, [apps.md](apps.md).

## Script model, not a persistent REPL

Each `run_python` call is a fresh [sandtrap](https://github.com/ashenfad/sandtrap)
execution against the workspace. There is no long-lived interpreter
holding your variables between calls — a call runs, its top-level
bindings come back in `result.namespace` for the host, and the
interpreter is gone.

Durable state lives in three planes instead, each with one job:

- **`cache`** holds **data** — picklable values, versioned with the
  workspace.
- **`helpers/`** holds **code** — `.py` files on the filesystem,
  re-imported on demand.
- **the filesystem** holds **artifacts** — everything else the agent
  writes.

This is deliberate and it matches [agex](https://github.com/ashenfad/agex),
so an agent's mental model transfers verbatim. The alternative — a
resident REPL whose namespace *is* the state — couples "what the agent
computed" to "a live process that must stay up," which is exactly the
coupling a persistent, forkable, restorable workspace is trying to
break. Reusable code as importable files (not as REPL history) is what
makes fork and rollback mean something: you're versioning source and
data, not a heap.

## Commit granularity is the tool call or turn, not the write

A mutating tool call stages its filesystem and cache writes and flushes
them as **one atomic commit** carrying `info` metadata (`{"tool":
"terminal"}`, etc.). Two knobs:

- **Per-call** (default): every mutating call is its own commit.
  Maximum durability, chattier history — right when the loop is opaque
  to you (an MCP server can't see turn boundaries).
- **Per-turn**: `WorkspaceTools(commit="turn")` defers commits to a
  turn boundary hook, so a many-call turn is one commit (the agex
  model). Cleaner history; a crash mid-turn loses that turn's staged
  work (kvgit staging is in-memory).

Storing the agno session in the workspace moves the per-turn commit
off that hook: the db fires it when agno persists the run, because post
hooks run *before* that, and a hook-driven commit would hold the turn's
files but leave its conversation for the next commit. See
[agno-sessions.md](agno-sessions.md).

Both of those are the *framework* committing — everything uncommitted,
at a moment the agent did not choose. That is safe to do at any moment
because the agent's own git is measured against the agent's own last
commit, not the store's head (next section).

Individual writes are deliberately *not* the unit: a handler that writes
three files mid-request, then raises, should leave nothing behind. The
staged buffer gives that atomicity for free, and high-tempo operational
state (an app's per-request scratch) belongs in an unversioned sidecar,
not in the commit history.

Results pin the commit they produced — `result.commit` is the id
(or `None` for a read-only call), so `ws.checkout(result.commit)` is
compensation by identity rather than counting steps. Read-only calls
don't commit at all; `ws.head` pins the state they observed.

## ws-git is a fiction over the store's history

An agent asks for git — an index it fills across several edits,
commits it names, a log it can read back. The store underneath has its
own history, made by the framework for durability at moments the agent
never chose. Those are not the same history, and the first version of
this tried to make them one: an agent commit WAS a framework commit,
and staging suspended autocommit so unstaged work stayed out of the
store until the agent landed it. That collided with the framework's
own durability points and with the plain rule that nothing an agent
writes should sit outside the store.

So the agent's git is metadata instead — a blob at a reserved key
holding the agent's head (a store commit hash), the staged paths, and
the context of an outstanding merge. Four consequences, and they are
the whole model:

- **The framework's commits are plumbing.** `status` diffs the working
  tree against the AGENT's head, so a turn hook, a session db or an
  autocommit between two edits leaves a composition exactly as it was.
  Nothing is withheld from the store and nothing suspends autocommit:
  an agent's work in progress is durable from the moment it is
  written, and still not in the agent's commit until the agent says
  so.
- **An agent commit's tree is exactly what the agent committed.** The
  paths modified but left out are written back to their content at the
  agent's head, the keyed commit is made, and the work in progress is
  written back into the tree straight after. Without that step a
  partial commit absorbs the unstaged edits into its own baseline —
  `status` goes clean over work the agent did not commit, and a later
  checkout brings it back as if it had.
- **`log`, `show` and `diff` walk the agent's graph**, threaded
  through `virtual_parents` in commit info. The framework's per-call
  commits are never in it.
- **Branches are real branches.** A session IS a branch, so `ws-git
  branch` and `ws-git merge` are refused with the host named as the
  one who invokes them, and `ws-git checkout <ref>` restores a tree
  rather than switching: the fiction rewinds, the store appends the
  restore as a new commit.

The one thing the substrate must provide is a keyed commit
(`provider.commit_keys`, gated by `caps.index`). Everything else is
bookkeeping over ordinary reads and writes, which is why the fiction
is provider-shaped rather than kvgit-shaped.

## Tags have two scopes, and nontainer picks them

A commit id is precise and unmemorable, so commits get names.
The question a naming feature has to answer is what a name *belongs
to*, and leaving that to embedders is how one flat namespace becomes a
convention nobody wrote down: session ids stuffed into tag strings, two
sessions racing for `v1`, and teardown that either orphans names or
deletes someone else's.

So the scope is part of the API. A **session** tag belongs to the
session that made it: another session's `v1` is a different tag, a
session lists only its own, and deleting the session deletes them —
that is what makes tagging cheap, because naming a state costs nothing
you have to clean up later. A **store** tag belongs to no session: it
is visible from every workspace on the store and survives the deletion
of the session that made it. That is the whole difference, and it is
the difference an embedder actually has: a commit worth naming
inside a conversation, versus a publication that has to outlive the
conversation — the snapshot an app serves, the state a link points at.

Storage keeps that separation honest rather than by convention: a
session tag is stored under `<session>/`, a store tag under `@store/`,
and a session id can never begin with `@` — so no session, however it
is named, can write into the scope that outlives it.

Both ride kvgit's tags, which are branch heads under a reserved name,
so a tagged commit anchors garbage collection with no rule of its
own: the state a publication names stays readable after everything else
about its session is gone. `store.tags.at` opens it as a **frozen**
workspace —
the files read-only down to the executor's filesystem, nothing able to
commit — because a published snapshot that could be written to is not
a snapshot.

## Tool exposure adapts to the environment

`WorkspaceTools(tools="auto")` picks the surface from the config:

- **Plain workspace** (no host objects, cache off) → one `terminal`
  tool, with `python` as a shell builtin bridging `run_python`. The
  shell frame tells no lies: `python` genuinely has script semantics
  and composes in pipelines.
- **Augmented workspace** (live host objects, `cache`, namespace-out
  conventions) → `terminal` **and** a separate `run_python` with its
  own framing, because script semantics would mislead about the
  namespace magic.

The diagnostic that settled it: if the terminal tool's description has
to explain namespace behavior, you wanted the split. `tools="terminal"`
/ `"split"` force it explicitly.

**Concurrency companion.** Tool descriptions instruct one call per turn
(batch via multiline scripts / `;` / pipes), so mutation is implicitly
sequential inside a script. Harnesses can't enforce per-tool
singularity (agno has no such flag; model-level `parallel_tool_calls=
False` is all-or-nothing), so a per-workspace lock is the backstop:
parallel calls serialize safely — each atomic and committed — rather
than corrupting each other. `Workspace.run_python()` is always the
embedder surface; the terminal `python` builtin is a thin bridge over
it, so the split is about framing, never behavior.

## Three seams, and where a session ends

nontainer separates three contracts that most sandboxes weld together,
and each is a real object rather than an internal detail:

- **`WorkspaceProvider`** — where state *lives*: fs + kv + the
  versioning verbs. `Workspace` is the session-scoped face of it.
- **`Executor`** — how code *runs* against that state: the python
  sandbox, the shell, worker lifecycle, result rendering. `Runtime`
  (`ws.runtime`) is the workspace-side face of it.
- **`SessionRunner`** — how an *agent turn* runs. Declared, not yet
  used; the embedder owns the model and the loop, so nontainer names
  the seam rather than implementing one.

The split that matters at runtime is the second one, and it has a
direction: executors never commit. A runtime produces results (and, on
a remote executor, a diff of what changed); the workspace absorbs
them, holds the single-writer lock, and decides what becomes a
commit. That is why swapping the executor for a real machine never
touches the versioning semantics — commit-per-call, fork,
rollback were always properties of the state layer.

The third object is `Store`, and it exists because a session is not
the only unit. Deleting a session, listing what a user has, sweeping
storage nothing reaches, and naming a commit that must outlive the
conversation that made it are all operations on the *set* of sessions.
Grafted onto a session handle they read as accidents of whichever
workspace happened to be open — `ws.tag(name, scope="store")` said
almost the opposite of what it does, since the whole point of that
scope is that the session is irrelevant. So store-level state lives on
`Store`, session-level state on `Workspace`, and a caller can tell
which is which by where the verb is.

`nontainer.workspace(session)` stays as sugar for
`Store(...).open(session)`: the one-session case is the common one and
should not get longer.

## Sandbox honesty

In-process mode (`isolation="none"`) is a walled garden for cooperative
LLM-generated code, not a boundary against adversarial code — inherited
straight from sandtrap's own framing. When you want real distance, the
`isolation="process"` / `"kernel"` ladder is there (with sandtrap's
kernel-degradation caveats). We don't use the word "sandbox" in the
pitch; the pitch is the *workspace*.

## Later / maybe

- **run-ts** — a Node sidecar wrapping
  [agex-ts](https://github.com/ashenfad/agex-ts)'s runtime worker,
  bridged over an RPC filesystem. The only piece that needs Node;
  deferred until something pulls for npm-ecosystem authoring.
- **AgentFS commit/restore** via whole-file snapshots. The provider
  spike is unversioned today; wiring snapshots as commits is future
  work.
- **Merge-fn presets** for concurrent sessions over one branch. kvgit
  has the CAS + three-way-merge machinery; shipping opinionated
  defaults doesn't yet.
- **Idle TTL for view workers.** `PythonConfig.warm_view_workers` bounds how
  many resident workers a view may have, but residency only ever
  *rises* toward that bound — it never decays. A burst of N concurrent
  app requests leaves `min(N, warm_view_workers)` workers held for the
  executor's life; the calls past the cap run in transient sandboxes
  that are reaped when they finish. Measured, a resident worker is
  ~113MB on a pandas/plotly policy.

  So the cap is not a ceiling you approach and retreat from — it is a
  floor you fill and then keep paying for, per distinct view, per
  workspace. At the default of 1 that is a small bill. It matters for
  exactly the embedders we tell to raise it (see `docs/apps.md`:
  anyone serving concurrent app traffic), and in a multi-user host
  with many open workspaces it multiplies.

  Reaping an idle worker is semantically free, which is what makes this
  attractive: `run_python` is a fresh execution per call (see "Script
  model" above), so a rebuilt worker loses nothing the contract offers
  — only warm imports, which cost time on the next call, not
  correctness.

  The design constraint worth recording, because it is not obvious:
  **lazy expiry on checkout does not work.** It fires only when there is
  traffic, and the case that matters is memory held while nothing is
  happening. So it needs either a background timer thread — nontainer
  has none today, and adding one to a library is a real cost — or an
  embedder-driven `reap_idle(max_age)` called from a periodic task the
  host already owns. The latter fits how the rest of this splits:
  mechanism here, policy and scheduling with the embedder.

- **Distribution** — upstreaming a thin `WorkspaceTools` to agno's
  toolkit registry, and an MCP Skill document. Channels, not code.
