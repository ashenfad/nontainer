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
serving, and embedder-supplied static assets served alongside the app
(`AppsConfig.static_assets` — vendored libraries, fonts; the air-gap
path).

Deliberately out of scope: websockets/SSE/streaming, background tasks,
middleware/auth hooks, `llm()` inside handlers, dynamic route segments
(`[id].py`), multi-file frontend bundling (esbuild/JSX — the no-build
HTM+Preact path only).

## App anatomy (convention over registration)

```
/workspace/app/index.html          ← entry; served at /
/workspace/app/*.js, *.css, ...    ← static assets, served as-is
/workspace/app/api/scores.py       ← handlers: routes /api/scores
/workspace/app/api/_lib.py         ← _-prefixed: importable, never routable
/workspace/app/logs/api.log        ← one line per request, + tracebacks
                                     and handler print() output
```

## Handler contract

File-based routing + verb exports (the Next.js/SvelteKit idiom):

```python
# /workspace/app/api/scores.py
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
  paths. Anything else → 500 + logged. Response headers are
  allowlisted on the way out (content/caching metadata plus `x-*`); see
  the CSP section below for what is dropped and why.
- **Structural REST (authoring)**: `get` handlers execute against a
  read-only filesystem view (`ReadOnlyFS`) — a GET that writes gets a
  `PermissionError`, which teaches the agent better than a style rule.
  During *authoring* (curl / test_app) mutating verbs get staged writes,
  atomic per request (a raise discards them). But **serving is frozen**
  (see below): a served handler is read-only regardless of verb, so a
  VFS write is always a 500.
- **App state guidance — this is the load-bearing convention.** The
  workspace (`cache`, files) is **not** the app's database. It's the
  agent's authoring scratchpad, and the served snapshot is read-only, so
  anything a served app must *remember or share* has to live in an
  **external store injected via `host_objects`** — a sqlite/postgres
  client the handlers call. `cache`/files are for single-session,
  authoring-time state only; shared mutable state goes to the store,
  which owns its own concurrency (serving is lock-free). Tell the agent
  about the injected store with a **primer** (see the adapters). The
  `webapp` example shows the whole pattern.

## Execution model (how a handler actually runs)

One core function, three consumers:

```
dispatch(ws, Request) -> Response
```

Dispatch resolves `/api/<name>` → `/workspace/app/api/<name>.py`, loads the file
source from the workspace fs, and executes it via the workspace's
extension surface (`Workspace.exec_python`, no checkpoint) with:

- the handler source prepended, the verb function invoked in a small
  trailer, `req` passed via the established `inputs=` channel
  (picklable dataclass), and the response captured via namespace-out
  (`__resp__` binding, filtered from agent-visible conventions);
- the same sandbox policy as `run_python` — handlers can do exactly
  what interactive agent code can do, nothing more (the symmetry rule);
- a per-request tick/timeout budget tighter than the interactive one
  (config: `AppsConfig.request_timeout`, `request_tick_limit`);
- stdout + tracebacks appended to `/workspace/app/logs/api.log` (the agent's
  repair loop is `tail`, edit, retry). Every `/api` request also logs a
  `METHOD path -> status` line, and the file opens with a header
  explaining the format. An empty log reads the same as a broken one to
  an agent tailing it — *logging is broken* rather than *nothing has
  errored* — so both records exist to make silence informative. The
  header is written when the log is first created rather than at
  `enable_apps`, because pre-creating it would materialize
  `<root>/app` before the agent has built anything, and embedders
  answer "is there an app yet?" with `isdir(<root>/app)`. Static
  assets are not logged; they would bury the tracebacks.
- request lines from READ-ONLY requests buffer in memory while the
  workspace is clean, and flush when writing is free (the workspace is
  dirty anyway), when a diagnostic is written, or on an explicit
  `AppRuntime.flush_log()`. This is not an optimization: per-request
  atomicity below is gated on `not ws.dirty`, so a log line written
  during a GET would silently disable handler rollback for the next
  mutating request — and page-GET-then-POST is the common order. The
  runtime cannot claim the dirt as its own and roll back anyway,
  because `discard()` is all-or-nothing at the provider level and the
  protocol exposes only a boolean: "my log line" is indistinguishable
  from a screenshot written mid-run. `curl` and `test_app` flush when
  they finish — both run inside a tool call that checkpoints anyway,
  and both are followed by the agent reading the log.

Handler executions hold the same per-workspace lock as tool calls —
serialized per session, by design (handlers are ms-scale).

Consumers:

1. **`curl` terminal builtin** (ships with `[apps]`, injected when the
   workspace has an `/workspace/app` dir or via config): `curl [-X POST] [-d body]
   /api/scores?limit=3` → dispatch → response rendered to the pipeline.
   The agent's fast inner loop; no browser, no server.
2. **`test_app`** (headless verify): Playwright intercepts ALL requests
   from a fresh browser context via `page.route` — static paths served
   from the workspace fs, `/api/*` through dispatch, external hosts
   default-denied except `AppsConfig.script_hosts` (default: esm.sh
   and friends for HTM/Preact). The workspace IS the origin; no port,
   no server.
3. **Live serving** (embedder opt-in): a Starlette `APIRouter` mounted
   by the host app at `/apps/{token}/{path}`, resolving token →
   workspace via an embedder-supplied lookup. Same dispatch, same
   static serving.

## Frontend tooling

Structural constraint first: termish commands are pure-Python over the
`FileSystem` protocol — **external binaries (esbuild, node) cannot see
a virtual filesystem**. That wall sorts the options:

What the DEFAULT `frontend_notes` block offers, for an embedder that
vendors nothing and can reach the CDN allowlist. An embedder that ships
its own stack replaces this wholesale — including which of these to
prefer, which is why the list is no longer phrased as a ranking.

1. **HTM + Preact, no build** — `import from 'https://esm.sh/preact'`
   + `html\`...\`` templates. What the built-in block teaches.
2. **Vanilla ESM + import maps** — multi-file module structure with
   bare specifiers mapped to esm.sh in `index.html`. Just modern JS.
   An import map can also be injected by a loader script before it
   imports, so a page need not carry one.
3. **JSX transpiled in the browser** — real JSX, the most-trained
   frontend idiom, with zero server tooling.

   Two things about (3) are measured rather than assumed, because the
   obvious approach fails in a way nothing reports. `@babel/standalone`
   is ~3.1MB, and its `transformScriptTags` entry point compiles to a
   **Blob** loaded as a module script — which the served CSP refuses
   (`script-src` has no `blob:`), *silently*, since a refused script
   does not throw. Inject the compiled source INLINE instead. And a
   line-preserving transpiler (sucrase, ~200KB) keeps `Error.stack`
   pointing at the agent's own JSX line, which test_app then quotes;
   add `//# sourceURL=app.jsx` so the frame names the file at all
   (`sourceMappingURL` does not reach `Error.stack`).

Deliberately out of scope:

- **esbuild as a termish command** — needs real files; viable later
  as an opt-in injected command restricted to the `dir` backend or a
  writable `Mount` ("external binaries need real files" — the same
  rule as sqlite app state). The materialize-shuttle variant (export
  /workspace/app to a temp dir, build, re-import dist/) is explicitly rejected:
  mostly-works complexity of exactly the kind this design keeps
  killing.
- **Node toolchains** (vite/npm) — same verdict as run-ts; deferred.

The insight: agents don't need build *tooling*, they need build
*semantics* — and at agent-app scale the browser supplies those
itself.

test_app used to be indifferent to how that happened, and is not any
more: it sends the served CSP, so an approach that only works without
one now fails verification instead of passing and breaking published.
That is the point, and it is why the blob caveat above is a blocker
rather than a preference.

### Vendored assets: files served with the app, absent from the workspace

`AppsConfig.static_assets` maps a URL prefix to a host directory:

```python
config = AppsConfig(static_assets={"vendor": "/srv/appassets"})
runtime = enable_apps(ws, config)
app.mount("/apps", build_router(resolve, config=config))   # the SAME config
```

`/srv/appassets/mui.js` then serves at `vendor/mui.js`, for the agent's
`curl`, for test_app, for the live preview, and for a published
snapshot — all four go through `dispatch`, so one declaration covers
them. This is the air-gap answer, and the way to put a house component
library in front of an agent.

**It is deliberately not a `Mount`.** A mount puts files in the
workspace, where the agent can read them and a remote executor ships
them to its guest. These bytes are not workspace state: they are fixed,
identical across every session, authored by nobody in the loop, and
consumed by the browser at request time. The right analogy is the
backend one — **static assets are to the browser what `host_objects`
are to handlers**: an embedder-supplied capability reached at request
time, outside the versioning plane. So they cost nothing in commits or
forks, never enter a guest tree, and a published snapshot needs no copy.

What follows from that:

- **The agent cannot `ls`, read, or edit them**, and is told so in the
  apps notes — a sentence derived from `static_assets` itself, not
  hand-written into `apps_primer`, so the two cannot drift. It *can*
  request one (`curl vendor/lib.js | head -c 300`), which is enough to
  confirm a bundle is really there.
- **No `script_hosts` entry is needed.** Vendored assets are
  same-origin, and `'self'` is always allowed by the served CSP. Adding
  a host for them would loosen the supply-chain pin for nothing.
- **Assets win over a workspace file at the same path**, and the
  collision is noted in `api.log` rather than shadowed silently — an
  agent that writes `app/vendor/lib.js` and sees no change would
  otherwise debug the app.
- **They are exempt from `max_response_bytes`.** That cap catches a
  handler returning something runaway; an asset's size is a decision the
  embedder already made, and a charting bundle clears the 2MB default on
  its own.
- **Readable ≠ servable.** A minified bundle teaches an agent nothing,
  so if it needs to *understand* a private library, ship a curated API
  surface — a components manifest, a props cheatsheet, a working example
  page — as a seeded skill, where reference files are working files to
  copy. A few KB that helps, instead of a few MB that doesn't.
- The two mechanisms compose: an embedder wanting the real source
  readable can declare `static_assets` *and* mount the unminified
  source.

The one obligation: declare it on the **one** `AppsConfig` passed to
both `enable_apps` and `build_router`. Assets present while authoring
and missing while serving are an app that verifies green and 404s
published — see the delivery note below.

**And tell the agent where the libraries are** — `static_assets` puts
the bytes in place, `frontend_notes` says they exist. See below; a
vendored stack the prompt still describes as living on a CDN is only
half a deployment.

### Frontend libraries: `frontend_notes` (the embedder's, not ours)

Once an embedder can supply the libraries, the prompt can no longer
hardcode which approach to take or where the code comes from.
`AppsConfig.frontend_notes` is the one block that states **the default
frontend choice and its supply** — which approach to reach for, which
libraries exist, and how to import them — and it **replaces** rather
than appends:

```python
AppsConfig(
    static_assets={"vendor": "/srv/appassets"},
    frontend_notes=(
        "Charts: <script src='vendor/plotly.min.js'></script> (window.Plotly).\n"
        "Components: import { h, render } from 'vendor/preact.mjs'."
    ),
)
```

- `None` (default) keeps the built-in block — *plain DOM is the most
  reliable choice*, Preact/htm from esm.sh, plotly from jsdelivr. That
  is the right answer when nothing is vendored and the allowlist is
  reachable, so a plain install sees no change.
- `""` omits it, for an embedder that would rather say nothing.
- A string replaces it. Import
  `nontainer.adapters.render.DEFAULT_FRONTEND_NOTES` to extend the
  default instead of discarding it.

**Why replace, not append.** `apps_primer` appends, which is right for
additive guidance and wrong here: the built-in block says *copy this
known-good pattern exactly* and names a CDN. An embedder correcting it
from below would leave the wrong instruction both first and more
emphatic. For an air-gapped deployment the agent would follow it, and
the block that fails is precisely the one it was told to trust.

**What stays in the template regardless:** relative URLs, and the rule
against swapping a named import for a `<script src>` build or a guessed
global. Those describe the shape of the code rather than which approach
to take, they are true wherever the bytes come from, and agents get them
wrong often enough that no embedder should be able to drop them by
accident.

*"Plain DOM is the most reliable choice"* is **not** in that set, though
it reads like it. It is a default-CHOICE opinion, written when the only
alternative was Preact over a CDN — and for an embedder that vendors a
component library and wants every app to look like it came from the same
place, it is precisely wrong. A prompt cannot both recommend plain DOM
and steer at a design system; the emphatic sentence wins, and it should
be the embedder's.

The anti-guessing rule in particular belongs in the template *because*
of this seam, not despite it: a vendored `vendor/mui.js` gives an agent
no CDN URL to anchor on, so reaching for `window.MUI` from memory gets
more likely, not less. The guidance must not travel with the block an
embedder replaces.

Air-gapped deployments will usually also set `script_hosts=()`, which
states the rule positively — *scripts may load only from this app
itself* — rather than printing an empty allowlist.

### Script hosts: one declaration, four surfaces

`AppsConfig.script_hosts` is the single statement of where browser
scripts may load from. Everything that used to be hand-synced derives
from it: test_app's request interception, the served-HTML CSP's
`script-src` (`serve.build_csp`), the allowlist sentence in the
agent-facing apps notes, and curl's external-URL error. What verifies
headlessly, what serves published, and what the agent is *told* cannot
disagree.

**test_app sends the served policy, it doesn't just mimic it.**
Interception reproduces a CSP's *origin* rules faithfully, and for a
long time that was taken as equivalent. It isn't: a CSP also governs
*behaviour* — `eval`, `new Function`, blob workers, blob module
scripts — and none of that involves a request there is anything to
intercept. So test_app also sets the real `Content-Security-Policy`
header on served HTML, derived from `AppsConfig.csp` (which defaults to
`build_csp(script_hosts)`).

The case that forced it: `Babel.transformScriptTags()` — the obvious
entry point for browser-side JSX — compiles to a **blob** and loads it
as a module script. Under interception that passes; under the served
policy `script-src` has no `blob:` and it is refused. Worse, a refused
script does not throw, so a page-level `try`/`catch` sees nothing and
`page_errors` stays empty. The app verified green and was silently
broken once published.

Declare the policy on the config rather than only on
`build_router(csp=…)`: verification reads the config, so a policy passed
only to the router is one test_app never sees. `csp=""` disables it in
both places.

**`blob:` is allowed for images and media, and refused for code.** The
default policy carries `img-src 'self' https: data: blob:` and the same
list on `media-src`. A blob URL names bytes the page already holds, in
this document and on this origin — it reaches no other origin's data,
and as an image or a video it is *displayed*, never executed, which is
the risk class `data:` already had on those directives. Charting
libraries need it: plotly rasterizes by drawing a Blob-backed `<img>`
onto a canvas, so without it the modebar's *download as png* and
`Plotly.toImage()` fail, and only once published — an image violation
is a warning during verification, not a failure. A blob-loaded script or
worker is the opposite case, code from a source no allowlist can name,
so `script-src` still has no `blob:` and no `worker-src`/`child-src` is
added (they fall through to `default-src 'self'`).

**`csp_extend` widens a directive; `csp` replaces the policy.**
`AppsConfig.csp_extend={"connect-src": ("http://api.internal",)}` appends
those sources to the derived policy, or adds a directive it does not
name at all. It cannot remove a source — a policy that must be *tighter*
is written out whole in `csp`, and setting both raises. The reason to
prefer it over a copied policy is the same one that put the CSP behind
`script_hosts`: a verbatim string is a snapshot, and it silently misses
whatever the derived policy gains later (`'wasm-unsafe-eval'` was such a
gain). The case it exists for is an intranet deployment where the
browser is the only network path an app has — handlers have no network —
and the tile server or API answers on plain `http://`, which
`connect-src 'self' https:` refuses.

**The configured policy is the one that goes on the wire.** A handler
returning its own `Content-Security-Policy` header does not get it
served — contained code does not choose its own containment. That is
one case of a general rule: handler response headers are allowlisted
the way request headers are. `content-type`, `cache-control`, `vary`,
`etag`, `last-modified`, `content-disposition`, `location`, and any
`x-*` custom header reach the browser; everything else is dropped,
because the rest grant privileges on the *embedder's* serving origin —
`set-cookie` plants a cookie there, `access-control-allow-*` hands
other origins read access to responses the embedder isolated, and
`x-frame-options` decides embedding. test_app applies the same filter,
so an app cannot verify against a header it will never be served. An
embedder who needs one of those wraps the mountable router in
middleware of their own; there is no config flag, which is what keeps
the guarantee structural.

Two edges of that rule are worth naming. `vary` is allowed because it
cannot be split from `cache-control`: a handler may vary its output on
an allowlisted request header (`authorization`, or an `x-*` tenant id),
and a cacheable response that does not say what it varied on lets a
shared cache key on the URL alone and hand one caller's variant to the
next. And the `x-*` allowance is not blanket — that namespace also
carries commands a server in front *executes* rather than forwards, so
`x-accel-*` (nginx), `x-sendfile` and `x-lighttpd-send-file` are
refused; without that, a handler could reach an internal location
through the proxy. That list covers the conventions that exist rather
than every one that could, which leaves an obligation on whoever
deploys this — see **Hosting for real (the embedder's half)**.

A violation is reported in `[rejected requests]` phrased as the fix, and
an external script the allowlist doesn't cover keeps the allowlist
wording it always had — the browser now refuses it before interception
can, and enforcing a policy must not *downgrade* a diagnostic.

`AppsConfig.apps_primer` is embedder guidance appended to those notes —
the place to teach a private component library's known-good import
block, in the same copy-this-exactly style as the built-in Preact
pattern:

```python
config = AppsConfig(
    script_hosts=(*DEFAULT_SCRIPT_HOSTS, "esm.corp.internal"),
    apps_primer=(
        "House design system: import { Button, DataGrid } from "
        "'https://esm.corp.internal/@acme/design-system@3' — "
        "copy this import exactly."
    ),
)
runtime = enable_apps(ws, config)
```

A private npm registry (Artifactory etc.) is not directly usable here:
registries serve package *tarballs* (CJS, bare specifiers), not
browser-loadable ES modules. The working pattern is a self-hosted
esm.sh instance configured with the private registry as its upstream,
added to `script_hosts` as above — or prebuilt ESM bundles vendored
into the workspace (`/vendor/lib.js`), which needs no config at all
since `'self'` is always allowed.

Air-gapped deployments, where agents' trained reflexes point at public
hosts that don't resolve, are a designed-for-later shape (a
`script_mirrors` host→mirror map: test_app reroutes intercepted
requests; served HTML gets an injected import map remapping the URL
prefixes). Where the deployment can manage split-horizon DNS plus an
internal CA, that solves it below nontainer with no config at all —
only the test_app browser's CA trust needs care.

## Namespace access from app code

Three tiers, three mechanisms (all reuse existing machinery):

1. **Handlers (backend) get the agent namespace by construction.**
   Dispatch runs through the same `exec_python` path as `run_python`:
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
`{"select": [selector, value]}`, `{"read": selector}`, `{"eval": js}`,
`{"assert": js}`, `{"goto": "about.html"}`, `{"screenshot": true}`,
`{"wait": ms}`.

- Waiting is two-tier. The idiom is OUTCOME-based — assertions that
  retry — and the `assert` action follows it, polled from the harness
  with `page.evaluate` until truthy or ~2s. (NOT Playwright's
  `wait_for_function`: it installs its poller into the page, which
  needs `'unsafe-eval'` — withheld by the CSP test_app now sends, so
  every retry died and a correct app that settles asynchronously
  failed. The deadline bounds each evaluation, since `evaluate` awaits
  a promise the expression returns.) But
  expectation-free `read` observations have no outcome to retry
  against (`networkidle` is sticky post-navigation and discouraged
  upstream), so click/type — and `read` itself, before observing —
  settle via an idle-gap heuristic: track in-flight requests, wait for
  a 300ms quiet gap, capped (`settle_cap`, default 5s). A settle that
  exits via the cap attaches a stale-risk note to that action's result
  instead of silently passing — false-green is the failure an agent
  can't catch. Slow apps use `{"wait": ms}`. Prefer `assert` over
  `read`-and-check when a condition is known — it's the robust form
  (no heuristic can wait for a fetch that hasn't *started* yet; retry
  semantics can).
- `select` is separate from `type` because they are separate
  Playwright calls: `fill()` raises on a `<select>`. It matches the
  option by value, then by visible label — agents pass whichever the
  DOM showed them — and `type` aimed at a `<select>` names `select` in
  its error rather than leaving the agent to rediscover
  `dispatchEvent(new Event('change'))`.
- Repeated console lines collapse to one entry with an `(xN)` count.
  A chatty CDN warning otherwise crowds out the console tail, which is
  what an agent actually reads.
- **A page error is attributed to the agent's own code, and quotes it.**
  A stack is mostly somebody else's: with a component library in play
  the top frame is deep inside a bundle and the actionable line is
  below it, so reporting the *first* frame reports the least useful
  one. test_app picks the first frame in a file the agent authored,
  says how many it skipped, and prints that source line:

  ```
  TypeError: svae is not a function (at Dashboard (app.js:42:13), +4 frames above it in library code)
       42 | <Button onClick={svae}>Save</Button>
  ```

  Frames are classified against what is actually being served: the
  synthetic test_app origin and bare `//# sourceURL=` names resolve to
  workspace files; a declared `static_assets` prefix or a third-party
  host is *library* code; `blob:`/`data:`/eval is *generated* code with
  no file to open. An inline `<script>` reports the document URL, which
  resolves to `index.html` — the common case in a first app.

  When no frame is the agent's, it says so (`no frame in your own files
  — all 6 frames are in library code`) instead of printing a location
  from a bundle. Same rule as the parse-error branch: a misleading
  diagnostic is worse than an absent one, because it sends the repair
  loop somewhere the agent cannot fix.
- `TestAppResult.ok` is: loaded, no action errored, no assert falsy, no
  CSP violation stopped code running, and no request used an **absolute
  url**. A refused image or font is a warning; refused *code* is a
  failure, and so is a relocatability bug — the harness 404s an absolute
  path with a JSON body, so an app that calls `.json()` without checking
  `.ok` renders as if fine while being broken in production.
- **`read` and `eval` both settle before observing.** They are the two
  expectation-free observations, and a stale answer from either is a
  false green the agent cannot catch.
- **A failed action captures the page.** The run stops there, so it is
  the last look available; without it the agent re-runs the whole test
  to add a screenshot. A selector that missed also gets the ids and
  `data-key`s actually present — Playwright says only what it waited
  for, which leaves an agent re-guessing blind.
- `TestAppResult`: per-action results, console messages, page errors,
  screenshots as PNG bytes (host-side; adapters write them to
  `/workspace/app/screenshots/` and return workspace paths in the observation —
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

nontainer's delivery surface is exactly: the `/workspace/app` convention, the
dispatch function, the mountable `APIRouter`, and the token shape.
Hosting, TLS, domains, user auth, deploy targets — the harness's. Some
of those are not merely yours to choose but load-bearing for the
guarantees above: **Hosting for real (the embedder's half)**, below,
says which and why. Composable paths that already exist with no new API:

- **Export**: `tar -czf app.tgz app` in the terminal + `ws.get(...)`
  — a frontend-only app is deliverable to any static host today.
  (No "freeze the API into static JSON" export: a degraded copy of
  an app masquerading as the app — rejected for the usual reason.)
- **Share-by-URL**: mount the router, hand out the capability URL.

`static_assets` adds one obligation to both paths, the same class as
`host_objects` and CSP rather than a new one: the assets live on the
host, not in the snapshot, so a `tar -czf app.tgz app` export does not
contain them and a served snapshot needs the router configured with the
same `AppsConfig`. An app that verifies green against assets the serving
side never declared is the one failure `test_app` cannot catch.

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

Because a frozen snapshot is immutable, serving is **stateless** — the
router keeps nothing:

```python
from nontainer.apps import build_router, mint_token

router = build_router(
    resolve,          # (token) -> read-only Workspace @ commit | None
    on_log=None,      # handler stdout/errors sink (default: logging)
)
app.mount("/apps", router)      # serves /apps/{token}/...
```

- **Stateless: `resolve → dispatch`.** Each request calls `resolve` and
  dispatches on a read-only sandbox of its own — no session cache, no
  residency, no lifecycle. `resolve` is called per request and its
  result is NOT closed by the router; if resolving is expensive, cache
  the read-only Workspace *inside* `resolve` (safe — it's immutable).
- **Concurrent, no per-session lock.** A sandbox per in-flight request →
  no staged buffer, no shared instance to race, no durability surface.
  This is what the frozen guarantee buys. Cheap when `resolve` caches
  its Workspace: the built policy is memoized, and under
  `isolation="process"`/`"kernel"` a worker is kept resident (see
  `PythonConfig.warm_view_workers`) rather than started per request — so a
  request costs neither policy registration nor a worker start.
  Requests beyond `warm_view_workers` fall back to a per-call sandbox rather
  than queueing, so **raise it toward your concurrency if you serve
  under load** — the default of `1` is sized for the build-and-preview
  loop, not for traffic. Two corollaries: concurrency, not the cache
  size, decides how many workers exist at once (so bound concurrent
  traffic at your edge if worker memory matters), and what stays warm
  afterwards is never reaped.

  **Prefer `preload_grants=True` with `warm_view_workers=0` where your
  grants allow it.** Preloading puts the granted stack in the forkserver
  broker, which drops a worker start to ~14ms — cheap enough to give
  *every* request a pristine worker. That is the simpler system and the
  better-behaved one: nothing stays resident, so there is no warm set to
  size and no memory floor to reason about, and each handler call gets a
  clean process rather than inheriting `sys.modules` and module globals
  from whatever ran before it (see the reuse caveat under
  `PythonConfig.warm_view_workers`).

  The cache exists for when that isn't available: `preload_grants` runs
  your grants' import-time code in the broker, so it is unsafe for a
  grant that starts threads on import, and without it a per-call worker
  costs ~235ms with a heavyweight stack. In that case keep a warm set
  and size it to your concurrency.
- **`{token}` is a capability** — long, unguessable, minted with
  `mint_token()`, mapped to snapshots in the embedder's storage.
- **Logs go off the VFS** (it's read-only): `on_log` receives handler
  stdout/errors, defaulting to the `nontainer.apps` logger.
- **Static requests are confined**: `.`/`..` collapse, the path must
  stay under `/workspace/app/`, and `/workspace/app/api/` is never served as a file — so
  backend source and workspace internals can't leak.
- **Rate limiting / quotas are edge concerns** — put them at your
  gateway; the router doesn't presume to.
- **Threat framing:** anonymous HTTP triggers agent-authored code under
  your sandbox policy. The default posture keeps the BACKEND boring —
  read-only VFS, no network unless the PythonConfig granted it,
  per-request budgets. The browser side is only as isolated as the
  origin you serve it from, which is yours to choose: see below.

## Hosting for real (the embedder's half)

Everything above describes what the library does. This describes what it
assumes you did, because several of its defaults are sound only under a
deployment condition it cannot see from the inside.

Read this before serving an app to anyone but yourself. A local testbed
that mounts the router next to its own API is fine as a testbed and is
not the shape to copy.

### Who owns what

**nontainer guarantees**, and an app cannot opt out:

- the CSP on the wire is the one you configured — a handler returning
  its own is dropped
- response headers are allowlisted, so an app cannot set a cookie on
  your origin or grant other origins read access to its responses, and
  cannot reach a proxy in front through the command headers that have a
  known convention (`x-accel-*`, `x-sendfile`, `x-lighttpd-send-file`).
  A custom `x-*` header *your* proxy consumes still passes — suppressing
  that one is yours, below
- frozen snapshots are read-only; handlers cannot mutate the VFS
- per-request time and output budgets; `{token}` is capability-grade

**You provide**, and nontainer cannot:

- **a dedicated origin for served apps** — the load-bearing one
- auth and cookies scoped to your control origin, never the app origin
- rate limits and quotas at your edge
- a tightened `connect-src` if egress matters
- `proxy_ignore_headers` if you front this with a proxy that consumes
  headers as commands

### Origins: the one that is not optional

An **origin** is scheme + host + port — `https://example.com` is a
different origin from `https://apps.example.com`, from
`http://example.com`, and from `https://example.com:8443`. **Path is not
part of it.** So mounting the router at `/apps` on the server that also
serves `/api/sessions` puts agent-authored code on *the same origin as
your control plane*, and the browser will treat it as your own first-party
code, because by that definition it is.

What follows from that, none of which nontainer can prevent:

- the app's JavaScript can `fetch('/api/sessions')` with the user's
  ambient credentials attached, and read the response
- nontainer's default `connect-src 'self'` **authorizes** it to, since
  `'self'` is that shared origin
- it shares `localStorage`, `sessionStorage` and IndexedDB with your
  control UI

Note what does *not* help. Binding to `127.0.0.1` is not a defence: the
attacker is a page in the user's own browser, and that page can reach
localhost. Neither is CORS — CORS governs whether a response can be
*read*, never whether a request is *sent*, and same-origin requests were
never subject to it in the first place.

The fix is not a header. It is an origin.

### Choosing a shape

Storage and cookies answer differently, so they get their own columns —
two apps can be unable to read each other's `localStorage` while still
sharing a cookie jar.

| shape | isolated from your control plane | apps' storage/DOM isolated | apps' cookies isolated |
|---|---|---|---|
| `example.com/apps/{token}` | ✗ | ✗ | ✗ |
| `localhost:9000/{token}` (second port, dev) | storage yes, cookies no | ✗ | ✗ |
| `apps.example.com/{token}` | ✓ | ✗ | ✗ |
| `{token}.apps.example.com` (wildcard DNS + cert) | ✓ | ✓ | ✗, unless the parent is a public suffix |
| `apps.example.com` on the Public Suffix List | ✓ | ✓ | ✓ |
| one registrable domain per app | ✓ | ✓ | ✓ (impractical past a handful) |

Three things worth knowing before you pick, all of which surprise people:

**Cookies do not respect ports.** A cookie set on `localhost:8000` is
sent to `localhost:9000` — the same-origin policy counts the port, the
cookie protocol does not. A second port is a real boundary for `fetch`
and storage, which makes it right for local development, but it is not
one for cookies. Keep session cookies off any host you also serve apps
from.

**A subdomain is a one-way boundary against the parent.** A cookie set
on `example.com` *without* a `Domain=` attribute is host-only and never
reaches `apps.example.com` — good. But a page on `apps.example.com` may
set a cookie *with* `Domain=example.com`, and the parent will receive
it. The response-header allowlist stops a handler doing this; nothing
stops the app's own JavaScript, which you are also not the author of.

**And sibling subdomains share a cookie jar.** The same mechanism runs
sideways: a page on `a.apps.example.com` can set `Domain=apps.example.com`,
and `b.apps.example.com` both receives that cookie and can read it from
`document.cookie`. Per-app subdomains separate storage, the DOM and
`fetch`; they do not by themselves separate cookies. So if your apps are
mutually untrusted, a wildcard alone is not the boundary you wanted.

**Making the sibling boundary real: the Public Suffix List.** A domain
on the PSL is treated by browsers as a registry-like boundary, so
nothing under it may set a cookie on it — which is exactly the missing
guarantee above, and exactly why `github.io`, `netlify.app` and
`vercel.app` are all on it. If you are hosting mutually untrusted apps
on subdomains at any scale, submitting your apps domain to the list's
PRIVATE section is the intended answer rather than an exotic one.

Go in knowing the cost: submission is reviewed by volunteers and takes
weeks, the boundary only exists for browsers that have shipped a list
including you, and removal is slower still — so use a domain dedicated
to this and never one you also serve your control plane from.

Until then, the honest fallback is that mutually untrusted apps want
mutually distinct registrable domains, and that this does not scale;
apps you are willing to treat as one trust domain (a single team's
dashboards, say) are fine on a shared parent.

### Auth and cookies

Give the app origin no ambient authority. The `{token}` in the URL is
the capability, and it is sufficient; a session cookie that also happens
to reach the app origin adds nothing except a credential the app can
spend. Requests reaching handlers already have `cookie` stripped by the
request allowlist, so this is about what the *browser* attaches on the
app's behalf, not about what your handlers can read.

### CSP

The default policy pins where executable code may come from
(`script_hosts`) and nothing more: `connect-src 'self' https:` permits
fetching **any HTTPS endpoint**, deliberately, because data apps need map
tiles, remote imagery and third-party APIs and handlers have no network
to proxy through. It is a supply-chain control, not an egress control.
If the app must not phone home, say so explicitly with
`AppsConfig(csp=...)`. Declare it on the config rather than on
`build_router` so `test_app` verifies against the policy that will
actually ship.

### If something sits in front

nginx acts on `X-Accel-Redirect` in an upstream response by default,
and `X-Sendfile` is the same mechanism in Apache and lighttpd. Those
names are refused, but the list covers the conventions that exist rather
than every one that could — a proxy with its own is beyond what a
library can know about its deployment. If yours consumes headers as
commands, tell it not to trust these (`proxy_ignore_headers` in nginx),
and remember that a custom `x-*` command header of your own invention
reaches it unfiltered, because to this library it is indistinguishable
from the app metadata the `x-*` allowance exists to permit.

**If you serve apps on per-app subdomains**, the router still routes by
path: it matches `/{token}` and `/{token}/{path}`, so a request to
`https://<token>.apps.example.com/` arrives with a path of `/` and
matches nothing. Wildcard DNS and a certificate are necessary but not
sufficient — something has to move the token from the host into the
path. In nginx:

```nginx
server {
    server_name ~^(?<token>[^.]+)\.apps\.example\.com$;
    location / {
        proxy_pass http://backend/apps/$token$request_uri;
    }
}
```

or as ASGI middleware in front of the mounted router, rewriting
`scope["path"]` to `f"/{token}{path}"` from the `Host` header. Either
way the app still sees itself under a prefix, which is why the
relative-URL rule holds unchanged.

## Known gaps

- **No per-host-object fs grant.** Pure-Python host resources that need
  the real filesystem have no flag yet (parallel to `ModuleGrant`); a
  `HostObjectGrant` would fill it. C-backed clients (sqlite) already do
  real I/O and don't need one.
- **App state on a virtual filesystem.** Relational / high-tempo state
  wants sqlite, a C extension that bypasses the virtual fs — so it needs
  the `dir` backend or a writable `Mount`. A documented sharp edge, not
  a solvable one.
