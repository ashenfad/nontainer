# App handlers (the `[apps]` extra)

The optional `[apps]` extra lets an agent author a full-stack app inside
its workspace — a no-build frontend plus Python request handlers — **verify
it headlessly** before any human sees it, and (optionally) serve it live.
This is the design and reference for that extra.

Serverless semantics throughout: there is no resident app process. A
"backend" is handler files on the (versioned) filesystem; requests are
dispatched into sandboxed executions on demand. No processes to babysit,
multi-tenancy reduces to routing, and the whole app — code and state —
forks/rolls back with the session.

## Scope

Supported: the dispatch core, the handler contract, a `curl` terminal
builtin, `test_app` via Playwright, a Starlette `APIRouter` for live
serving.

Deliberately out of scope: websockets/SSE/streaming, background tasks,
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
  commits (see the commit-granularity note in the
  [design notes](design.md)). The serving layer checkpoints
  periodically / on quiesce with `info={"source": "api"}`.
- **App state guidance**: tiny state → `cache` or JSON files
  (versioned, works on all backends);
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

## Frontend tooling

Structural constraint first: termish commands are pure-Python over the
`FileSystem` protocol — **external binaries (esbuild, node) cannot see
a virtual filesystem**. That wall sorts the options:

Supported (all zero-machinery — conventions in the app template that
ships in the tool description, plus the CDN allowlist):

1. **HTM + Preact, no build** — `import from 'https://esm.sh/preact'`
   + `html\`...\`` templates. The default idiom.
2. **Vanilla ESM + import maps** — multi-file module structure with
   bare specifiers mapped to esm.sh in `index.html`. Just modern JS.
3. **JSX via Babel-standalone** — `<script type="text/babel"
   data-type="module">`; the browser transpiles at load. Real JSX
   (the most-trained frontend idiom) with zero server tooling. ~2MB +
   transpile-at-load is irrelevant at agent-app scale. This is the
   answer to "agents keep writing JSX": let them.

Deliberately out of scope:

- **esbuild as a termish command** — needs real files; viable later
  as an opt-in injected command restricted to the `dir` backend or a
  writable `Mount` ("external binaries need real files" — the same
  rule as sqlite app state). The materialize-shuttle variant (export
  /app to a temp dir, build, re-import dist/) is explicitly rejected:
  mostly-works complexity of exactly the kind this design keeps
  killing.
- **Node toolchains** (vite/npm) — same verdict as run-ts; deferred.

The insight: agents don't need build *tooling*, they need build
*semantics* — and at agent-app scale the browser supplies those
itself. test_app is indifferent to all of this; it serves whatever is
under /app.

## Namespace access from app code

Three tiers, three mechanisms (all reuse existing machinery):

1. **Handlers (backend) get the agent namespace by construction.**
   Dispatch runs through the same `_exec_python` path as `run_python`:
   same policy, same injected `cache` and `host_objects`. A handler
   calling `db.query(...)` or reading `cache['scores']` needs no new
   mechanism. Purity refinement: GET handlers get a **read-only cache
   view** to match their read-only filesystem (a GET that writes cache
   raises, same lesson).
2. **Host objects do real I/O naturally when they're C-backed.** An
   embedder-provided sqlite client in `host_objects` works against
   real files with no grant: C extensions bypass monkeyfs's
   Python-level patches. Caveat: *Python-level* `open()` inside a
   host object's methods runs while the patch context is active and
   hits the VFS — C-level I/O is the clean path. (Known gap: no
   per-host-object grant flags yet, parallel to `ModuleGrant`; add a
   `HostObjectGrant` if a pure-Python host resource needs real fs.)
3. **Frontends get NO framework bridge — they talk to agent-written
   handlers, period.** A blanket "read a cache key from the frontend"
   bridge only makes sense when apps have no backend; here handlers are
   first-class, so it's unnecessary. An agent exposing cache data to
   its UI writes the two-line handler and
   thereby chooses *which* keys are visible, with what shaping — a
   deliberate API instead of a blanket cache-enumeration surface. No
   reserved routes, no exposure config, one fewer boundary to secure.

## test_app

Tool signature (exposed by adapters alongside terminal/run_python when
`[apps]` is installed and enabled):

```
test_app(actions: list[Action], viewport: str|dict = "desktop") -> TestAppResult
```

Actions: `{"click": selector}`, `{"type": [selector, text]}`,
`{"read": selector}`, `{"eval": js}`, `{"assert": js}`,
`{"screenshot": true}`, `{"wait": ms}`.

- Waiting is two-tier. Playwright's idiom is OUTCOME-based — web-first
  assertions that retry — and the `assert` action follows it
  (`wait_for_function`, retry until truthy or ~2s). But
  expectation-free `read` observations have no outcome to retry
  against (`networkidle` is sticky post-navigation and discouraged
  upstream), so click/type settle via an idle-gap heuristic: track
  in-flight requests, wait for a 300ms quiet gap, capped. Slow apps
  use `{"wait": ms}`. Prefer `assert` over `read`-and-check when a
  condition is known — it's the robust form.
