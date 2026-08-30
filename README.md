# nontainer 📦

**Versioned, forkable workspaces for code-using agents.**

Give any Python agent loop a stateful terminal and Python tool over a
workspace that checkpoints files and cache together, forks in O(1), and
rolls back as a unit. Run locally — where agent code can work through
whitelisted live host objects — or on a microVM while the workspace history
stays in the state layer.

Think of it as a fake little computer with branchable history, packaged as a
library. No Docker, cloud sandbox, or service required for the local default:
`pip install nontainer`.

> **Status: pre-alpha.** Usable and tested end to end; the API will still
> move before 1.0.

## The core

Nontainer keeps three concerns separate:

| | Responsibility |
|---|---|
| **`WorkspaceProvider`** | Where files and cache live, and which history operations are real. The default [kvgit](https://github.com/ashenfad/kvgit) provider supplies cheap checkpoints, forks, rollback, and audit; other providers declare narrower capabilities rather than pretending equivalence. |
| **`Executor`** | Where terminal and Python code run and how they reach workspace state: locally through [sandtrap](https://github.com/ashenfad/sandtrap) and [monkeyfs](https://github.com/ashenfad/monkeyfs), or on a real machine through [dud](https://github.com/ashenfad/dud). |
| **Adapters** | How the two tools enter an existing agent loop: the core Python API, an [agno](https://github.com/agno-agi/agno) toolkit, or an MCP server. |

The model-facing surface stays small: a `terminal` and a `run_python` tool.
Unlike stateless sandbox calls, both are **stateful and bound to a session**:
the shell's `cd` sticks, files one call writes the next call reads, and a
`cache` dict persists for the whole conversation.

Because that state is a **versioned workspace**, each state-changing call can
be checkpointed as one unit. The host can fork a session in O(1), roll back
to any commit, or audit its history without teaching the agent a version
control protocol.

| | |
|---|---|
| **Terminal tool** | ~33 shell builtins (grep, sed, jq, tar, ...) over the virtual filesystem via [termish](https://github.com/ashenfad/termish). |
| **Python tool** | Policy-gated sandboxed execution via [sandtrap](https://github.com/ashenfad/sandtrap); safe stdlib on by default, `open()`/`os`/`pathlib` routed to the workspace via [monkeyfs](https://github.com/ashenfad/monkeyfs). |
| **In-process** | Agent code can call *your* whitelisted host objects -- the live model, the db pool -- under policy. No cloud sandbox can. |

> **What the sandbox is (and isn't).** In-process, the Python sandbox
> ([sandtrap](https://github.com/ashenfad/sandtrap)) is a **walled garden
> for cooperative LLM-generated code** — it gates what agent code can
> reach (modules, host objects, the filesystem) to an allowlist you
> control (safe stdlib on by default, everything else opt-in), not a
> hardened boundary against code *trying* to escape. That's the right
> posture for your own agent's code. For crash containment and
> kernel-enforced defense-in-depth around cooperative code, use
> `isolation="process"` / `"kernel"`. For actively untrusted code, or
> execution exposed to anonymous clients, step off the local model and
> use `DudExecutor()`'s microVM backend (see Executors). Full framing in
> the [design notes](docs/design.md).

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

Checkpoints cover workspace-owned files and cache. Host-object calls
and mounts are external effects: their data is not checkpointed,
restored, or copied by a fork. A fork does inherit the mount *points*,
and sees the same live directories behind them.

Files live under the **workspace root** — `/workspace` by default
(`workspace(..., root=)`) — and cwd starts there, so relative paths
just work. The root is the one absolute-path contract shared across
executors: a dud VM mounts its guest workspace at the same path, so
`/workspace/data/in.csv` names the same file whether agent code runs
in the local sandbox or a real machine.

Values an agent wants *shown* go in a `ui` dict. Anything that can
cross as data stays as it is; a live object that cannot — a plotly
figure, a DataFrame, a matplotlib figure, a PIL image — is written to
`<root>/ui/<name>.<ext>` and the binding names where it went:

```python
r = ws.run_python("""
import pandas as pd
ui = {"chart": pd.DataFrame({"a": [1, 2, 3]}), "note": "top three"}
""")

r.namespace["ui"]["chart"]        # ArtifactPath('/workspace/ui/chart.table.json',
                                  #              kind='table')
r.namespace["ui"]["chart"].kind   # 'table'  -- derived from the suffix
r.namespace["ui"]["note"]         # 'top three'  -- plain data is untouched
r.ui_problems                     # () -- or why something did not render
```

`ArtifactPath` is a `str` subclass, so knowing about it is optional:
code that has never heard of it still gets a working absolute path.
Code that cares asks `isinstance(v, ArtifactPath)` — which a bare
string could not answer, since agents put ordinary strings in `ui` too.

`ws.read_artifact(path)` returns the bytes, or `None` if it cannot be
read — the shape the a2ui envelope wants, so wiring a surface is one
argument rather than a hand-rolled wrapper:

```python
turn_to_a2ui(prose, artifacts, ws.read_artifact, file_url, surface_id=sid)
```

This happens in `run_python` itself, so it is the same on every
executor. On a VM the object cannot leave the guest, so it is
serialized there; in-process it is serialized here. Either way you get
the same binding and the same file.

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
| `LocalExecutor` (default) | sandtrap's walled garden; optional process/kernel defense-in-depth | emulated shell + filesystem |
| `DudExecutor()` -- i.e. `backend="vm"` | a disposable microVM -- vfkit on macOS, firecracker on Linux/KVM | real machine |
| `DudExecutor(backend="subprocess")` | **none** -- host process | real bash, real files |

```python
from nontainer.executor_dud import DudExecutor

ws = workspace("user-42", executor_factory=lambda: DudExecutor())
```

The default `"vm"` picks the right hypervisor for the host; name
`"vfkit"` or `"firecracker"` directly if you need to pin one. Asking
for one the host can't provide fails closed (`IsolationUnavailable`)
rather than quietly degrading.

Same `terminal` / `run_python` tools, same checkpoints, same O(1)
forks -- [dud](https://github.com/ashenfad/dud) receives a tree,
executes against a real filesystem, and returns a diff, which the
provider commits exactly as it commits a local one. What you buy is
fidelity: C extensions, real subprocesses, sqlite on real files,
memory-mapped parquet -- the workloads the in-process emulation serves
worst.

Note the last row: `backend="subprocess"` is real bash and real Python
with **no containment at all** -- agent code runs as you, with your
network and your files. It buys fidelity, not a boundary, so it's
opt-in rather than the default: it's the only backend that needs no
hypervisor, which makes it the dev/CI floor. If you want policy gating,
crash containment, or kernel defense-in-depth without a VM, use
`LocalExecutor`, not this.

## App handlers (the `[apps]` extra)

Agents author full-stack apps: a no-build frontend plus **request
handlers** -- serverless semantics, not resident servers. A file's path is
its route (`/workspace/app/api/scores.py` → `/api/scores`), its exported `get`/`post`
are the verbs. The agent builds and verifies entirely in-loop: a `curl`
builtin hits the dispatcher from the terminal, and `test_app` runs the app
headlessly through Playwright with the workspace as the origin -- no server,
no Node. To share it, publish a **frozen snapshot**: `build_router` serves
the app read-only and concurrently at `/apps/{token}/...`; mutable app state
lives in an external store injected via `host_objects`, not the (frozen)
workspace.

Which frontend the agent reaches for is the **embedder's** call, not
ours: `AppsConfig.frontend_notes` states the approach and the libraries,
and `static_assets` serves the bytes alongside the app without them
entering the workspace. Together they are what a house design system
rides on, and what makes an **air-gapped** deployment work with no CDN
in reach. Leave both unset and agents get the built-in guidance --
plain DOM first, Preact and plotly from the CDN allowlist.

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
