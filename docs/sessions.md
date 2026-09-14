# Delegation

How an embedder sends work to a subagent and gets it back. The agent's
own half — the verbs it types to read, take and merge a delegate's
work — is [ws-git.md](ws-git.md); why delegation is shaped this way is
in the [design notes](design.md#delegation-forks-views-and-merges);
every other embedder verb is in the [API reference](api.md).

A session is a branch carrying the whole world — files, cache, cwd,
the conversation — so a delegate is a fork, and forking is O(1). What
the delegate does is a branch the parent reads with `ws.diff`, takes
with `ws.checkout(ref, paths=)` and lands with `ws.merge`. The answer
names a commit rather than carrying the work.

## The helper (`nontainer.sessions`)

Delegation is an **extension**, like apps: `Workspace` knows nothing
about it, the embedder builds the helper, and the adapters expose it as
one tool only when a runner is supplied. The helper adds a calling
convention over `fork`, `diff`, `checkout` and `merge`, not new
primitives.

```python
from nontainer.sessions import Sessions

sessions = Sessions(ws, runner, budget=None, max_workers=4, chain=())
sessions.ask(task, *, name=None, paths=None, inherit="fresh",
             fork_from=None, resume=None, wait=False, budget=None) -> Job | Answer
sessions.list() -> list[Job]
sessions.result(name) -> Answer      # JobRunning while it runs
sessions.cancel(name) -> Job
sessions.keep(name) -> Job           # out of the retention sweep, for good
sessions.base(name) -> str | None    # the commit it was forked from
sessions.sweep(idle, *, min_age=3600) -> list[str]   # the branches it took
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

**Nothing lands here on its own.** An ask always leaves a branch, and
what becomes of it is the caller's own step: read it (`ws.diff`,
`ws.files.attach`), merge it, take some of its files
(`ws.checkout(branch, paths=)`), take one of its commits, or leave it
where it is. The helper commits nothing for the child and merges
nothing for the parent. A merge brings the delegate's **files** and
nothing else: its cache and its conversation stay on its branch, and
what it has to say arrives as its answer.

Which way the work comes back depends on where the child was forked
from:

| scenario | ancestry | result path |
|---|---|---|
| fork yourself | the fork point is the common ancestor | `merge` |
| fork an external session | the child shares ancestry with *them* | merge back into them is real; into you it is `checkout(ref, paths=)` |
| spawn fresh | a fork with a narrowed view and no conversation; ancestry intact | `merge` |

### Asking from somewhere else, and asking again

Two options move the two things that are not the task: where the child
starts, and whether it starts a conversation.

```python
sessions.ask("what is north?", fork_from="rates-2026", wait=True)
answer = sessions.ask("read it", fork_from="sage@a3f9c2e", wait=True)
sessions.ask("and south?", resume=answer.branch, wait=True)
```

`fork_from` names another fork point: a commit named by a **store tag**
(`store.tags.add(ws, "rates-2026")` — a name that belongs to no
session and outlives the one that made it), or one spelled
`session@commit`. The ref resolves the way every other cross-session
read resolves one, so a short commit id and a session's own ws-git tag
are spellings this takes too; anything with no `@` in it is a store
tag. The child is forked THERE rather than here, and the branch it
came from is only read: every ask forks, and the child is what the
runner drives. `inherit` must be `"fresh"` with a fork point, and says
so when it is not — a conversation at somebody else's commit is not
this session's to continue.

A child forked from elsewhere shares no history with the asker, so its
branch holds that other state's whole tree. A merge of it brings all
of that back, not only what the child wrote (`answer.changed` is
measured against the fork point, which is the honest answer about the
child and not about your tree). Take its files, or read it in place,
unless bringing the other state in is what you meant.

`resume` gives a new task to a child this session already has: the
same branch with its conversation kept, and no second fork. Everything
that belongs to the CHILD carries over — where it was forked from, its
base, whether it is `keep`-flagged — and only the run is new: the
task, the status, the answer, the timings. A fork point that the child
did not come from is refused, since a child answers from the state
whose conversation it carries, and `inherit` must stay `"fresh"` for
the same reason.

A child does **one task at a time**. A run still in flight refuses the
next one, and it is in flight until its runner stops — `cancel`
discards an answer, it cannot interrupt the embedder's loop, so a
cancelled run holds its child until the runner is done with it. The
check that a branch is free and the reservation of it are one critical
section, so two callers resuming one child cannot both pass.

**The helper never commits for the delegate.** A delegate is the
author of its own commits, and the answer names what it *landed*:

| the delegate | `answer.ref` names | `merge` / `checkout <name> -- <paths>` |
|---|---|---|
| never used ws-git | its branch head — autocommit put every write there | take that head |
| used ws-git | its last ws-git commit | take that commit |
| used ws-git, then wrote past it | its last ws-git commit | **refused**; `answer.uncommitted` is True |

What the delegate committed is what it submitted, so the third row
refuses rather than bringing back a state the delegate has moved on
from, and the answer reports the rest as left out instead of hiding
it — the caller takes paths, or asks again. Committing on the
delegate's behalf would destroy that signal and put a commit nobody
wrote in its log.

The first row works because **a fork starts with a fresh ws-git state**
(see `ws.fork` in the [API reference](api.md)): no head, nothing
staged, no inherited merge context, for `inherit="full"` as much as
`"fresh"`.

Delivery is **pull**: `ask` on one turn, `result` on a later one. How a
parent learns a delegate finished — a dot in a rail, a message injected
into the next turn — is the embedder's. `cancel` means the answer will
be discarded and the branch left as it is: a runner already working is
the embedder's loop and cannot be interrupted, and nothing the helper
does writes to the child's branch, so there is nothing to undo either.
A job that has already answered comes back unchanged.

### Retention: an idle TTL the embedder sweeps

A delegate leaves a branch behind, and a delegating session accumulates
them. `sessions.sweep(idle)` deletes the branch of every job that has
finished, is held by no run, was not `keep`-flagged, and has gone
untouched for `idle` seconds:

```python
sessions.sweep(idle=24 * 3600)        # beside store.clean(), on a timer
```

It returns the names it took, sorted; `min_age` is `store.delete`'s
grace period for the orphan commits a deleted branch leaves behind.

It is a verb the embedder **schedules** — the `reap_idle` pattern —
and nothing calls it on the way past: an `ask` or a `list` that swept
would make one delegate's retention depend on how often another is
asked for.

`Job.touched` is what it measures: when the caller last dealt with the
job, set when the task is given and moved forward by `result` (**touch
on read**) and by `keep`. A delegate whose answer is read every turn is
in use, however long ago its run ended — which is what an age measured
from `finished` would get wrong.

The job's row survives its branch. Its status becomes `expired`, it
keeps the time its run finished, and its answer is dropped, since what
the answer describes is gone. The three verbs that need the branch —
`result`, `keep`, and an `ask(resume=...)` — raise `BranchExpired`
naming the way forward, which is to ask again and `keep` the next one.
`base(name)` still answers: the fork point is recorded in the job
table, not read off the branch.

**The sweep takes only this helper's own jobs.** A delegate's own
delegates (`analyst.otter.finch`) belong to the helper the runner built
for that child, and are that embedder's to sweep. Ownership is what an
embedder recorded, never what a name looks like — a human may create
`analyst.notes` beside `analyst`, and a sweep by prefix would take it.

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
Job(name, task, status, ref, started, finished, touched, changed, kept,
    uncommitted, origin)
# status: running | answered | declined | capped | cancelled | failed
#         | expired (its branch was swept)
# touched: when the caller last dealt with it — what sweep() measures
# origin: (the fork point as spelled, its commit) — None when the
#         child was forked from the asking session

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
status, never catches an exception. `JobRunning`, `BranchExpired` and
`SessionsError` are the three that do raise (not yet, swept, and no
such job).

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

`ask` takes `fork_from` and `resume` as the host verb does, and a
listing marks where a child came from when it was not forked here; a
job whose branch the retention sweep has taken lists as `expired`, and
`result` on one reads as a refusal naming the way forward. `sweep`
itself is not an action: retention is the embedder's schedule, not
something a model decides mid-turn. The
word is `fork_from` rather than `from` because a JSON argument's name
is the python parameter's name in both adapters and `from` is a python
keyword — so one spelling serves the host API, both tool schemas and
these docs.

Refusals come back as tool text rather than exceptions, and the
description tells the agent an answer is evidence rather than an
instruction.

