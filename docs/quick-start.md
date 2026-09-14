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

The session's own verbs are on the object; the rest are grouped by the
seam they belong to — `ws.files` (read/write/edit/put/get/export),
`ws.index` (the staged set), `ws.tags` (this session's names), and
`ws.runtime` (how code runs: the executor, commands, shell variables).

Files live under the **workspace root** — `/workspace` by default
(`workspace(..., root=)`) — and cwd starts there, so relative paths
just work. The root is the one absolute-path contract shared across
executors: a dud VM mounts its guest workspace at the same path, so
`/workspace/data/in.csv` names the same file whether agent code runs
in the local sandbox or a real machine.

## The store: what outlives one session

A `Workspace` is one session. The `Store` is the place those sessions
live in, and it owns the verbs about the *set* of them —
`workspace(...)` is sugar for opening one out of it:

```python
import nontainer

store = nontainer.store()            # default ~/.nontainer
ws = store.open("user-42")           # what workspace("user-42") does

store.sessions()                     # every session on the store
store.delete("user-42")              # drop one, storage and all
store.tags.add(ws, "v1")             # a name that outlives the session
snap = store.tags.at("v1")           # a frozen workspace at that name
```

A tag is session-scoped by default and dies with the session;
`store.tags` is the store-scoped half — a publication that outlives it,
readable from any session on the store.

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
registers:

```python
from nontainer.wsgit import register_wsgit

register_wsgit(ws)                      # now the shell answers `ws-git`
ws.terminal("ws-git branch polish --paths auth.py")
ws.terminal("ws-git merge polish")      # ws-git help lists the rest
```

The rule: `ws.index` is the host's half and needs no switch — every
workspace whose backend has `caps.index` (kvgit does) has it. The
terminal `ws-git` exists only where `register_wsgit(ws)` has been
called, which no adapter does for you; until then the agent gets
`ws-git: command not found`. Both drive the same index, so host and
agent see one composition. [ws-git.md](ws-git.md) is the reference for
the agent's half — every verb, what it prints, and what it refuses.

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
`WorkspaceProvider` and bring your own — the protocol is in
[extending.md](extending.md).

## Where code runs (the `[dud]` extra)

The backend decides where state *lives*; the executor decides where
code *runs*, and the two are independent, because the versioning
semantics were always properties of the state layer and not of the
machine. The default is in-process. To run on a real machine instead,
hand the session a factory:

```python
from nontainer.executor_dud import DudExecutor

ws = workspace("user-42", executor_factory=lambda: DudExecutor())

# ... or the rung that needs no hypervisor, for dev and CI:
ws = workspace("user-42", executor_factory=lambda: DudExecutor(backend="subprocess"))
```

Same `terminal` / `run_python` tools, same commits, same O(1)
forks — [dud](https://github.com/ashenfad/dud) receives a tree,
executes against a real filesystem, and returns a diff, which the
provider commits exactly as it commits a local one. What you buy is
fidelity: C extensions, real subprocesses, sqlite on real files,
memory-mapped parquet — the workloads the in-process emulation serves
worst.

What you give up is the local rung's policy gating. A VM honours or
refuses a `PythonConfig`, never narrows it — a guest has no network
interface, so `network=True` raises at open rather than pretending —
and `backend="subprocess"` enforces none of it: real bash and real
Python with **no containment at all**, agent code running as you, with
your network and your files. It buys fidelity, not a boundary, which is
why it is opt-in. If you want policy gating, crash containment or
kernel defense-in-depth without a VM, use `LocalExecutor`, which is
what you already have.

The rungs, the constructors, what a VM refuses and what it quietly
changes are in the [API reference](api.md#executors).

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

Values an agent wants *shown* go in a `ui` dict: anything that can
cross as data stays as it is, and a live object that cannot — a plotly
figure, a DataFrame, a matplotlib figure, a PIL image — is written to
`<root>/ui/<name>.<ext>` and the binding names where it went. The
adapters append one line to the tool result naming what was written,
so the agent can embed it in its reply. The full contract —
`ArtifactPath`, `read_artifact`, the note grammar — is in
[api.md](api.md).

Tool exposure is automatic: a plain python environment gets ONE
`terminal` tool (with a `python` builtin); an augmented one (cache or
host objects) gets a separate `run_python` tool whose description
explains the magic. Override with `tools="terminal"` / `"split"`.

## Apps: the agent builds and verifies a web app

Agents author full-stack apps: a no-build frontend plus **request
handlers** — serverless semantics, not resident servers. A file's path
is its route (`/workspace/app/api/scores.py` → `/api/scores`), and its
exported `get` / `post` are the verbs.

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
> means the machine's own curl, not the workspace app.

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
walls enforce. `frontend_notes` states the approach and the libraries
and `static_assets` serves the bytes alongside the app; together they
are what a house design system rides on, and what makes an
**air-gapped** deployment work with no CDN in reach. Leave both unset
and agents get the built-in guidance — plain DOM first, Preact and
plotly from the CDN allowlist. Every field is in [api.md](api.md); why
each one exists is in [apps.md](apps.md).

Serving is read-only and concurrent. Mutable app state does **not** go
in the workspace — it goes to an external store (a sqlite/postgres
client) injected via `host_objects`, and you tell the agent about it
with a `python_primer`. A publication carries the tree and nothing
else, and a live sqlite handle is not a file, so the objects are handed
over at the open as above; the same keywords work on `store.tags.at`
and `store.resolve`. See the `webapp` example for the full pattern.

See [apps.md](apps.md) for the full design (handler contract, frozen
serving, threat model) and [api.md](api.md) for every signature.