- `TestAppResult`: per-action results, console messages, page errors,
  screenshots as PNG bytes (host-side; adapters write them to
  `/app/screenshots/` and return workspace paths in the observation —
  bytes never inline in model text; vision-capable harnesses can load
  the file).
- Result caps mirror `max_observation`; screenshot count capped per
  call.
- **One shared Chromium per process, a fresh context per call.** Sync
  Playwright pins a browser to one thread, so instead we run *async*
  Playwright on a dedicated loop-thread and marshal every call to it
  (`nontainer.apps.browser`). That means many sessions verify
  concurrently on one browser — a context per concurrent test, not a
  browser per test — so memory scales with concurrency, not with
  sessions. A semaphore bounds concurrent contexts (default 8;
  `configure_browser(max_concurrent=…)`). The browser is lazy-launched,
  relaunched transparently if Chromium crashes, and torn down at exit.
  The route dispatch is synchronous, so it's hopped off the browser
  loop into a thread and serialized per workspace, so a page's parallel
  fetches never reenter the sandbox.

## Delivery (where nontainer's concern ends)

nontainer's delivery surface is exactly: the `/app` convention, the
dispatch function, the mountable `APIRouter`, and the token shape.
Hosting, TLS, domains, user auth, deploy targets — the harness's.
Composable paths that already exist with no new API:

- **Export**: `tar -czf app.tgz app` in the terminal + `ws.get(...)`
  — a frontend-only app is deliverable to any static host today.
  (No "freeze the API into static JSON" export: a degraded copy of
  an app masquerading as the app — rejected for the usual reason.)
- **Share-by-URL**: mount the router, hand out the capability URL.

The ONE delivery opinion nontainer owns, because it must be baked
into authoring: **apps are relocatable**. They are served under an
arbitrary prefix (`/apps/{token}/`), so the convention mandates
relative URLs — `fetch('api/scores')`, never `/api/scores`; relative
asset paths — and `test_app` serves under a synthetic prefix so
violations fail during verification, not at delivery.

## Live serving: frozen snapshots

Serving is **read-only by design.** The agent authors an app in its
mutable workspace; to share it, you publish a **frozen snapshot** — a
Workspace pinned to a commit — and the router serves that. Handlers may
READ the workspace and call injected `host_objects` (a read-only
telemetry client, say), but they cannot mutate the VFS: a write attempt
is a 500. Mutable app state belongs in an **external store** reached
through `host_objects` (a sqlite/postgres client), not the served VFS —
at which point you've graduated from "shared dashboard" to "small real
app," and the store owns its own concurrency.

Because nothing mutates, serving is simple and concurrent:

```python
from nontainer.apps import build_router, mint_token

router = build_router(
    resolve,                    # (token) -> read-only Workspace @ commit | None
    rate_limit_per_min=120,
    max_snapshots=64,           # benign LRU cache of snapshots
    on_log=None,                # handler stdout/errors sink (default: logging)
)
app.mount("/apps", router)      # serves /apps/{token}/...
```

- **Concurrent, no per-session lock.** Each request runs on a *fresh
  read-only sandbox*, so requests to one snapshot run in parallel — no
  staged buffer, no checkpointing, no durability surface. This is what
  the frozen guarantee buys.
- **Snapshots are a benign cache.** `max_snapshots` bounds residency;
  eviction just closes a read-only workspace (lossless — there's
  nothing dirty to persist).
- **`{token}` is a capability** — long, unguessable, minted with
  `mint_token()`, mapped to snapshots in the embedder's storage.
- **Logs go off the VFS** (it's read-only): `on_log` receives handler
  stdout/errors, defaulting to the `nontainer.apps` logger.
- **Static requests are confined** (unchanged): `.`/`..` collapse, the
  path must stay under `/app/`, and `/app/api/` is never served as a
  file — so backend source and workspace internals can't leak.
- **Threat framing:** anonymous HTTP triggers agent-authored code under
  your sandbox policy. The default posture keeps it boring — read-only
  VFS, no network unless the PythonConfig granted it, per-request
  budgets, a rate limit, and a strict-ish CSP on served HTML.

## Known gaps

- **No per-host-object fs grant.** Pure-Python host resources that need
  the real filesystem have no flag yet (parallel to `ModuleGrant`); a
  `HostObjectGrant` would fill it. C-backed clients (sqlite) already do
  real I/O and don't need one.
- **Served-HTML CSP.** `Response(headers=)` sets per-response headers
  today; a strict default Content-Security-Policy for served HTML (with
  the esm.sh CDN allowed) is not yet baked in.
- **App state on a virtual filesystem.** Relational / high-tempo state
  wants sqlite, a C extension that bypasses the virtual fs — so it needs
  the `dir` backend or a writable `Mount`. A documented sharp edge, not
  a solvable one.
