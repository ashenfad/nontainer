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

sessions = Sessions(ws, runner, budget=None, max_workers=4, chain=(), on_answer=None, loop=None)
sessions.ask(task, *, name=None, paths=None, inherit=None,
             fork_from=None, resume=None, wait=False, budget=None) -> Job | Answer
sessions.list() -> list[Job]
sessions.result(name) -> Answer      # JobRunning while it runs
sessions.take() -> list[tuple[str, Answer]]   # every answer not yet collected
sessions.outstanding() -> list[str]  # running, or answered and not yet collected
sessions.wait(timeout=None) -> list[str]   # blocks until an answer is waiting or none runs
sessions.on_answer = fn              # fn(name, answer), once per recorded answer
sessions.cancel(name) -> Job        # the answer is discarded; an async run is stopped
sessions.keep(name) -> Job           # out of the retention sweep, for good
sessions.base(name) -> str | None    # the commit it was forked from
sessions.sweep(idle, *, min_age=3600) -> list[str]   # the branches it took
sessions.close()                     # joins the workers; the branches stay

# from a coroutine
await sessions.aask(task, *, wait=False, ...) -> Job | Answer
await sessions.await_ready(timeout=None) -> list[str]    # wait(), awaited
async for name, answer in sessions.answers(): ...        # each answer as it lands, once
await sessions.aclose()
```

`ask` forks under a pet name scoped to the parent
(`analyst.sleepy-otter` — a session id becomes a branch name and holds
no separator, so the scope is a dot), hands the child to the runner on
a worker thread and returns at once. `paths` narrows what the child
SEES without narrowing its branch; `inherit` decides only whether the
conversation comes along — a brief or a summary is content the task
carries. Unset, it follows where the child is forked from: a child of
this session starts `"fresh"` (against `fork`'s own `"full"`), and one
forked from elsewhere carries the conversation there (below). `wait=True`
blocks and returns the `Answer` instead.

**A name you give is a name, not a preference.** `ask(name="editor")`
on a session that already has an `editor` child is refused, naming the
branch and pointing at `resume="editor"` for giving that child its next
task. A silent `.2` would hand back a branch that is not the one asked
for, and every later `result` / `merge` / `diff` spelled with the name
would go to the wrong child. A minted pet name carries no such intent,
so a collision there is settled with a numeric suffix and reported to
nobody.

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
sessions.ask("what did you find?", fork_from="sage", wait=True)
sessions.ask("what is north?", fork_from="rates-2026", inherit="fresh", wait=True)
answer = sessions.ask("read it", fork_from="sage@a3f9c2e", wait=True)
sessions.ask("one like it", fork_from="myapp/v1", wait=True)
sessions.ask("and south?", resume=answer.branch, wait=True)
```

`fork_from` names another fork point: a **session** by its bare name,
as it is now (its latest commit, resolved once when the ask is made),
a commit named by a **store tag** (`store.tags.add(ws, "rates-2026")` —
a name that belongs to no session and outlives the one that made it),
or one spelled `session@commit`. The ref resolves the way every other
cross-session read resolves one, so a short commit id and a session's
own ws-git tag are spellings this takes too. A name with no `@` that is
both a store tag and a session is the tag: a tag is a name someone
chose for one commit, and the session has the `session@commit`
spelling. A name that is neither is refused, listing the sessions and
store tags there are. The child is forked THERE rather than here, and
the branch it came from is only read: every ask forks, and the child
is what the runner drives — so asking a live session a question never
touches it.

`inherit` says what the child arrives with, and both values mean
something with a fork point. `"full"`, the default from elsewhere,
keeps the conversation the fork point holds: the delegate is then the
agent that was there, as of that commit, and the task is its next turn.
That is how you ask another session about its work, or the author of a
published app for another one like it, long after the session that
built it is gone (the commit is readable for as long as a store tag
names it). `"fresh"` is a delegate that starts a chat of its own over
that state's files. What a fork inherits is the conversation at the
FORK POINT, not this session's, so a full inherit from elsewhere
carries none of yours. It arrives as the child's own: a branch holds
one session's conversation, so the stored session record is rebound to
the child, and the session it came from is kept as the child's
lineage.

