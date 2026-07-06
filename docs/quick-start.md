# Quick Start

nontainer gives your agent a fake little computer: a versioned
filesystem, a shell, and sandboxed Python — as tools for any
Python-based agent harness. No Docker, no cloud sandbox.

```bash
pip install nontainer            # core: workspace + terminal + run_python
pip install nontainer[agno]     # + agno Toolkit adapter
pip install nontainer[mcp]      # + MCP server
pip install nontainer[apps]     # + app handlers, curl, test_app, serving
pip install nontainer[agentfs]  # + AgentFS backend
```

## Your first workspace

```python
from nontainer import workspace

with workspace("demo", backend="dir", store="/tmp/nt") as ws:
    r = ws.terminal("mkdir -p data; echo 'a,b\n1,2' > data/in.csv; cat data/in.csv | wc -l")
    print(r.stdout)      # 2
    print(bool(r))       # True (exit code 0)

    r = ws.run_python("""
rows = open('data/in.csv').read().splitlines()
count = len(rows)
print(f"{count} rows")
""")
    print(r.stdout)          # 2 rows
    print(r.namespace)       # {'rows': [...], 'count': 2} — for YOUR code, not the model
```

Both tools share one world: files written by the shell are read by
python and vice versa, and `cd` is stateful across calls. Results
never raise for agent-code failure — check `r.exit_code` / `r.error`
or just truthiness.

## The versioned workspace (kvgit backend, the default)

```python
from nontainer import workspace

ws = workspace("user-42")               # ~/.nontainer, branch per session
ws.terminal("echo v1 > report.md")
ws.run_python("cache['step'] = 1")      # cache: the persistent dict

# every mutating tool call is checkpointed (autocheckpoint=True)
for c in ws.history(limit=3):
    print(c.id[:8], c.info)

fork = ws.fork("what-if")               # O(1); shares storage
ws.rollback(1)                          # files + cache + cwd rewind together
```

Three persistence planes, one job each:

| plane | lifetime | what for |
|---|---|---|
| `result.namespace` | one call | handing values to the host |
| `cache` | session, **versioned** | data (picklable values) |
| files | session, versioned | artifacts; reusable code goes in `helpers/` |

## Configuring the python sandbox

```python
import math, pandas as pd
import httpx
from nontainer import workspace, PythonConfig, ModuleGrant, Mount

ws = workspace(
    "analyst",
    python=PythonConfig(
        modules=[math, pd, ModuleGrant(httpx, network=True)],
        host_objects={"db": my_connection_pool},   # live objects, in-process
        timeout=30.0,
    ),
    mounts={"/data": Mount("/srv/datasets")},      # read-only host volume
)

r = ws.run_python("df = pd.read_csv('/data/big.csv'); n = len(df)")
r = ws.run_python("rows = db.query('select 1')")   # your REAL pool
```

Notes:

- Bare modules get no passthroughs; `ModuleGrant(..., network=True)`
  or `host_fs=True` grants per module. `host_objects` are live host
  resources — a superpower no cloud sandbox has.
- Mounts are visible to BOTH tools and are not versioned; prefer
  `readonly=True` (the default) and copy inputs in when the agent
  should own them.
- The sandbox is a walled garden for cooperative LLM-generated code
  (see sandtrap's security docs); `PythonConfig(isolation="process")`
  or `"kernel"` when you want real distance.

## Moving files in and out

```python
ws.put("~/Downloads/report.csv", "data/report.csv")   # host → workspace
data = ws.get("out/summary.md", "~/Desktop/summary.md")  # workspace → host
```

Or from the terminal: `tar -czf out.tgz out` then `ws.get("out.tgz", ...)`.

## Backends

| backend | what it is | versioning |
|---|---|---|
| `kvgit` (default) | one shared store, branch per session | ✅ checkpoints, O(1) forks, rollback |
| `dir` | a plain real directory per session | ❌ (but sqlite/mmap/C extensions work natively) |
| `agentfs` | one SQLite file per session ([Turso AgentFS](https://github.com/tursodatabase/agentfs)) | ❌ (spike) — but SQL-inspectable |

Pick kvgit for fork/undo/audit, `dir` when agent code needs real files,
`agentfs` for the one-file-artifact + SQL-audit story. Or implement
`WorkspaceProvider` and bring your own.

## Hooking up an agent

**agno:**

```python
from agno.agent import Agent
from nontainer import workspace
from nontainer.adapters.agno import WorkspaceTools

ws = workspace(session_id)
agent = Agent(model=..., tools=[WorkspaceTools(ws)])
```

**MCP** (Claude Code, or any MCP client):

```bash
python -m nontainer.adapters.mcp --session my-project --module math
```

Agents also get `file_write` / `file_edit` tools in every mode — the
quoting-free path for multiline files and surgical exact-string edits
(the Claude-Code Write/Edit contract models already know).

Commit granularity is yours: the default checkpoints every mutating
call (max durability); `WorkspaceTools(ws, checkpoint="turn")` plus
`Agent(post_hooks=[tk.end_turn])` gives the agex model — one commit
per agent turn, so `rollback(1)` undoes a whole turn.

Tool exposure is automatic: a plain python environment gets ONE
`terminal` tool (with a `python` builtin); an augmented one (cache or
host objects) gets a separate `run_python` tool whose description
explains the magic. Override with `tools="terminal"` / `"split"`.

## Apps: the agent builds and verifies a web app

```python
from nontainer import workspace
from nontainer.adapters.agno import WorkspaceTools
from nontainer.apps import enable_apps

ws = workspace(session_id)
runtime = enable_apps(ws)                 # registers the `curl` builtin
agent = Agent(model=..., tools=[WorkspaceTools(ws, apps=runtime)])
```

The agent now has the full loop, no server anywhere:

```
echo 'def get(req): return {"ok": True}' > app/api/health.py
curl /api/health                          # test the backend instantly
# write app/index.html, then verify headlessly (screenshots included):
test_app([{"click": "#add"}, {"assert": "..."}, {"screenshot": true}])
```

`test_app` needs `pip install nontainer[apps]` and
`playwright install chromium`. Screenshots come back as real images
to vision models AND persist at `/app/screenshots/`.

To serve an app live, mount the router in your web app:

```python
from nontainer.apps import build_router, mint_token

router = build_router(lambda token: my_tokens.get(token))
app.mount("/apps", router)     # FastAPI or Starlette
# hand out: https://your.host/apps/{token}/
```

See [apps.md](apps.md) for the full design (handler contract, threat
model, quiesce checkpointing) and [api.md](api.md) for every signature.
