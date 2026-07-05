# The `[apps]` extra — design

> Status: design, pre-implementation. Decisions here extend the README's
> "App handlers" section; where they conflict, this doc wins.

## Goal

An agent authors a full-stack app inside its workspace — a no-build
frontend plus Python request handlers — and can **verify it headlessly**
before any human sees it. Embedders can optionally serve it live.

Serverless semantics throughout: there is no resident app process. A
"backend" is handler files on the (versioned) filesystem; requests are
dispatched into sandboxed executions on demand. No processes to babysit,
multi-tenancy reduces to routing, and the whole app — code and state —
forks/rolls back with the session.

## v1 scope

In: the dispatch core, the handler contract, a `curl` terminal builtin,
`test_app` via Playwright, a Starlette `APIRouter` for live serving.

Out (deliberately): websockets/SSE/streaming, background tasks,
middleware/auth hooks, `llm()` inside handlers, dynamic route segments
(`[id].py`), multi-file frontend bundling (esbuild/JSX — the no-build
HTM+Preact path only).

## App anatomy (convention over registration)

```
/app/index.html          ← entry; served at /
/app/*.js, *.css, ...    ← static assets, served as-is
/app/api/scores.py       ← handlers: routes /api/scores
/app/api/_lib.py         ← _-prefixed: importable, never routable
/app/logs/api.log        ← tracebacks + handler print() output
```

## Handler contract

File-based routing + verb exports (the Next.js/SvelteKit idiom):

```python
# /app/api/scores.py
def get(req):
    rows = json.loads(open('/data/scores.json').read())
    return {"scores": rows[: int(req.params.get("limit", 10))]}

def post(req):
    body = req.require("name", str)          # 400 on missing/wrong type
    ...
    return Response(status=201, body={"ok": True})
```

- **Request** is a frozen dataclass: `method`, `path`, `params`
  (query, str→str), `headers` (allowlisted subset), `body: bytes`,
  `json` (lazy parse), plus `require(name, type)` sugar → clean 400s.
  It is picklable data — it crosses the sandbox boundary as an input.
- **Liberal returns**: `dict`/`list` → JSON 200 · `str` → text/html by
  extension sniff · `bytes` → octet-stream · `Response(status=, body=,
  headers=)` for control · `raise HttpError(404, "msg")` for error
  paths. Anything else → 500 + logged.
- **Structural REST**: `get` handlers execute against a read-only
  filesystem view (`ReadOnlyFS`) — a GET that writes gets a
  `PermissionError`, which teaches the agent better than a style rule.
  Mutating verbs get normal staged writes.
