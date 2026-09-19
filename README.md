# nontainer 📦

**Versioned, forkable workspaces for code-using agents.**

Give any Python agent loop a stateful terminal and Python tool over a
workspace that commits files and cache together, forks in O(1), and
checks out as a unit. Run locally — where agent code can work through
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
| **`WorkspaceProvider`** | Where files and cache live, and which history operations are real. The default [kvgit](https://github.com/ashenfad/kvgit) provider supplies cheap commits, forks, checkout, and history; other providers declare narrower capabilities rather than pretending equivalence. |
| **`Executor`** | Where terminal and Python code run and how they reach workspace state: locally through [sandtrap](https://github.com/ashenfad/sandtrap) and [monkeyfs](https://github.com/ashenfad/monkeyfs), or on a real machine through [dud](https://github.com/ashenfad/dud). |
| **Adapters** | How the two tools enter an existing agent loop: the core Python API, an [agno](https://github.com/agno-agi/agno) toolkit, or an MCP server. |

The model-facing surface stays small: a `terminal` and a `run_python` tool.
Unlike stateless sandbox calls, both are **stateful and bound to a session** —
the shell's `cd` sticks, files one call writes the next call reads, and a
`cache` dict persists for the whole conversation. Because that state is a
**versioned workspace**, each state-changing call can be committed as one
unit: the host can fork a session in O(1), check out any commit, or audit
its history without teaching the agent a version control protocol.

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
> use `DudExecutor()`'s microVM backend (see the
> [quick start](docs/quick-start.md)). Full framing in the
> [design notes](docs/design.md).

## What it looks like

```python
from nontainer import store

st = store()                        # default ~/.nontainer
ws = st.open("user-42")             # one session, one kvgit branch

ws.terminal("mkdir -p data && echo 'a,b\n1,2' > data/in.csv")
r = ws.run_python("""
import csv
rows = list(csv.reader(open('data/in.csv')))   # sees the shell's file
cache['n_rows'] = len(rows)                    # persists across the session
print(rows)
""")

r.commit                            # this call's commit; ws.checkout(it) restores it
ws.index.commit("baseline")         # or compose one and name it yourself

child = ws.fork("what-if")          # O(1) branch; the original is untouched
child.files.write("notes.md", "# findings\n")
child.index.commit("a thought")
ws.merge("what-if")                 # and bring its work back
child.close()
```

The sandbox config, the backends, the executor rungs, the adapters and the apps loop are all in the [quick start](docs/quick-start.md).

## Related work

- **Cloud sandboxes** (E2B, Daytona, Modal, Fly Sprites): real isolation, real infra. They have persistence; none have history, forking, or in-process host-object access.
- **[mcp-run-python](https://github.com/pydantic/mcp-run-python)** (Pydantic): the incumbent local run-python (Pyodide-in-Deno). Stateless per call, no workspace, needs Deno.
- **[AgentFS](https://turso.tech/blog/agentfs)** (Turso): SQLite-backed agent FS + KV + SQL-queryable audit, snapshots by file copy. It comes at the problem from storage where nontainer comes from execution -- and nontainer runs on it as one of its backends.
- **[Val Town](https://www.val.town/)**: agents-deploying-endpoints as a polished cloud product (TS). The handler design here is the self-hosted, session-scoped, Python, versioned take on the same instinct.

## Part of the agex stack

nontainer composes [kvgit](https://github.com/ashenfad/kvgit), [monkeyfs](https://github.com/ashenfad/monkeyfs), [termish](https://github.com/ashenfad/termish), and [sandtrap](https://github.com/ashenfad/sandtrap) -- each independently useful, each zero/minimal-dep -- and optionally [dud](https://github.com/ashenfad/dud) when the little computer should be a real one. [agex](https://github.com/ashenfad/agex) is the full agent framework over the same substrate; nontainer is the environment layer alone, offered to someone else's loop.

## Documentation

**Using it**

- [Quick Start](docs/quick-start.md) -- first workspace, sandbox config, backends, picking an executor rung, adapters, the apps loop
- [API Reference](docs/api.md) -- every class, method, and flag
- [Delegation](docs/sessions.md) -- subagents as branches: ask, read the answer, decide what to merge
- [Apps](docs/apps.md) -- handler contract, execution model, test_app, serving and threat model
- [agno sessions](docs/agno-sessions.md) -- the conversation in the workspace, so rewind and fork cover memory too

**What the agent sees**

- [ws-git](docs/ws-git.md) -- its git over its own session: every verb, what it prints, what it refuses
- [Unit tests](docs/testing.md) -- `ws-pytest` and `ws-vitest`: discovery, the flags, calling a handler with a fake, mocking at the fetch boundary

**Extending it**

- [Extending](docs/extending.md) -- the three seams: a new substrate, a new place code runs, the agent loop behind a delegation

**Why it's shaped this way**

- [Design notes](docs/design.md) -- execution model, commit granularity, tool exposure, and what's still ahead
- [Browser executor](docs/browser-executor.md) -- proposal: agent compute in a Pyodide tab, publications served from the visitor's browser, and what that moves

**Seeing it run**

- [Tour](examples/tour.py) -- the whole surface end to end, with no LLM in the loop
- [Examples](examples/) -- live agno agents: a data analyst (`analyst.py`) and a build-and-verify web app (`webapp.py`)

## Install

```bash
pip install nontainer            # workspace + terminal + run_python
pip install nontainer[agno]     # + agno Toolkit adapter
pip install nontainer[mcp]      # + MCP server (python -m nontainer.adapters.mcp)
pip install nontainer[apps]     # + handlers/curl, Playwright test_app, serving router
pip install nontainer[agentfs]  # + AgentFS substrate (agentfs-sdk)
pip install nontainer[dud]      # + real-machine / microVM execution (needs 3.11+)
```

## Development

```bash
uv sync --extra dev --extra apps --extra agno --extra mcp --extra dud
uv run pytest -q
uv run ruff check nontainer tests && uv run ruff format --check nontainer tests
```

Include `--extra dud` on 3.11+: the DudExecutor tests guard themselves
with `importorskip`, so without it the whole real-machine suite skips
silently and the run is green for the wrong reason. (On 3.10 the extra
installs nothing — it carries a `python_version >= "3.11"` marker — and
those tests skip by design.)

## License

MIT
