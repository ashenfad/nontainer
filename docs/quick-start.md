# Quick Start

nontainer gives your agent a fake little computer: a versioned
filesystem, a shell, and sandboxed Python — as tools for any
Python-based agent harness. No Docker, no cloud sandbox.

```bash
pip install nontainer            # core: workspace + terminal + run_python
pip install nontainer[agno]     # + agno Toolkit adapter
pip install nontainer[mcp]      # + MCP server
pip install nontainer[apps]     # + app handlers, ws-curl, test_app, serving
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

# every mutating tool call is committed (autocommit=True)
for c in ws.log(limit=3):
    print(c.id[:8], c.info)

fork = ws.fork("what-if")               # O(1); shares storage
ws.checkout(c.id)                       # files + cache + cwd rewind together
```

Each `run_python` is a **fresh execution** — there's no resident REPL
holding variables between calls. State persists in three planes
instead, each with one job:

| plane | lifetime | what for |
|---|---|---|
| `result.namespace` | one call | handing values to the host |
| `cache` | session, **versioned** | data (picklable values) |
| files | session, versioned | artifacts; reusable code goes in `helpers/` |

So an agent's reusable code becomes a file under `helpers/` that later
calls `import`, and data it carries forward lands in `cache` — rather
than a REPL namespace surviving between calls. See the
[design notes](design.md) for why that shape.

## Fork, edit, merge

Delegation is a branch operation, not a VM operation. A fork is O(1), a
merge is three-way, and the whole thing is four verbs:

```python
from nontainer import store

st = store()
ws = st.open("user-42")
ws.files.write("/workspace/auth.py", "def login(): ...\n")
ws.files.write("/workspace/billing.py", "def charge(): ...\n")
ws.index.commit("baseline")             # the agent's own commit

# hand a delegate one file to work on: its BRANCH holds everything,
# its filesystem shows only the seed
child = ws.fork("refactor", inherit="fresh", paths=["auth.py"])
child.files.list("/workspace")          # ['/workspace/auth.py']
child.files.write("/workspace/auth.py", "def login(user): ...\n")
child.files.write("/workspace/notes.md", "why I did it\n")   # new: allowed
child.index.commit("refactored auth")

# see what it did before deciding — grouped by what you sent it to do
d = ws.diff(ws.head, child.index.head)
d.in_seed, d.elsewhere                  # {auth.py}, {notes.md}

ws.merge("refactor")                    # or take a subset instead:
# ws.checkout("refactor", paths=["auth.py"])
child.close()
```

A merge takes only what has been committed, on both sides. Overlapping
edits come back as `<<<<<<<` markers *inside* the merge commit rather
than blocking it: `ws-git status` shows them as `UU`, the agent fixes
them with ordinary edits, and the next commit clears the merge context.
To read another session's tree in place without copying it:
`ws.files.attach("refactor", "reviews")`.

### Giving the agent the versioning verbs

The agent's half of this is `ws-git`, a terminal builtin the embedder
registers — like apps, it is opt-in, and no adapter turns it on for you:

```python
from nontainer.wsgit import register_wsgit

register_wsgit(ws)                      # now the shell answers `ws-git`
ws.terminal("ws-git branch polish --paths auth.py")
ws.terminal("ws-git merge polish")      # also: status, commit, log, checkout
```

The rule: `ws.index` is the host's half and needs no switch — every
workspace whose backend has `caps.index` (kvgit does) has it. The
terminal `ws-git` exists only where `register_wsgit(ws)` has been
called; until then the agent gets `ws-git: command not found`. Both
drive the same index, so host and agent see one composition.
[ws-git.md](ws-git.md) is the reference for the agent's half — every
verb, what it prints, and what it refuses.

[examples/tour.py](../examples/tour.py) runs all of it end to end.

## Configuring the python sandbox

A safe stdlib set (math, json, csv, datetime, re, VFS-routed os/pathlib,
archives, ...) is granted by default — `import math` just works. Add
heavy libraries via presets, and anything else via modules/grants:

```python
import httpx
from nontainer import workspace, PythonConfig, ModuleGrant, Mount
from nontainer.presets import dataframes, plotting

ws = workspace(
    "analyst",
    python=PythonConfig(
        modules=[dataframes(), plotting(), ModuleGrant(httpx, network=True)],
        host_objects={"db": my_connection_pool},   # live objects, in-process
        timeout=30.0,
    ),
    mounts={"/data": Mount("/srv/datasets")},      # read-only host volume
)

r = ws.run_python("import pandas as pd; df = pd.read_csv('/data/big.csv')")
r = ws.run_python("rows = db.query('select 1')")   # your REAL pool
```

Notes:

- `stdlib=False` gives a truly bare cell (no imports at all).
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
ws.files.put("~/Downloads/report.csv", "data/report.csv")   # host → workspace
data = ws.files.get("out/summary.md", "~/Desktop/summary.md")  # workspace → host
```

Or from the terminal: `tar -czf out.tgz out` then `ws.files.get("out.tgz", ...)`.

## Backends

| backend | what it is | versioning |
|---|---|---|
| `kvgit` (default) | one shared store, branch per session | ✅ commits, O(1) forks, checkout |
| `dir` | a plain real directory per session | ❌ (but sqlite/mmap/C extensions work natively) |
| `agentfs` | one SQLite file per session ([Turso AgentFS](https://github.com/tursodatabase/agentfs)) | ❌ (spike) — but SQL-inspectable |

Pick kvgit for fork/undo/history, `dir` when agent code needs real files,
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
python -m nontainer.adapters.mcp --session webdev --apps  # + ws-curl & test_app
```