- **Transactions**: a mutating handler that raises leaves nothing
  behind (staged writes discarded for that request). Successful writes
  fold into the session's normal commit flow — requests do NOT mint
  commits (per the README's commit-granularity decision). The serving
  layer checkpoints periodically / on quiesce with
  `info={"source": "api"}`.
- **App state guidance** (goes in the agent-facing description): tiny
  state → `cache` or JSON files (versioned, works on all backends);
  high-tempo or relational state → sqlite in a real directory — which
  requires the `dir` backend or a writable `Mount`, because `sqlite3`
  is a C extension that bypasses the virtual fs. This is a documented
  sharp edge, not a solvable one.

## Execution model (how a handler actually runs)

One core function, three consumers:

```
dispatch(ws, Request) -> Response
```

Dispatch resolves `/api/<name>` → `/app/api/<name>.py`, loads the file
source from the workspace fs, and executes it via the existing
`Workspace._exec_python` machinery (no checkpoint) with:

- the handler source prepended, the verb function invoked in a small
  trailer, `req` passed via the established `inputs=` channel
  (picklable dataclass), and the response captured via namespace-out
  (`__resp__` binding, filtered from agent-visible conventions);
- the same sandbox policy as `run_python` — handlers can do exactly
  what interactive agent code can do, nothing more (the symmetry rule);
- a per-request tick/timeout budget tighter than the interactive one
  (config: `AppsConfig.request_timeout`, `request_tick_limit`);
- stdout + tracebacks appended to `/app/logs/api.log` (the agent's
  repair loop is `tail`, edit, retry).

Handler executions hold the same per-workspace lock as tool calls —
serialized per session, by design (handlers are ms-scale).

Consumers:

1. **`curl` terminal builtin** (ships with `[apps]`, injected when the
   workspace has an `/app` dir or via config): `curl [-X POST] [-d body]
   /api/scores?limit=3` → dispatch → response rendered to the pipeline.
   The agent's fast inner loop; no browser, no server.
2. **`test_app`** (headless verify): Playwright intercepts ALL requests
   from a fresh browser context via `page.route` — static paths served
   from the workspace fs, `/api/*` through dispatch, external hosts
   default-denied with a small CDN allowlist (esm.sh, unpkg for HTM/
   Preact). The workspace IS the origin; no port, no server.
3. **Live serving** (embedder opt-in): a Starlette `APIRouter` mounted
   by the host app at `/apps/{token}/{path}`, resolving token →
   workspace via an embedder-supplied lookup. Same dispatch, same
   static serving.

## Namespace access from app code

Three tiers, three mechanisms (all reuse existing machinery):

1. **Handlers (backend) get the agent namespace by construction.**
   Dispatch runs through the same `_exec_python` path as `run_python`:
   same policy, same injected `cache` and `host_objects`. A handler
   calling `db.query(...)` or reading `cache['scores']` needs no new
   mechanism — the symmetry rule delivers it. Purity refinement: GET
   handlers get a **read-only cache view** to match their read-only
   filesystem (a GET that writes cache raises, same lesson).
2. **Host objects do real I/O naturally when they're C-backed.** An
   embedder-provided sqlite client in `host_objects` works against
   real files with no grant: C extensions bypass monkeyfs's
   Python-level patches. Caveat: *Python-level* `open()` inside a
   host object's methods runs while the patch context is active and
   hits the VFS — C-level I/O is the clean path. (Known gap: no
   per-host-object grant flags yet, parallel to `ModuleGrant`; add a
   `HostObjectGrant` if a pure-Python host resource needs real fs.)
3. **Frontends get NO framework bridge — they talk to agent-written
   handlers, period.** (Studio's `getCacheValue()` postMessage bridge
   existed because studio apps had no backend; that reason doesn't
   survive into a design where handlers are first-class.) An agent
   exposing cache data to its UI writes the two-line handler and
   thereby chooses *which* keys are visible, with what shaping — a
   deliberate API instead of a blanket cache-enumeration surface. No
   reserved routes, no exposure config, one fewer boundary to secure.

## test_app

Tool signature (exposed by adapters alongside terminal/run_python when
`[apps]` is installed and enabled):

```
test_app(actions: list[Action], viewport: str|dict = "desktop") -> TestAppResult
```

Actions (the studio DSL, pruned): `{"click": selector}`,
`{"type": [selector, text]}`, `{"read": selector}`, `{"eval": js}`,
`{"assert": js}`, `{"screenshot": true}`, `{"wait": ms}`.

- Playwright locators' auto-waiting replaces studio's idle heuristics;
  a `networkidle` settle runs after load and after each click/type.
- `TestAppResult`: per-action results, console messages, page errors,
  screenshots as PNG bytes (host-side; adapters write them to
  `/app/screenshots/` and return workspace paths in the observation —
  bytes never inline in model text; vision-capable harnesses can load
  the file).
- Result caps mirror `max_observation`; screenshot count capped per
  call.
- One shared Playwright browser per process, fresh context per call
  (contexts are ~10ms; isolation between tests for free).

## Live serving & multi-tenancy

- The router is an `APIRouter` the embedder mounts — nontainer never
  owns an app or a port.
- `{token}` is a capability: long, unguessable, minted by the embedder,
  distinct from session ids (which may be guessable). nontainer ships
  `mint_token()` sugar but the embedder owns the token→workspace map.
- Per-session serialization via the workspace lock; a bounded queue
  with 429 overflow (config: `queue_depth`).
- Quotas are config with enforced defaults: requests/min per session,
  request timeout, response size cap. Egress: handlers inherit the
  sandbox policy — no network unless the workspace's PythonConfig
  granted it (the kernel-degradation warning story applies unchanged).
- Threat framing for the docs: enabling live serving means anonymous
  HTTP can trigger agent-authored code under YOUR sandbox policy.
  The default posture (no network, workspace-only fs, tight budgets)
  makes that boring; every grant you add makes it less boring.

## Milestones

1. **M1 — dispatch + curl** (no new heavy deps): handler contract,
   Request/Response/HttpError, log file, curl builtin, conformance
   tests. The agent can build and test a backend entirely in-terminal.
2. **M2 — test_app** (`playwright` dep + `playwright install
   chromium`): route interception, action DSL, screenshots, adapter
   exposure.
3. **M3 — serving**: APIRouter, tokens, quotas. Ships last because
   it's the only piece with an anonymous-input threat model.

Each milestone is independently useful; M1 alone closes the
write→verify loop for backends, and M1+M2 close it for frontends.

## Open questions (resolve during M1/M2)

- Does the `python` builtin's script-semantics rule imply `curl` should
  also exist in split-tools mode as part of the terminal (yes, leaning:
  curl is shell-native and carries no namespace magic)?
- `Response` headers allowlist for live serving (CSP for served HTML
  in particular — probably a strict default CSP with esm.sh allowed).
- Whether `test_app` belongs on `Workspace` (like put/get) or only in
  adapters — leaning `Workspace.test_app()` for embedder parity.