A fork point outside the asker's own history shares none with it, so
that child's branch holds the other state's whole tree, and a merge of
it brings all of that back, not only what the child wrote
(`answer.changed` is measured against the fork point, which is the
honest answer about the child and not about your tree). Take its
files, or read it in place, unless bringing the other state in is what
you meant. Such an answer's provenance carries `"outside": True`, and
its next step offers `ws-git checkout <name> -- <paths>` and says what
a merge would bring rather than offering it as taking the work; `ws-git
diff <name>` shows what the child changed since it began. A fork point inside the asker's history — a store tag of
one of its own commits, say — is an ordinary ancestor, and the child
merges back as any fork does.

`resume` gives a new task to a child this session already has: the
same branch with its conversation kept, and no second fork. Everything
that belongs to the CHILD carries over — where it was forked from, its
base, whether it is `keep`-flagged — and only the run is new: the
task, the status, the answer, the timings. A fork point that the child
did not come from is refused, since a child answers from the state
whose conversation it carries, and `inherit` must stay `"fresh"`
there: a resumed child keeps the conversation it has, and there is no
second fork to seed.

A child does **one task at a time**. A run still in flight refuses the
next one, and it is in flight until its runner stops — `cancel`
discards an answer, it cannot interrupt the embedder's loop, so a
cancelled run holds its child until the runner is done with it. The
check that a branch is free and the reservation of it are one critical
section, so two callers resuming one child cannot both pass.

**Everything a delegate wrote is its answer.** A delegate need not
think about ws-git at all, and the answer names what it *landed*:

| the delegate | `answer.ref` names | `merge` / `checkout <name> -- <paths>` |
|---|---|---|
| never used ws-git | its branch head — autocommit put every write there | take that head |
| used ws-git | its last ws-git commit | take that commit |
| used ws-git, then wrote past it | a commit of the rest, made for it when it answered | take that commit |

The third row is the one the helper acts on. A delegate that committed
part of its work and then wrote more would otherwise answer with less
than it did, and have its merge refused. So when it answers, whatever
it wrote past its last commit is committed for it, under a message
saying the delegation mechanism made the commit, and the answer names
that commit; the delegate's own commits stay in its log as the
checkpoints they were. Not while a merge of the delegate's own is
unresolved, though: its conflict markers would go into that commit and
on to the caller unannounced. Then, or if the commit fails,
`answer.uncommitted` is True, the merge refuses, and the caller takes
paths or asks again.

**Some paths are never part of an answer.** Anything the delegate
wrote outside the workspace root, the app runtime's handler logs and
test_app captures (`app/logs`, `app/screenshots`), and whatever the
embedder's ignore patterns name stay on the delegate's branch and out
of everything else: the answer's `changed` paths, the check for
uncommitted work, and the merge, which keeps the asking session's own
copies. A delegate that checks its work
with test_app after its last write still answers with its work alone.

The first row works because **a fork starts with a fresh ws-git state**
(see `ws.fork` in the [API reference](api.md)): no head, nothing
staged, no inherited merge context, for `inherit="full"` as much as
`"fresh"`. A delegate is measured from the tree it was forked with, so
what it inherited never reads as its work and `ws-git add -A` takes
only what it changed.

Delivery is **pull**: `ask` on one turn, `result` on a later one. How a
parent learns a delegate finished — a dot in a rail, a message injected
into the next turn — is the embedder's. `cancel` means the answer will
be discarded and the branch left as it is: a runner already working is
the embedder's loop and cannot be interrupted, and nothing the helper
does writes to the child's branch, so there is nothing to undo either.
A job that has already answered comes back unchanged.

`take()` is the collection point for an embedder that pushes: every
answer that has landed and has not been collected, in landing order.
It touches each job exactly as `result` does and marks it collected,
and the two share that one mark — an embedder that reads answers
between turns and takes them mid-turn never hands the same answer over
twice. A cancelled job never appears, since its answer was discarded,
and an expired one drops out with its branch; a delegate asked again
(`resume`) lands a new answer and appears again.

The helper says when an answer lands; what the parent does about it
is the embedder's. Two ways to hear it, both without polling:

