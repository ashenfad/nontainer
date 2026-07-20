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
| **Terminal tool** | ~33 shell builtins (grep, sed, jq, tar, ...) over the virtual filesystem via [termish](https://github.com/ashenfad/termish). |
| **Python tool** | Policy-gated sandboxed execution via [sandtrap](https://github.com/ashenfad/sandtrap); safe stdlib on by default, `open()`/`os`/`pathlib` routed to the workspace via [monkeyfs](https://github.com/ashenfad/monkeyfs). |
| **In-process** | Agent code can call *your* whitelisted host objects -- the live model, the db pool -- under policy. No cloud sandbox can. |
| **Pluggable substrate** | [kvgit](https://github.com/ashenfad/kvgit) (versioned), [AgentFS](https://github.com/tursodatabase/agentfs), or a plain directory -- same tools. |
| **Pluggable execution** | the same tools run in-process *or* on a real machine via [dud](https://github.com/ashenfad/dud) -- the versioning is unchanged either way. |
| **Thin adapters** | [agno](https://github.com/agno-agi/agno) toolkit and an MCP server over one core. |

> **What the sandbox is (and isn't).** In-process, the Python sandbox
> ([sandtrap](https://github.com/ashenfad/sandtrap)) is a **walled garden
> for cooperative LLM-generated code** — it gates what agent code can
> reach (modules, host objects, the filesystem) to an allowlist you
> control (safe stdlib on by default, everything else opt-in), not a
> hardened boundary against code *trying* to escape. That's the right
> posture for your own agent's code. When you need a real boundary
> (untrusted code, or serving to anonymous clients), escalate with
> `isolation="process"` / `"kernel"`, or step off the in-process model
> entirely and run the session in a microVM (see Executors). Full
> framing in the [design notes](docs/design.md).

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

Files live under the **workspace root** — `/workspace` by default
(`workspace(..., root=)`) — and cwd starts there, so relative paths
just work. The root is the one absolute-path contract shared across
executors: a dud VM mounts its guest workspace at the same path, so
`/workspace/data/in.csv` names the same file whether agent code runs
in the local sandbox or a real machine.

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

## Executors (the `[dud]` extra)

The second seam. `WorkspaceProvider` decides where state *lives*;
`Executor` decides where code *runs* -- and the two are independent,
because the versioning semantics were always properties of the state
layer, not the machine.

| Executor | isolation | fidelity |
|---|---|---|
| `LocalExecutor` (default) | sandtrap's walled garden, in-process | emulated shell + filesystem |
| `DudExecutor(backend="subprocess")` | **none** -- host process | real bash, real files |
| `DudExecutor(backend="vfkit")` | a disposable Linux microVM (macOS/HVF) | real machine |

```python
from nontainer.executor_dud import DudExecutor

ws = workspace("user-42", executor_factory=lambda: DudExecutor(backend="vfkit"))
```

Same `terminal` / `run_python` tools, same checkpoints, same O(1)
forks -- [dud](https://github.com/ashenfad/dud) receives a tree,
executes against a real filesystem, and returns a diff, which the
provider commits exactly as it commits a local one. What you buy is
fidelity: C extensions, real subprocesses, sqlite on real files,
memory-mapped parquet -- the workloads the in-process emulation serves
worst.

Note the middle row: `backend="subprocess"` is real bash and real
Python with **no containment at all** -- agent code runs as you, with
your network and your files. It's for fidelity during development, not
for isolation. The microVM backend is the one that gives you a
boundary.

## App handlers (the `[apps]` extra)

Agents author full-stack apps: a Preact/HTM frontend plus **request
handlers** -- serverless semantics, not resident servers. A file's path is
its route (`/workspace/app/api/scores.py` → `/api/scores`), its exported `get`/`post`
are the verbs. The agent builds and verifies entirely in-loop: a `curl`
builtin hits the dispatcher from the terminal, and `test_app` runs the app
headlessly through Playwright with the workspace as the origin -- no server,
no Node. To share it, publish a **frozen snapshot**: `build_router` serves
the app read-only and concurrently at `/apps/{token}/...`; mutable app state
lives in an external store injected via `host_objects`, not the (frozen)
workspace.

Full design -- handler contract, execution model, test_app DSL,
serving/threat model: [docs/apps.md](docs/apps.md).

## Related work

- **Cloud sandboxes** (E2B, Daytona, Modal, Fly Sprites): real isolation,
  real infra. They have persistence; none have history, forking, or
  in-process host-object access.
- **[mcp-run-python](https://github.com/pydantic/mcp-run-python)** (Pydantic):
  the incumbent local run-python (Pyodide-in-Deno). Stateless per call, no
  workspace, needs Deno.
- **[AgentFS](https://turso.tech/blog/agentfs)** (Turso): SQLite-backed
  agent FS + KV + SQL-queryable audit, snapshots by file copy. It comes at
  the problem from storage where nontainer comes from execution -- and
  nontainer runs on it as one of its backends.
- **[Val Town](https://www.val.town/)**: agents-deploying-endpoints as a
  polished cloud product (TS). The handler design here is the self-hosted,
  session-scoped, Python, versioned take on the same instinct.

## Part of the agex stack

nontainer composes [kvgit](https://github.com/ashenfad/kvgit),
[monkeyfs](https://github.com/ashenfad/monkeyfs),
[termish](https://github.com/ashenfad/termish), and
[sandtrap](https://github.com/ashenfad/sandtrap) -- each independently
useful, each zero/minimal-dep -- and optionally
[dud](https://github.com/ashenfad/dud) when the little computer should
be a real one. [agex](https://github.com/ashenfad/agex) is
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
pip install nontainer[dud]      # + real-machine / microVM execution (needs 3.11+)
```

## License

MIT
