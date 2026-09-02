# agno sessions in the workspace

Status: implemented. Ships under the `[agno]` extra as
`nontainer.adapters.agno_db` (`KvgitSessionDb`, `fork_session`); the
API reference has the usage shape.

## Goal

One kvgit commit holds everything a turn touched: files, `cache`,
cwd, **and the agent's conversation**. `ws.restore(commit)` rewinds
all four together; `ws.fork(name)` branches all four together.

Today the conversation lives wherever the embedder points agno's
`db=` (sqlite, json, postgres), so an embedder that wants to rewind
memory alongside files has to stamp the workspace head onto each
turn, truncate agno's run list by hand, and hope the two writes
never diverge. Putting the session in the workspace makes the join
disappear: there is one store, one commit, one restore.

This is the agex model, reached through agno's own storage
extension point rather than through agex.

## Where the session goes

agno's `AgentSession.to_dict()` is a flat dict plus a `runs` list of
run dicts, each carrying a `run_id`. The workspace stores it as
**one key per run**, not one blob:

```
__agno__/session              session dict minus runs, plus
                              "run_ids": [...] in order
__agno__/runs/<run_id>        one run dict each
```

kvgit shares structure at the key level: a commit that changes one
key writes a few HAMT nodes and reuses every other subtree by hash.
A turn therefore adds one run key and rewrites the small session
key; the hundred earlier runs are shared with every prior commit,
every fork, and every branch. Storing the whole session under one
key would rewrite the entire conversation every turn and share
nothing, because kvgit has no content-defined chunking of byte
streams — its dedup is per key and per codec leaf.

Per-run keys also make runs individually addressable, which is what
a rewind, a transcript projection, or an a2ui egress wants anyway.

The `__agno__/` prefix follows the existing convention: framework
keys are `__`-prefixed (`__cwd__`, `__cache__/...`), and the agent's
`cache` view rejects `__` keys at write time, so agent code cannot
reach these by construction.

## One session per workspace

A `KvgitSessionDb` is constructed from one workspace and holds
exactly one session: the one whose `session_id` is recorded in
`__agno__/session`. There is no routing. Whatever agno writes
through it lands in that branch.

- `get_session(session_id)` returns the branch's session when the id
  matches, else `None`.
- `get_sessions(...)` returns a list of at most one.
- `upsert_session(session)` with a `session_id` that does not match
  the branch's (once one is recorded) raises `NotSupportedError`.
  This is the guard that keeps agno's own `Agent.fork_session` from
  writing a second session into the branch (see Fork below).
- `delete_session` clears the keys; `rename_session` rewrites the
  session key.

One thing to know about every refusal above: agno's run loop wraps
its storage calls in a catch-all that logs a warning and carries on.
Calling the db directly raises; driving it through an `Agent` shows
a warning and the observable effect is that nothing was written. In
per-turn mode a refused or failed upsert also means no commit fired,
so the turn's staged files ride into the next turn's commit. That is
agno's behaviour, not a choice here, and it is why the guards refuse
*before* writing anything.

Only `AgentSession` is supported. Team and workflow sessions raise
`NotSupportedError`.

Everything that is not the sessions table — user memories, metrics,
traces, eval runs, knowledge, and the rest of `BaseDb`'s surface —
is inherited unchanged from agno's `JsonDb` and lives on disk at the
path the embedder gives it. Those tables are cross-session by
design (a user's memories span many conversations), so they must
not version with a branch. The line is the same one the workspace
draws everywhere: session state versions, world state does not.

Subclassing `JsonDb` rather than implementing `BaseDb` from scratch
is deliberate. `BaseDb` is roughly 150 abstract methods; the sessions
table is seven of them. Overriding seven public
methods is the whole exposure to agno churn.

## Values

Run dicts and the session dict are stored as the JSON-shaped dicts
agno hands over — no pickling of agno objects, so a kvgit tree never
depends on agno's class layout, and the exporter story (dump a
branch to plain files) stays trivial. `AgentSession.from_dict` /
`RunOutput.from_dict` rebuild the objects on read.

## The commit trigger

The two checkpoint modes stay as they are. What changes is what
fires the per-turn commit when a session db is wired in.

`WorkspaceTools(checkpoint="turn")` means one commit per turn. Today
that commit comes from the `end_turn` post hook. With a
`KvgitSessionDb` on the same workspace it comes from the db instead:
`upsert_session` writes its keys and then, if the upsert added or
changed a run, calls `ws.checkpoint(info={"tool": "turn"})`. The
post hook becomes a no-op on such a workspace, so wiring it stays
harmless and existing embedder code keeps working.

So the turn's files, cache, cwd, and conversation land in one
commit, and the commit happens at the moment agno persists the run.

Why the db and not the hook: in agno's sync run loop, post hooks
execute before the session is persisted. A hook-driven commit would
capture the turn's files but not its conversation, and the
conversation would ride into the *next* turn's first commit — which
is exactly the files-versus-memory divergence this design exists to
remove. Anchoring the commit to the persist makes agno's internal
ordering irrelevant: whenever agno writes the run, that is when the
turn is done.

agno may also upsert the session before the first run (creating the
record). That write carries no run, so it does not commit; it is
staged and rides into the turn's commit.