- **`on_answer(name, answer)`** is called once for every answer that is
  recorded, on the worker thread that ran the job, after the job has
  settled: the answer is collectable, the branch is free for a
  `resume`, and the child handle is closed. An answer a `resume` has
  replaced by then is still reported, since it was recorded. A
  cancelled job's answer is discarded rather than recorded, so it calls
  nothing, and a hook that
  raises is logged without touching the job. It is the place to start
  the parent's next turn when nobody is talking to it.
- **`wait(timeout=None)`** blocks until an answer is waiting to be
  collected, or until no job is running, and returns the names with an
  answer waiting. It collects nothing. An empty list means there was
  nothing to wait for, the timeout passed, or the helper closed.
- **Waiting from agent code is safe.** A `run_python` call holds the
  parent's workspace lock while it runs. Landing an answer reads only
  committed history, through the child's handle, and never takes that
  lock, so code that asks a delegate and waits for it (`wait=True`, or
  `wait()` then `result()`) gets the answer, on every rung.

`outstanding()` lists the jobs not yet heard the end of: still running,
or answered and not collected.

Delivering an answer is still the embedder's too. The agno adapter's
inbox (the [API reference](api.md) has the wiring) is one way: with
`tool_hooks` bound it calls `take()` at every tool result and delivers
the answers there — mid-turn rather than on the next one — framed as
the delegation mechanism speaking rather than as the person the agent
works for. `inbox.on_delivered` is where an embedder records that
delivery.

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

One method: run this session with this task and answer. nontainer
cannot own it because nontainer has no model. A plain `str` means an
`answered` answer with that text; an `Answer` says more. A runner that
raises does not lose the job — it resolves as `failed` with the
exception's text as the answer.

**Synchronous or async.** A synchronous `run` is called on a worker
thread of the helper's own, up to `max_workers` at once. An
`async def run` is scheduled on an event loop instead: the one the
helper was built in, or `Sessions(..., loop=)`. Then:

- `max_workers` bounds the runs in flight;
- landing an answer, which does store work, runs on a thread, so the
  loop never waits on it;
- `cancel` stops the run, where a synchronous runner's answer can only
  be discarded, and the branch is free once the run has ended;
- on that loop's own thread, `ask(wait=True)` and `close()` would block
  the loop they wait on, so they are refused: `aask` and `aclose` await
  instead.

**A reply is not always the answer.** A delegate may delegate, and an
agent told to end its turn while its own delegates work (rather than
poll for them) replies "waiting on them" with its delegates still
running. Returned as the answer, that strands their results: the parent
gets the waiting message, and the delegate's delegates answer to a
session nobody runs again. So a runner checks the child's own helper
after each reply. While `outstanding()` is not empty, the reply is the
child waiting: the runner blocks in `wait()`, runs another turn that
delivers what `take()` hands over, and returns the reply the child
gives once nothing is outstanding. A runner bounds those extra turns by
its own measure, and when it stops early it should say in the answer
which delegates went unread.

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
# status: nontainer.JobStatus — running | answered | declined | capped
#         | cancelled | failed | expired (its branch was swept)
# touched: when the caller last dealt with it — what sweep() measures
# origin: (the fork point as spelled, its commit) — None when the
#         child was forked from the asking session

Answer(text, status, ref, branch, changed, artifacts, provenance, uncommitted)
# status: nontainer.AnswerStatus — answered | declined | capped |
#         failed. Fewer words than a job's: an answer exists only once
#         the run is over, so it is never running, cancelled or expired
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

A delegate asked without `paths` was seeded with the whole tree, so
there is nothing outside it: **`changed["seed"]` holds every path it
touched and `changed["elsewhere"]` is empty.** The grouping is a
narrowing's answer, and an ungrouped delegate is not one that changed
nothing.

**The job table lives as long as the `Sessions` object.** It is
process-local — nothing about a job is written to the store, so a
restart, a second process, or a `Sessions` built fresh over the same
workspace starts with no jobs and no answers. What survives is what
the store holds: the child's branch, exactly as the delegate left it.
`store.sessions()` lists it, `ws-git diff <branch>` and
`ws.diff(...)` read it, `store.open(branch)` opens it, and `ws.merge`
or `ws.checkout(branch, paths=)` takes the work. So the recovery after
a restart is the branch, never the job: an embedder that needs job
records to outlive its process writes them where its own state lives.

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

