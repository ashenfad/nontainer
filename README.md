# nontainer 📦

A fake little computer for your agent: versioned filesystem, shell, and
sandboxed Python -- as tools for any Python-based agent harness. No Docker,
no cloud sandbox, no infra. `pip install nontainer`.

> **Status: pre-alpha, feature-complete for v1.** Workspace + terminal
> + run_python over three backends — kvgit (versioned), dir (plain),
> AgentFS (SQLite artifact; unversioned spike) — agno and MCP adapters,
> and the `[apps]` extra: handler dispatch + curl, headless test_app
> (Playwright), and a mountable serving router. APIs will still move.

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

## The API in one glance

```python
from nontainer import workspace

ws = workspace("user-42")   # kvgit branch per session, store at ~/.nontainer

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
# or:  python -m nontainer.adapters.mcp --session s1  # MCP server (stdio)
```

## Design notes

- **Script model, not persistent REPL.** Each `run_python` call is a fresh
  sandtrap execution against the workspace. State lives in three places, each
  with one job: `cache` holds **data** (picklable values), `helpers/` holds
  **code** (source files, re-imported on demand), the filesystem holds
  **artifacts**. Same conventions as [agex](https://github.com/ashenfad/agex),
  so agent mental models transfer verbatim.
- **Commit granularity is the tool call or turn, not the individual write.**
  Staged writes flush as one atomic commit with `info` metadata.
- **Tool exposure adapts to the environment** (`tools="auto"`): a plain
  workspace gets one `terminal` tool with `python` as a builtin; an
  augmented one (host objects, cache) splits out `run_python` so its
  namespace semantics get their own framing. Tool descriptions instruct
  one call per turn; a per-session lock serializes stragglers safely.
- **Sandbox honesty, inherited from sandtrap:** in-process mode is a walled
  garden for cooperative LLM-generated code, not a boundary against
  adversarial code. The `isolation="process"` / `"kernel"` escalation ladder
  is available when you want real distance. We do not use the word
  "sandbox" in the pitch; the pitch is the *workspace*.

## Substrates

`WorkspaceProvider` is the pluggable seam: `read/write/list/checkpoint/
restore` plus **capability flags** rather than pretended equivalence:

| Provider | `cheap_fork` | `staging` | `merge` | `sql_audit` | `fuse` |
|---|---|---|---|---|---|
| kvgit (default) | ✅ O(1), shared storage | ✅ | ✅ key-level | ❌ | ❌ |
| AgentFS (spike) | ❌ file copy | ❌ | ❌ | ✅ | ❌ (not in py SDK) |
| plain dir (`IsolatedFS`) | ❌ | ❌ | ❌ | ❌ | n/a |

Guidance: kvgit for fork-heavy / multi-session / scientific-data workloads
(chunked numpy/pandas dedup); AgentFS for audit-heavy single-agent work
(cache values are SQL-inspectable when they round-trip as JSON); plain dir
when you just want the tools against a normal folder, or when real
subprocesses / C extensions must see the files. Session ids are validated
before touching any storage path -- they often flow from untrusted input.

## App handlers (the `[apps]` extra)

Agents author full-stack apps: a Preact/HTM frontend plus **request
handlers** -- serverless semantics, not resident servers.

```
/app/index.html, /app/app.js     ← frontend, served from the workspace
/app/api/scores.py               ← def get(req): ... / def post(req): ...
```

Path = route, exported `get`/`post`/... = verbs; liberal returns (dict →
JSON, `Response(...)` for control); GET handlers see a read-only
filesystem, mutating verbs get atomic staged writes. Tracebacks land in
`/app/logs/api.log`, a `curl` builtin hits the dispatcher from the
terminal, and `test_app` verifies headlessly via Playwright (the VFS *is*
the origin -- no server, no Node). Live serving ships as a
Starlette/FastAPI router the host mounts at `/apps/{session_token}/...`.

Full design -- handler contract, execution model, test_app DSL,
serving/threat model: [docs/apps.md](docs/apps.md).

## Later / maybe

- **run-ts**: a Node sidecar wrapping
  [agex-ts](https://github.com/ashenfad/agex-ts)'s runtime worker, bridged
  over an RPC filesystem. Deferred until something pulls for npm-ecosystem
  authoring.
- **AgentFS checkpoint/restore** via whole-file snapshots (the spike is
  unversioned today).
- **Merge-fn presets** for concurrent sessions over one branch (kvgit has
  the machinery; opinionated defaults don't ship yet).
- **Upstreaming `WorkspaceTools`** to agno's toolkit registry, and an MCP
  Skill document.

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

## Documentation

- [Quick Start](docs/quick-start.md) -- first workspace, sandbox config,
  backends, adapters, the apps loop; runnable examples
- [API Reference](docs/api.md) -- every class, method, and flag
- [Apps design](docs/apps.md) -- handler contract, execution model,
  test_app, serving/threat model
- [Examples](examples/) -- live agno agents: a data analyst
  (`analyst.py`) and a build-and-verify web app (`webapp.py`)

## Install

```bash
pip install nontainer            # workspace + terminal + run_python
pip install nontainer[agno]     # + agno Toolkit adapter
pip install nontainer[mcp]      # + MCP server (python -m nontainer.adapters.mcp)
pip install nontainer[apps]     # + handlers/curl, Playwright test_app, serving router
pip install nontainer[agentfs]  # + AgentFS substrate (agentfs-sdk)
```

## License

MIT
