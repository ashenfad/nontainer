# Compaction

> **Status, 2026-10-03: a design; nothing here is built.** How a long
> conversation stays inside the model's context window. Past a token
> budget, the older turns are replaced, in what the model is sent, by
> one summary. The person still sees the whole transcript, and the
> stored conversation is never rewritten. The mechanism is
> harness-neutral and lives in nontainer. A thin adapter wires it into a
> harness; agno's comes first. The embedder sets the policy.
> [Where this stands](#where-this-stands) is the ledger.

## Why now

The studio sent its agent only the last three runs: agno's default when
`num_history_runs` is unset. A delegate's answer wakes its parent as a
run of its own, so after one round of delegation the agent no longer
had the turns in which it had asked. Asked how delegation had gone, it
said it had never delegated. The moving window also changed the start
of the prompt on every run, so from the fifth run on no run hit the
prompt cache.

The fix (nontainer-studio #78) sends every earlier run, so history is
unbounded. agno's tool-result compression summarises old tool results
past a watermark, but it never touches prose or tool-call arguments (a
large `file_write` is all argument), and it has a cost of its own (see
[Tool-result compression](#tool-result-compression)). Compaction is the
bound, and it doesn't use tool-result compression.

## The short version

- **Trigger:** the request about to be sent is over a token budget.
- **Fold:** every earlier run is summarised into one summary. Only the
  run in progress stays as it is: it holds the person's latest message,
  and its messages are what the harness stores. The next fold
  summarises the previous summary plus the runs since, so there is only
  ever one summary in context. This is what Claude Code's compaction
  does.
- **Record:** each fold writes a record (which runs it covers, the
  summary, and the sizes before and after) into the workspace. Records
  are only ever added. The stored runs are never touched.
- **Model's view:** system prompt, the summary, then any runs after the
  fold, word for word, and the run in progress.
- **Person's view:** the full transcript, with a marker where a fold
  happened that opens to the summary. What the agent remembers is then
  something the person can read.
- **Cache:** one miss per fold. Between folds the start of the prompt
  is stable: the context shrinks in waves rather than through a sliding
  window.

## What agno offers, and why not that

agno 3 (checked on 3.0.11, with nothing new in 3.1's release notes) has
four mechanisms. None is a size-triggered summary of past turns:

| Mechanism | What it does | Problem |
|---|---|---|
| `num_history_runs` / `num_history_messages` | Sends only the last N runs or messages | Old turns are dropped with no summary, and the window moves every run |
| `CompressionManager` | Summarises old tool results past a token limit | Tool results only |
| Offload (`agno.offload`) | Moves oversized tool results to files | Tool results only |
| Session summaries | Re-summarises the session after every run, into the system prompt | Adds to the context rather than replacing anything, costs a model call per run, and changes the system prompt every run |

agno [PR #9873](https://github.com/agno-agi/agno/pull/9873) ("auto
compaction with a searchable archive") is close to this design: a
derived view over an append-only transcript, a run-based tail and a
size trigger. It is open and unreviewed as of this writing. If it lands,
the agno adapter could hand the agno side to it. The reasons to have
our own anyway:

- **It isn't agno-only.** Other harnesses get the same behaviour.
- **Records version with the workspace,** so rewind and fork already do
  the right thing (below). #9873 keeps them in an agno db table beside
  the conversation.
- **Room for chaptering** later, with no new mechanism.

## Where the records live

The records go in a plane of their own, `__compaction__/`, named in
`planes.py` beside `__agno__/` and `__cache__/`.

- **Not inside `__agno__/`.** That plane is the agno adapter's, and
  these records are not agno's.
- **One key per fold** (`__compaction__/<n>`), never rewritten. The
  latest is the one in force. Earlier ones stay as history, and also
  let a reader say what the agent remembered at any commit.
- **Merge policy is ours,** like the conversation. A delegate's folds
  describe a delegate's conversation and never merge into its caller.
- **Rewind:** a checkout to an earlier commit takes the records with
  the runs. A fold made after that commit is gone with the turns it
  folded.
- **Fork with `inherit="full"`** copies runs and records together, so
  the child starts with the parent's summary and the parent's tail.
- **Fork with `inherit="fresh"`** drops the conversation. A record whose
  runs no longer exist is ignored (below), so `fresh` drops the records
  too, in effect, without a special case.

A record names runs by id. Run ids are opaque strings to the core:
agno's are UUIDs, and another harness's can be anything ordered and
stable.

## The record

```python
@dataclass(frozen=True)
class Fold:
    first: str | None    # first run folded; None = from the start
    through: str         # last run folded
    summary: str
    runs: int            # how many runs it covers
    tokens_before: int   # the request size that triggered it
    tokens_after: int    # the same request, folded
    model: str           # what wrote the summary
    at: float
```

Basic compaction always folds from the start, so `first` is `None` and
each new fold covers everything its predecessor did plus more. `first`
is there for chaptering: a fold over a range in the middle, with a name
and a projection, is the same record with `first` set.

**Failing open.** If `through` names a run the conversation does not
hold (an edit unsaid it, a `fresh` fork dropped it), the record is
ignored and the full history is sent. A cut in the wrong place would
hand the model a summary of turns that, as far as the person can see,
never happened. An over-long request is visible and corrects itself on
the next fold.

## The neutral core: `nontainer.compaction`

The core knows run ids, token counts and text, and nothing about any
harness:

- **Records:** read, write and list them in the workspace.
- **Policy:** given the ordered run ids and the request's token count,
  is it time? A fold covers every earlier run, so a cut always falls at
  a run boundary and a tool call is never separated from its result.
  There are no knobs beyond the budget: no tail of recent runs kept
  verbatim, and no minimum, since right after a fold the context is
  small and the next fold waits for the budget to be crossed again. A
  kept tail can come back as an option if summaries lose the exchange
  just before the fold.
- **View:** given the ordered run ids and the record in force, return
  the summary (or none) and the run ids to send verbatim.
- **Summary prompt:** what to keep (decisions, file paths, names, open
  threads, what was delegated and what came back), what to drop
  (routine tool output), and how to fold a prior summary in rather than
  stacking summaries.

The model call itself is not the core's: nontainer does not own models.
The adapter passes a `summarize` callable, and that is where the
cache-friendly choice lives (below).

## The agno adapter

agno calls its `compression_manager` before **every** model call, with
the full message list for that request: system prompt, history and the
current run (`agno/models/base.py`, `should_compress` then `compress`).
The adapter is a `CompressionManager` subclass. It replaces `compress`
entirely and never calls agno's tool-result compression, so it only
applies and makes folds. agno calls the manager only when
`compress_tool_results=True`, so that flag stays on as the gate despite
its name.

- **Applying the record in force** replaces the folded runs' messages
  with the summary, edited in place in `messages`. History messages are
  per-run copies flagged `from_history`, and agno strips everything
  with that flag before a run is stored. The summary messages carry the
  flag too, so the edit changes what the model is sent and never what
  is stored. Each message keeps the `id` it has in its stored run, and
  that is how the adapter maps a fold's run ids to messages.
- **Deciding to fold** happens when the request is still over budget
  after that. The adapter asks the core for the cut, calls `summarize`,
  writes the record and applies it, all before the model call goes out.
  The turn waits for the summary, as it does in Claude Code.
- **Measuring the request** uses what the provider reported rather than
  a tokenizer. `compress` is handed the run's metrics, which carry the
  input tokens of the run's previous request; a run's first request
  uses the last stored run's. Anything appended since is estimated at
  four characters a token. The count is exact where most of the request
  is, and nothing needs `tiktoken` or `tokenizers`.
- **The summary's shape** is a user/assistant pair (a "summary of the
  conversation so far" message and a short acknowledgement), so roles
  still alternate for every provider.
- **`on_fold(record)`** is the embedder's hook for showing it: the
  studio emits a transcript marker.

**The cache-friendly summary.** The cheapest summary is written by the
agent's own model, sent the same messages the agent was about to be
sent plus one instruction to summarise. Almost all of that request is
already in the provider's cache. A separate cheaper model sees the
history cold, which is cheaper per token and costs more overall for a
long history. The adapter's default `summarize` does the former; the
embedder can pass the latter.

A cache hit needs the request to start the same way, and a provider
puts the tool definitions ahead of the messages (Anthropic does). So
the summary request carries the agent's tool list unchanged, and
forbids calling a tool (`tool_choice="none"`) rather than leaving the
tools out. Leaving them out would miss the cache on the whole history.

**A fold that would not fit.** The summary request is at least as long
as the history it summarises. When that is past the model's window,
the plain request fails, and so does every later turn's: the session
would be stuck. So a fold too large for one request is summarised from
a reduced copy of the history: tool results become placeholders
(`[tool result: 48,213 chars]`) and long tool-call arguments are cut
off. If that is still too large, it is summarised in chunks, in order
and at message boundaries, each chunk folded into the summary so far.
This path misses the cache and only runs in this edge case. It is what
lets a session recover after a single run outgrew the window.

## Other harnesses

The adapter is the only harness-specific piece, and the harnesses worth
supporting each have a seam in the same place:

- **pydantic-ai:** `history_processors` hand you the message list before
  each request.
- **OpenAI Agents SDK:** a `Session`'s `get_items` decides what history
  goes in.
- **LangGraph:** `pre_model_hook` sees the messages before the model
  node.

Each would be an adapter of the same size as agno's: map its messages
to run ids, splice in the summary, and pass a `summarize`.

## The embedder's side (the studio)

- **Budget:** a per-model watermark, 60% of the model's context window.
  The studio computed the same figure for tool-result compression
  before #79 removed it. The core has no default of its own.
- **Marker:** a transcript event ("Context compacted: 12 turns
  summarised") that expands to the summary. It is a projection of the
  record, so a reload shows it again.
- **Delegates:** a delegate's turn budget is small enough that it
  should rarely fold. It runs the same mechanism if it does.

## Tool-result compression

Compaction doesn't use it, and an embedder using compaction shouldn't
need it. If one enables agno's anyway, it has a cost to know about.
agno rebuilds history from the stored runs every run, as copies that it
drops before storing the run, so it keeps a compression only when it is
made in the result's own run. A result that first crosses the watermark
in a later run is compressed again on every turn after: a model call
each, and a reworded summary that changes the prompt. With every
earlier run sent, that grows with the conversation. The fix (writing
the compression back onto the stored message, which nontainer-studio
#78 did) belongs to the embedder, not to compaction. The studio has
since dropped tool-result compression instead (#79).

## Chaptering, later

Chaptering, as agex does it, is the richer strategy on the same pieces.
The agent itself picks ranges of finished work, names them and writes
each summary, and the originals stay browsable as files under
`/chapters/<slug>/`.

- **Records:** chapters are folds with `first` set, plus a name.
- **Folding:** the agent's own model, with the agex chapter prompt.
- **Originals as files:** that is the trace projection of
  [curation](curation.md)'s step 2. Built once, it serves both.

None of this needs to be decided to build basic compaction. The one
thing basic compaction must not do is close the door, and a record that
can hold a range is enough.

## Rough edges

- **The seam is behavioural, not a contract.** It relies on agno passing
  `messages` by reference, honouring edits made to it in place, and
  stripping `from_history` messages before storing a run. Tests that
  drive agno's real run loop pin all three, and they belong in the
  `agno-versions` CI matrix.
- **`compress_tool_results` must stay `True`,** or agno never calls the
  manager. The adapter doesn't compress tool results despite the name.
- **One run is not bounded.** Folds cut at run boundaries and never
  touch the run in progress, whose messages are what agno stores. A
  single very long run (a delegate's 60 tool calls, a huge file read)
  can still outgrow the window, and that turn ends in the provider's
  error. The session recovers on the next turn, through the reduced or
  chunked fold above. Preventing the overflow itself means compacting
  within the run, as Claude Code does mid-turn. Here that would mean
  sending the model a different list from the one agno stores, which is
  seam research for later, if this happens in practice.
- **A run folded mid-turn returns once.** A fold made during a run
  cannot cover that run, so on the next turn it arrives in full as an
  earlier run. If it was what crossed the budget, the next turn's first
  request folds again, summarising the summary plus that run: one more
  summary call, and then it settles.
- **The summary is lossy.** That is the trade. The full conversation
  stays in the branch, the person sees all of it, and chaptering's
  browsable originals are the later answer for the agent.

## API sketch

```python
from nontainer.compaction import Policy
from nontainer.adapters.agno_compaction import CompactingCompression

compression = CompactingCompression(
    ws,
    policy=Policy(budget=150_000),
    on_fold=lambda fold: emit_marker(fold),
)
agent = Agent(
    ...,
    db=db,
    add_history_to_context=True,
    num_history_runs=sys.maxsize,
    compress_tool_results=True,   # the gate agno calls the manager behind
    compression_manager=compression,
)
```

```python
from nontainer.compaction import folds, in_force

in_force(ws)   # -> Fold | None, the record the next request uses
folds(ws)      # -> list[Fold], oldest first
```

## Where this stands

| Step | State |
|---|---|
| The studio sends every earlier run (nontainer-studio #78) | merged |
| The studio drops tool-result compression (nontainer-studio #79) | merged |
| Spike: the agno seam against agno's real run loop | next |
| `__compaction__/` plane and the `Fold` record | not started |
| Core: policy, view, summary prompt | not started |
| agno adapter (`CompactingCompression`) | not started |
| Studio: budget, marker event | not started |
| Chaptering | later; see above |

## Open questions

- **A kept tail.** Keeping the last run or two verbatim protects the
  exchange just before a fold. Left out to start; an option to add if
  summaries prove lossy there.
- **Compacting within a run.** See the rough edge above.
- **Manual compaction.** A "compact now" for the person (Claude Code's
  `/compact`) is a fold with the budget check skipped. Cheap to add once
  the fold exists.
- **Summarising a summary.** Each fold re-summarises the previous
  summary, which drifts over many folds. Chaptering's nested summaries
  are one answer; carrying a few key facts forward verbatim is another.