Agents also get `file_write` / `file_edit` tools in every mode — the
quoting-free path for multiline files and surgical exact-string edits
(the Claude-Code Write/Edit contract models already know).

Commit granularity is yours: the default commits every mutating
call (max durability); `WorkspaceTools(ws, commit="turn")` plus
`Agent(post_hooks=[tk.end_turn])` gives the agex model — one commit
per agent turn, so checking out the commit before one undoes a whole
turn.

The conversation can live in the workspace too, so a checkout or a
fork carries the agent's memory along with the files:

```python
from nontainer.adapters.agno_db import KvgitSessionDb, fork_session

ws = workspace(session_id)
db = KvgitSessionDb(ws, db_path="/var/agno")   # agno's other tables go here
tk = WorkspaceTools(ws, commit="turn", session_db=db)
agent = Agent(model=..., db=db, session_id=ws.session, tools=[tk])

child = fork_session(ws, "what-if")            # files + chat, O(1)
```

One commit per turn then holds files, `cache`, cwd and the run agno
just persisted; `ws.checkout(commit)` restores all four and
`fork_session()` branches all four. Drive the fork with the same three
objects built over `child`. When agno's cross-session features matter — its
past-sessions tool, AgentOS, its own `fork_session` — use
`KvgitStoreDb` over the whole store instead of a db per workspace.
The reasoning is in [agno-sessions.md](agno-sessions.md); the shapes
are in the [API reference](api.md).

Tool exposure is automatic: a plain python environment gets ONE
`terminal` tool (with a `python` builtin); an augmented one (cache or
host objects) gets a separate `run_python` tool whose description
explains the magic. Override with `tools="terminal"` / `"split"`.

## Apps: the agent builds and verifies a web app

```python
from nontainer import store
from nontainer.adapters.agno import WorkspaceTools
from nontainer.apps import AppsConfig, enable_apps

APPS = AppsConfig()                       # build ONE; see serving below
st = store()
ws = st.open(session_id)
runtime = enable_apps(ws, APPS)           # registers the `ws-curl` builtin
agent = Agent(model=..., tools=[WorkspaceTools(ws, apps=runtime)])
```

The agent now has the full loop, no server anywhere:

```
echo 'def get(req): return {"ok": True}' > app/api/health.py
ws-curl $APP_ORIGIN/api/health           # test the backend instantly
# write app/index.html, then verify headlessly (screenshots included):
test_app([{"click": "#add"}, {"assert": "..."}, {"screenshot": true}])
```

`test_app` needs `pip install nontainer[apps]` and
`playwright install chromium`. Screenshots come back as real images
to vision models AND persist at `/workspace/app/screenshots/`.

> **`ws-curl` is a workspace verb.** It's a terminal builtin injected
> into termish and ferried into `DudExecutor` guests over hostcall — so
> it exists in the in-process shell and in guests running real bash.
> The tool description gates on `Executor.supports_commands` /
> `supports_ws_verbs` and simply won't teach it where it's absent;
> there, `test_app` is the verification path. Bare `curl` in a guest
> means the machine's own curl, not the workspace app. Don't reach for importing a handler and calling its verb
> directly as a substitute: that skips routing and runs GET without
> its read-only filesystem, so it can pass on code the real request
> path rejects.

To share an app, publish a **frozen snapshot** and mount the router:

```python
from nontainer.apps import build_router, mint_token

pub = st.publish(ws, "scoreboard")     # v1: a commit of app/ and nothing else
snapshot = pub.open(                   # frozen Workspace; immutable, so reuse it
    python=PythonConfig(host_objects={"db": db})   # the handlers' live store
)
token = mint_token()                   # the embedder's table maps token -> app

router = build_router(lambda t: snapshot if t == token else None, config=APPS)
app.mount("/apps", router)     # FastAPI or Starlette
# hand out: https://your.host/apps/{token}/
```

**Pass the SAME `AppsConfig` to `enable_apps` and `build_router`.** It is
one declaration governing two lifecycles: authoring drives `test_app` —
its request interception, the policy it enforces, and the agent's tool
description — while serving drives what a published snapshot is served
under. Two configs that disagree are an app that verifies green and
breaks published, and verification cannot catch it: the workspace
test_app runs against is not the one the router serves. Both default when
omitted, so a mismatch stays invisible until you customize one.

`AppsConfig()` is where the app becomes the embedder's rather than the
library's — which frontend the agent reaches for, which asset bytes are
served beside the app without entering the workspace, and the CSP both
walls enforce. Every field is in [api.md](api.md); why each one exists
is in [apps.md](apps.md).

Serving is read-only and concurrent. Mutable app state does **not** go
in the workspace — it goes to an external store (a sqlite/postgres
client) injected via `host_objects`, and you tell the agent about it
with a `python_primer`. A publication carries the tree and nothing
else — a live sqlite handle is not a file — so you hand the objects
over when you open it: `pub.open(python=PythonConfig(host_objects={"db":
db}))`, and the same keywords on `store.tags.at` and `store.resolve`.
See the `webapp` example for the full pattern.

See [apps.md](apps.md) for the full design (handler contract, frozen
serving, threat model) and [api.md](api.md) for every signature.
