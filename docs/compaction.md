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

The fix (nontainer-studio #78) sends every earlier run. That makes the
history unbounded, and the only thing still bounding the context is
agno's tool-result compression. It summarises old tool results past a
watermark and never touches prose or tool-call arguments, so a long
enough session will still outgrow the window. A large `file_write` is
all argument. Compaction is the bound.

## The short version

- **Trigger:** the request about to be sent is over a token budget.
- **Fold:** every run before the last few is summarised into one
  summary. The next fold summarises the previous summary plus the runs
  since, so there is only ever one summary in context.
- **Record:** each fold writes a record (which runs it covers, the
  summary, and the sizes before and after) into the workspace. Records
  are only ever added. The stored runs are never touched.
- **Model's view:** system prompt, the summary, then the runs after the
  fold, word for word.
- **Person's view:** the full transcript, with a marker where a fold
  happened that opens to the summary. What the agent remembers is then
  something the person can read.
- **Cache:** one miss per fold. Between folds the prefix is stable,
  which is the "waves, not a sliding window" rule the studio already
  follows for tool results.

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
  is it time, and where is the cut? It always cuts at run boundaries,
  so a tool call is never separated from its result. It keeps the last
  `keep_runs` runs verbatim, and declines a fold that would cover too
  little to be worth the summary (`min_fold_runs`).
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
The adapter is a `CompressionManager` subclass, so nothing of agno's is
overridden:

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
- **Tool-result compression** is the inner layer and runs as it does
  today. Within a single long run there are no earlier runs to fold, so
  it is all that bounds that run.
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

- **Budget:** the per-model watermark the studio already computes,
  `compress_token_limit`. Tool-result compression runs first because it
  is cheaper and needs no turn boundary. The fold budget should sit
  above it, so a fold happens only when compressing tool results was
  not enough.
- **Marker:** a transcript event ("Context compacted: 12 turns
  summarised") that expands to the summary. It is a projection of the
  record, so a reload shows it again.
- **Delegates:** a delegate's turn budget is small enough that it
  should rarely fold. It runs the same mechanism if it does.

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
  manager.
- **A compression of an earlier run must be stored.** agno rebuilds
  history from the stored runs every run, as copies it drops before
  storing the run, so anything computed on an earlier run's copy is
  lost and computed again next turn. Folds are safe by design: the
  summary is a record, and applying it needs no model call. Tool-result
  compression is not. agno keeps a compression only when it is made in
  the result's own run, so a result that first crosses the watermark
  later is compressed again on every turn after. The studio fixes this
  for now by copying the compression onto the session's original
  message, which agno then stores (nontainer-studio #78). That rewrites
  old runs, so a run is no longer one blob written once, and it relies
  on a pre-hook receiving the session agno later saves. The adapter
  should instead store these compressions as records in
  `__compaction__/`, keyed by message id, and apply them as it applies a
  fold. Runs then stay write-once, and every derived view lives in one
  plane. Once folds exist the problem is smaller anyway: only the runs
  kept word for word still have tool results to compress.
- **Token counts are estimates** until the response reports real usage.
  The trigger uses agno's own estimate, `model.count_tokens(messages,
  tools, response_format)`, the same one `should_compress` uses today.
- **The summary is lossy.** That is the trade. The full conversation
  stays in the branch, the person sees all of it, and chaptering's
  browsable originals are the later answer for the agent.

## API sketch

```python
from nontainer.compaction import Policy
from nontainer.adapters.agno_compaction import CompactingCompression

compression = CompactingCompression(
    ws,
    policy=Policy(budget=150_000, keep_runs=2, min_fold_runs=3),
    tool_results_at=120_000,   # agno's compress_token_limit
    on_fold=lambda fold: emit_marker(fold),
)
agent = Agent(
    ...,
    db=db,
    add_history_to_context=True,
    num_history_runs=sys.maxsize,
    compress_tool_results=True,
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
| The studio sends every earlier run (nontainer-studio #78) | open |
| `__compaction__/` plane and the `Fold` record | not started |
| Core: policy, view, summary prompt | not started |
| agno adapter (`CompactingCompression`) | not started |
| Studio: budget, marker event | not started |
| Chaptering | later; see above |

## Open questions

- **`keep_runs` or a token tail?** Runs never cut a turn in half, but
  one run can be enormous. #9873 offers either. Runs first.
- **Manual compaction.** A "compact now" for the person (Claude Code's
  `/compact`) is a fold with the budget check skipped. Cheap to add once
  the fold exists.
- **Summarising a summary.** Each fold re-summarises the previous
  summary, which drifts over many folds. Chaptering's nested summaries
  are one answer; carrying a few key facts forward verbatim is another.
