# Browser compute

> **Status: design proposal, not implemented.** Two proposals and one
> prerequisite. A **published app served from the visitor's browser**,
> with the server reduced to a host-object bridge; a **browser
> executor** — a third rung beside `LocalExecutor` and `DudExecutor`,
> where the agent's Python runs in a Pyodide worker in a tab while the
> provider, the loop and every versioning verb stay on the server; and,
> before either, a **ranged read on the filesystem protocol** that
> lives in termish and monkeyfs. The full picture reaches into
> [nontainer-studio](https://github.com/ashenfad/nontainer-studio); the
> nontainer half is written down here so the studio half has a contract
> to build against. Why the seams make this cheap is in the
> [design notes](design.md#three-seams-and-where-a-session-ends); the
> seam itself is in [extending.md](extending.md#executor--where-code-runs).

## The idea

`WorkspaceProvider` says where state lives; `Executor` says where code
runs against it; **executors never commit**. So an executor whose guest
is a browser tab changes nothing about sessions, forks, checkout,
delegation or publication — those were always the provider's. What it
changes is who pays for compute: each user's machine runs each user's
agent, and the server that holds kvgit and drives the loop becomes an
I/O manager. Bring-your-own-compute, the way a hosted studio would
already be bring-your-own-key.

"Serverless Python in the browser" is not a change of execution model
either. The apps design is already serverless — "there is no resident
app process… requests are dispatched into sandboxed executions on
demand" — and handlers are re-executed per request with no module
state. Moving that into a tab relocates a dispatcher that never had a
server.

Three things fall out, in increasing order of how much they change:

1. **An author's session computes in the author's tab.** Same trust,
   same host objects, one more transport.
2. **A studio that can be hosted multi-user.** The server never
   executes agent code, so the reason the studio is "single-user,
   localhost, no auth" goes away, and the API key stays server-side —
   which a fully client-side studio cannot offer.
3. **A published app served from the visitor's browser.** Serving is
   already frozen and stateless; move the dispatcher into the tab and
   per-request server compute is the host-object bridge and nothing
   else. This one moves a trust boundary — and it is the piece to
   build first, because it stands without the executor.

## The short version

The decisions, for a reader who wants them before the argument:

- **Publications first, executor second, a protocol change before
  both.** A browser-served publication needs only the frozen subset of
  a guest and no executor on the server; the executor reuses what that
  builds.
- **Moving a handler into the visitor's browser moves a trust
  boundary, and the embedder answers it with the object.** The
  caller's identity is unprovable, so nothing between the visitor and
  a host object can be trusted to mediate. **A visitor-facing host
  object is a public API**: every public method, any arguments, anyone
  with the URL. The whole app runs in the visitor's browser; the
  embedder hands the publication an object shaped for strangers, and
  hands the agent that same object in preview. nontainer's part is the
  bridge and a report of the surface, never a filter over the call.
- **Two principals, two endpoints, two origins.** The author's
  hostcalls land on the control origin under session auth; a
  visitor's on the apps origin under the token. The dispatcher runs in
  the principal's page.
- **The visitor's filesystem is `files_at` over the wire.** A
  publish-time manifest, content-addressed blobs on demand, ranged
  once phase 0 lands. Nothing in a publication is private.
- **`test_app` is a driver protocol**, and the driver's dispatcher is
  the dispatcher the publication will use.
- **Warmth is the embedder's; correctness after a restore is
  nontainer's.** A snapshot is a pristine interpreter taken before
  `boot()`; `boot()` builds the policy and reseeds.
- **Executors are switchable, so the browser rung is an offload.** The
  server keeps every capability; a tab takes a turn when it is there.
  Closing the server rung for the hosted-cheap posture is a separate
  product decision the switch does not make.
- **The far end is the harness in the tab.** Then the executor is
  `LocalExecutor` as-is, the server is a kvgit *remote* (agex-ts's
  design) plus the bridge, and no Python runs there but host objects.
  What it gives up is server-driven turns; the question after Phase 1
  is whether those matter.
- **nontainer's `Executor` protocol changes in one place:** `open`
  may bind lazily.

## What is already true

- **The stack runs under Pyodide today.** kvgit, termish, sandtrap and
  monkeyfs are pure Python with zero or one dependency;
  [agex-studio](https://github.com/ashenfad/agex-studio) installs
  termish, sandtrap, monkeyfs and agex into a Pyodide worker with
  micropip and runs the agex-py loop there. The walled garden, the
  shell and the VFS routing all boot in a browser already.
- **The `Executor` contract is transport-shaped.** `ExecutionContext`
  says so: "a remote executor instead treats the context as the two
  ends of its transport." `open` / `exec_python` / `exec_shell` /
  `diff` → `StagedDiff` / `sync` / `guest_to_host` is the whole seam.
- **`DudExecutor` has solved every piece once.** Tree materialization
  as a tar, the write harvest as whole-file `StagedDiff`, `HarvestLost`
  for a guest that dies between exec and harvest, `ctx.head` as a
  content-addressed affinity tag, opaque-bytes cache, the `host`
  module prelude with traceback renumbering, view execs with a
  prelude/epilogue for the contract classes, and the `ws-*` verb ferry
  through one `ws_verb` hostcall object. A browser executor is a
  sibling of `executor_dud.py`, not a new kind of thing.
- **Synchronous hostcalls from sandboxed Pyodide code are solved.**
  Agent code calls `db.query(...)` synchronously and sandtrap runs it
  synchronously; agex-studio's `docs.py` and `sheets.py` already make
  host calls from the worker over synchronous `XMLHttpRequest`.
- **Serving is frozen and stateless.** `resolve(token)` returns a
  read-only workspace and each request is one `exec_python(view=)`
  with `readonly_fs`; a publication is the derived commit holding
  `app/` and nothing else.

## When it is worth it

For a single-user local workbench, the executor as *sole* compute is
never worth it: it moves compute from one process on a laptop to
another process on the same laptop and pays with background turns. As
an *offload* beside the server rung (below) it is a latency-and-cost
win with nothing given up, and worth building on those terms. The
larger question is whether the studio is going to be **hosted**. If it
is, closing the server rung is plausibly the cheapest path to
multi-tenancy — cheaper than a microVM pool, with keys server-side —
and the browser-served publication is the piece to build first, alone,
because it stands without the executor and removes the ugliest line in
the current threat model: anonymous HTTP triggering agent-authored code
on the server.

What it costs, so the judgment is made with the bill in view:

- **Background turns.** "Close the tab mid-turn, the work continues"
  dies by construction on the browser rung.
- **The Pyodide ceiling.** No native dependency outside Pyodide's
  index, no real bash, iOS Safari's per-tab memory budget. agex-studio
  shipped Python first and made TypeScript primary because Pyodide was
  heavy; nontainer has no TypeScript escape hatch, because Python is
  the point.
- **A third rung to keep honest.** The cross-rung conformance suites
  grow a variant, and so does whatever the rung does differently on
  the embedder's side of the seam — host-side `ws.cache` reads of
  guest-written keys returning bytes, wall-clock deadlines in place of
  ticks. What the agent is told does not: local and browser are both
  termish plus sandtrap, observations render the same way (sandtrap
  captures stdout and stderr in-process on both, so the `[stderr]`
  block the adapter labels is there or not for the same reasons), and
  the package set is invariant by construction (below).
- **First data paint on a browser-served app** is the runtime boot
  plus the data the first handler touches. Warmth and lazy fetch make
  that small; they do not make it zero.

None of these is a security cost for the author's session. For
publications, risk is traded rather than added: today's surface is
sandbox plus handler plus host object, all on the server; browser-side
it is the bridge alone, exposing an object the embedder chose for
strangers, and the handler and the sandbox stop being the server's
problem. An object's public methods are a smaller thing to defend than
a sandbox, and they are the embedder's to shape.

### Executors are switchable, so the browser rung is an offload

The costs above assume the tab *is* the compute. It need not be. The
design notes already say "swapping the executor for a real machine
never touches the versioning semantics"; the studio already reads on
one executor what was authored on another; `shell_env`'s contract
already allows "one workspace can carry several runtimes"; and `diff` /
`sync` are specified as "called around execs, never during" — which is
exactly the turn boundary a switch needs. A switch is choosing which
runtime takes *this* turn, and `ctx.head` affinity makes a switch back
to a tab that still holds the head free.

Switchable, every cost of the form "when the tab is absent, X dies"
becomes a fallback: a closed tab means the next turn runs server-side
and background turns come back; a phone's memory budget or a slow
connection means the server for that user; a helper importing outside
the Pyodide profile means the server for that turn — the capability
chooses the rung. The server keeps everything it can do today; the browser is compute
close to the user when it is there. That is the incremental path: the
rung lands in the current studio as a latency-and-cost win, giving
nothing up, and Phase 2 stops depending on the hosted decision.

Two things it does not dissolve. The **threat-model win** — the server
never runs agent code — comes only from *removing* the server rung,
which is a product choice, not a switch; so the executor has two modes
with different prerequisites, offload (now, no regression) and
tab-only (hosted-cheap, commits), and this doc names both rather than
blurring them. And **the environment the agent is told about must be
the same on both rungs** — a constraint on nontainer and the studio,
invisible to the agent exactly as long as it is honoured. Skills and
tool descriptions are resolved per executor today
(`_resolve_skill_conditionals` rewrites SKILL.md in place) because
local and dud genuinely differ: real bash against termish, command
substitution, no injected commands. The offload pair is **local ↔
browser**, and those two do not differ that way: both are termish plus
sandtrap, same builtins, same `ws-*` verbs as real commands, ticks on
both, one `PythonConfig` (the `isolation` decision below). The one
thing that could differ is the package set, and it is made invariant
by construction: the Pyodide profile is derived from the same grants,
and a grant the profile cannot cover is refused at `open`, the way dud
refuses what a rung cannot honour — absent rather than narrowed. Then
"what libraries exist here" is one sentence, true on both rungs, and
the in-place rewrite never runs on a switch. The one rule left is not
to pair **dud ↔ browser** per turn: that crosses the bash/termish line
and the agent would notice. Smaller: cache pickles cross rungs, so the
guest stack is pinned to the server's, the discipline dud's "image
matched to your interpreter" already imposes.

Optionality is not free, and this stack has a lot of it. Every rung
kept open costs a conformance suite, a tool-description variant and a
skill block, and no rung gets the deep optimization a committed system
gets — Cloudflare committed to Pyodide and got sub-second cold starts;
agex-studio committed to TypeScript-primary and got a fast boot. The
switch is the right *default* because it is cheap here, in a stack
whose one hard commitment — the state model — is what makes the rest
swappable. Whether to then close the server rung is the commitment
question, and it stays a visible product decision rather than
something the switch quietly answers.

## Publications in the visitor's browser

A visitor's browser needs only the frozen subset of a guest: a
read-only tree, no harvest, no sync, no verbs, no delegation. Ship
`app/` plus a Pyodide runtime, intercept `api/*` in the page, dispatch
into the worker; the server sees hostcalls and nothing else. Authoring
can stay server-side while publications move.

### The trust boundary moves, and what that does and does not mean

Today a published app has three parties: the author's agent wrote the
handler, the server runs it, the visitor sends `req.params`. The
handler is a boundary because it is author-controlled code running
where the visitor cannot touch it; `serve.py`'s threat framing says
exactly this. Move the handler into the visitor's browser and it is a
file on the attacker's machine. The `db` handle granted to *the
author's agent* is now, in effect, granted to *everyone with the URL*.

This is **not** a reason to trust the agent less. The embedder's
judgment — what is safe to give a cooperative agent acting for the
author against the author's data — still holds, and for the author's
own session (use 1 above) nothing narrows: same object, same allowlist,
one socket. What changes is a *second principal* standing where the
handler used to stand, and a judgment that has to be re-made for it:
"safe to give the author's agent" is not "safe to give a stranger".
nontainer made the same move once already, on the filesystem side: the
derived publication commit exists because "a handler under a frozen
workspace reads the entire tree, and an export hands over the entire
tree". Whatever the artifact can reach, the visitor can reach.

Two things follow, and both are worth stating precisely.

**The identity of the caller is unprovable.** No mechanism lets the
server know that *this frozen code* executed on the visitor's machine
and produced *this call*. A key in the handler is extractable; a hash
of the running code is a value the client can send; attestation APIs
vouch for a browser or a device, never for which script ran. The
client owns the machine. Do not build anything that pretends
otherwise.

**So nothing between the visitor and the object can be trusted to
mediate.** Not the handler, not a check in it, not a harvest of what
the handler was written to call — every one of those is a property of
code the visitor can rewrite. What remains is the object, and the
object is the embedder's. That judgment is not nontainer's to make and
not nontainer's to narrow with a mechanism; a filter that can be
half-right invites reliance on the half that is wrong.

### The host object is the API

The rule, and where it lives in the API:

**Where an object is handed decides who reaches it.** Objects on a
session's `PythonConfig` are the author's: the cooperative agent
acting for the author, against the author's data, with whatever the
embedder judged safe for that. Objects passed at `pub.open` are the
visitor's. Under browser-side serving a visitor-facing object is a
**public API** — every public method, with any arguments, to anyone
holding the URL — and it must be safe on those terms. The same object
serves three audiences depending on where it is handed: the author's
agent in a session, visitors through author-written handlers when
served server-side, visitors directly when served browser-side. The
internal-site and public-internet cases differ only in the size of the
third audience.

That is the whole security story, and it is one the embedder was
already telling. `apps.md`'s hosting half is "the embedder's half"
throughout; `serve.py`'s threat framing was always "the embedder
serving untrusted audiences"; and the studio, as its own embedder,
already keys a db per app. Validation that used to sit in a handler
sits in the object — `add_score(name, score)` clamps and checks
because the object does, not because a handler did — and once it does,
writes are as safe browser-side as reads. So the whole app runs in
the visitor's browser, GET and POST alike; there is no verb split,
no per-handler routing, and "the server never runs agent code" is
true for every published app rather than for read-only ones.

What nontainer owes is the seam, kept deliberately dumb:

- **The bridge exposes public methods and nothing else.** The same
  allowlist dud uses (`dud.public_methods`), JSON in and out. A method
  whose arguments or result cannot cross as JSON is refused at
  `publish`, not at a visitor's first call.
- **The visitor endpoint takes only the token** (below): no cookies,
  no control-origin credential, so an operator visiting their own app
  never spends session authority through it.
- **A publish-time surface report.** `publish` lists the public
  methods being exposed, with signatures. It is the moment the
  embedder — or the author reading the publish output — sees what
  "anyone with the URL" now has, and it is a report, not a gate.
- **One sentence in the hosting half of apps.md, and one in the
  skill.** *A visitor-facing host object is a public API. Give the
  published app a narrower object than the session, and give the
  agent that object in preview, so nothing verifies green and breaks
  published.* The second half is already how the repo answers CSP and
  `script_hosts`: one declaration, both lifecycles.

What is left is a judgment the embedder makes once and writes down.
For a per-app sqlite file that is the app's own data, a studio can
hand out a read-only connection and say "anyone with the URL can read
this app's whole database" — true, stated, and fine for what the
studio publishes. Writes need the studio to design the object that
takes them, which was always its decision; this framing stops
pretending nontainer could make it. An embedder fronting a shared
production db hands the agent a narrower object *from the start*, in
preview and published alike, so the agent never sees a difference
between the two.

### Two principals, two endpoints, two origins

Host objects are reached over a bridge, and the bridge serves two
principals: the author's session and a publication's visitor. They do
not share an endpoint, because [apps.md](apps.md#hosting-for-real-the-embedders-half)
already forbids them sharing an origin — *give the app origin no
ambient authority; auth and cookies scoped to your control origin,
never the app origin; `{token}` is the capability and it is
sufficient*:

| principal | where the hostcall lands | credential | what it reaches |
|---|---|---|---|
| the author's session | `/api/sessions/{name}/host/…` on the **control origin** | the studio's session auth, like every other `/api/sessions/{name}` route | that session's live host objects, the full public-method allowlist |
| a publication's visitor | `/apps/{token}/host/…` on the **apps origin** | the token in the URL — the capability, sufficient, no cookie | the publication's visitor-facing host objects, as handed at `pub.open` — their public methods, any arguments |

Two routes, two origins, two principals, sharing only the dispatch
helper. The endpoint's existence encodes the principal; nothing
inspects a caller to decide which rules apply.

**The dispatcher runs in the principal's page.** That is the rule the
table falls out of. For a visitor, the Pyodide worker lives in the
visitor's page on the apps origin, so its hostcalls are token-scoped by
construction. For the author, the worker lives in the *studio* page,
not in the preview iframe, so its hostcalls carry the studio session by
construction. The preview iframe — sandboxed, opaque origin, as the
studio's `cors_for_apps` describes it — never reaches the control
origin at all: its `fetch('api/x')` is intercepted in-page and posted
to the parent, which runs the handler in the author's worker. That
interceptor is the one a browser-served publication needs anyway.
Today the preview fetches the router directly, which is what the `*`
CORS header exists for; under the tab driver it does not, which is
strictly less exposure. A delegate that runs in the author's tab
inherits the author's principal, which is right: a delegate acts for
the author.

What is new, and what a hosted studio owes regardless:

- **Nothing new for visitors.** Anonymous traffic hits handlers on the
  server today; browser-side it reaches the visitor-facing object
  directly, with no handler between, and the object was designed for
  that. Rate limits stay an edge concern; the Referer leak of a URL
  token is unchanged; the studio already keys the db by token in its
  manifest, so publication → visitor host objects has a home.
- **Owed anyway:** session auth on the control origin and CSRF defence
  on its mutating routes (SameSite cookies or a header token). The
  author hostcall endpoint is one more POST route under that
  protection, not a special case.
- **The one deliberate line:** the visitor endpoint takes *only* the
  token. It refuses cookies and any control-origin credential outright,
  so an operator visiting their own published app never spends session
  authority through it. That is "no ambient authority" applied to the
  bridge — one line of code, one line of doc.

### The app's data ships lazily

Shipping `app/` to the visitor as a tar makes a 50MB parquet a 50MB
download before first data paint. Two things to say about that, and the
first is what it is *not*: it is not a new disclosure. `_dispatch_static`
serves everything under `app/` except `app/api/`, so
`app/data/records.parquet` is already fetchable by any visitor at
`/apps/{token}/data/records.parquet`. What does change is `app/api/`
itself: today it is the one part of a publication static serving
refuses, and the skill leans on that ("`_`-prefixed files under
`app/api/` are as private as handler source"). Browser-side, the
handler source ships to the visitor and that sentence inverts. Under
browser-side serving **nothing in a publication is private** — a
secret belongs in a host object, server-side, or nowhere. A stronger
and simpler rule than the current one; one line in the skill.

The bytes problem has the provider's own answer. `files_at(commit)` is
"a read view, not a copy: values are read on demand." The tab should
get exactly that, over the wire:

- **Metadata up front, blobs on demand.** `store.publish` already reads
  every blob to copy it into the derived commit; while it does, it
  hashes each and writes a manifest — `{path: (sha256, size)}` — as one
  reserved key in that commit, beside `published_from`. The tab builds
  its termish `FileSystem` from the manifest (`MemoryFS` populated with
  metadata-only entries: listings, `exists`, `stat` all answer
  locally), and a file's contents are fetched on first `read()`, over
  the same sync channel hostcalls use, from
  `/apps/{token}/blob/{sha256}`. monkeyfs already routes sandboxed
  `open()` there, so `pd.read_parquet("/workspace/app/data/x.parquet")`
  fetches that one blob, when a handler actually needs it. With
  [phase 0](#phase-0-one-filesystem-protocol-and-a-ranged-read), it
  fetches the byte ranges it needs.
- **Content-addressed, so cached across everything.** The blob URL is
  immutable: a v2 that did not touch the parquet does not re-download
  it, two apps over one dataset share it, and the browser cache does
  the rest. One guard: a hash is servable only if it is in *this*
  token's manifest, or content addressing becomes a cross-app oracle.
  (kvgit already hashes values with sha256 in the HAMT; surfacing that
  through the provider would save re-hashing at publish, and is an
  optimization, not a requirement — the publish-time manifest is
  provider-neutral.)
- **No executor on the server.** A browser-served publication does not
  instantiate an `AppRuntime` or an executor server-side at all: the
  server does provider reads, static, and the hostcall bridge.
  `serve.py`'s browser mode is simpler than its current mode.

Size: ~30 lines in `store.publish`, ~40 in `serve.py` (manifest, blob
route with `Cache-Control: immutable`), ~100–150 for `LazyFS` in the
guest. No seam moves.

The honest limit: first data paint on a data-heavy app is the runtime
boot plus the blobs the first handler touches. Lazy fetch makes that
the *working set* rather than the *tree*, and phase 0 makes it the
*columns* rather than the *file* — but the working set is what it is.
Browser-side serving is for apps whose working set fits a download,
which is most of what the studio produces because the skill already
says "convert big source data once, then handlers read the parquet."
For the tail, the cheap tell is a publish-time report — "this app
ships 48MB under `app/data/`," read straight off the manifest — so the
author knows before a visitor does.

## `test_app` becomes a driver protocol

One lie is open. Under browser-side serving, `test_app` is still Playwright
on the server hitting server-side dispatch — so it never exercises the
runtime the publication will run on. A handler that imports something
present in the server's grants and absent from the Pyodide profile
passes every check and 500s for every visitor. The fix is not "run
Playwright in the tab"; it is to name the seam `testapp.py` already
has.

Of its ~1250 lines, the Playwright-specific part is one bounded region:
context setup, `page.route` interception, the action loop's
`page.click` / `fill` / `evaluate` / `goto` / `screenshot`, the console
and pageerror hooks. Everything around it is driver-neutral —
`coerce_actions` and the DSL, `parse_frames` / `classify_frame` /
`describe_page_error` (which read workspace lines to annotate a stack,
host-side by nature), the CSP checker, the relocatability hint,
`ActionResult` / `TestAppResult`, `render_test_app`.

```python
class AppDriver(Protocol):
    def run(self, spec: DriveSpec) -> DriveReport: ...
```

`DriveSpec` carries what the neutral half has already decided — the
actions, viewport, CSP string, `script_hosts`, timeouts — and how the
app is served: a callback into `AppRuntime.dispatch` for Playwright,
"your own tree, your own dispatcher" for a tab. `DriveReport` comes
back raw: per-action results, console lines, page errors with unparsed
stacks, rejected requests, CSP hits, screenshot bytes by name.
`testapp.py` keeps annotating stacks against the workspace, saving
screenshots under `app/screenshots/` so they version with the session,
and rendering. None of that moves.

Two drivers, both already written somewhere in the family:

- **Playwright** — what is there now, extracted. Server-side Chromium,
  `page.route`, a fresh context per call.
- **Tab** — the spec crosses the executor's socket, the tab builds a
  hidden iframe from its own tree (agex-studio's `buildAppHtml` and
  postMessage bridge are this, down to the action set and the
  `__agex_logs` buffer), and posts the report back. Under this driver
  the server needs no Playwright at all, so the `[apps]` extra's
  heaviest dependency becomes per-rung.

**Who picks.** `AppsConfig.driver` if set; else the executor's, if it
offers one (`Runtime.app_driver`, optional, probed the way
`supports_ws_verbs` is — a public name on `Runtime`, so `apps/` reaches
it without crossing the layering rule); else Playwright. And the
invariant that makes this close the gap rather than only abstract it:
**the driver's dispatcher is the dispatcher the publication will use.**
Playwright verifies against server-side dispatch, which is what
server-side serving runs; the tab driver verifies against the in-tab
Pyodide dispatcher, which is what browser-side serving runs. Preview
and publication share a runtime by construction, and a handler
importing outside the Pyodide profile fails in `test_app`, where the
agent reads it.

Two consequences worth having. **`ws-vitest` is the second consumer**:
`jsharness.py` and `harness.js` drive a harness page through the same
Chromium, and two consumers are what make a driver protocol earn its
keep. And **publish-time verification comes free**: the tab driver can
run the same actions against the frozen publication tree on the
visitor-shaped runtime before `publish` returns — the strongest form
of "what verifies green is what ships".

### Where the tab driver is weaker

Rung-specific caveats. Most are the driver's to absorb without the
agent noticing; the one that reaches the agent is the screenshot, and
the `test_app` description says so on that rung:

- **Screenshots.** Playwright's `page.screenshot()` is a compositor
  capture. A hidden iframe on a separate apps origin cannot be captured
  by its parent; the page rasterizes itself and posts the PNG back,
  which loses WebGL plotly, tainted canvases and some fonts. Fine for
  "is there a chart", not pixel-faithful — or the tab driver returns no
  screenshot and the description says so.
- **Fresh context.** A Playwright context starts with empty storage; a
  cross-origin iframe has a persistent partition for that origin, so
  repeated tests see each other's `localStorage` unless the driver
  seeds and discards, as agex-studio's `testApp` does. Same observable
  semantics, more bookkeeping.
- **Blocked requests.** `page.route` denies and *reports* off-allowlist
  fetches. An iframe enforces through an injected `<meta>` CSP (a
  `document.write` page gets no server header) and observes through
  `securitypolicyviolation` events — which is what `window.__nt_csp`
  already collects, so the report shape survives while the enforcement
  is a little weaker: no `frame-ancestors`, no report-only.
- **Presence.** `test_app` on the tab driver waits for a browser the
  way `run_python` does. Same gating, same rail state.

## The executor

The inversion from dud is that **the guest dials in**. dud's host
drives a guest it owns; here the guest is a tab that connects to the
server, so the server-side executor is a stub bound to a socket. It
reuses the guest wheel and the tab driver the publication work builds.

| concern | `DudExecutor` | browser executor |
|---|---|---|
| tree | tar push over the dud channel | the same tar over the socket; `ctx.head` affinity so a tab already holding the tree skips it |
| harvest | scan / overlay diff → `StagedDiff` | scan-diff in the worker against a shadow of the pushed tree; whole-file payloads |
| host objects | hostcall proxies behind `dud.public_methods` | the same allowlist; a proxy method is a sync XHR to the control origin, session-scoped, which dispatches to the live object exactly as `_host_object_rpc_handler` does |
| `ws-*` verbs | bash functions → `ws_verb` hostcall; the host *pulls* the guest's diff before dispatching | the guest shell is termish, so each tagged verb is a relay *command* in the guest registry; `supports_commands` is True and the `FerrySpec` path mapping applies unchanged. One inversion: a worker blocked in a sync XHR cannot answer a pull, so the relay command computes its scan-diff and ships it *inside* the verb call, and the host's absorb step consumes a pushed diff rather than calling `diff()` |
| cache | guest pickles, host stores bytes | identical |
| isolation | a VM exceeds any `isolation` | `none` and `process` are satisfied — a worker crash costs the worker, not the page, which is what `process` promises; `kernel` is refused at open, the way `_prepare_config` refuses what a rung cannot honour, since it promises a syscall filter the browser does not have |
| ticks | gone; wall-clock only | back — sandtrap is in-process in the worker |
| packages | grants → image package list | grants → Pyodide package list, the same `_merge_packages` idea |
| view reentrancy | one channel, serialized | one interpreter per tab, serialized; a worker pool is a memory question the tab answers |

**Push-tree for authoring, `LazyFS` later.** For the author's rung,
push-plus-harvest matches the existing contract and the cross-rung
conformance suites exactly, so it starts there. The lazy filesystem
already exists by then, built for publications where it was mandatory
rather than nice; the author's rung later swaps `sync()` from "push a
tar" to "send a fresh manifest of `working_files()`" against the same
`LazyFS` and a per-session, authenticated blob endpoint. Only the
host→guest direction changes; the write harvest is the same scan-diff
either way.

**Network honesty.** sandtrap's network denial patches `socket`, which
does not exist under Pyodide; `network=True` would have to mean
allowing `pyfetch` / XHR, and the policy story needs one paragraph
saying so rather than a knob that reads as a policy and does nothing.

**A closed tab is a lost guest.** `HarvestLost` already describes the
torn call and the workspace already surfaces it. What changes is the
product: a turn that runs while the tab is closed cannot exist on this
rung, because the tab is the compute. Delegates stay on `LocalExecutor`
at first (see *Smaller decisions*), which keeps part of that story —
sessions authored on one executor are readable on another today.

### What nontainer has to change

One thing. `Executor.open` is called synchronously as the last step of
`Runtime.__init__`, and a browser executor's guest may not exist at
that moment: the socket connects when the tab does. So `open` must be
allowed to **bind lazily** — hold the context, attach when a guest
dials in, and let the first exec wait, bounded, with "no browser
attached" as an errored result rather than an exception. That is a
note in the protocol docstring, in the same register as `close` being
best-effort. Nothing else on the `Executor` protocol moves.

The cross-rung suites (`tests/test_wsgit_conformance.py`,
`tests/test_wscurl_conformance.py`, the two test verbs) are the
acceptance test: run them with a Playwright-driven Pyodide guest and
the rung is real or it is not.

## The far end: the harness in the tab

The executor above exists because the harness is on the server. Move
the harness into the tab and the executor question dissolves: with the
loop local, the executor is `LocalExecutor` — termish and sandtrap in
the same Pyodide interpreter as the `Workspace`. No socket, no tree
push, no harvest, no lazy `open`, no `HarvestLost`. nontainer core is
pure Python and installs under Pyodide in principle today; agex-studio
already runs a Python harness in a Pyodide worker behind a JS shell
and already mounts OPFS so kvgit's disk store persists. The client
half is agex-studio's py-kernel with nontainer as the environment.

**The server is a kvgit remote, not a provider.** Two shapes were
possible. `WorkspaceProvider` over RPC — the `Workspace` in the tab
holding the single-writer lock across a wire, every commit and read a
round trip — works and is chatty. The other is agex-ts's
[`kvgit-remotes.md`](https://github.com/ashenfad/agex-ts/blob/main/kvgit-remotes.md)
verbatim: kvgit lives in the tab, the server is a passive object store
with CAS-able refs, sync exchanges deltas on commit, "merge is not in
the protocol — reconciliation is always local", v1 fast-forward only
with divergence surfaced. That doc targeted GitHub as the remote and
contorted around the Git Data API; a nontainer server is the same
protocol on a simpler transport, and it is worth having on its own for
multi-device sync. It is the shape in the stack's spirit: state is
git-shaped and authoritative, so "the server is a remote" is the
natural role rather than a hack.

What the server does then: auth, the kvgit remote, the publication
registry, browser-served publications (static, blobs, bridge — Phase
1 unchanged), the host-object bridge, and optionally an LLM proxy so
keys stay server-side. **No Python executes there except host
objects.** That is the I/O manager fully realized — cleaner than the
tab-only posture above, which still carried an exec socket.

**The harness is the studio's, as it always was.** nontainer names the
`SessionRunner` seam and stops; it never owned the loop. agno under
Pyodide is unproven (its dependency weight, not its shape); agex-py's
loop is proven there; and an **agex v2 whose environment is nontainer**
is the natural closing of a loop the README already draws — "agex is
the full agent framework over the same substrate; nontainer is the
environment layer alone, offered to someone else's loop." Code-as-action
over `ws.run_python`, the script-not-REPL model chosen "so an agent's
mental model transfers verbatim", pure Python end to end. Whichever it
is, what nontainer owes the far end is core under Pyodide (the `[apps]`
extra split so `dispatch.py` imports neither starlette nor playwright —
packaging, since the layering rule already keeps dispatch on core's
API) and the remote.

**What it gives up: server-driven turns.** A nightly analysis, a
webhook-triggered turn, a delegate that outlives the session — anything
that needs an agent to run without a human's tab has no place to run.
That is a real class, and it is the class the current studio's
background turns and delegates serve. Two honest answers: accept it,
as agex-studio does; or let the server be a *client of its own remote*
— a `LocalExecutor` harness on the server, syncing like any other
device, for scheduled work only. That is the switchability point
reappearing as "the server runs agent code by choice, for autonomy",
and the product decision stays visible. The Pyodide ceiling and the
first-visit cost now apply to the author, not only to visitors.

**What it does to the plan.** Phase 2 as written — server harness, tab
executor — is one point on a line whose other end is this. Both share
Phases 0 and 1 entirely: the guest wheel, `LazyFS`, the blob endpoint,
the hostcall bridge, the tab driver, browser-served publications, the
surface report. They differ in what is built next: an executor stub
and its socket, versus a kvgit remote and nontainer core under Pyodide
— and the far end is plausibly *less* nontainer work, because
`LocalExecutor` is used as-is and the remote is already designed. So
the question after Phase 1 is not "build the executor?" but **"do
server-driven turns matter?"** — yes, the hybrid; no, the far end. The
same commitment underneath either way.

## Warmth is the embedder's

Cold boot is where this could have died: agex-studio's Pyodide worker
takes ~30s to first prompt. Almost none of that is fundamental.

| cost | what it is | fix |
|---|---|---|
| download | core + numpy + pandas + plotly (~40–60MB gz) | one-time per device; service worker, `immutable` headers |
| wasm compile | the core and every package's side module | browser code cache on repeat loads |
| micropip resolution | N PyPI metadata fetches and a dependency solve | gone: a static lockfile of wheel URLs, no micropip at runtime |
| unpack | wheels unzipped into MEMFS | a pre-built bundle |
| Python import | `import pandas`, `import plotly` — seconds of interpreter work | a **memory snapshot**, and nothing else |

The last row is the golden-image move dud already makes: Pyodide's
memory snapshot (`_makeSnapshot` / `_loadSnapshot`, still marked
experimental; Cloudflare's Python Workers are the production user) is
the browser's forkserver — sandtrap's broker with `preload_grants=True`
is a pristine worker with the stack imported and nothing else, and the
snapshot is the same object in a tab. The "swap in the fs" half is
nearly free here because workspace files never live in MEMFS: monkeyfs
routes sandboxed `open()` to termish's `FileSystem`, a Python object,
so the pristine snapshot holds no workspace state and materializing a
session is building a `MemoryFS` from a tar or a manifest.

None of that is nontainer's. dud owns its VM pool, sandtrap owns the
forkserver, the studio owns `warm_view_workers` and
`NONTAINER_STUDIO_VM_WARM`; whether a tab's warmth comes from a
snapshot, a shared worker across tabs of one apps origin, or a profile
per app derived from the handlers' imports is the studio's to decide.
nontainer's contribution is `ctx.head` as the affinity tag and the
lazy `open` above: the tab boots in the background and the first
`run_python` waits, the way `dud-vm`'s first image build already does
with the agent none the wiser.

Where the cost lands matters more than its size. For an author it is
one warm worker for a session. For a visitor it is per tab, once, and
it lands on **time-to-first-data**: ~1s on a repeat visit (code-cached
wasm plus a memcpy-scale restore), download-bound on the first. A
profile with pandas is 10–40× the app's own vendor bundle. That number,
measured per profile, decides whether the pragmatic hybrid — route
`api/*` to the server's existing route until the worker reports ready,
then flip — is a knob or the default. The hybrid keeps the steady-state
win and gives back "the server never runs agent code" for one or two
requests per visit; no worse than today, just not the clean story.

### What a snapshot freezes, and `boot()`

A snapshot captures the heap after imports, so anything drawn from the
environment *at import* is frozen into every restore: `random`'s
global generator and `numpy.random`'s (seeded from OS entropy when the
module loads — every visitor's first `random.sample(rows, 10)` is the
same ten rows), the per-process `hash()` secret (identical dict and
set order on every restore, known to whoever built the image), any
module-level capture of a clock (`_T0 = time.monotonic()` is the
*builder's* page), `os.environ`, cwd, locale. What does not freeze:
`secrets`, `uuid4` and `os.urandom` reach `crypto.getRandomValues` at
call time; `time.time()` is `Date.now()` at call time. Tokens and ids
are fine; "pick a random tip" and "sample the frame" are the ones that
bite.

In this deployment that is a correctness footgun, not a boundary.
Cloudflare's snapshot serves many tenants from one process, so a
shared hash seed is a cross-tenant flooding vector and a predictable
`random` is a cross-request leak; here the heap is the visitor's own
tab, and the only party who could exploit a known seed against it is
the operator who built the image — who already runs code in it.

Whether to snapshot is the embedder's; the guest's correctness after a
restore is nontainer's, because the guest wheel is nontainer's. So:

- **`nontainer_guest.boot()` reseeds unconditionally** — `random.seed()`
  and `numpy.random.seed()` with no argument, every boot, snapshot or
  not. Two calls, and nothing for an embedder to remember, since
  `boot()` is the only entry point either way.
- **The snapshot is taken before `boot()`, never after.** That is the
  one rule for the embedder, and it is the same rule that makes the
  image **policy-free**: sandtrap's `Policy` and the sandbox are built
  in `boot()` from what the server sends, `LazyFS` from the manifest,
  cwd from the workspace root — so one image per *package profile*,
  not per policy or per app. Reseeding is one more thing `boot()`
  does because the image deliberately holds nothing derived from the
  environment.
- **The principle, once:** *a snapshot is a pristine interpreter —
  imports, no execution.* Anything derived from the environment at
  import is re-derived at boot. That covers the list above and
  whatever is not on it.
- **`hash()` is accepted.** CPython fixes the secret at start and it
  cannot be reseeded; it is harmless here for the reason above, and
  the doc says so rather than pretending.

Rather than trust the enumeration, the spike restores one image
twice, runs `boot()` in each, and asserts the two diverge on
`random.random()` and `numpy.random.random()`. It rides the same
harness as the dynamic-linking check.

## Smaller decisions

Questions with one decision each, recorded so nobody re-derives them.

**Cancellation: terminate by default, interrupt buffer opt-in; hostcalls
are sync XHR.** sandtrap's `cancel()`, `timeout` and `tick_limit` are
checkpoint flags, and a busy worker never runs its event loop, so a
`postMessage` carrying "cancel" is never *delivered* while sync Python
runs. Two ways in: `pyodide.setInterruptBuffer` — a `SharedArrayBuffer`
the page writes, raising `KeyboardInterrupt` at the next bytecode
boundary, which sandtrap reports as cancelled and the worker survives —
but SAB needs COOP/COEP on the studio origin, which constrains what the
page may embed (the apps-origin preview iframe would need
`credentialless` or CORP headers); or `worker.terminate()` — no
headers, always works, costs the warm image (~1s restore). Neither
interrupts a C call mid-flight; that is true of every rung. Cancel is
rare and a second on cancel is fine; a header requirement on the whole
studio page is not, so terminate is the default and the interrupt
buffer is an option for an origin that is already cross-origin
isolated. The same header requirement rules out `Atomics.wait` as the
hostcall channel by default, so hostcalls are synchronous XHR from the
worker, as agex-studio's already are. Deadlines mirror dud: the tab
enforces `PythonConfig.timeout` plus a grace by terminating; the server
treats silence past that as a lost guest. A sync XHR hostcall carries
the exec's remaining budget as its `timeout` (allowed in workers), so a
slow server cannot wedge a call past its deadline.

**`isolation`: `process` is satisfied, `kernel` is refused.** The
first draft refused anything but `none`, modelled on
`_prepare_config`. That is wrong by dud's own reasoning: dud refuses
`network=True` because it is a *capability* it cannot honour, and
accepts any `isolation` because a VM *exceeds* it. A Web Worker
exceeds `process` — a crash costs the worker, not the page — and does
not exceed `kernel`, which promises a syscall filter. So `none` and
`process` are accepted as met and `kernel` is refused. The studio's
default `isolation="process"` is then valid on both rungs with one
`PythonConfig`, and a delegate on `LocalExecutor` under a parent on
the browser rung shares the parent's config, as a fork does today.

**Delegates stay on `LocalExecutor` first.** A delegate is compute the
user never watches; running it in the author's tab makes the tab a
compute farm and ties it to the tab's lifetime. The store already
tolerates sessions authored on one executor and read on another, and
the previous decision makes one `PythonConfig` serve both rungs. A
second worker in the author's tab is a later option, not the first.

**Two tabs on one session: last attached wins.** State is
server-side and the workspace lock serializes, so this is
availability, not correctness. A new attach detaches the previous
guest — its socket closed with a reason it can show — after any exec
in flight on it finishes, and `ctx.head` affinity means the newcomer
pushes the tree only if its head differs. The tab in front of the
user is the one they are using.

**The image holds the stack, not the guest wheel.** Key a snapshot on
(Pyodide version, package lockfile hash). sandtrap, termish, monkeyfs
and kvgit are in it; `nontainer-guest` is not — it is pure Python and
small, its heavy imports are already resident, so loading it after
restore is milliseconds, and a nontainer release then invalidates
nothing unless it moved a stack floor. Everything else about images
is operational and the studio's.

## Phase 0: one filesystem protocol, and a ranged read

Everything above reads workspace files across a wire — a visitor's
browser pulling a publication, a tab pulling a tree from the server, a
worker process pulling from the host over RPC. Today every one of those
reads is whole-file, because the protocol says so: termish's
`FileSystem.read(path) -> bytes`, and monkeyfs's `VirtualFile` is a
`BytesIO` over the whole file, seeked to zero, written back on close.
"Seekable" is a fiction over a full buffer. So a handler that wants two
parquet columns of twenty downloads the parquet. This phase fixes that
at the protocol, in two other repositories, before anything above needs
it — and nothing above *blocks* on it: the whole-file `LazyFS` works
without it and gets faster when it lands.

### Who owns the protocol

termish and monkeyfs each declare a structural `FileSystem` protocol.
They already agree: the same sixteen-odd methods, and a `FileMetadata`
that is field-for-field identical (`size`, `created_at`,
`modified_at`, `is_dir`), monkeyfs's a strict superset with `st_*`
properties so `os.stat()` can hand it back. That is a convention that
has held without anyone enforcing it. The proposal is to keep it a
convention and enforce it:

- **No shared import.** Both protocols are `typing.Protocol`; a shared
  definition would change nothing at runtime. There is no natural
  "below" — a shell over a filesystem and stdlib routing over a
  filesystem are both consumers of one shape — and a third package
  would cost both libraries their zero-dependency line for twenty lines
  of protocol. Structural duplication is the Python idiom for this
  (file-like objects, `PathLike`, WSGI).
- **One promise, written in both READMEs.** *monkeyfs's backend
  protocol is termish's protocol plus the ranged read; `open()` is what
  monkeyfs provides over it, and a backend that has a better one may
  offer it.* That shrinks monkeyfs's backend contract (`open` becomes
  optional and probed, as does `readlink`, the way nontainer probes
  `refresh()`), and makes every termish-shaped filesystem a Python
  `open()` for free. `VirtualFile` is the adapter from bytes-level to
  file-object-level and the fallback when a backend offers no `open`
  of its own — which is already the shape of `MountFS.open` and
  `ReadOnlyFS.open`, both of which delegate. `IsolatedFS` keeps its
  `open`: it returns a real `io.open` handle over the resolved host
  path, so `fileno()`, streaming writes and files larger than memory
  work natively there, and routing it through a buffered `VirtualFile`
  would narrow the one backend that has real files.
- **A drift test where both are installed.** The repository's rule for
  conventions is that they should not be conventions —
  `tests/test_layering.py` walks the package with `ast` rather than
  asking anyone to remember. Same here: nontainer is the first place
  termish and monkeyfs meet, so it carries a meta-test that
  `inspect.signature`s every method on both protocols and fails on
  drift, and runs both libraries' conformance kits
  (`termish.fs.check_filesystem`, `monkeyfs.check_filesystem` — each
  shipped so a backend author needs neither the other library nor
  nontainer) against `KvgitProvider.fs`, `MemoryFS`, `VirtualFS` and
  the browser guest's `LazyFS`.

### The primitive

```python
def read(self, path: str, offset: int = 0, size: int = -1) -> bytes: ...
```

Defaults are today's whole-file read, so every existing backend and
caller is unchanged; `MemoryFS` and `VirtualFS` implement the range by
slicing; wrappers (`MountFS`, `ReadOnlyFS`, `IsolatedFS`) forward the
two arguments. `stat().st_size` already exists for `seek(0, 2)`. Then
`VirtualFile` in binary read mode becomes lazy — a small block cache
over `read(path, offset, size)` instead of a materialized buffer —
while text mode and write modes keep materializing: decoding across
block boundaries is not worth having, and writes are whole-file by the
protocol's own design.

Who benefits, in order:

1. **The browser guest.** `LazyFS.read(path, offset, size)` is an HTTP
   `Range` request against the blob endpoint, so
   `pd.read_parquet(..., columns=[...])` reads the footer and the
   column chunks it needs. This works because pandas opens a local
   parquet path through Python `open()` and hands pyarrow the file
   object — which is also why handlers can read parquet under monkeyfs
   *today*; if pyarrow opened the path itself in C++, monkeyfs would
   never see it.
2. **Process and kernel isolation.** The worker reaches workspace files
   host-side over an RPC bridge that speaks the bytes-level protocol,
   so a large file crosses whole today. Once the bridge forwards the
   two arguments, it is lazy for free.
3. **The author's browser rung, later.** The same `LazyFS` against a
   per-session, authenticated endpoint.

### Size, and the real cost

- termish: two optional parameters on `read`; `MemoryFS` slices. ~20
  lines.
- monkeyfs: `open` optional on the backend protocol, probed; a lazy
  binary-read `VirtualFile` with a block cache as the fallback;
  wrappers forward. ~100–150 lines,
  and the ones worth testing carefully: block boundaries, `seek` past
  EOF, `readline` across a boundary.
- sandtrap: the process-isolation RPC filesystem forwards the
  arguments. ~10 lines.
- nontainer: the drift test and both conformance runs; `LazyFS` grows
  `Range`; the blob endpoint answers 206. ~50 lines.

The cost is not code. It is a protocol change across termish and
monkeyfs (both to 0.2.0), sandtrap's `monkeyfs>=0.1.9,<0.2.0` floor
moving, nontainer's floors moving, and the four shipping in dependency
order. That choreography is why it is phase 0: it lives in other
repositories, it can run in parallel with everything above, and the
whole-file `LazyFS` means nothing waits on it.

## Spikes before committing

1. **Does Pyodide's snapshot survive dynamically-linked packages** on
   the pinned version (agex-studio pins 0.27.7)? numpy and pandas ship
   `.so` side modules; early snapshot support refused them. Cloudflare
   snapshots both, so it is at least solvable. Same harness: restore
   twice, `boot()` each, assert `random` and `numpy.random` diverge.
2. **First-visit bytes per profile**, and what a minimal profile
   (core Pyodide, no data stack) costs for a handler that only reads
   a JSON file. This number decides whether the first-byte hybrid is a
   knob or the default.
3. **Ranged reads reach pyarrow.** Confirm that pandas hands pyarrow
   the Python file object (it must — handlers read parquet under
   monkeyfs today) and that pyarrow then does column-selective `seek`
   / `read` against it rather than slurping the file. If it slurps,
   phase 0 still helps every other reader and the RPC bridge, but the
   parquet win needs a `pyarrow.parquet.ParquetFile` in the handler.

## Build order

**Phase 0 — the filesystem protocol** (termish, monkeyfs, sandtrap;
parallel with everything below, blocks nothing):
the ranged `read`, `open` made optional on monkeyfs's backend contract,
the lazy binary `VirtualFile` as the fallback, both conformance kits,
the promise in both READMEs, and the drift test in nontainer. Four
releases in dependency order.

**Phase 1 — publications, browser-served** (nontainer, no executor
needed):

1. The publish-time manifest in `store.publish`; the manifest and blob
   routes in `serve.py`; `LazyFS` in a `nontainer-guest` wheel (pure
   Python, installable with micropip), whole-file first and `Range`
   once phase 0 lands.
2. The visitor host-object bridge: the public-method allowlist, the
   JSON boundary refused at `publish`, the token-only endpoint, the
   publish-time surface report; the sentence in apps.md's hosting half
   and in the studio's building-apps skill.
3. `AppDriver`: extract the Playwright driver from `testapp.py` behind
   the protocol, move `ws-vitest` onto it, add `Runtime.app_driver`.
4. The frozen guest: the `api/*` interceptor, the in-tab dispatcher
   over `LazyFS` — `dispatch.py` itself under Pyodide, which is why
   the `[apps]` extra split lands here rather than at the far end —
   the hostcall endpoint, the tab driver, and publish-time
   verification through it.

**Phase 2 — the executor, as an offload** (nontainer):

5. `nontainer/executor_browser.py` beside `executor_dud.py`, reusing
   the guest wheel and the tab driver; the lazy-`open` note on the
   protocol; the cross-rung suites under a Playwright-driven guest.
   Push-tree for `sync()` first; `LazyFS` over a per-session endpoint
   when the author's rung wants it. Per-turn runtime choice with the
   server rung as fallback, the Pyodide profile derived from the
   grants and uncovered grants refused at `open`, so descriptions are
   true on both rungs by construction. Closing the server rung is a
   separate, later product decision, not a step here.

**Phase 2′ — the far end** (the alternative to Phase 2, sharing 0 and
1; chosen by whether server-driven turns matter):

5′. nontainer core under Pyodide (the `[apps]` split is already done
    by step 4); `LocalExecutor` as-is over a kvgit store on OPFS. A kvgit remote protocol with a Python twin of agex-ts's
    design and a nontainer server implementing it. A harness in the
    tab — agex-py, or agex v2 over nontainer — behind a JS shell.

**Phase 3 — the studio:**

6. An executor knob, a per-session socket, a rail state for "waiting
   for a browser", the snapshot pipeline, and the first-byte hybrid if
   the measured first visit needs it.
