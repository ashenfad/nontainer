# nontainer 📦

A fake little computer for your agent: versioned filesystem, shell, and
sandboxed Python -- as tools for any Python-based agent harness. No Docker,
no cloud sandbox, no infra. `pip install nontainer`.

> **Status: pre-alpha.** Usable and tested end to end; the API will still
> move before 1.0.

## The pitch

You hand your agent a `terminal` and a `run_python` tool. Unlike a
stateless sandbox call, these are **stateful and bound to a session**: the
shell's `cd` sticks, files one call writes the next call reads, and a
`cache` dict persists for the whole conversation. It's a little computer the
agent keeps *using* -- not a fresh box each call.

And because that computer is a **versioned workspace**, you get the
operations durable state makes possible: checkpoint every call, fork a
session in O(1), roll back to any commit, audit the history. All in-process,
`pip`-installable, running wherever Python runs.

| | |
|---|---|
| **Terminal tool** | 33 shell builtins (grep, sed, jq, tar, ...) over the virtual filesystem via [termish](https://github.com/ashenfad/termish). |
| **Python tool** | Policy-gated sandboxed execution via [sandtrap](https://github.com/ashenfad/sandtrap); safe stdlib on by default, `open()`/`os`/`pathlib` routed to the workspace via [monkeyfs](https://github.com/ashenfad/monkeyfs). |
| **In-process** | Agent code can call *your* whitelisted host objects -- the live model, the db pool -- under policy. No cloud sandbox can. |
| **Pluggable substrate** | [kvgit](https://github.com/ashenfad/kvgit) (versioned), [AgentFS](https://github.com/tursodatabase/agentfs), or a plain directory -- same tools. |
| **Thin adapters** | [agno](https://github.com/agno-agi/agno) toolkit and an MCP server over one core. |

## The API in one glance

```python
from nontainer import workspace

ws = workspace("user-42")            # versioned; a kvgit branch per session

ws.terminal("mkdir -p data && echo 'a,b\n1,2' > data/in.csv")
r = ws.run_python("""
import csv
rows = list(csv.reader(open('data/in.csv')))   # sees the shell's file
cache['n_rows'] = len(rows)                      # persists across the session
print(rows)
""")

r.checkpoint                 # commit id this call produced; ws.restore(it) undoes it
fork = ws.fork("what-if")    # O(1) branch; the original is untouched
ws.rollback(steps=1)         # or time-travel by steps
```

Adapters are one import away:

```python
from nontainer.adapters.agno import WorkspaceTools   # agno Toolkit
# or:  python -m nontainer.adapters.mcp --session s1  # MCP server (stdio)
```

## Substrates

`WorkspaceProvider` is the pluggable seam -- one filesystem-and-KV protocol,
**capability flags** instead of pretended equivalence:

| Provider | versioned | `cheap_fork` | `sql_audit` |
|---|---|---|---|
| kvgit (default) | ✅ | ✅ O(1) | ❌ |
| plain dir | ❌ | ❌ | ❌ |
| AgentFS (spike) | ❌ | ❌ | ✅ |

kvgit for fork/undo/audit, `dir` when agent code needs real files (C
extensions, subprocesses), AgentFS for the one-file-artifact + SQL story --
or bring your own provider. Full guidance in the [API reference](docs/api.md).

## App handlers (the `[apps]` extra)

Agents author full-stack apps: a Preact/HTM frontend plus **request
handlers** -- serverless semantics, not resident servers. A file's path is
its route (`/app/api/scores.py` → `/api/scores`), its exported `get`/`post`
are the verbs; GET handlers see a read-only filesystem, mutating verbs get
atomic staged writes. The agent builds and verifies entirely in-loop: a
`curl` builtin hits the dispatcher from the terminal, and `test_app` runs the
app headlessly through Playwright with the workspace as the origin -- no
server, no Node. Live serving ships as a Starlette/FastAPI router the host
mounts at `/apps/{session_token}/...`.

Full design -- handler contract, execution model, test_app DSL,
serving/threat model: [docs/apps.md](docs/apps.md).

## Related work

- **Cloud sandboxes** (E2B, Daytona, Modal, Fly Sprites): real isolation,
  real infra. They have persistence; none have history, forking, or
  in-process host-object access.
- **[mcp-run-python](https://github.com/pydantic/mcp-run-python)** (Pydantic):
  the incumbent local run-python (Pyodide-in-Deno). Stateless per call, no
  workspace, needs Deno.
- **[AgentFS](https://turso.tech/blog/agentfs)** (Turso): the closest cousin
  -- SQLite-backed agent FS + KV + SQL-queryable audit, snapshots by file
  copy. Storage-up where nontainer is execution-down; we'd rather interop
  (it's a substrate above) than compete.
- **[Val Town](https://www.val.town/)**: agents-deploying-endpoints as a
  polished cloud product (TS). The handler design here is the self-hosted,
  session-scoped, Python, versioned take on the same instinct.

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
- [Design notes](docs/design.md) -- why it's shaped this way (execution
  model, commit granularity, tool exposure) and what's still ahead
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
