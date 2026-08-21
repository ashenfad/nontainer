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

- **Per-call** (default): every mutating call is its own checkpoint.
  Maximum durability, chattier history — right when the loop is opaque
  to you (an MCP server can't see turn boundaries).
- **Per-turn**: `WorkspaceTools(checkpoint="turn")` defers commits to a
  turn boundary hook, so a many-call turn is one commit (the agex
  model). Cleaner history; a crash mid-turn loses that turn's staged
  work (kvgit staging is in-memory).

Individual writes are deliberately *not* the unit: a handler that writes
three files mid-request, then raises, should leave nothing behind. The
staged buffer gives that atomicity for free, and high-tempo operational
state (an app's per-request scratch) belongs in an unversioned sidecar,
not in the commit history.

Results pin the commit they produced — `result.checkpoint` is the id
(or `None` for a read-only call), so `ws.restore(result.checkpoint)` is
compensation by identity rather than counting steps. Read-only calls
don't commit at all; `ws.head` pins the state they observed.

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
parallel calls serialize safely — each atomic and checkpointed — rather
than corrupting each other. `Workspace.run_python()` is always the
embedder surface; the terminal `python` builtin is a thin bridge over
it, so the split is about framing, never behavior.

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
- **AgentFS checkpoint/restore** via whole-file snapshots. The provider
  spike is unversioned today; wiring snapshots as checkpoints is future
  work.
- **Merge-fn presets** for concurrent sessions over one branch. kvgit
  has the CAS + three-way-merge machinery; shipping opinionated
  defaults doesn't yet.
- **Idle TTL for view workers.** `PythonConfig.view_workers` bounds how
  many resident workers a view may have, but residency never *decays*:
  a burst of N concurrent app requests leaves N workers held for the
  executor's life. Measured, that is ~113MB apiece on a
  pandas/plotly policy — so peak concurrency becomes a permanent memory
  floor, which bites hardest in a multi-user host with many open
  workspaces.

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
