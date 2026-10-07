# Curation

> **Status, 2026-10-02: a design; nothing here is built.** How agents
> in nontainer could learn from each other's sessions. A
> **maintainer** reads other sessions' conversations and code and
> keeps a wiki of what recurs; a **builder** turns the wiki into
> skills — prose, and code with tests; a **gate** on the host checks
> each proposal, tries it against real work, and releases it on
> probation; ordinary sessions install the releases. The shape comes
> from WikiSkill (Tang et al.,
> [arXiv 2608.27454](https://arxiv.org/abs/2608.27454)), which ran
> this loop over benchmark rollouts and found the persistent wiki
> worth about fifteen points. Two things here go past the paper: code,
> which agents in a workspace write anyway, and a gate with no answer
> key — the paper's needs one, and an open-ended system has none.
> Most of what the loop needs already exists.
> [Where this stands](#where-this-stands) is the ledger; the rest is
> the design and the rough edges it turned up.

## The idea

A session is a branch that holds what an agent did: the files it
wrote and, with the agno session db, the conversation that wrote them,
in the same commit. Every session on a store is readable from every
other one — `ws-git worktree add` mounts a tree, and `diff` and `log`
read across. So a store full of finished work is already a corpus, and
an agent with a terminal can already read the code half of it.

WikiSkill splits learning from experience into three layers —
immutable traces, a wiki compiled from them, and skills refined from
the wiki — with an agent to move between each pair and a validation
gate deciding which skill changes stay. Two of its findings shape this
design. The wiki must persist across iterations even when skill
changes are rolled back: taking it away from the skill proposer cost
fifteen points on average. And the agent doing the tasks should not see
the wiki: letting it cost about three, because its traces stop telling
you whether the skills work.

What the paper has and an open-ended system does not is an answer key.
Every task in its benchmarks has a known answer, so every trace is
labeled passed or failed, and a proposed skill stays only if it scores
better on a labeled validation set. That is a prompt optimizer's
setting — supervised optimization over a fixed task distribution — and
a store of real sessions is not a task distribution with answers. So
this design keeps the paper's structure and replaces its oracle: the
labels come from what each session's principal did, and the gate is a
sequence of trials and a probation rather than a score on a held-out
set. [The gate](#the-gate) is the longest section, because it is where
the paper does not carry over.

Map that onto nontainer and almost every piece is a branch, a mount or
a merge. Two things are missing: a way to read a conversation as files
— the trace half of a session is stored but not browsable — and the
verbs that move a skill between sessions as a versioned thing, which
starts to matter once a skill carries code. The rest is convention:
who writes where, what a page cites, and when the host merges.

The stretch beyond the paper is code. WikiSkill's skills are prose
procedures. Agents here also write `helpers/` modules and `app/api/`
handlers, so the corpus holds working code beside the conversations
that produced it, and the builder can promote a helper three sessions
wrote independently into a tested library every session can import. A
skill stays the unit — `SKILL.md` is still what the model reads first
— and a library is a skill whose instructions say `import this`.

## The short version

- **A trace is a projection, not a copy.** `ws-git worktree add
  --trace <dir> <ref>` mounts the session's files at `<dir>` and its
  conversation at `<dir>/.trace/`, both pinned at the same commit,
  rendered on read from the stored runs. Read-only and outside
  versioning, like every worktree.
- **The files and the conversation share a commit, so a run knows
  what it changed.** Each run renders a `changed.md`, and "which
  conversation wrote `helpers/excel.py`" is a grep.
- **Curators work in passes, forked fresh from a home session the
  host composes.** A maintainer pass forks `wiki`, a builder pass
  forks `lib`; each starts with no conversation, because its memory is
  the tree, and lands only if the host merges it. The paper's "roll
  the skills back, keep the wiki" becomes: a rejected proposal is a
  branch nobody merged. Two shapes of work, which may be two agents or
  one with two prompts.
- **No agent's tree has a second author.** Agents write in their own
  sessions; everyone else reads by mount. The verbs that do reach into
  an agent's tree — installing or upgrading a skill — land as commits
  in the agent's own graph, the way a host `merge` or `revert` already
  does.
- **Proposals are commits, verdicts are files, releases are
  publications.** The gate records each verdict in `wiki/impact/`,
  merges what passes into `lib`, and publishes the skill's subtree as
  `<skill>/vN` — first on probation, then as current.
- **Skill code is vendored, and upgrades are three-way.** A session
  takes a release into its tree at a version, so a rewind or a fork
  sees exactly the library the agent had; an upgrade applies the
  change between two releases, so a local patch survives and stays
  visible — and a patch is a bug report the maintainer can find.
- **No oracle.** WikiSkill keeps a skill only if it beats a labeled
  validation set; an open-ended system has no labels. Evidence comes
  in three stages instead: checks that need no labels (for code, a
  regression test built from the evidence), paired trials — two arms
  forked from the same real state, one with the skill, judged blind —
  and probation, where a release goes to some sessions first and is
  kept, rolled back or retired on what their principals do.
- **Trials start fresh from real states.** Both arms get the files of
  a real session at a real moment and no conversation, so neither
  inherits a history written by an agent without the skill. A trial is
  one user turn by default. A fork inherits live host objects, which
  makes trials the most dangerous thing in this document.
- **Curators never see what judges them.** Trial runs stay out of
  every corpus, and verdicts reach the curators as scores — the paper's
  wall between training and validation, kept.
- **A store is already a trust boundary.** Within one, live mounts;
  across stores or tenants, specimens: derived, redacted copies, made
  the way `publish` derives an app.
- **Mechanism here, loop elsewhere.** nontainer gains the projection
  and a handful of verbs. Scheduling, scoring and policy are the
  embedder's, and the first consumer is an example, the way `tour.py`
  is.

## Where this stands

| piece | status |
|---|---|
| mounting another session's tree, pinned and read-only (`worktree add`, `files.attach`) | exists |
| the conversation stored per run, in the same commit as the turn's files | exists (`KvgitSessionDb` with `commit="turn"`) |
| delegation with a fresh conversation over a forked tree (`Sessions.ask(inherit="fresh")`) | exists |
| a fork of a real moment with its files and no conversation (`fork_from="s@c", inherit="fresh"`) | exists |
| each run's tokens and duration, stored with the run | exists (agno's `RunMetrics`) |
| telling a running agent something changed (an inbox note) | exists |
| publishing a subtree as an immutable version — from a past commit, too | exists: `store.publish(store.resolve(ref), ...)` |
| a release recorded without becoming current (`publish(current=False)`) | exists |
| taking a published skill into a session (`ws.checkout("sheets/v1", paths=[...])`) | exists, and records `taken_from` |
| importing skill code (`from skills.sheets.cells import f`) | exists for modules; not for packages or hyphenated names |
| skill tests left out of a session's default `ws-pytest` run | exists, by design |
| three-way upgrade of a vendored skill (`apply(base=v1, theirs=v2)`) | exists in the provider; no public verb |
| the trace projection (`--trace`, `.trace/`, `changed.md`) | proposed |
| `ws-git show <session>@<commit>` and `<ref>:<path>` | released in 0.8.6; `.trace/` paths arrive with the projection |
| a turn commit naming the runs it carried (`"runs": {id: status}`) | released in 0.8.6 |
| `skills.install` from a release; `upgrade`, `installed`, `patched` | proposed |
| `Sessions.ask(take=...)`: a fork point with paths taken in | proposed |
| `metadata.requires` read from `SKILL.md` and checked at install | proposed |
| paired trials: two asks with `take=`, a blind judge, staged statistics | proposed, as an example first |
| probation: random assignment by version, signals by `.installed` | proposed |
| trial-safe forks (host objects, mounts, network) | open |
| replaying an agent principal; simulated users | later |
| specimens across stores | open |

## What is already true

What this design leans on, checked against the code. The last four
were run, not just read.

- **A worktree is a pinned, read-only mount of a whole branch.**
  `ws-git worktree add <dir> <session>[@<commit>] | <tag>` attaches
  the source's provider filesystem under `<dir>` — the whole branch,
  not the view a delegate was given — frozen at one commit, outside
  versioning, listed under `worktrees:` in `status`. It is
  `ws.files.attach` spelled for the terminal, and every attachment
  goes through one composed filesystem (`AttachFS`), so anything that
  is a filesystem can be attached the same way.
- **The conversation is stored by run.** With `KvgitSessionDb`, the
  conversation's index lives at `__conversation__/index` (the harness,
  and its `runs` in order), agno's session record at
  `__conversation__/record`, and each run at `__conversation__/runs/<id>`
  (older sessions, under `__agno__/` until their next write). The index
  names the harness, which is what a `TraceSource` picks a renderer by.
  A run's messages hold the
  system prompt as it was built for that run, the user's input, the
  assistant's turns, and every tool call with its result. agno leaves
  out only the history it replayed into the run — the earlier runs hold
  it — and media, when told to. Core already names the plane
  (`planes.CONVERSATION_PREFIX`); the run format is the adapter's.
- **A turn's files and its run share a commit** under `commit="turn"`:
  the db commits when agno persists the run, with `info={"tool":
  "turn", "runs": {run_id: status}}` naming the runs it carried. Under `commit="call"` each mutating tool call commits under
  its own name and the run lands in a trailing commit, so the commits
  between two run landings are one run's work either way. agno can
  also persist a run before it ends (`checkpoint="tool-batch"`), with
  its status `running`.
- **Going back appends.** A checkout writes the target's state as a
  new commit, so the runs it stepped off are still in history — not in
  the head's run list, but reachable.
- **Every session on a store is readable from every other.** `ws-git
  branch` lists them; `worktree add`, `diff`, `log` and `checkout <ref>
  -- <paths>` read them, and `show` reads one of their commits or one
  of their files. The store is the trust boundary, and has been all
  along.
- **A fork can take a moment's files and leave its conversation.**
  `Sessions.ask(task, fork_from="analyst-42@<commit>",
  inherit="fresh")` starts a child on that session's tree as it was at
  that commit, with a chat of its own; `inherit="full"` keeps the
  conversation as it stood there.
- **A run records what it cost.** agno stores each run's token counts
  (input, output, cache reads, reasoning) and its duration with the
  run; its tool calls and their errors are in its messages.
- **A release can wait.** `store.publish(..., current=False)` records
  a version without making it current; `set_current` switches later,
  and back.
- **A running agent can be told something.** An inbox note
  (`Inbox.put(text, kind="mechanism")`) reaches the agent at its next
  tool result, framed as the machinery speaking rather than its
  principal.
- **Skills live in the tree.** `<root>/skills/<name>/SKILL.md`,
  cataloged into the agno toolkit's instructions by frontmatter, and
  installed by `skills.install` from bytes, a file, a directory or a
  package resource.
- **`ws-pytest` leaves vendored tests alone.** With no paths it
  collects `tests/` only, so that a vendored library's tests cannot
  fail a session's run; naming a path runs it.
- **Imports resolve from the workspace root.** `from
  skills.sheets.cells import read_range` imports
  `<root>/skills/sheets/cells.py`. `import skills.sheets` and `from
  skills.sheets import read_range` are refused — the sandbox resolves
  `.py` modules, not a package's `__init__.py` as an import's target —
  and a directory with a hyphen in its name cannot be named in an
  import at all.
- **`publish` takes a frozen workspace.**
  `store.publish(store.resolve("lib@4f2a1c9"), "sheets",
  paths=("skills/sheets/",))` publishes exactly the commit a gate
  tested, whatever its session has done since.
- **A take from a release is recorded.** `ws.checkout("sheets/v1",
  paths=["skills/sheets"])` mirrors the directory and commits
  `{"tool": "checkout", "taken_from": "@store/tag/sheets/v1@…"}`.
- **The upgrade primitive exists.** `provider.apply(base=<v1>,
  theirs=<v2>)`, run on a session holding v1 plus a local fix, lands
  v2's change and keeps the fix. No public verb reaches it:
  `ws.cherry_pick` takes `session@commit` only, and a published
  version's commit has the store's empty root as its parent, so its
  "change" would be the whole tree.

## The cast

| who | writes in | reads | what it is |
|---|---|---|---|
| **workers** | their own sessions | their own trees, installed skills | ordinary agents doing ordinary work; also training rollouts in benchmark mode — never the gate's trial runs |
| **the maintainer** | a pass forked from `wiki` | workers' files and traces; `wiki/impact/` | an agent, one pass per batch of new work |
| **the builder** | a pass forked from `lib` | the wiki, the evidence it cites, past proposals | an agent, one proposal per pass |
| **the gate** | `wiki/impact/`; merges into `wiki` and `lib`; publications | proposals, its own trial runs, probation signals | host code with a judge inside, not an agent |

```
 workers ──trace + files, read-only──► maintainer pass ──merge, after a lint──► wiki
    ▲                                                                             │
    │                                        builder pass ◄────── mount ──────────┘
    │                                             │ one commit
    │                                             ▼
    │                                           gate ──paired trials, on forks of real states
    │                                             ├──► scores and counts, in wiki/impact/
    │                                             │ on a pass: merge into lib, publish on probation
    └──── install / upgrade ◄──── sheets/vN ◄─────┘
```

**Two home sessions, composed by the host.** `wiki` and `lib` have no
agent of their own. A curator's work arrives in them only by merge —
a maintainer pass after a lint, a builder pass after the gate — and
the gate writes its verdicts into `wiki/impact/` directly. Each home
session has one composer, the host, and the paper's central rule falls
out of branching: the wiki grows only by merges, and a skill change
that fails is a branch nobody merged. Rollback is not an operation
here; not merging is.

**Passes are fresh delegates.** A pass is `Sessions.ask(task,
inherit="fresh")` from the home session: the tree as of the last
merge, and no conversation. A curator's memory is its tree — the
wiki's pages, the library's code and its log — which is the paper's
arrangement too (its maintainer is one call per iteration over the
whole wiki), and it means a curator never needs compaction. Each pass
also leaves a branch holding its own trace, so the curators can be
audited with the same tool they use on everyone else.

**Why two passes, not one.** The two outputs live differently. A page
should be kept whatever happens next; a proposal is a bet that usually
loses — roughly two-thirds to three-quarters of the paper's did. If
one pass produced both, refusing the bet would drop what the pass
learned, and landing only its wiki half would be a take, which mirrors
a directory: with passes in parallel it would delete the pages another
pass added. One branch per lifecycle keeps every landing an ordinary
merge. Run separately, the cheap pass can follow every batch of new
work while the expensive one — every proposal costs trials — waits for
evidence; one wiki can feed several builders, which matters because
skills turned out model-specific and knowledge does not; and the
record stays descriptive, written by a pass with no fix to justify.
The paper never tested the split itself — its fifteen-point ablation
removed the wiki and the maintainer together — so what this design
holds to is two kinds of pass with two homes. Whether they are two
agents or one with two prompts is open.

**No agent's tree has a second author.** This is the rule the rest
leans on. ws-git measures `status` against the agent's own last
commit, so a file the host writes into an agent's tree reads as the
agent's uncommitted work, and blocks the agent's merges until it
commits something it never wrote. Home sessions avoid that by having
no agent. Workers cannot — installing a skill writes into their tree
— so install and upgrade land as commits in the agent's graph, keyed
to the skill's paths, the way a host `ws.merge` or `ws.revert` already
does ([design notes](design.md#ws-git-is-a-fiction-over-the-stores-history)).

How the paper's parts map:

| WikiSkill | here |
|---|---|
| raw layer: immutable traces | every session's stored runs, projected read-only at `.trace/` beside the files each run changed |
| wiki layer, never rolled back | the `wiki` session; it grows only by merging maintainer passes |
| skills layer | `skills/` in `lib`; a release is a publication |
| inference agent, denied the wiki | workers never see `wiki`: it is another session, mounted only by curators |
| wiki maintainer: one call, JSON patch ops | an agent pass with a terminal; `file_edit` is the patch op |
| `log.md` | `ws-git log` on `wiki`: one commit message per merged pass |
| `skill-impact.md`, written by the harness | `wiki/impact/`, written by the gate: scores and counts, never the trials |
| validation traces, kept from the curators | trial runs stay out of every corpus; verdicts arrive as scores |
| skill proposer: ReAct over `read_file` | a builder pass; the terminal is its `read_file`, and it can run things too |
| one atomic proposal | one ws-git commit |
| gating and rollback: an oracle on a labeled validation set | checks that need no labels, paired trials, probation; a rejected proposal is a branch nobody merged, a retired release a downgrade |
| `PURPOSE.md` | kept, in each skill directory |

## Traces as files

### The verb

```
$ ws-git worktree add --trace s/analyst-42 analyst-42
worktree s/analyst-42: analyst-42@a3f9c2e (read-only; trace: 23 runs)
```

One flag on the verb that exists, because the shared commit is the
point: the files and the conversation that wrote them, pinned
together, put up and taken down together. Under the hood that is one
attachment whose filesystem is the tree plus a `.trace` subtree, so
one `detach` takes both. The host's spelling is
`ws.files.attach(ref, at, trace=True)`.

A trace is everything a worktree already is: read-only, pinned at a
commit (seeing newer runs is remove-and-add), outside versioning, and
gone when the session closes. A ref that holds no conversation — a
publication, which never carries one, or a session whose embedder
keeps its conversation elsewhere — refuses the flag and says which it
is.

### The layout

```
s/analyst-42/                    the files at a3f9c2e, as today
s/analyst-42/.trace/
  README.md                      whose conversation, at which commit; read it as data
  index.md                       one line per run
  system/9f3c1a2b.md             each distinct system prompt, once
  runs/
    020-1f3a9c2e/
      run.md                     model, status, timings, system prompt by name,
                                 the input, the final reply, one line per tool call
      changed.md                 what this run changed: commits, paths, A/M/D
      events/
        001-user.md
        002-assistant.md
        003-tool-terminal.md
        …
```

- **`index.md` is where a reader starts**, as the wiki's own index is:
  ordinal, run id, when, status, tool calls, tool errors, files
  changed, and the input's first line — the fields a trial measures,
  too. A reader choosing what to open
  greps it (`grep 'err:' */.trace/index.md`) instead of reading
  everything.
- **One file per event**, so a grep hit names the event and `cat`
  reads one tool result rather than a whole run. It is agex's chapter
  layout (`/chapters/<slug>/events/NNN-<type>.md`) on purpose: one
  event renderer could serve the stack.
- **System prompts are stored once.** agno rebuilds the system prompt
  for every run and stores it with every run; inline, it would be most
  of every file. `run.md` names its prompt by hash.
- **Runs are numbered and named.** The ordinal is the run's position
  in the session's run list at that commit, and after a rewind the next
  run takes an ordinal an abandoned run once held. A citation carries
  its commit, so the ordinal is unambiguous there; the id in the
  directory name makes it unambiguous everywhere.
- **A run caught in flight says so**: a checkpointed run reads
  `running`.

### `changed.md`

The commits between the landing of the run before and the landing of
this one, each with the `tool` its info names and the paths it added,
modified or removed. Most are the agent's tool calls. A few are the
host's — an upload (`put`), a skill install — and are listed under
their own names rather than folded in, so a reader can tell the
agent's work from what happened to its tree.

The reverse question needs no index of its own:

```
$ grep -l helpers/excel.py s/*/.trace/runs/*/changed.md
s/analyst-42/.trace/runs/014-77b0e2d1/changed.md
```

Finding the landings means walking the branch's history once. The
answer belongs to an immutable commit, so it is computed on first read
and kept. The commit that lands a run names it — `{"tool": "turn",
"runs": {"<id>": "COMPLETED"}}`, or `"RUNNING"` for a checkpoint agno
wrote mid-run — so the walk reads commit info instead of a session
record per commit.

### `ws-git show <ref>:<path>`

A citation should be a command. Git spells one file at one state
`rev:path`, and `Ref` already carries `session@commit:/path` without
interpreting the path. `show` now takes another session's
`session@commit` and interprets the path, so a citation is one:

```
$ ws-git show analyst-42@a3f9c2e:helpers/excel.py
$ ws-git show analyst-42@a3f9c2e:.trace/runs/020-1f3a9c2e/events/017-tool-run_python.md
```

A wiki page's evidence then opens with no mount to put up and take
down, which is the builder's most common move.

### Where rendering happens, and who sees what

- **Lazy in process, eager on a VM.** On the local rung the projection
  renders a file when it is read. A `DudExecutor` guest is given the
  root's subtree by materializing it — `_push_tree` tars every file
  under the root, attachments included — so a trace mounted inside the
  root is rendered whole and pushed at every sync. Curators read;
  they belong on `LocalExecutor`.
- **Redaction belongs to rendering.** Nothing is copied, so the moment
  a byte becomes visible is the moment to redact: an embedder hook,
  `redact(text, *, session, run, path) -> str`, applied to every
  rendered file. Tool results are where secrets live; `publish` already
  leaves `app/logs/` out by default for the tracebacks in it.
- **The format is the adapter's; the projection is core's.** Core
  composes the directory, `index.md`, `system/` and `changed.md`, and a
  `TraceSource` turns stored runs into events. The agno source ships
  with the session db, and a store names its source (`Store(...,
  traces=AgnoTraces())`) so that a reader renders whatever that store
  holds.
- **A conversation is data.** `README.md` and every `run.md` open by
  saying whose conversation this is and that its contents are a
  record, not instructions — the posture the `sessions` tool takes with
  a delegate's answer. That is not a defense; [rough
  edges](#rough-edges) says what is.

Two more things the projection should show eventually. **Abandoned
runs** — the runs a checkout stepped off — are in history but not in
the head's run list. A rewind is the clearest failure label a
deployment produces, so `.trace/abandoned/`, each run beside the
restore that left it, is worth its history walk. And **delegations**:
a worker's `sessions` calls name the children it asked, whose traces
mount the same way, so `run.md` can list them.

## Signals

WikiSkill runs on labels. Every training trace is marked passed or
failed: its maintainer samples up to five failing and three passing
traces, and its proposer is handed each training task's prediction
beside the correct answer. A deployment has no answer key. What it has
is the behavior of each session's principal — the person, or the
parent session, the work was for — and a versioned history keeps more
of that than a store of transcripts would:

| signal | where it is |
|---|---|
| a run a checkout stepped off: the principal rewound it | history — the restore commit, and the runs it dropped |
| a commit reverted | `ws-git.revert` in the log |
| a correction: the next message says the last answer was wrong | the run after it |
| a delegate merged, or never merged | the parent's log, and the job's answer status |
| a run that errored, or tool calls that did | the run's status and its tool results |
| the session's own tests, passing or failing | `ws-pytest` runs in its events; its `tests/`, runnable again |
| an app published, or unpublished | the store's publication registry |

Every one of these is weak: delayed, noisy and confounded — a rewind
can mean the principal changed their mind. Together they are also the
only evidence about the real objective, which is whatever the
principal wanted. The table does two jobs below. It is the
maintainer's triage, where the paper had pass and fail; and it is what
[probation](#probation) measures a release by.

## The maintainer's pass

The host decides when a pass runs and what it covers — mechanism here,
schedule with the embedder, as with `sessions.sweep` and
`store.clean`. A pass covers the sessions whose heads moved since the
last one. The host keeps each pass's watermark as a session tag on
`wiki` (`wiki.tags.add("pass-12", info={"corpus": {session: commit,
…}})`), compares heads, forks a pass with a fresh conversation, mounts
the changed sessions under `corpus/` with `trace=True` and `since=`
the watermark, and runs it. The task says what is mounted and what to
do: document what recurs, cite evidence, commit once.

A pass, illustrated; everything under `.trace/` is proposed:

```
$ cat index.md
- [openpyxl-formulas-unevaluated](patterns/openpyxl-formulas-unevaluated.md): load_workbook(data_only=True) reads formulas as None until Excel has saved the file; compute in pandas, or keep the formulas.
- [csv-sniffing-fails-on-semicolons](patterns/csv-sniffing-fails-on-semicolons.md): …

$ ws-git worktree list
worktree corpus/analyst-42: analyst-42@a3f9c2e (read-only; trace: 23 runs, 4 new)
worktree corpus/analyst-51: analyst-51@77d01b4 (read-only; trace: 6 runs, 6 new)

$ grep -h '^+' corpus/*/.trace/index.md
+ 020-1f3a9c2e  2026-09-30 14:02  completed  12 tools  err:3  3 files  "reconcile the Q3 sheet against…"
…
$ cat corpus/analyst-42/.trace/runs/020-1f3a9c2e/events/017-tool-run_python.md
$ cat corpus/analyst-42/.trace/runs/020-1f3a9c2e/changed.md
$ grep -rl merged_cells corpus/*/helpers corpus/*/.trace/runs/*/events
corpus/analyst-51/helpers/sheet_utils.py
corpus/analyst-42/.trace/runs/020-1f3a9c2e/events/017-tool-run_python.md
$ diff corpus/analyst-42/helpers/excel.py corpus/analyst-51/helpers/sheet_utils.py
…
$ ws-git commit -m "pass 12: merged-cells-read-as-none (new; 2 sessions); openpyxl-formulas-unevaluated (+1)"
```

**A page** is the paper's — what happens, why, the fix, in ten to
thirty lines — with frontmatter the rest of the loop reads, and
evidence given as refs with the lines that matter quoted:

```markdown
---
summary: A merged range reads as None everywhere but its top-left cell; map each cell to its range's anchor before reading.
status: open
---
# Merged cells read as None

**What happens.** …
**Why.** openpyxl stores a merged range's value in its anchor cell only.
**Fix.** Build a cell → anchor map from `sheet.merged_cells.ranges` and read through it.

## Evidence
- analyst-42@a3f9c2e:.trace/runs/020-1f3a9c2e/events/017-tool-run_python.md
  > KeyError: None … (the three lines that matter)
- analyst-51@77d01b4:helpers/sheet_utils.py: a working workaround, `anchor_of()`
```

- **Quote, then cite.** A ref is a soft reference, like a
  publication's `published_from`: a worker's session can be deleted
  and its commits swept, and a page must still say what it saw.
- **`status`** is how the builder finds work (`open`) and how a page
  records that a release answered it (`addressed: sheets/v4`).
- **The log is `ws-git log`.** Each merged pass is a commit whose
  message says what changed; the paper's `log.md` is history the
  session already keeps.
- **The lint before the merge is cheap and mechanical**: every index
  line names a page that exists, every page has a `summary`, every
  citation parses as a ref. A pass that fails it is not merged, which
  makes the lint the wiki's own small gate.
- **The watermark wants one convenience.** Comparing heads across a
  store is a `branch_head` per session today; `store.heads()` would make
  it one call.
- **Triage reads the signals.** A pass starts from the runs the
  [signals](#signals) flag — errors, corrections, rewinds — and samples
  a few clean ones too, as the paper samples passing traces, so a page
  records what works as well as what breaks.
- **The corpus is workers' sessions, never trials.** The gate's trial
  runs live on the same store and mount the same way; a pass that read
  them would carry the tasks that judge a skill into the wiki the
  builder reads.

**Run passes one at a time, at first.** Parallel passes over disjoint
slices of the corpus merge cleanly on pages and collide on `index.md`
every time, because the paper's maintainer rewrites the whole index on
every pass. When passes go parallel, make the index derived: each
page's `summary` is its index line, and a script in the wiki's own
tree (`tools/index.py`) regenerates the file — after a pass, and after
a merge that conflicted on it. The wiki can carry its own tooling,
because it is a session.

## The builder's pass

A pass forked fresh from `lib`, with `wiki` mounted read-only at its
latest merge. One proposal, one commit:

```
$ grep -l '^status: open' wiki/patterns/*.md
wiki/patterns/merged-cells-read-as-none.md

$ cat wiki/impact/index.md
4f2a1c9  sheets  rejected  trials: 21 won, 34 lost, 95 tied   write formulas as values   lib.ember-heron
9d10e3b  sheets  current   trials: 48 won, 22 lost, 80 tied   anchor-aware range reads   sheets/v4

$ cat wiki/patterns/merged-cells-read-as-none.md
$ ws-git show analyst-51@77d01b4:helpers/sheet_utils.py
$ cat skills/sheets/SKILL.md skills/sheets/cells.py
  … file_edit skills/sheets/cells.py, file_write skills/sheets/tests/test_merged.py,
    file_edit skills/sheets/SKILL.md …
$ ws-pytest skills/sheets/tests/test_merged.py
2 passed
$ ws-git commit -m "sheets: read merged ranges through their anchor (merged-cells-read-as-none)"
```

- **Past proposals are readable, rejected ones included.**
  `wiki/impact/` keeps each verdict with its diff, and a rejected
  proposal's branch stays on the store until it is swept: `ws-git
  worktree add --trace prev lib.ember-heron` shows the change and the
  reasoning that produced it. The paper's rule — read what was tried
  before trying it again — needs nothing more.
- **The trials that judged it are not.** A verdict carries counts —
  won, lost, tied, how often the skill was opened — and never the
  tasks or the trial runs. A builder that read the tasks judging it
  would start fitting them, and the gate's numbers would stop meaning
  anything. The paper keeps the same wall: validation traces reach its
  curators only as a score and a verdict.
- **Every fix carries its test.** For a code skill, the evidence on a
  page is a concrete failing case, so a proposal ships a test that
  fails without the change and passes with it — a bug report turned
  into a regression test, the way software already works. It is the
  one oracle this loop makes for itself, and prose skills have no
  equivalent.
- **The builder can try before it proposes.** Granted a `sessions`
  runner, it can ask a delegate to attempt one of the wiki's evidence
  cases with the candidate skill in its tree and then read the
  delegate's trace — an experiment the paper's proposer has no way to
  run. Evidence is training data by definition and never a trial task,
  so trying on it costs the gate nothing.
- **"One proposal" is the gate's policy, not the agent's discipline.**
  The gate can refuse a pass that changed more than one skill
  directory or made more than one commit; a pass that wrote past its
  last commit has the rest committed for it when it answers, under
  the delegation mechanism's message, which the gate can count.
- **Parallel builders cost nothing extra.** Several passes at once,
  each a proposal judged against the same baseline: EvoSkill's
  frontier, with forks instead of a population table. Two accepted
  proposals can interact, so the gate judges the second one merged
  onto the first.

## Skills that ship code

A skill directory may hold code beside its instructions:

```
skills/sheets/
  SKILL.md         when to use it, when not, the API in a dozen lines, examples
  PURPOSE.md       the wiki patterns it answers; the sessions and files it came from, as refs
  cells.py         from skills.sheets.cells import read_range
  formulas.py
  tests/
    test_cells.py  run by the gate; skipped by a session's default ws-pytest run
  scripts/
    inspect.py     the spec's runnable scripts: python skills/sheets/scripts/inspect.py book.xlsx
```

`SKILL.md` stays the model's entry point: the catalog lists it, and an
agent that never imports anything still reads it. Code is an
implementation detail of some skills — prose for judgment (when, when
not, why), code for mechanics. The paper's negative-transfer result
argues the same way: a small model's skills hurt a strong one because
they were prose rules — single-line Python commands, string-conversion
rules — that kept the strong model from writing whole scripts. A
function is called, not obeyed.

**Where library code comes from.** `helpers/` is one session's code
plane ([design notes](design.md#script-model-not-a-persistent-repl));
`skills/<name>/` is the shared one. A library usually starts as
somebody's helper. The maintainer's grep finds the same workaround in
three sessions' `helpers/`, the traces say why each was written, and
the builder generalizes the best of them, writes tests seeded from the
failures the wiki documents, and records in `PURPOSE.md` where it came
from.

**Tests are the first gate.** They are cheap and deterministic, and
they run before any trial costs a model call. The ones that matter
most come from the wiki's evidence — the failing case a page cites,
turned into a test the fix must pass — which is how code skills get an
oracle that prose skills cannot. `ws-pytest` already runs a vendored
library's tests when they are named, and skips them otherwise.

**Skill code runs with the session's grants.** It is agent-written code
executing in the installing session's sandbox, so it reaches nothing
that session's own code could not. That rules out escalation, not
harm: a library function handed a live `db` can do whatever the
session's code could do with it. Hence review at the gate for code,
below.

Three things are in the way today:

1. **Package imports are refused.** `from skills.sheets.cells import
   read_range` works; `from skills.sheets import read_range` does not,
   because the sandbox resolves `.py` modules and not a package's
   `__init__.py` as an import's target. Agents reach for package style
   by habit, so sandtrap's VFS importer should learn `__init__.py`.
2. **Hyphens.** The [Agent Skills
   spec](https://agentskills.io/specification) allows lowercase letters,
   digits and hyphens in a name, and requires the directory to match it;
   Python needs identifiers. A single word satisfies both, and
   `excel-tools` satisfies only one. Packaging settled the same clash by
   treating `-` and `_` as equal (PEP 503); the VFS importer can do
   likewise for path segments, exact match first, so that
   `skills.excel_tools.cells` finds `skills/excel-tools/cells.py`.
3. **Requirements.** A skill whose code imports `openpyxl` is broken
   in a session that was not granted it, and the failure arrives
   mid-task. The spec's `compatibility` field is free text for people;
   its place for extension keys is `metadata`, a map of strings — a
   `metadata:` block holding `requires: openpyxl`. nontainer's
   frontmatter parser skips nested YAML on purpose, so it needs to read
   one level of nesting (still with no YAML dependency), and `install`
   then checks `requires` against the session's `PythonConfig` and
   refuses by name.

## The gate

### What the paper's gate was

Code in the outer loop, not an agent. The agent being evolved runs
every validation task with the candidate skills in its system prompt;
the benchmark's own scoring function grades its answers against the
known ones; and the candidate stays only if it beats the best score so
far, strictly. The bar starts at the score with no skills and only
rises, the loop stops early if it reaches 100%, and the harness appends
each verdict — the diff, the score, accepted or rejected — to
`skill-impact.md`. Validation traces never reach the curators; only
the score and the verdict do.

Three things about it matter here:

- **It is an oracle**, and the paper's whole loop runs on one: the
  labels behind the gate also pick the maintainer's samples and tell
  the proposer what went wrong. In the paper's setting that is
  legitimate — the oracle is the objective, as a validation loss is in
  training. An open-ended system has no answer key. And with one in
  hand, how much of the gain needed the experience loop at all is
  untested: the paper has no baseline that asks an agent to write
  skills for the known task family without any traces.
- **Something still has to decide.** Skills often hurt, and the agents
  proposing them cannot tell which: roughly two-thirds to
  three-quarters of the paper's proposals failed validation, one
  model's skills took another from 50.5 to 18.1 on SpreadSheet, and a
  baseline made Gemma worse on LiveMath. A loop with no evidence
  drifts.
- **It is noisy.** Validation splits of 10 to 40 tasks, one evaluation
  per proposal, and a best-so-far bar that a single lucky acceptance
  raises for good.

So the oracle does not go away; it moves — from a labeled set consulted
before acceptance to the principal's behavior after release. The gate
here is three stages, and none of them needs an answer key:

1. **[Checks that need no labels](#checks-that-need-no-labels)**,
   before anything runs.
2. **[Paired trials](#paired-trials)**, before release: one real state,
   with the skill and without it.
3. **[Probation](#probation)**, after release: outcomes by version, in
   real use.

It is code with a judge inside, not an agent. Sampling, pairing,
blinding, metrics and statistics are deterministic; the one model call
is a constrained verdict per pair. A gate agent that read traces would
be exactly what a poisoned session aims at, and the gate is never the
builder. Below, `gate` is a `Sessions` helper over a session of the
gate's own, so its trial runs are named `gate.<pet>`, and the proposal
is what a builder pass's answer names: `lib.quiet-wren@7c4e1d2`.

### Checks that need no labels

- **Shape.** One skill directory changed; `requires` satisfiable by
  the configurations the release will ship to.
- **Tests.** Fork the proposal (`store.fork("lib.quiet-wren",
  "gate.t-7c4e1d2", at="7c4e1d2")`), register `ws-pytest`, and run the
  skill's tests, the regression test from its evidence first.
- **Review.** A human or a reviewing model reads every code diff (see
  [rough edges](#rough-edges)).

They are cheap, and they stop most broken proposals before a single
trial runs.

### Paired trials

Two arms forked from the same commit, differing only in the skill:

```python
for skills in (base, proposal):        # base: the lib commit the pass forked from
    gate.ask(task.prompt, fork_from=task.state, inherit="fresh",
             take={"skills/": skills})      # take= is proposed
```

`take=` composes the fork point: the paths are taken before the fork's
`ws-git.fork` mark, so they are each arm's base rather than its first
change. Today the runner can take them itself just after the fork, at
the cost that the arm's `answer.changed` and its first `changed.md`
list the skills as its own work.

- **The control runs now.** Never the historical original, which came
  from an older model or prompt, with different sampling luck. The
  control takes `skills/` from the commit the builder's pass forked
  from, so the arms differ by the proposal and nothing else: for an
  upgrade the control has the version before it, and for a new skill
  the same set without it — absent from the catalog too, since the
  catalog is read from the tree.
- **Where trials start.** The trial should match the question:
  - **A fresh start from a real state** is the default, for "does
    this skill help a session that has it?" `task.state` is
    `analyst-42@<the commit before turn 7>`, forked with
    `inherit="fresh"`: both arms get that session's files at that
    moment and no conversation, and start the way any new session
    does, with the skill in their catalog or without it. The prompt is
    the principal's message from that turn plus a brief written from
    earlier turns only — no hindsight — identical across arms, and
    dropped when the message stands alone. The brief costs realism,
    not fairness.
  - **First turns** of real sessions, when the pattern shows early.
    Nothing about them is artificial.
  - **Mid-session**, with `inherit="full"`, answers one question only:
    what happens if the skill is installed into a session already
    running? A replayed history is on-policy for the control and
    off-policy for the treatment — turns of an agent working without
    the skill, which a model tends to go on doing — so as a test of
    the skill it is biased against it: mildly for a skill looked up at
    the moment of need, badly for one that should shape how a session
    starts. As a test of a mid-session install it is exact, provided
    the install is announced as it would be in deployment, by an inbox
    note. The paper's setup would have hidden this, since every
    skill's whole text is in every system prompt there; nontainer's
    catalog leaves the agent to decide to look.
- **Which tasks, chosen how.** By a rule fixed before the skill is
  seen: runs whose [signals](#signals) match the pattern the skill
  answers, plus runs it should not touch — the paper's worst results
  were skills misfiring where they did not belong, and only unrelated
  tasks catch that. Never the wiki's cited evidence: that is the
  builder's training data, and the skill fixes it by construction.
- **The judge sees outcomes, blind.** The trajectory gives the arm away
  — the treatment `cat`s the skill — so the judge sees what each arm
  produced: the reply, the files, the app, a diff of the two arms'
  trees. It states a preference, ties allowed, with the order
  randomized, and ideally comes from a different model family than
  the arms, since judges favour their own. Two things make it better:
  what the principal said next in the original session, as a rubric
  ("no, per-quarter totals" names what the original got wrong); and
  calibration on history — turns that were kept and turns that were
  rewound are free labeled pairs, and a judge has to prefer the kept
  ones before it is trusted.
- **Outcomes first, costs second.** Outcomes are tests passing, tool
  errors, tracebacks, `test_app` results. Costs — turns, tokens, time —
  count only when both arms succeeded, since fewer turns because the
  agent gave up is worse, and they include the skill's own tokens. All
  of it is in the run records: agno stores each run's tokens and
  duration, and the tool calls and their errors are its messages. A
  pair whose treatment never opened the skill says nothing about the
  skill, and is counted that way.
- **More pairs than feels reasonable.** Only pairs whose arms disagree
  carry information. If a skill wins 20% of pairs and loses 10%, a
  paired test needs on the order of a hundred pairs to see that
  difference at all and over two hundred to see it reliably; the
  paper's 10 to 40 tasks could not. So trials are staged: a small
  pilot, stopped early when it is clearly bad or flat, and widened only
  for what looks promising.
- **A trial is a fork, and a fork inherits the world.** It shares live
  host objects, sees the same directories behind every mount (writable
  ones included), and keeps its network grants; a trial of a turn that
  wrote to a database writes to it again. nontainer cannot tell which
  host-object calls have effects, so trials need a configuration the
  embedder supplies: host objects swapped for fakes or read-only
  facades, mounts read-only, network off. The runner opens each arm,
  so the seam exists; making it the default for trials is the open
  part.

### More than one turn

A trial is one user turn — a whole agentic episode, run until the
agent replies — and every task in the paper is of that kind. One turn
is the default, because most of what this loop produces is procedural
and shows within a turn. Past it, the original session's follow-ups
cannot be replayed: they answered the original output, and "the
totals are wrong" means nothing to an arm that got them right. They
stay useful as the judge's rubric and as material for a simulated
principal. There are two ways further:

- **When the principal is an agent, replay it.** For delegated work the
  principal is the parent session, versioned like everything else:
  fork the parent at the commit before it received the delegate's
  answer, deliver one arm's answer to each fork, and let each react.
  That is the real principal, re-run against each output.
- **When the principal is a person, simulate one**: a model with the
  same goal card in both arms, a fresh memory for each, the same
  settings, and no way to tell the arms apart. Its goal comes from what
  the principal eventually accepted and its voice from their own
  messages; it never sees the solution, because a simulator that knows
  the answer leaks it. Simulators are more patient and explicit than
  people, so they understate what a skill does for an agent that is
  being misunderstood; τ-bench is the usual precedent. Measure the
  episode: finished or not, corrections needed, principal turns to
  done, and whether the simulated principal gave up.

Both cost several turns per arm and compound the noise. They are an
escalation — for skills about interaction itself (clarifying,
presenting, recovering from a correction), for payoffs that arrive
turns later, and for one-turn results that are ambiguous — and not
part of a first version.

### Probation

Trials are a sample; real use decides. A proposal that passes its
trials is merged into `lib` and published with `current=False`:
recorded, but not yet what new sessions install. The embedder's
install policy gives it to a share of new sessions, assigned at random
so that the sessions on each version differ only by chance, and
`.installed` records which version every session ran. The
[signals](#signals) of the two cohorts — rewinds, corrections, errors,
failing tests per session, over the same weeks — are then attributable
exactly. One of three things follows: `set_current` promotes the
release; it stays on probation for more evidence; or it is retired —
no longer installed, and downgraded in the sessions that have it, with
an announcement and a three-way apply, so their local patches survive.
Retiring a skill matters as much as accepting one: a library that only
grows keeps whatever luck let through.

The paper's strict rule also threw away neutral proposals — changes
that add tests or generality without moving the score. With probation
they have somewhere to go: a proposal whose trials find no harm can
enter probation, where real use settles it. Whether that is wise, or
lets drift in, is an [open question](#open-questions).

### The record

`wiki/impact/7c4e1d2.md` holds the stage a proposal reached; its trial
counts — won, lost, tied, and how often the skill was opened; its
probation numbers when they arrive; and the unified diff, which
outlives the branch. Never the trial tasks or their runs: curators see
a score, the way the paper's see a validation number, and nothing they
could fit to. The trial runs stay on the store, out of every corpus,
until they are swept. `wiki/impact/index.md` gets one line per
proposal.

### Benchmark mode

The paper's setting stays useful as a test harness for the loop's
machinery: with an answer key, every stage can be checked against the
truth while it is being built. It keeps the paper's two rollout sets
in every iteration — training rollouts with the current skills, which
are workers and feed the curators, and validation rollouts, which are
trials and give the gate one number — and the second never becomes
the first. It is a way to build and debug the loop, not the thing that
decides what ships.

## Installing, upgrading, patching

```python
from nontainer import skills

skills.install(ws, "sheets")      # the current release, as a take from sheets/v4
                                  # (or "sheets/v5": a named version, as probation assigns one)
skills.installed(ws)              # {"sheets": Ref("@store/tag/sheets/v4@…")}
skills.upgrade(ws, "sheets")      # to the current release, three-way
skills.patched(ws)                # {"sheets": WorkspaceDiff(modified=[…/cells.py])}
skills.uninstall(ws, "sheets")    # a retired skill with no earlier release to go back to
```

**Vendored, at a version.** A release lands in the session's tree, so
it versions with the session: a rewind restores the library the agent
had, a fork carries it, and every trace reads against exactly the code
its agent ran. A live mount of the release would be smaller, but
it would sit outside the session's history, change under the agent,
and leave every trace ambiguous about which code it ran against.

**What is installed is a file in the tree.** `skills/<name>/.installed`
holds the ref it came from. Reading provenance off the log instead
goes wrong after a rewind: the log's newest take is the upgrade the
rewind just undid.

**An upgrade is three-way.** The base is what is installed and theirs
is the target release, applied to the skill's paths: upstream changes
land, a local fix survives, and an overlap comes back with markers as
a merge's would. Downgrading — retiring a release — is the same verb
pointed backwards, and `uninstall` takes away a skill that has nothing
earlier to go back to; `set_current` only decides what new installs
get.

**Both land in the agent's graph.** An install or an upgrade is a
commit keyed to the skill's paths and recorded as the agent's, as
`ws-git.install` or `ws-git.upgrade`, like a host merge. An agent that
uses ws-git sees it in its log rather than as modified files it never
wrote, and its merges are not blocked by it.

**A change mid-session is announced.** An install, an upgrade or a
downgrade into a session already running comes with an inbox note
naming the skill and what changed. The trials show why
([mid-session](#paired-trials)): an agent with turns of history behind
it tends to keep working the way it was, and a skill nobody mentions
may never be opened.

**A local patch is a bug report.** An agent that fixes the library in
place leaves a diff against the release it installed. `skills.patched`
reads it, and the trace says why the agent made it. The maintainer can
surface it; the builder can take it with `ws-git checkout <session> --
skills/sheets/cells.py` and propose it.

## Closing the loop

Sessions that install releases are the next pass's corpus, and the
library is part of what they show:

- **Adoption.** Whether an agent read `SKILL.md` or imported the code
  is in its events.
- **Bugs, attributed.** A traceback naming
  `/workspace/skills/sheets/cells.py` comes from a tree whose
  `.installed` says which release it was.
- **Patches.** Local fixes, diffable against the release they patch.
- **Probation's evidence.** Signals by release, across the sessions
  that installed it, in cohorts assigned at random: the paper's skill
  impact, measured on real work rather than on a validation split.

## Reach: one store, and more than one

A live mount works within one store, and within one store everything
is already readable by every agent with ws-git. That is the right
boundary for one user's store, a team's, or a gate's benchmark store.

It is the wrong one across users. agno's session listing is per store,
which makes a store per user the natural layout, and a maintainer
learning from many users' sessions should not read their raw traces
anyway. Across that line, derive. A **specimen** is a new commit in the
curation store holding chosen paths from one session's tree, plus its
trace rendered to files and passed through the redactor, with
provenance as a soft reference — `publish`'s reasoning, applied to a
session instead of an app. It is redacted once, at derivation;
independent of its source for garbage collection; readable after the
source is gone; and a copy of blobs, which is cheap at these sizes. A
specimen mounts like any session, and its `.trace/` is ordinary files.

That leaves a policy only the embedder can set. Knowledge distilled
from a session outlives the session: a user who deletes their work does
not un-teach the wiki or the library built from it. Citations make that
findable (grep the wiki for the session's name); they do not make it go
away.

## Rough edges

- **The loop is a supply chain.** A poisoned session — a user's
  message, a file, a page a tool fetched — can steer a maintainer pass,
  whose page steers a builder, whose code ships to every session that
  installs it. Framing a trace as data does little. What does help:
  scoping the corpus, redaction, the gate's trials and probation, and
  review of every code diff before it is published. Skill code runs
  with the session's own grants, so the damage is bounded by what
  sessions may already do.
- **A host write into an agent's tree reads as the agent's work.**
  Every verb that writes there must land in the agent's graph, or it
  breaks `status` and blocks merges. Today's `skills.install` commits
  as the framework (`{"tool": "skill"}`) and has this problem as soon as
  the agent has made one commit.
- **Traces on a VM rung are eager.** Curators belong on
  `LocalExecutor`.
- **`.trace` can collide** with a real `.trace` in a session's tree;
  the projection should shadow it, and `worktree add` should say so.
- **Host writes inside a run's range** — uploads, installs — show up in
  its `changed.md`. They are listed under their own tool names, not
  hidden.
- **Evidence is expensive.** A ten-point effect takes a hundred to two
  hundred trial pairs, each two agent runs and a judge call. Staging
  and early stopping help; a store without volume cannot run
  probation at all, though it still has the checks.
- **The judge is a model.** It has the biases judges have — position,
  length, its own model family — and builders can learn what it likes.
  Blinding, randomized order, pairwise verdicts and calibration
  against kept-versus-rewound history are the defenses; none is
  complete.
- **Probation is observational.** Assignment has to be random, or the
  sessions that got a release differ from those that did not in ways
  that look like its effect; cohorts have to run over the same weeks,
  because tasks drift; and real principals absorb a bad release until
  it is caught. A small share first, and a quick `set_current` back.
- **Skills are model-specific.** The paper's transfer results cut both
  ways. A release records the models its trials ran; installing it for
  another model is the embedder's call.
- **Citations dangle.** Sessions are deleted and trial runs are swept;
  quote what matters.
- **The conversation is not always there.** An MCP session's
  conversation lives in its client. `--trace` refuses there; the corpus
  is the sessions whose conversations the store holds.
- **The job table is process-local.** A gate that restarts mid-trial
  recovers from branches, not jobs, as delegation always has.

## API sketch

Agent-facing:

```
ws-git worktree add [--trace [--since <ref>]] <dir> <ref>   files, and the conversation at .trace/
ws-git show <ref>:<path>                                    one file at one state, .trace/ paths included
```

Host-facing, core:

```python
ws.files.attach(ref, at, *, trace=False, since=None)
Store(..., traces: TraceSource | None = None)       # how this store's conversations render
ws.apply(base, theirs, *, paths=None) -> MergeOutcome
    # the named-base three-way, public, landing in the agent's graph as revert does
Sessions.ask(..., take={path: ref})                 # a fork point with paths taken in
store.heads(sessions=None) -> dict[str, str]        # a watermark in one call
# where a run lands, its commit info says which: {"tool": "turn", "runs": {"<id>": "COMPLETED"}}

class TraceSource(Protocol):                        # nontainer.traces
    def runs(self, read) -> list[RunInfo]                    # at one commit
    def events(self, read, run) -> list[tuple[str, bytes]]   # (name, rendered)
# RunInfo: id, ordinal, created, status, tool calls, tool errors, tokens, duration —
#          what index.md prints, and what a trial measures
# redact(text, *, session, run, path) -> str: an embedder hook on every rendered file
```

Host-facing, skills:

```python
skills.install(ws, "sheets" | "sheets/v4" | ref)   # a take in the agent's graph; writes .installed;
                                                   # checks metadata.requires
skills.upgrade(ws, name, to=None) -> MergeOutcome  # ws.apply(installed, target, paths=the skill)
skills.installed(ws) -> dict[str, Ref]
skills.patched(ws) -> dict[str, WorkspaceDiff]
skills.uninstall(ws, name)                         # in the agent's graph, like install
skills.frontmatter(content)                        # reads one level of nesting, for metadata
# a change to a running session is also announced: Inbox.put(..., kind="mechanism")
```

The trials and probation are not API. They are the embedder's code —
the example's, first — over `Sessions.ask`, `take=`, `RunInfo` and
`publish(current=False)`, and nothing in core can tell a trial from
any other fork.

Elsewhere:

- the agno adapter's `AgnoTraces`;
- sandtrap importing a package's `__init__.py`, and matching `-` to
  `_` in path segments;
- the MCP adapter cataloging skills in its server instructions, as the
  agno toolkit already does;
- the `sessions` tool's `result` naming `ws-git worktree add --trace`
  beside `diff` and `merge`.

Later: `store.specimen(...)`, `.trace/abandoned/`, and a self-trace
(see open questions).

## Build order

1. **`ws-git show <ref>:<path>` and the run stamp.** Both are small and
   useful before anything else here exists. Both are built.
2. **The trace projection**: local rung, agno source, redaction hook.
   It pays for itself in delegation alone — a parent reading how a
   delegate reached its answer, not only the answer.
3. **Skills as versioned libraries**: install from a release,
   `.installed`, `ws.apply`, upgrade, agent-graph commits,
   `metadata.requires`; and the two sandtrap import fixes.
4. **`Sessions.ask(take=)`.**
5. **The loop as an example** — workers, a maintainer pass, a builder
   pass, and a gate with label-free checks and paired trials under a
   scripted judge, with benchmark mode as its harness — scripted, with
   no LLM, the way `tour.py` runs, so every seam is exercised.
6. **Real models**: paired trials on fresh starts from real states,
   once trial-safe forks have an answer; then probation, in a
   deployment with the volume for it.
7. **Later**: replaying agent principals and simulating human ones,
   abandoned runs, specimens, and parallel passes with a derived index.

## Open questions

- **One agent or two?** The maintainer's and the builder's passes are
  two shapes of work with two homes. Whether they are two agents, or
  one with two prompts, is a tuning question this design leaves open.
- **A trace of your own session?** `worktree add` refuses this
  session's own name, because its files are already yours. Its older
  runs are not: past agno's history window, they are invisible to the
  agent that had them. A self-mount of `.trace/` alone would be agex's
  `/chapters` without the compaction. Is it worth it?
- **How many homes?** One `wiki` and one `lib` per store, or one per
  domain? Per domain keeps passes small and indexes short; one per
  store keeps cross-domain patterns visible.
- **Should neutral proposals reach probation?** A change whose trials
  find no harm and no gain could go to probation and let real use
  decide — or that is how drift gets in.
- **Who reviews code?** A human before every publish is the safe answer
  and the slow one. A reviewing model is faster, and it is exactly what
  a poisoned page would aim at.
- **Who judges?** A judge from another model family avoids its own
  family's bias and means a second provider; calibrating it on kept
  and rewound turns needs enough history to calibrate on.
- **Do probation's principals know?** Real people use a release that
  is still on trial. Telling them, letting them opt out, or neither is
  the embedder's policy, but a deployment that relies on probation
  should decide it on purpose.
- **Is "skill" still the word?** Here a skill may be a library. The
  case for keeping it: `SKILL.md` is what the model reads first, code
  or no code, and the catalog, the spec and every other harness already
  know the word.
