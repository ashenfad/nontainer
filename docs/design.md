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

## A session's history is append-only

Within a session, a branch head only ever moves forward. `ws.checkout`
does not rewind it: the target's state is *written* into the working
tree and committed, so going back is a new commit whose keyset equals
the target's — files, cache, cwd, the ws-git blob, the stored
conversation, everything the session holds. `rollback(steps)` is the
same verb counting back over `log()`.

That gives three properties worth naming:

- **Abandoned turns stay in the log.** The commits a checkout stepped
  off are still reachable from the head, so garbage collection never
  reclaims them and `store.clean()` finds nothing after a checkout.
  Nothing has to be tagged or forked before stepping off it.
- **Undo is redo-able.** The commit before a restore is the one the
  restore stepped off, so `rollback(1)` straight after a checkout goes
  back to where the checkout was invoked.
- **One verb, one behaviour.** The agent's `ws-git checkout` restores a
  tree and rewinds its own head while the store appends; the host's
  `ws.checkout` restores the whole session and the store appends. Two
  callers, one story about the store.

The only thing that moves a head backward is store-level admin —
`Store.delete` drops a branch, and its commits become collectable.
A live session never loses history it wrote.

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
- **The fiction's bookkeeping is part of its operations.** That
  restore, the tree a checkout writes, and the blob a merge records
  are committed by the operation itself, keyed to the paths it
  touched — not left to `autocommit`, which a per-turn host turns off,
  and not allowed to sweep up unrelated dirty work. A refused commit
  (a CAS conflict) puts the working tree and the index back. Which
  commits are the agent's is one rule: `info["tool"]` is `ws-git` or
  `ws-git.merge`; the bookkeeping's `ws-git.restore`,
  `ws-git.checkout` and `ws-git.merge-record` are plumbing like the
  framework's own.
- **`log`, `show` and `diff` walk the agent's graph**, threaded
  through `virtual_parents` in commit info. The framework's per-call
  commits are never in it.
- **Branches are real branches.** A session IS a branch, so `ws-git
  branch <name>` forks a session rather than making a pointer, and
  `ws-git checkout <ref>` restores a tree rather than switching: the
  fiction rewinds its own head, the store appends the restore as a new
  commit — which is exactly what the host's `ws.checkout` does with the
  whole session, so a host checkout rewinds the fiction too (its head
  is a key the restore carries). A merge takes only what has been
  committed on both sides, which under the fiction means
  *agent*-committed, and refuses a source that has more: the target
  refuses while it has work in flight, the source is merged at its last
  agent commit rather than at its store head, and a source whose agent
  has written since is refused rather than merged at a state it has
  moved past.

The one thing the substrate must provide is a keyed commit
(`provider.commit_keys`, gated by `caps.index`). Everything else is
bookkeeping over ordinary reads and writes, which is why the fiction
is provider-shaped rather than kvgit-shaped.

## Delegation: forks, views and merges

A session is a kvgit branch carrying the whole world — files, cache,
cwd, the conversation. Forking one is O(1) and kvgit already three-way
merges branches, so delegating to a subagent, spawning a fresh worker
and consulting another session need no new primitives. They need a
calling convention over the ones that exist, plus a sparse view.

**Three scenarios, two mechanisms.**

| scenario | ancestry | result path |
|---|---|---|
| fork yourself | the fork point is the common ancestor | `merge` |
| fork an external session | the child shares ancestry with *them* | merge back into them is real; into you it is `checkout(ref, paths=)` |
| spawn fresh | a fork with a narrowed view and no conversation; ancestry intact | `merge` |

**Consult is not a merge.** Two unrelated sessions share only the
store's initial empty commit, so a three-way merge from there sees
every shared path as added on both sides and conflicts, and the
conversation record has no merge rule at all. Getting files from an
unrelated session is a take — `ws.checkout(ref, paths=[...])` — with
provenance in the commit's info, never a merge.

**A fork point is always a commit.** If the parent has uncommitted
writes, the fork lands them first and branches from *that* commit.
Forking a copy of the staging buffer would give the child a base that
never existed in history, no commit to point at as "delegated here",
and a merge base predating the parent's own uncommitted edits — which
would then come back looking like the child's work.

**The view is sparse, not a fence and not a prune.** `fork(paths=)`
narrows what the child's filesystem shows; its branch still holds
everything the parent had. A prune commit would make the merge back a
special case (every pruned path reads as a deletion on the child's
side), and a fence — refusing reads outside a set while the tree still
lists them — is a lie the agent trips over. A sparse checkout is the
shape git already has for "I only need this part", and the merge stays
ordinary three-way with no rule to special-case.

