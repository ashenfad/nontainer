# Notes and blame roadmap

> **Status, 2026-10-29: a proposal; nothing is built.** Two pieces. A
> **note** is a sentence an agent leaves on a tool call that changes
> its tree, saying why; it is recorded with the commit the call makes.
> **`ws-git blame`** answers "where did these lines come from" with
> pointers into the session's [trace](curation.md#traces-as-files): the
> commit, the run, the tool call, and the note. Together they give a
> later agent the reasoning behind a span of code without a comment in
> the file and without reading a whole transcript.
> [Where this stands](#where-this-stands) is the ledger; the slices it
> tracks are in [Build order](#build-order). The work starts after
> nontainer's move to kvgit 0.5.

## The idea

A session already keeps everything needed to explain its code. Every
mutating tool call is a commit ([design notes](design.md#commit-granularity-is-the-tool-call-or-turn-not-the-write)),
the conversation is stored beside the files, and a trace renders that
conversation as files an agent can grep. What is missing is the link
from a span of code to the moment that wrote it, and a sentence at that
moment written for whoever comes looking.

Code comments are the wrong home for that sentence. A comment states
what the code is and travels with it to every reader and every tool; a
comment that narrates how the code got there goes stale the moment the
code moves on. The reasoning behind a change belongs to the change: it
describes a moment and claims nothing about the code now, so it never
goes stale.

A note is that sentence, and it is addressed to **future agents**, the
ones that later explore the transcript or blame a file. It is not a
message to the user, though a UI may show it.

## The short version

- **One optional argument, `note`, on every tool that can commit:**
  `terminal`, `file_write`, `file_edit` and `run_python` (`run_ts` in
  agex-ts's world). The same name and the same description everywhere.
  It asks for why, not what: the diff already shows what.
- **A note is recorded in the commit's info**, beside `tool`. Blame
  reads it from history, so it needs no transcript, survives compaction,
  and comes back with a delegate's merged commits.
- **Traces show it:** each commit in a run's `changed.md` carries its
  note.
- **`ws-git blame` prints pointers, not prose:** one line per hunk,
  naming the commit, the session, the run, the tool and the note, plus
  the trace file of the call that made the change. The agent opens a
  file only if it wants more.
- **What blame quotes, in order:** the agent's ws-git commit message,
  the note, the reasoning from the same model response as the call, the
  call itself, and the run's input. It never reaches back past a tool
  result for reasoning that belongs to an earlier step.
- **Asking the author** is a delegation forked from the blamed commit:
  the last resort when the excerpt is not enough.
- **nontainer owns all of it**; world ports it, checked by the corpora;
  agex and agex-ts get it with their tools.

## Where this stands

| piece | state | where |
|---|---|---|
| Per-call commits stamped `{"tool": <name>}` | exists | `Workspace._maybe_commit` |
| Commits assigned to runs | exists | `nontainer.traces.run_ranges` |
| Traces with `changed.md` per run, `worktree add --trace` | exists | [ws-git.md](ws-git.md#the-conversation-beside-the-files) |
| Merges commit with the source's commit as a parent | exists | `KvgitProvider.merge`: always a merge commit, never a fast-forward |
| The `note` argument and its recording | proposal | slice 1 |
| The note in `changed.md` | proposal | slice 1, pinned by the trace corpus |
| A response id and a call id on `TraceEvent` | proposal | slice 3 |
| `ws-git blame` | proposal | slice 4, pinned by the ws-git corpus |
| The world port | proposal | slice 5 |
| Notes in the studios' UI | proposal | slice 6, per app |
| Asking the author | proposal | slice 7 |

## The note

### One field, not two

A model's reasoning already has a home. agex records a provider's
reasoning as a `Thinking` part (`agex/record.py`): per response, not
per call, present only when the provider was asked for it, and possibly
redacted or summarized. It is kept so it can be sent back to the
provider, not written for a reader. agex before its rebuild asked for
reasoning in the reply itself, as a `title` and a `thinking` section
before every action; native reasoning has replaced the second, and a
note takes the first's place with a narrower purpose.

So a call carries one field written for later readers, and it is not
called `thinking` or `thought`: one trace holding two things named
thinking, the provider's and the agent's, would be read as one.

### The argument

```
note: string, optional
  Why this call changes files, in one sentence, for agents who later
  read this session's history. Skip it when the reason is obvious from
  the change; don't describe the change itself.
```

- **Optional, with a description that says when.** Required, it fills
  with restatements of the diff; optional with a weak description, it
  is skipped. The description is the lever, and the traces measure it.
- **The same on every tool.** Two names for one idea read as two ideas.
- **Only on tools that can commit.** `view_image`, `sessions` and the
  read-only verbs take none. `ws-git commit` has its message, and a
  host verb (`merge`, `revert`, `checkout`) records its own provenance.
- **Breadth follows the tool.** A `file_edit` note covers one path. A
  `terminal` or `run_python` note covers whatever the call changed,
  which can be one file or forty, and blame says how many it covers.

Whether a terminal or code call writes anything is known only after it
runs, so the argument is always offered and recorded only when the call
commits. A read-only call's note stays in the trace's record of the
call and nowhere else.

### Where it is recorded

In the info of the commit the call makes, beside `tool`:

```
{"tool": "file_edit", "note": "Keep the earlier creation time: a merge must not make a file newer than its first write."}
```

The commit is the right home for three reasons:

- **Blame reads it straight from history.** Lines resolve to a commit,
  and the commit holds the note. No transcript, no event lookup, no
  trace source that can read the harness's format.
- **It outlives the conversation.** Compaction folds turns; a store's
  harness may be one this nontainer cannot read; a session may keep its
  files and lose its transcript. The note is in none of those.
- **It travels with the work.** A delegate's conversation never comes
  back in a merge, but its commits do: a merge commit has the
  delegate's commit as a parent, so the delegate's notes stay reachable
  from the parent's history.

**Per-turn commit mode.** With `WorkspaceTools(commit="turn")`, calls
don't commit; the turn lands one commit for all of them. That commit's
info carries a list instead, one entry per call that wrote something:

```
{"tool": "turn", "runs": {...}, "notes": [{"tool": "file_edit", "note": "...", "paths": ["/src/merge.py"]}, ...]}
```

Blame resolves lines to the turn's commit and the path to an entry.
Two calls in one turn that wrote the same file can't be told apart at
that grain; per-call commits are the mode that can.

### In the trace

`changed.md` lists a run's commits under the tool each names. The note
follows the commit's heading:

```
## 4e5f6a7 file_edit

Keep the earlier creation time: a merge must not make a file newer than its first write.

- M src/merge.py
```

That makes every run's `changed.md` a list of changes with their
reasons, useful before blame exists: `grep -A2 merge.py
an/.trace/runs/*/changed.md` is already a coarse blame. The rendering
is pinned by the trace corpus like the rest of the trace.

## `ws-git blame`

```
$ ws-git blame src/merge.py -L 40,60
40-47  4e5f6a7  analyst  run 002-77b0e2d1  file_edit  Keep the earlier creation time: a merge must not…
48-60  9b0c1d2  (this)   run 005-a81c3f02  terminal   (note shared by 7 files) Rename region to area…

analyst's trace: ws-git worktree add --trace an analyst@4e5f6a7
  the call: an/.trace/runs/002-77b0e2d1/events/014-tool-file_edit.md
```

### What it walks

Blame attributes lines through the **full history** of the session, the
per-call commits included, as `log --all` does. The per-call commits
are the plumbing the agent's own graph hides, but they are where a
line's tool call and note are. A blame that stopped at the agent's
commits would name the ws-git commit that took a line in, and nothing
about the call that wrote it.

Lines that came in through a merge are followed into the source: the
merge commit's second parent is the source's commit, so blame continues
in the delegate's history and names the delegate's session. A take, an
apply, a revert or a cherry-pick records its source as a soft reference
in its info (`applied_from`), not as a parent; blame follows the
reference while the commit it names is still readable, and stops at
the take when it is not.

### What it prints

- **Pointers, one line per hunk.** Adjacent lines from the same commit
  are one hunk; past a cap, the rest is counted, not printed.
- **The narrowest file worth opening:** the event file of the tool call
  that made the change, not the whole run. The reasoning just before it
  is in the neighbouring event files.
- **A command that already works.** Code from another session resolves
  to `session@commit`, and `worktree add --trace` takes exactly that,
  so the author's trace mounts with no new verb.
- **Quoted words are data.** A note, a commit message and an excerpt
  are another agent's words. Blame's output says so, as a trace's
  README does.

### What it quotes

For each hunk, the first of these that exists, from most deliberate to
least:

1. **The agent's ws-git commit message**, when a ws-git commit covers
   the change: written on purpose, for a whole composition.
2. **The note**, written on purpose, for this call.
3. **The reasoning from the same model response** as the call: the
   thinking and text emitted before it, with no tool result between.
   When the response made several calls, the reasoning covers all of
   them, and blame says so.
4. **The call itself**: the command, or the edit's arguments.
5. **The run's input**, the first line `index.md` shows. Every run has
   one.

A level that is empty is skipped, never filled from elsewhere. Text
written after an earlier tool result answered that result; it is not
the reason for this call, and quoting it would attribute one step's
reasoning to another.

## Asking the author

When the excerpt is not enough, the agent can ask the agent that wrote
the code. That is an ordinary delegation from a ref: fork the author's
session with its conversation (`inherit="full"`) and ask. Where it
forks decides what the author knows:

- **At the blamed commit**, it knows only what it knew when it wrote
  the code. That answers "why did you write this?"
- **At the author's head**, it knows what happened since. That answers
  "do you still think so?"

Its limits:

- **It reconstructs; it doesn't remember.** A model reading an old
  transcript, perhaps compacted, perhaps on another model, gives a
  plausible account of it. Ask for reasons and some answers will be
  rationalizations.
- **It costs a whole context** to answer one question, where the
  excerpt costs a file read.
- **Some authors are gone.** A deleted session has no conversation to
  fork, and blame says so rather than failing. A publication's derived
  commit holds no conversation by design.

The embedder's rules for who may consult whom, and its budgets, apply
as they do to every delegation.

## Gaps

- **`TraceEvent` has no response id and no call id.** A run is a flat
  list of events. "The reasoning from the same response" can only be
  inferred by adjacency: the thinking and text before a tool event,
  back to the previous tool event. Parallel calls break it (consecutive
  tool events share one response, and adjacency gives its reasoning to
  the first), and so does a harness that stores events out of order.
  Both ids are optional fields on the neutral shape, filled by the agno
  and agex trace sources, which have them from their providers.
- **From a commit to its event.** `run_ranges` assigns a commit to a
  run by position; nothing yet names the tool call within the run. The
  fix is the call id again: a per-call commit records it, and blame
  matches it to the event with that id. Without it, blame points at
  the run.

## Where it lives

| layer | what | where |
|---|---|---|
| nontainer | the argument and its description | `adapters/tools.py`, `adapters/render.py` |
| nontainer | recording, per call and per turn | `Workspace`, the commit flow |
| nontainer | the note in `changed.md`; the ids on `TraceEvent` | `nontainer.traces`, the trace corpus |
| nontainer | `ws-git blame` | `wsgit.py`, the ws-git corpus, [ws-git.md](ws-git.md) |
| world (agex-ts) | the port of all four | `tools/tools.ts`, `tools/descriptions.ts`, `traces.ts`, `wsgit/command.ts`, checked by both corpora |
| agex, agex-ts | nothing for the agent: their tools and instructions come from below | their trace sources already read a call's arguments |
| each studio | showing a note on a tool call | per app |

## Prior art

Zed's DeltaDB records every edit as an operation with a stable
identity, links each edit to the conversation that produced it, and
anchors comments and references to characters rather than lines, so
they follow code as it moves. The link from code to conversation is
the part worth taking.

Character identity is not. A person's typing has no natural unit, so
attributing it needs one per character. An agent's edits already come
in units: the tool call, which here is a commit. Line-level blame over
per-call commits reaches the call, and the call reaches the
conversation. What character identity adds is tracking code across
files and refactors, and that is not needed to answer "why is this
line like this".

## Open questions

- **The name.** `note` or `reason`. `note` invites more than a reason;
  `reason` may read as required.
- **How far terminal and code calls are covered.** A note on a command
  that touched forty files says little about any one of them. Whether
  those notes earn their tokens is a question for the dogfood slice.
- **Per-turn mode's grain.** Two calls in one turn writing one file
  share an entry's path. If that matters, per-turn mode records a
  call's changed line ranges as well as its paths.
- **What a note costs.** Roughly 15 to 30 output tokens per edit, and
  re-read as context on later turns. Small beside the edit, but output
  tokens are the dear ones.

## Build order

1. **The note.** The argument on the four tools, recording in commit
   info for both commit modes, and the note in `changed.md`, with the
   trace corpus updated.
2. **Dogfood and measure.** From traces: how often calls that commit
   carry a note (fill rate), how many notes restate the diff (filler),
   and, once blame exists, how often a later agent reads one (reach).
   The slice decides whether the rest is worth building.
3. **The ids on `TraceEvent`**, through the agno and agex trace
   sources and the corpus.
4. **`ws-git blame`**, with its corpus cases and its section in
   [ws-git.md](ws-git.md).
5. **The world port**, checked by both corpora.
6. **The UI**: a note as a muted line under its tool call, in each
   studio.
7. **Asking the author**, as a calling convention over `sessions`.
