# agno sessions in the workspace (design)

Status: design, not yet implemented. Ships under the `[agno]` extra
as `nontainer.adapters.agno_db` when it lands.

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
- `get_tool_results_for_session` (agno 3.x) is reassembled from the
  run keys, because the inherited implementation would read the
  sessions table on disk and find nothing.

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
table is seven of them (eight on 3.x). Overriding seven public
methods is the whole exposure to agno churn.

## Values

Run dicts and the session dict are stored as the JSON-shaped dicts
agno hands over — no pickling of agno objects, so a kvgit tree never
depends on agno's class layout, and the exporter story (dump a
branch to plain files) stays trivial. `AgentSession.from_dict` /
`RunOutput.from_dict` rebuild the objects on read.

## The commit trigger

`WorkspaceTools(checkpoint="session")` is a third mode beside
`"call"` and `"turn"`:

- the toolkit does **not** commit on tool calls and registers no
  post hook;
- `KvgitSessionDb.upsert_session` writes its keys and then, if the
  upsert added or changed a run, calls `ws.checkpoint(info={"tool":
  "session"})`.

So the turn's files, cache, cwd, and conversation land in one
commit, and the commit happens at the moment agno persists the run.

Why the db and not a post hook: in agno's sync run loop, post hooks
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

Under `checkpoint="call"` the db still commits its own trailing
write, so the head at the next user message includes the
conversation. Under `checkpoint="turn"` the post hook commits files
and the db commits the conversation as a second commit; that mode
keeps working but is not the point of this feature, and the docs
steer session-db users to `"session"`.

## Rewind

`ws.restore(commit)` rewinds the run keys with everything else.
agno reads the session from the db at the start of every run
(`Agent.cache_session` defaults to `False`), so the next run sees
the rewound conversation with no invalidation step.

`cache_session=True` breaks this: agno would append to a stale
in-memory run list and write the rewound turns back. The db cannot
see the agent, so it cannot enforce this; the adapter docs state it
as a requirement, and the studio-style embedder that rebuilds the
agent after a restore is safe either way.

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
  `delete_session`, `delete_sessions`, `rename_session`; on 3.x also
  `get_tool_results_for_session(session_id, limit)`.
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

## Tests

Against a real `Agent` with a scripted model (no LLM key):

- a turn produces exactly one new commit, whose tree contains the
  turn's file writes and one new `__agno__/runs/<id>` key;
- `ws.restore(previous_head)` followed by a run yields a
  conversation that does not contain the rewound turn;
- `fork_session(..., conversation="inherit")` produces a branch whose
  session id is the fork's and whose runs equal the parent's at that
  commit; `"fresh"` produces an empty run list over the same files;
- `Agent.fork_session()` over the db raises `NotSupportedError`;
- a team session raises `NotSupportedError`;
- on 3.x, `get_tool_results_for_session` returns the tool results
  from the run keys.
