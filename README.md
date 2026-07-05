# nontainer 📦

A fake little computer for your agent: versioned filesystem, shell, and
sandboxed Python -- as tools for any Python-based agent harness. No Docker,
no cloud sandbox, no infra. `pip install nontainer`.

> **Status: pre-alpha, core working.** Workspace + terminal + run_python
> over three backends — kvgit (versioned), dir (plain), and AgentFS
> (SQLite artifact; unversioned spike) — with agno and MCP adapters.
> The `[apps]` extra (testApp, handlers) is not yet built. APIs will
> still move.

## The pitch

Every agent framework's answer to "let the agent run code and keep files" is
either a cloud sandbox (E2B, Daytona, Modal -- API keys, billing, network
round-trips), an unsandboxed host exec, or a stateless wasm interpreter.
nontainer is the missing fourth option: an **in-process, persistent,
forkable workspace** that installs with pip and runs wherever Python runs.

| Feature | Description |
|---|---|
| **Versioned workspace** | Files, agent cache, and logs move together. Checkpoint per tool call; fork, roll back, and audit any session. |
| **Terminal tool** | 33 shell builtins (grep, sed, jq, tar, ...) over the virtual filesystem via [termish](https://github.com/ashenfad/termish). |
| **Python tool** | Policy-gated sandboxed execution via [sandtrap](https://github.com/ashenfad/sandtrap) -- stdlib `open()` routes to the workspace via [monkeyfs](https://github.com/ashenfad/monkeyfs). |
| **In-process** | Agent code can call *your* whitelisted host objects -- the live model, the db pool -- under policy. No cloud sandbox can do this. |
| **Pluggable substrate** | [kvgit](https://github.com/ashenfad/kvgit) (default), [AgentFS](https://github.com/tursodatabase/agentfs), or a plain directory. |
| **Thin adapters** | [agno](https://github.com/agno-agi/agno) toolkit and an MCP server over the same core. |

## Sketch of the API

```python
from nontainer import Workspace

ws = Workspace(session="user-42", store="~/.nontainer")  # kvgit branch per session

ws.terminal("mkdir -p data && echo 'a,b\n1,2' > data/in.csv")
ws.run_python("""
import csv
rows = list(csv.reader(open('data/in.csv')))
cache['n_rows'] = len(rows)          # persistent session cache
print(rows)
""")

ws.checkpoint(info={"tool": "run_python"})   # one commit: files + cache together
fork = ws.fork("what-if")                    # cheap branch; original untouched
ws.rollback(steps=1)                         # time-travel
```

Adapters are one import away:

```python
from nontainer.adapters.agno import WorkspaceTools   # agno Toolkit
# or:  python -m nontainer.mcp                        # MCP server (stdio)
```

## Design decisions (recorded here so we stop re-deciding them)

### Execution model

- **Script model, not persistent REPL.** Each `run_python` call is a fresh
  sandtrap execution against the workspace. State lives in three places, each
  with one job: `cache` holds **data** (picklable values), `helpers/` holds
  **code** (source files, re-imported on demand), the filesystem holds
  **artifacts**. Same conventions as [agex](https://github.com/ashenfad/agex),
  so agent mental models transfer verbatim.
- **Commit granularity is the tool call / turn, not the individual write.**
  Staged writes flush as one atomic commit with `info` metadata. High-tempo
  operational data (an app's request state) belongs in an unversioned
  sidecar (JSON files, or per-session sqlite on a real dir), not in commits.
- **Tool exposure is configurable, matched to how "plain" the python
  environment is (open question — resolve via adapter spike).** Two
  modes, fitting two configs:
  - *Terminal-only*: one `terminal` tool; `python` is a reserved
    builtin bridging `run_python` (the agex pattern), composing in
    pipelines. Fits the "plain computer" config — no host objects,
    cache off — where `python` truly has script semantics and the
    shell frame tells no lies.
  - *Split tools*: separate `run_python` with its own framing. Fits
    the "augmented environment" config — live host objects, `cache`,
    namespace-out conventions (e.g. a `ui` dict) — where script
    semantics would mislead; unusual behavior gets unusual framing.

  Diagnostic: if the terminal tool's description has to explain
  namespace magic, you wanted the split.

  Concurrency companion (the agex convention, ported): tool
  descriptions instruct ONE call per turn, batching via multiline
  scripts / `;` / pipes — mutation is then implicitly sequential
  inside the script. Harnesses can't enforce per-tool singularity
  (agno has no such flag; model-level `parallel_tool_calls=False` is
  all-or-nothing), so the adapter's per-session lock is the backstop
  when models ignore the convention: parallel calls serialize safely
  (each atomic + checkpointed) rather than being rejected. Core is unaffected either
  way: `Workspace.run_python()` is always the embedder surface, and
  the terminal `python` builtin is a thin bridge over it (same
  execution semantics — the split is about framing, not behavior).
  Terminal-first ergonomics want heredoc support in termish;
  write-to-file-then-`python script.py` is the fallback idiom (and
  leaves agent code as versioned artifacts).
- **Sandbox honesty, inherited from sandtrap:** in-process mode is a walled
  garden for cooperative LLM-generated code, not a boundary against
  adversarial code. The `isolation="process"` / `"kernel"` escalation ladder
  is available when you want real distance. We do not use the word
  "sandbox" in the pitch; the pitch is the *workspace*.

### Substrate protocol

`WorkspaceProvider` is the pluggable seam: `read/write/list/checkpoint/
restore` plus **capability flags** rather than pretended equivalence:

| Provider | `cheap_fork` | `staging` | `merge` | `sql_audit` | `fuse` |
|---|---|---|---|---|---|
| kvgit (default) | ✅ O(1), shared storage | ✅ | ✅ key-level | ❌ | ❌ |
| AgentFS (spike) | ❌ file copy | ❌ | ❌ | ✅ | ❌ (not in py SDK) |
| plain dir (`IsolatedFS`) | ❌ | ❌ | ❌ | ❌ | n/a |

Guidance: kvgit for fork-heavy / multi-session / scientific-data workloads
(chunked numpy/pandas dedup); AgentFS for audit-heavy single-agent work or
when real subprocesses / C extensions must see the files (FUSE mount as a
power mode -- with its platform caveats); plain dir when you just want the
tools against a normal folder.

- Session ids are validated (`[A-Za-z0-9_-][A-Za-z0-9_.-]*`) before touching
  any storage path -- session ids often flow from untrusted input.
- The AgentFS spike is DONE and clean: terminal + python + cache work
  unchanged over one SQLite file per session (`agentfs-sdk`; async SDK
  behind a sync facade). Spike scope: unversioned — wiring AgentFS
  whole-file snapshots as checkpoint/restore is future work. Cache
  values pass through as JSON when they round-trip identically (SQL-
  inspectable) and fall back to pickle-b64 otherwise.

### App handlers (the `[apps]` extra, trails v1)

> Full design: [docs/apps.md](docs/apps.md) — the handler contract,
> execution model, test_app DSL, serving/threat model, and milestones.
> Where this summary and that doc disagree, the doc wins.

Agents author full-stack apps: a Preact/HTM frontend plus **request handlers**
-- serverless semantics, not resident servers. No processes to babysit, and
multi-tenancy reduces to one static catch-all route.

```
/app/index.html, /app/app.js     ← frontend, served from the workspace
/app/api/scores.py               ← def get(req): ... / def post(req): ...
```

- **File-based routing**: path = route, exported `get`/`post`/... = verbs;
  `_prefixed.py` files are non-routable shared code.
- **Liberal returns**: dict/list → JSON, str → text, bytes → blob,
  `Response(...)` for control, `raise HttpError(404, ...)` for errors,
  `req.require("user", str)` for one-line validation.
- **Structural REST**: GET handlers get a read-only filesystem
  (`ReadOnlyFS`); mutating verbs get staged writes -- atomic per request
  (crash = nothing half-written), folded into turn-level commits.
- **Observability is the repair loop**: tracebacks and `print()` land in
  `/app/logs/api.log` where the agent can `tail` them; a `curl` builtin in
  the terminal hits the dispatcher directly for shell-speed endpoint tests.
- **Serving**: shipped as a Starlette/FastAPI `APIRouter` the host mounts at
  `/apps/{session_token}/...`. The session token is a capability -- long,
  unguessable, distinct from internal ids. Handlers run under sandbox policy
  with per-request tick/timeout limits; egress default-deny; serialize
  handler execution per session.
- **`testApp`**: headless verification via Playwright (`page.route`
  intercepts requests and serves them from the workspace -- the VFS *is* the
  origin; same dispatch function as live serving). Screenshots, console
  logs, scripted click/type/assert actions. Playwright-python only -- no
  Node anywhere.
- Explicitly **out** of v1: websockets/streaming, background tasks,
  middleware/auth hooks, `llm()` inside handlers (wants the quota story
  solved first).

### Later / maybe

- **run-ts**: a Node sidecar wrapping
  [agex-ts](https://github.com/ashenfad/agex-ts)'s `runtime-worker` (Node
  `worker_threads` target), bridged to the workspace via an RPC
  implementation of termish-ts's async `FileSystem` protocol. Only piece
  that needs Node; deferred until something actually pulls for npm-ecosystem
  authoring.
- **Merge-fn presets** for concurrent sessions over one branch (kvgit CAS +
  three-way merge -- the machinery exists; shipping opinionated defaults
  doesn't yet).
- **Upstreaming a thin `WorkspaceTools` to agno's toolkit registry**, and an
  MCP Skill document -- distribution channels, not code.

## Positioning (who else is in this space)

- **Cloud sandboxes** (E2B, Daytona, Modal, Fly Sprites): real isolation,
  real infra. They have persistence; none have history, forking, or
  in-process host-object access.
- **[mcp-run-python](https://github.com/pydantic/mcp-run-python)** (Pydantic):
  the incumbent local run-python (Pyodide-in-Deno). Stateless per call, no
  workspace, needs Deno.
- **[AgentFS](https://turso.tech/blog/agentfs)** (Turso): the closest cousin
  -- SQLite-backed agent FS + KV + SQL-queryable audit, snapshots by file
  copy. Storage-up where nontainer is execution-down; we'd rather interop
  (see substrate table) than compete.
- **[Val Town](https://www.val.town/)**: agents-deploying-endpoints as a
  polished cloud product (TS). The handler design here is the self-hosted,
  session-scoped, Python, versioned take on the same instinct.

nontainer's unclaimed square is the intersection: **zero-infra +
persistent-and-forkable + policy-gated access to live host objects**.

## Part of the agex stack

nontainer composes [kvgit](https://github.com/ashenfad/kvgit),
[monkeyfs](https://github.com/ashenfad/monkeyfs),
[termish](https://github.com/ashenfad/termish), and
[sandtrap](https://github.com/ashenfad/sandtrap) -- each independently
useful, each zero/minimal-dep. [agex](https://github.com/ashenfad/agex) is
the full agent framework over the same substrate; nontainer is the
environment layer alone, offered to someone else's loop.

## Install

```bash
pip install nontainer            # workspace + terminal + run_python
pip install nontainer[agno]     # + agno Toolkit adapter
pip install nontainer[mcp]      # + MCP server (python -m nontainer.adapters.mcp)
pip install nontainer[apps]     # (planned) Playwright testApp + handler serving
pip install nontainer[agentfs]  # + AgentFS substrate (agentfs-sdk)
```

## License

MIT