One commit per turn is a preference, not a requirement. It keeps
the "a commit is a turn" invariant that `rollback(steps=)` and
`history()` lean on, and it costs nothing when the db is the
trigger. If that proves awkward in practice, the acceptable fallback
is the post hook committing files and the db committing the
conversation as a second commit, with the first stamped
`info={"tool": "turn", "partial": True}` so history consumers can
skip it. That fallback is what the two-commit shape costs: step
counts off by one unless callers skip partials.

Under `checkpoint="call"` every mutating tool call still commits,
and the db commits its own trailing write, so the head at the next
user message includes the conversation.

## Rewind

`ws.restore(commit)` rewinds the run keys with everything else.
agno reads the session from the db at the start of every run
(`Agent.cache_session` defaults to `False`), so the next run sees
the rewound conversation with no invalidation step.

`cache_session=True` would break this: agno keeps the loaded session
object in memory across runs, so after a restore it would append to
the stale, pre-rewind run list and write the rewound turns back.

The db catches that. agno's upsert hands over the session's run
list, so the db checks that everything before the turn's new run is
a contiguous tail of the branch's `run_ids`. When the incoming list
carries runs the branch does not hold, the in-memory session is
stale, and the upsert raises with a message naming `cache_session`.
Nothing is written. The same guard catches any other path that would
write a conversation the branch's history does not lead to.

A tail rather than the whole list because agno 3.x reads the session
with a run limit and writes back only the most recent runs plus the
new one. The branch keeps its full list on such a write; a limited
read never shortens history.

A crash mid-turn loses the staged conversation and the staged files
together. That is the correct outcome: both or neither.

## Fork

Forking is a **workspace verb**, not an agno one:

```python
from nontainer.adapters.agno_db import fork_session

child = fork_session(ws, "what-if", conversation="inherit")  # or "fresh"
```

which does `ws.fork(name)` and then, in the fork, rewrites one key:
`__agno__/session` gets `session_id = name` and
`forked_from_session_id = <parent session id>` (agno's own lineage
field, so tooling that reads it stays honest). With
`conversation="fresh"` the run keys are deleted and `run_ids`
cleared, giving a clean chat over the forked files. Run ids are
left as they are: agno mints fresh ones on its own fork only to
avoid collisions inside a shared db, and branches never share one.

agno's `Agent.fork_session` cannot be used over this db. It
deep-copies every run into a *new session id through the same db*,
which assumes one db holding many sessions. The single-session
guard above turns that into a `NotSupportedError` whose message
names `fork_session(ws, ...)`.

Rewind-then-fork branches from any checkpoint with the conversation
as it was at that checkpoint.

## agno assumptions this rides on

Checked on agno 2.6.22 and 3.0.1; CI's `agno-versions` matrix should
add the session-db tests so a bump re-checks them.

- `BaseDb` session methods and signatures: `get_session`,
  `get_sessions`, `upsert_session`, `upsert_sessions`,
  `delete_session`, `delete_sessions`, `rename_session`. agno 3.x
  also declares `get_tool_results_for_session`, which is its
  offloaded-payload index and not a tool-results view; `JsonDb` does
  not implement it, nothing in agno calls it, and this db inherits
  that not-implemented state rather than answering a different
  question under the same name.
- `AgentSession.to_dict()` emits `runs` as a list of dicts with
  `run_id`; `AgentSession.from_dict` accepts the same. `RunOutput`
  carries `forked_from_session_id`.
- `Agent.cache_session` defaults to `False`.
- `JsonDb.__init__(db_path=..., session_table=..., ...)` and that
  every non-session table goes through the inherited implementation
  untouched.
- agno's `isinstance(db, BaseDb)` checks pass because the class is a
  `JsonDb`.
- The run loop never compares the loaded record's `session_id`
  against the agent's, and never filters runs by `session_id` on
  read (this is what lets a forked branch carry the parent's run
  dicts unchanged).

## Costs and non-goals

- The session key is rewritten every turn. It is small (metadata
  plus the run id list).
- Each run is one immutable blob. A run with large tool outputs is a
  large blob, once.
- Concurrency: one agent per workspace, which the workspace's
  single-writer lock already assumes.
- The embedder's own event log or transcript is not addressed here.
  With runs individually addressable it can become a projection of
  the run keys later; it is not required to.
- The app `db` host object is unaffected. External state has no
  history, on purpose.
- No attempt to version user memories or any cross-session table.
- No cross-session listing. agno's `get_sessions` is its "all of a
  user's sessions" call, and this db can only answer with its own
  branch, so agno's session-history tools, the AgentOS session
  routers, and the chat-interface adapters see one session through
  it. Listing sessions is the embedder's registry's job here, since
  the embedder owns the workspaces. If agno's listing features are
  wanted, the fix is a store-level helper that reads the session key
  at each branch head, not a multi-session db.

## Tests

Against a real `Agent` with a scripted model (no LLM key):

- a turn produces exactly one new commit, whose tree contains the
  turn's file writes and one new `__agno__/runs/<id>` key;
- `ws.restore(previous_head)` followed by a run yields a
  conversation that does not contain the rewound turn;
- `fork_session(..., conversation="inherit")` produces a branch whose
  session id is the fork's and whose runs equal the parent's at that
  commit; `"fresh"` produces an empty run list over the same files;
- `Agent.fork_session()` over the db writes nothing (the guard
  raises inside the db; agno logs and swallows it);
- a team session raises `NotSupportedError`;
- an upsert whose prior runs are not the branch's runs (the
  `cache_session=True` shape: restore, then a run from a stale
  in-memory session) raises and writes nothing.