It comes with **one write rule, better than git's**: the child may
create a new path anywhere (it merges as an addition, and a delegate
needs somewhere to put notes), and a created path joins its view so it
can read back what it made; modifying or deleting a path that exists
outside the view is refused at write time, naming the view. You cannot
overwrite what you cannot see — and since a link is a second name for
a file, the view is drawn around the file: a link whose target it
hides cannot be made, and one that already exists reads as absent. The provider knows the full keyset, so
the check is cheap, and a guest rung applies the same rule to the
write harvest — the only way a guest can reach a hidden path is by
recreating it by name.

**The merge unit is the child's whole diff**, not the paths the caller
remembers pushing. Collateral edits are the common case — the delegate
touched a caller in `main.py` — and taking only what was seeded yields
a tree that does not run. `diff` groups changed paths as *in the
child's seed* vs *elsewhere* so collateral cannot be missed;
`checkout <ref> -- <paths>` is the deliberate narrowing.

**Merge is filesystem-only.** Files merge three-way; every other plane
takes ours:

| keys | rule |
|---|---|
| file keys (the VFS blobs + its metadata table) | three-way, marker merge; the table field-aware |
| `__cache__/*` | ours — a delegate's working memory does not come back |
| `__agno__/*` | ours — a delegate's conversation does not come back; what it has to say arrives as its answer |
| cwd, the ws-git blob, the view record | ours |

"Ours" has to own the *whole* prefix, not only contested keys: a merge
function runs only where both sides changed a key, so a run the
delegate added under `__agno__/runs/<id>` would ride in untouched. So
those two are registered as a `MergeChoice` over the prefix — a
whole-side policy that also drops their-only adds — and only for the
merge verb. An ordinary commit that loses its CAS to a second handle on
the SAME session must still take that handle's conversation, which is
this session's own.

**Judgment to the LLM, bytes to the machine.** The merge is
mechanical: child-only changes apply, disjoint two-sided changes union,
identical bytes on both sides are not a conflict, and overlapping edits
come back with `<<<<<<<` markers. It never guesses. And it always
commits or changes nothing: a conflicted merge lands *with* the markers
and the provider tracks an outstanding merge context — `ws-git status`
shows `## merging` and `UU`, `diff --check` finds the markers, and the
next commit that removes them clears it. A deliberate deviation from
git, which leaves conflicts uncommitted: here the conflicted state is
itself a checkpoint you can restore to, and there is no working tree to
leave things in.

**Merge takes only what is agent-committed, on both sides.** The
target refuses while it has anything modified against its own last
ws-git commit; the source is merged at *its* last agent commit and is
refused when it has written since. A delegate's result is what it
committed, so a runner should make the child commit at the end of its
task (a session that never used ws-git merges at its store head, which
is the honest answer for it).

**Providers degrade honestly.** kvgit does all of it. AgentFS refuses
`fork` and `merge` by name until it has a merge engine — a fork you
cannot merge back is a trap, not a rung — while `diff` and take still
work. The `dir` backend refuses both.

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

## Publications

`store.publish(ws, name)` is the tag with the three things a served app
needs added to it. It derives a **new commit** holding only the app
subtree, on a reserved branch of its own, tagged store-scoped, recorded
in a small registry.

The derived commit is the whole idea. Tagging the session's own commit
would have been one line, and it would have published the transcript,
the uploads and the cache along with the app — a handler under a frozen
workspace reads the entire tree, and an export hands over the entire
tree. It would also have pinned the session's whole history alive,
because a tag keeps its ancestry reachable, so publishing v1 would make
the session immortal. And a fork from a version would inherit somebody
else's conversation, which is not what "work on this app" means.
Deriving fixes all three at once: privacy, garbage collection, and
forkability are the same property seen from three sides. The cost is
copying blobs, which is nothing at app sizes and would be nothing at
all under content addressing.

Provenance still matters, so it is kept as a **soft reference** in the
commit's info rather than a parent pointer: `published_from` names the
session and commit a version came from, and pins neither.

The branch is not bookkeeping. kvgit reads through a branch handle, so
without one a publication could only be opened by borrowing some live
session — which fails on exactly the store the store scope exists for,
the one whose sessions are all gone. The branch is the anchor that
makes a publication readable on its own terms.

The registry — which versions exist, which one is current — is the
mutable half, and it is deliberately **generic**: no token, no route,
no database. Those describe a deployment of an app, not the app, and
they differ per embedder; a studio keeps them in its own table keyed by
name. What nontainer owns is the part every consumer would otherwise
reinvent identically.

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
