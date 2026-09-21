# Browser roadmap

> **Status, 2026-09-21: the prerequisite and the seams are shipped;
> the browser-side pieces are not started.** Two proposals and one
> prerequisite. A **published app served from the visitor's browser**,
> with the server reduced to a host-object bridge; a **browser
> executor** — a third rung beside `LocalExecutor` and `DudExecutor`,
> where the agent's Python runs in a Pyodide worker in a tab while the
> provider, the loop and every versioning verb stay on the server; and,
> before either, a **ranged read on the filesystem protocol** that
> lives in termish and monkeyfs. [Where this stands](#where-this-stands)
> is the ledger; the rest is the design, kept at the length its
> decisions need. The full picture reaches into
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
either. The apps design is already serverless — no resident app
process, requests dispatched into sandboxed executions on demand,
handlers re-executed per request with no module state. Moving that
into a tab relocates a dispatcher that never had a server.

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
- **Most publications are static, and a static publication has no
  executor.** A tree with no handler under `app/api/` is served as
  files, on the server today and from anywhere tomorrow; the browser
  work is for the publications that have handlers.
- **Moving a handler into the visitor's browser moves a trust
  boundary, and the embedder answers it with the object.** The
  caller's identity is unprovable, so nothing between the visitor and
  a host object can be trusted to mediate. **A visitor-facing host
  object is a public API**: every public method, any arguments, anyone
  with the URL. The embedder hands the publication an object shaped
  for strangers, and hands the agent that same object in preview.
  nontainer's part is the bridge and a report of the surface, never a
  filter over the call.
- **Two principals, two endpoints, two origins.** The author's
  hostcalls land on the control origin under session auth; a
  visitor's on the apps origin under the token. The dispatcher runs in
  the principal's page.
- **The visitor's filesystem is `files_at` over the wire.** A
  publish-time manifest, content-addressed blobs on demand, byte
  ranges from day one. Nothing in a publication is private.
- **`test_app` is a driver protocol**, and the driver's dispatcher is
  the dispatcher the publication will use. The protocol and the
  Playwright driver are shipped; the tab driver is the half still to
  write.
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
  What it buys is statelessness and deferred sync, not compute, and
  offline authoring only with bring-your-own-key. What it gives up is
  server-driven turns; the question after Phase 1 is whether those
  matter.
- **nontainer's `Executor` protocol changes in one place:** `open`
  may bind lazily.

## Where this stands

The build order at the end is the plan; this is the ledger against it.

| piece | state | where |
|---|---|---|
| Phase 0: ranged `read` on the protocol, lazy binary `open`, both conformance kits, the drift test | **shipped** | termish 0.2.0, monkeyfs 0.2.2, sandtrap 0.4.0, nontainer 0.7.7 |
| Static publications: no handler, no executor | **shipped**, not in the original plan | nontainer 0.7.7, `ws.runtime.executes` |
| Phase 1 step 3: `AppDriver`, the Playwright driver, `ws-vitest` on it | **shipped** | nontainer 0.7.7, `docs/apps.md` "The driver seam" |
| Phase 1 steps 1 and 4: manifest and blob routes, `LazyFS`, the in-tab dispatcher, the hostcall endpoint, the tab driver | not started | one deliverable; the first Pyodide engineering |
| Phase 1 step 2: the visitor host-object bridge | not started | earns its keep only with steps 1 and 4 |
| Phase 2 or 2′: the executor as an offload, or the harness in the tab | proposal | gated on whether server-driven turns matter |
| Phase 3: the studio | proposal | follows whichever of the above is chosen |

What the drift test bought before the browser work used it: six
divergences, three in monkeyfs, one in nontainer's view layer and two
in the AgentFS provider, every one a listing or a directory-creation
edge that a browser guest would have hit on its first day.

**The open decision** is whether to build the browser-served tier now.
Two of its pieces have not been spiked: the in-tab request interceptor
and the hostcall endpoint. The recommendation is one bounded spike on
those before the brief is written, the way the two spikes below
de-risked Phase 0.

**Issues tracking the rest.** [#112](https://github.com/ashenfad/nontainer/issues/112)
is the serving-substrate dispatcher behind `test_app`, the half of the
driver invariant the seam did not close;
[#110](https://github.com/ashenfad/nontainer/issues/110) is the typed
return codec, now scoped to server-side serving of publications with
handlers; [#113](https://github.com/ashenfad/nontainer/issues/113) is
idle view-worker reaping, untouched by any of this;
[#102](https://github.com/ashenfad/nontainer/issues/102) drops the
legacy on-disk migrations and waits for a release willing to say so.

## What is already true

- **The stack runs under Pyodide today, measured.** nontainer core and
  `dispatch.py` run unmodified from the published wheels under Pyodide
  0.27.7 — a memory-backed workspace, `terminal`, `run_python` with
  `cache`, commit, fork, three-way merge, `ws-git`, `enable_apps`,
  `ws-curl`, a GET and a POST through `AppRuntime.dispatch`, and
  sandbox timeout enforcement, with pandas from Pyodide's own index —
  at 1.5 s from `loadPyodide` to a working workspace and under a
  millisecond per warm request. The one thing that does not exist
  there is the process-isolation worker (no `multiprocessing`), which
  sandtrap reports as `IsolationUnavailable`. kvgit already ships an
  IndexedDB backend, selected automatically under Pyodide, beside an
  OPFS-mounted disk option, so the far end's tab-side store exists;
  what it lacks is the remote protocol.
  [agex-studio](https://github.com/ashenfad/agex-studio) has run the
  same libraries plus a Python harness in a Pyodide worker for a year.
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
  read-only workspace, a publication is the derived commit holding
  `app/` and nothing else, and since 0.7.7 a publication with no
  handlers has no executor at all: `ws.runtime.executes` is false,
  `/api/` is the missing-endpoint 404, no view worker is warmed, and
  execution settings passed to the open are accepted and unused so an
  embedder's per-publication table need not know which tier a row
  lands in.

## When it is worth it

For a single-user local workbench, the executor as *sole* compute is
never worth it: it moves compute from one process on a laptop to
another on the same laptop and pays with background turns. As an
*offload* beside the server rung it is a latency-and-cost win with
nothing given up. The larger question is whether the studio is going
to be **hosted**. If it is, closing the server rung is plausibly the
cheapest path to multi-tenancy — cheaper than a microVM pool, with
keys server-side — and the browser-served publication is the piece to
build first, alone, because it stands without the executor and removes
the ugliest line in the current threat model: anonymous HTTP
triggering agent-authored code on the server.

What it costs, so the judgment is made with the bill in view:

- **Background turns.** "Close the tab mid-turn, the work continues"
  dies by construction on the browser rung.
- **The Pyodide ceiling.** No native dependency outside Pyodide's
  index, no real bash, iOS Safari's per-tab memory budget. nontainer
  has no TypeScript escape hatch, because Python is the point.
- **A third rung to keep honest.** The cross-rung conformance suites
  grow a variant, and so does whatever the rung does differently on
  the embedder's side of the seam. What the agent is told does not
  (below).
- **First data paint on a browser-served app** is the runtime boot
  plus the data the first handler touches. Warmth and lazy fetch make
  that small; they do not make it zero.

None of these is a security cost for the author's session. For
publications, risk is traded rather than added: today's surface is
sandbox plus handler plus host object, all on the server; browser-side
it is the bridge alone, exposing an object the embedder chose for
strangers. An object's public methods are a smaller thing to defend
than a sandbox, and they are the embedder's to shape.

### Executors are switchable, so the browser rung is an offload

The costs above assume the tab *is* the compute. It need not be. The
design notes already say swapping the executor never touches the
versioning semantics; the studio already reads on one executor what
was authored on another; and `diff` / `sync` are specified as "called
around execs, never during", which is exactly the turn boundary a
switch needs. A switch is choosing which runtime takes *this* turn,
and `ctx.head` affinity makes a switch back to a tab that still holds
the head free. Every cost of the form "when the tab is absent, X dies"
becomes a fallback: a closed tab, a phone's memory budget, a helper
importing outside the Pyodide profile — each means the server for that
turn, and the capability chooses the rung.

Two things the switch does not dissolve:

- **The threat-model win** — the server never runs agent code — comes
  only from *removing* the server rung, which is a product choice. So
  the executor has two modes with different prerequisites, offload
  (now, no regression) and tab-only (hosted-cheap, commits), and this
  document names both rather than blurring them.
- **The environment the agent is told about must be the same on both
  rungs.** Skills and tool descriptions are resolved per executor
  today because local and dud genuinely differ (real bash against
  termish). The offload pair is **local ↔ browser**, and those two do
  not differ that way: both are termish plus sandtrap, same builtins,
  same `ws-*` verbs, ticks on both, one `PythonConfig`. The package
  set is made invariant by construction: the Pyodide profile is
  derived from the same grants, and a grant the profile cannot cover
  is refused at `open`, the way dud refuses what a rung cannot honour.
  Never pair **dud ↔ browser** per turn: that crosses the bash/termish
  line and the agent would notice. Cache pickles cross rungs, so the
  guest stack is pinned to the server's.

Whether to then close the server rung is the commitment question, and
it stays a visible product decision rather than something the switch
quietly answers.

## Publications in the visitor's browser

This section is about publications that have handlers. A publication
without them is already served as files with no executor anywhere,
which is most of them, and nothing here applies to it.

A visitor's browser needs only the frozen subset of a guest: a
read-only tree, no harvest, no sync, no verbs, no delegation. Ship
`app/` plus a Pyodide runtime, intercept `api/*` in the page, dispatch
into the worker; the server sees hostcalls and nothing else. Authoring
can stay server-side while publications move.

### The trust boundary moves, and what that does and does not mean

Today a published app has three parties: the author's agent wrote the
handler, the server runs it, the visitor sends `req.params`. The
handler is a boundary because it is author-controlled code running
where the visitor cannot touch it. Move it into the visitor's browser
and it is a file on the attacker's machine: the `db` handle granted to
*the author's agent* is now, in effect, granted to *everyone with the
URL*.

This is **not** a reason to trust the agent less. For the author's own
session nothing narrows: same object, same allowlist, one socket. What
changes is a *second principal* standing where the handler used to
stand, and a judgment that has to be re-made for it: "safe to give the
author's agent" is not "safe to give a stranger". nontainer made the
same move once on the filesystem side — the derived publication commit
exists because a handler reads the whole tree and an export hands over
the whole tree. Whatever the artifact can reach, the visitor can reach.

Two things follow. **The identity of the caller is unprovable.** No
mechanism lets the server know that *this frozen code* on the
visitor's machine produced *this call*: a key in the handler is
extractable, a hash of the running code is a value the client can
send, attestation vouches for a browser or a device and never for
which script ran. **So nothing between the visitor and the object can
be trusted to mediate.** Not the handler, not a check in it, not a
harvest of what the handler was written to call — every one is a
property of code the visitor can rewrite. What remains is the object,
and the object is the embedder's. A filter that can be half-right
invites reliance on the half that is wrong, so nontainer does not
build one.

### The host object is the API

**Where an object is handed decides who reaches it.** Objects on a
session's `PythonConfig` are the author's: the cooperative agent
acting for the author, with whatever the embedder judged safe for
that. Objects passed at `pub.open` are the visitor's. Under
browser-side serving a visitor-facing object is a **public API** —
every public method, with any arguments, to anyone holding the URL —
and it must be safe on those terms. The same object serves three
audiences depending on where it is handed: the author's agent in a
session, visitors through author-written handlers when served
server-side, visitors directly when served browser-side.

That is the whole security story, and it is one the embedder was
already telling: `apps.md`'s hosting half is "the embedder's half"
throughout, and the studio, as its own embedder, already keys a db per
app. Validation that used to sit in a handler sits in the object —
`add_score(name, score)` clamps and checks because the object does —
and once it does, writes are as safe browser-side as reads. So the
whole app runs in the visitor's browser, GET and POST alike, with no
verb split and no per-handler routing.

What nontainer owes is the seam, kept deliberately dumb:

- **The bridge exposes public methods and nothing else.** The same
  allowlist dud uses (`dud.public_methods`), JSON in and out. A method
  whose arguments or result cannot cross as JSON is refused when the
  bridge is built — at the `Publication.open` that serves, where the
  objects are bound — before any visitor, not at a visitor's first
  call. `publish` never sees a host object, so it cannot be the place.
- **The visitor endpoint takes only the token** (below): no cookies,
  no control-origin credential, so an operator visiting their own app
  never spends session authority through it.
- **A surface report at the same moment.** The serving open lists the
  public methods being exposed, with signatures, and an embedder that
  opens on publish — the studio does — shows it in the publish output,
  which is where the author sees what "anyone with the URL" now has. A
  report, not a gate.
- **One sentence in the hosting half of apps.md, and one in the
  skill.** *A visitor-facing host object is a public API. Give the
  published app a narrower object than the session, and give the
  agent that object in preview, so nothing verifies green and breaks
  published.* One declaration, both lifecycles, the way CSP and
  `script_hosts` already work.

For a per-app sqlite file that is the app's own data, a studio can
hand out a read-only connection and say "anyone with the URL can read
this app's whole database" — true, stated, and fine for what the
studio publishes. Writes need the studio to design the object that
takes them, which was always its decision. An embedder fronting a
shared production db hands the agent a narrower object *from the
start*, in preview and published alike.

### Two principals, two endpoints, two origins

The bridge serves two principals, and they do not share an endpoint
because [apps.md](apps.md#hosting-for-real-the-embedders-half) already
forbids them sharing an origin — *give the app origin no ambient
authority; `{token}` is the capability and it is sufficient*:

| principal | where the hostcall lands | credential | what it reaches |
|---|---|---|---|
| the author's session | `/api/sessions/{name}/host/…` on the **control origin** | the studio's session auth, like every other `/api/sessions/{name}` route | that session's live host objects, the full public-method allowlist |
| a publication's visitor | `/apps/{token}/host/…` on the **apps origin** | the token in the URL — the capability, sufficient, no cookie | the publication's visitor-facing host objects, as handed at `pub.open` |

The endpoint's existence encodes the principal; nothing inspects a
caller to decide which rules apply. **The dispatcher runs in the
principal's page**, which is the rule the table falls out of. For a
visitor, the Pyodide worker lives in the visitor's page on the apps
origin, so its hostcalls are token-scoped by construction. For the
author, the worker lives in the *studio* page, not the preview iframe,
so its hostcalls carry the studio session by construction; the
sandboxed preview iframe never reaches the control origin at all — its
`fetch('api/x')` is intercepted in-page and posted to the parent, which
runs the handler in the author's worker. That interceptor is the one a
browser-served publication needs anyway, and it retires the `*` CORS
header the preview needs today. A delegate that runs in the author's
tab inherits the author's principal, which is right.

What a hosted studio owes regardless: session auth on the control
origin and CSRF defence on its mutating routes; the author hostcall
endpoint is one more POST route under that protection. The one
deliberate line is the visitor endpoint refusing cookies and any
control-origin credential outright — "no ambient authority" applied to
the bridge, one line of code, one line of doc. Rate limits stay an
edge concern and the Referer leak of a URL token is unchanged.

### The app's data ships lazily

Shipping `app/` as a tar makes a 50MB parquet a 50MB download before
first data paint. It is not a new disclosure — static serving already
serves everything under `app/` except `app/api/` to any visitor. What
does change is `app/api/` itself: browser-side, the handler source
ships to the visitor, and the skill's "`_`-prefixed files under
`app/api/` are as private as handler source" inverts. Under
browser-side serving **nothing in a publication is private** — a
secret belongs in a host object, server-side, or nowhere. A stronger
and simpler rule than the current one; one line in the skill.

The bytes problem has the provider's own answer: `files_at(commit)` is
"a read view, not a copy: values are read on demand", and the tab gets
exactly that over the wire.

- **Metadata up front, blobs on demand.** `store.publish` already reads
  every blob to copy it into the derived commit; while it does, it
  hashes each and writes a manifest — `{path: (sha256, size)}` — as one
  reserved key in that commit, beside `published_from`. The tab builds
  its termish `FileSystem` from the manifest (listings, `exists` and
  `stat` all answer locally), and a file's contents are fetched on
  first `read()`, over the same sync channel hostcalls use, from
  `/apps/{token}/blob/{sha256}` — with
  [phase 0](#phase-0-one-filesystem-protocol-and-a-ranged-read)
  shipped, as the byte ranges a handler actually touches.
- **Content-addressed, so cached across versions.** The blob URL is
  immutable, so a v2 that did not touch the parquet does not
  re-download it, and the browser cache does the rest. Not across
  apps: the token is in the path, so two apps over one dataset are two
  URLs and the HTTP cache shares nothing between them. Cross-app reuse
  would need a token-independent route on the apps origin with the
  token carried in a header for the manifest check, and is an option,
  not a promise. The guard holds either way: a hash is servable only
  if it is in *this* token's manifest, or content addressing becomes a
  cross-app oracle.
- **No executor on the server.** A browser-served publication
  instantiates no `AppRuntime` and no executor server-side: the server
  does provider reads, static, and the hostcall bridge.

Size: ~30 lines in `store.publish`, ~40 in `serve.py`, ~100–150 for
`LazyFS` in the guest. No seam moves. The honest limit is that first
data paint on a data-heavy app is the runtime boot plus the *working
set* of the first handler — the columns, after phase 0, rather than
the file — and the working set is what it is. A publish-time report
("this app ships 48MB under `app/data/`", read off the manifest) tells
the author before a visitor finds out.

## `test_app` is a driver protocol

*Shipped in 0.7.7: `AppDriver`, `DriveSpec` and `DriveReport`, the
Playwright driver, `ws-vitest` on the same driver, `AppsConfig.driver`
and `Runtime.app_driver`, and a fake driver that tests the neutral
half without a browser. `docs/apps.md` has the shipped shape. Still to
write: the tab driver, and the serving-substrate dispatcher behind the
Playwright driver ([#112](https://github.com/ashenfad/nontainer/issues/112)).*

Under browser-side serving, a `test_app` that is Playwright on the
server hitting server-side dispatch never exercises the runtime the
publication will run on: a handler importing something present in the
server's grants and absent from the Pyodide profile passes every check
and 500s for every visitor. The fix is not "run Playwright in the
tab"; it is a seam. A driver takes a spec — the actions, viewport,
policy, timeouts, init scripts, and how the app is served — runs the
whole action list, and returns a raw report; the neutral half keeps
annotating stacks against the workspace, saving screenshots, and
rendering. The action loop lives in the driver because a tab driver is
a batch postMessage bridge, not a per-primitive RPC.

**Who picks:** `AppsConfig.driver` if set; else the executor's
(`Runtime.app_driver`, probed the way `supports_ws_verbs` is); else
Playwright. **The invariant that makes this close the gap rather than
only abstract it: the driver's dispatcher is the dispatcher the
publication will use.** Playwright verifies against server-side
dispatch, which is what server-side serving runs; the tab driver
verifies against the in-tab Pyodide dispatcher, which is what
browser-side serving runs. Preview and publication share a runtime by
construction, and publish-time verification comes free: the tab driver
can run the same actions against the frozen tree on the
visitor-shaped runtime before `publish` returns.

The tab driver is the spec crossing the executor's socket, the tab
building a hidden iframe from its own tree (agex-studio's
`buildAppHtml` and postMessage bridge are this, down to the action set
and the `__agex_logs` buffer), and the report posted back. Under it
the server needs no Playwright at all.

### Where the tab driver is weaker

Rung-specific caveats, most absorbed by the driver; the one that
reaches the agent is the screenshot, and the description says so on
that rung:

- **Screenshots.** A hidden iframe on a separate apps origin cannot be
  captured by its parent; the page rasterizes itself and posts the
  PNG back, which loses WebGL plotly, tainted canvases and some fonts.
  Fine for "is there a chart", not pixel-faithful — or the tab driver
  returns no screenshot and says so.
- **Fresh context.** A cross-origin iframe has a persistent partition,
  so repeated tests see each other's `localStorage` unless the driver
  seeds and discards, as agex-studio's `testApp` does.
- **Blocked requests.** `page.route` denies and *reports* off-allowlist
  fetches. An iframe enforces through an injected `<meta>` CSP and
  observes through `securitypolicyviolation` events — what
  `window.__nt_csp` already collects — so the report shape survives
  while the enforcement is a little weaker: no `frame-ancestors`, no
  report-only.
- **Presence.** `test_app` on the tab driver waits for a browser the
  way `run_python` does.

## The executor

The inversion from dud is that **the guest dials in**. dud's host
drives a guest it owns; here the guest is a tab that connects to the
server, so the server-side executor is a stub bound to a socket. It
reuses the guest wheel and the tab driver the publication work builds.

| concern | `DudExecutor` | browser executor |
|---|---|---|
| tree | tar push over the dud channel | the same tar over the socket; `ctx.head` affinity so a tab already holding the tree skips it |
| harvest | scan / overlay diff → `StagedDiff` | scan-diff in the worker against a shadow of the pushed tree; whole-file payloads |
| host objects | hostcall proxies behind `dud.public_methods` | the same allowlist; a proxy method is a sync XHR to the control origin, session-scoped, dispatched to the live object exactly as `_host_object_rpc_handler` does |
| `ws-*` verbs | bash functions → `ws_verb` hostcall; the host *pulls* the guest's diff before dispatching | the guest shell is termish, so each tagged verb is a relay *command* in the guest registry; `supports_commands` is True and the `FerrySpec` path mapping applies unchanged. One inversion: a worker blocked in a sync XHR cannot answer a pull, so the relay command ships its scan-diff *inside* the verb call and the host's absorb step consumes a pushed diff rather than calling `diff()` |
| cache | guest pickles, host stores bytes | identical |
| isolation | a VM exceeds any `isolation` | `none` and `process` are accepted on crash containment — a worker crash costs the worker, not the page — with memory containment the tab's budget rather than a promise; `memory_limit_mb` and `kernel` are refused at open, the way `_prepare_config` refuses what a rung cannot honour |
| ticks | gone; wall-clock only | back — sandtrap is in-process in the worker |
| packages | grants → image package list | grants → Pyodide package list, the same `_merge_packages` idea |
| view reentrancy | one channel, serialized | one interpreter per tab, serialized; a worker pool is a memory question the tab answers |

**Push-tree for authoring, `LazyFS` later.** Push-plus-harvest matches
the existing contract and the cross-rung conformance suites exactly,
so the author's rung starts there; later it swaps `sync()` from "push
a tar" to "send a fresh manifest of `working_files()`" against the
same `LazyFS` and a per-session, authenticated blob endpoint. Only the
host→guest direction changes.

**Network honesty.** sandtrap's network denial patches `socket`, which
does not exist under Pyodide; `network=True` would have to mean
allowing `pyfetch` / XHR, and the policy story needs one paragraph
saying so rather than a knob that reads as a policy and does nothing.

**A closed tab is a lost guest.** `HarvestLost` already describes the
torn call. What changes is the product: a turn that runs while the tab
is closed cannot exist on this rung. Delegates stay on `LocalExecutor`
at first (see *Smaller decisions*).

### What nontainer has to change

One thing. `Executor.open` is called synchronously as the last step of
`Runtime.__init__`, and a browser executor's guest may not exist at
that moment: the socket connects when the tab does. So `open` must be
allowed to **bind lazily** — hold the context, attach when a guest
dials in, and let the first exec wait, bounded, with "no browser
attached" as an errored result rather than an exception. A note in the
protocol docstring, in the same register as `close` being best-effort.
The cross-rung suites (`tests/test_wsgit_conformance.py`,
`tests/test_wscurl_conformance.py`, the two test verbs) are the
acceptance test: run them with a Playwright-driven Pyodide guest and
the rung is real or it is not.

## The far end: the harness in the tab

The executor above exists because the harness is on the server. Move
the harness into the tab and the executor question dissolves: the
executor is `LocalExecutor` — termish and sandtrap in the same Pyodide
interpreter as the `Workspace`. No socket, no tree push, no harvest,
no lazy `open`, no `HarvestLost`. nontainer core runs there today; the
client half is agex-studio's py-kernel with nontainer as the
environment, and kvgit's IndexedDB backend persists state in the tab
without any new store.

**The server is a kvgit remote, not a provider.** `WorkspaceProvider`
over RPC — the `Workspace` in the tab holding the single-writer lock
across a wire — works and is chatty. The other shape is agex-ts's
[`kvgit-remotes.md`](https://github.com/ashenfad/agex-ts/blob/main/kvgit-remotes.md)
verbatim: kvgit lives in the tab, the server is a passive object store
with CAS-able refs, sync exchanges deltas on commit, reconciliation is
always local, v1 fast-forward only with divergence surfaced. A
nontainer server is that protocol on a simpler transport, worth having
on its own for multi-device sync, and the natural role for a server in
a stack whose state is git-shaped and authoritative. What the server
does then: auth, the remote, the publication registry, browser-served
publications (Phase 1 unchanged), the host-object bridge, and
optionally an LLM proxy so keys stay server-side. **No Python executes
there except host objects.**

**What it buys, stated precisely.** Not compute: a harness turn is LLM
calls, tool dispatch and commits, all milliseconds, and the expensive
part already moved to the tab in Phase 2. What it removes is *state*:
the hybrid holds a live `Workspace`, a `Runtime`, an executor stub, a
socket and the single-writer lock per active session in one Python
process, so sessions are pinned to a process and memory scales with
connected users; a remote holds nothing per session and any replica
answers any request. Beyond that, deferred sync — a flaky connection
or a server restart mid-session loses nothing — and a transport with
no moving parts. It does *not* buy offline authoring on its own: a
turn needs a model, so unless the user brings their own key
(agex-studio's shape) or runs a local one, every turn still goes
through the server's LLM proxy. Offline is a property of the key
arrangement, not of the architecture.

**The harness is the studio's, as it always was.** nontainer names the
`SessionRunner` seam and stops. agno under Pyodide is unproven (its
dependency weight, not its shape); agex-py's loop is proven there; an
**agex v2 whose environment is nontainer** is the natural closing of a
loop the README already draws. What nontainer owes the far end is core
under Pyodide (the `[apps]` extra split, so `dispatch.py` imports
neither starlette nor playwright — packaging, since the layering rule
already keeps dispatch on core's API) and the remote.

**What it gives up: server-driven turns.** A nightly analysis, a
webhook-triggered turn, a delegate that outlives the session — anything
that needs an agent to run without a human's tab has no place to run,
and that is the class the current studio's background turns and
delegates serve. Two honest answers: accept it, as agex-studio does; or
let the server be a *client of its own remote* — a `LocalExecutor`
harness on the server, syncing like any other device, for scheduled
work only, which is switchability reappearing as "the server runs
agent code by choice, for autonomy".

**What it does to the plan.** Phase 2 as written and the far end share
Phases 0 and 1 entirely and differ in what is built next: an executor
stub and its socket, versus a kvgit remote and core under Pyodide — and
the far end is plausibly *less* nontainer work, because `LocalExecutor`
is used as-is and the remote is already designed. So the question after
Phase 1 is **"do server-driven turns matter?"** — yes, the hybrid; no,
the far end.

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
memory snapshot (`_makeSnapshot` / `_loadSnapshot`, still experimental;
Cloudflare's Python Workers are the production user) is the browser's
forkserver. The "swap in the fs" half is nearly free because workspace
files never live in MEMFS: monkeyfs routes sandboxed `open()` to
termish's `FileSystem`, a Python object, so the pristine snapshot holds
no workspace state and materializing a session is building a
`MemoryFS` from a tar or a manifest.

None of that is nontainer's. Whether a tab's warmth comes from a
snapshot, a shared worker across tabs of one apps origin, or a profile
per app derived from the handlers' imports is the studio's to decide.
nontainer's contribution is `ctx.head` as the affinity tag and the
lazy `open` above: the tab boots in the background and the first
`run_python` waits, as `dud-vm`'s first image build already does.

Where the cost lands: for an author, one warm worker per session; for
a visitor, per tab, once, on **time-to-first-data** — ~1s on a repeat
visit, download-bound on the first. A profile with pandas is 10–40×
the app's own vendor bundle, and that number, measured per profile,
decides whether the first-byte hybrid — route `api/*` to the server's
existing route until the worker reports ready, then flip — is a knob
or the default.

### What a snapshot freezes, and `boot()`

A snapshot captures the heap after imports, so anything drawn from the
environment *at import* is frozen into every restore: `random`'s and
`numpy.random`'s global generators (every visitor's first
`random.sample(rows, 10)` is the same ten rows), the per-process
`hash()` secret, any module-level clock capture, `os.environ`, cwd,
locale. `secrets`, `uuid4`, `os.urandom` and `time.time()` reach the
platform at call time and are fine. In this deployment that is a
correctness footgun, not a boundary: the heap is the visitor's own tab,
and the only party who could exploit a known seed against it is the
operator who built the image, who already runs code in it.

Whether to snapshot is the embedder's; the guest's correctness after a
restore is nontainer's, because the guest wheel is nontainer's. So:

- **`nontainer_guest.boot()` reseeds unconditionally** — `random.seed()`
  and `numpy.random.seed()` with no argument, every boot, snapshot or
  not.
- **The snapshot is taken before `boot()`, never after.** The one rule
  for the embedder, and what makes the image **policy-free**: the
  `Policy`, the sandbox, `LazyFS` and cwd are all built in `boot()`
  from what the server sends, so one image per *package profile*, not
  per policy or per app.
- **The principle, once:** *a snapshot is a pristine interpreter —
  imports, no execution.* Anything derived from the environment at
  import is re-derived at boot.
- **`hash()` is accepted.** CPython fixes the secret at start and it
  cannot be reseeded; harmless here for the reason above.

The spike restores one image twice, runs `boot()` in each, and asserts
the two diverge on `random.random()` and `numpy.random.random()`.

## Smaller decisions

Questions with one decision each, recorded so nobody re-derives them.

**Cancellation: terminate by default, interrupt buffer opt-in; hostcalls
are sync XHR.** sandtrap's `cancel()`, `timeout` and `tick_limit` are
checkpoint flags, and a busy worker never runs its event loop, so a
`postMessage` carrying "cancel" is never delivered while sync Python
runs. `pyodide.setInterruptBuffer` raises `KeyboardInterrupt` at the
next bytecode boundary and the worker survives, but its
`SharedArrayBuffer` needs COOP/COEP on the studio origin, which
constrains what the page may embed; `worker.terminate()` needs no
headers, always works, and costs the warm image (~1s restore). Cancel
is rare and a second on cancel is fine; a header requirement on the
whole studio page is not, so terminate is the default and the interrupt
buffer is an option for an origin already cross-origin isolated. The
same header requirement rules out `Atomics.wait` as the hostcall
channel, so hostcalls are synchronous XHR from the worker, as
agex-studio's are. Deadlines mirror dud: the tab enforces
`PythonConfig.timeout` plus a grace by terminating; the server treats
silence past that as a lost guest; a sync XHR hostcall carries the
exec's remaining budget as its `timeout`.

**`isolation`: `process` is accepted on crash containment,
`memory_limit_mb` and `kernel` are refused.** dud refuses
`network=True` because it is a *capability* it cannot honour, and
accepts any `isolation` because a VM *exceeds* it. A Web Worker does
not exceed `process`; it meets half of it. sandtrap's `process`
promises that a crash, a memory blowup or a segfault in sandboxed code
cannot affect the host. A worker gives the crash half: a trap or an
uncaught exception kills the worker and not the page. It does not give
the memory half: workers share the renderer, a worker that outgrows
the browser's per-tab budget takes the tab with it, and nothing on the
wasm side enforces a megabyte limit. The party at risk from that
shortfall is the user's own tab, not the server, whose state is the
provider's and survives a lost guest. So the rung accepts `process` as
met on crash containment, says so in the executor's description, and
refuses the one promise it cannot keep: `memory_limit_mb` set is a
capability the browser cannot honour and is refused at `open`, as
`kernel` is for its syscall filter. The studio's default
`isolation="process"` is then valid on both rungs with one
`PythonConfig`; a config that also sets a memory limit is a config
that has chosen the server rung.

**Delegates stay on `LocalExecutor` first.** A delegate is compute the
user never watches; running it in the author's tab makes the tab a
compute farm and ties it to the tab's lifetime. The store already
tolerates sessions authored on one executor and read on another. A
second worker in the author's tab is a later option.

**Two tabs on one session: last attached wins.** State is server-side
and the workspace lock serializes, so this is availability, not
correctness. A new attach detaches the previous guest — its socket
closed with a reason it can show — after any exec in flight on it
finishes, and `ctx.head` affinity means the newcomer pushes the tree
only if its head differs.

**The image holds the stack, not the guest wheel.** Key a snapshot on
(Pyodide version, package lockfile hash). sandtrap, termish, monkeyfs
and kvgit are in it; `nontainer-guest` is not — it is pure Python and
small, so loading it after restore is milliseconds, and a nontainer
release invalidates nothing unless it moved a stack floor.

## Phase 0: one filesystem protocol, and a ranged read

*Shipped: termish 0.2.0, monkeyfs 0.2.2, sandtrap 0.4.0, nontainer
0.7.7, in one day, in dependency order.* Every browser piece above
reads workspace files across a wire, and every read used to be
whole-file because the protocol said so: `read(path) -> bytes`, and a
binary `open()` was a `BytesIO` over the whole file. The shape that
landed, recorded here because the guest's `LazyFS` will be built on
it:

- **The primitive** is `read(path, offset=0, size=-1) -> bytes`, on
  both termish's and monkeyfs's `FileSystem` protocols. Defaults are
  the old whole-file read; a negative offset is an error; a read at or
  past the end is empty; an overrun is truncated.
- **No shared protocol import.** Both protocols stay structural
  duplicates, and the convention is enforced rather than declared:
  nontainer's `tests/test_fs_protocol.py` compares every method
  signature across the two and runs both libraries' conformance kits
  (`termish.fs.check_filesystem`, `monkeyfs.check_filesystem`, each
  shipped so a backend author needs neither the other library nor
  nontainer) against every filesystem nontainer ships. The guest's
  `LazyFS` joins that list when it exists.
- **`open()` is monkeyfs's to provide, and optional on the backend.**
  A backend that offers none gets `LazyBinaryFile`, a small block cache
  over the ranged read; text and write modes still materialize. The
  real-directory backend keeps its real `io.open` handles. sandtrap's
  process-isolation bridge forwards the two arguments, so an isolated
  worker's binary `open()` is lazy for free.

Who benefits, in order: the browser guest, where a ranged `read` is an
HTTP `Range` request so `pd.read_parquet(path, columns=[...])` moves
only the columns; process and kernel isolation, already; the author's
browser rung later, over a per-session endpoint. What the estimate
missed: the conformance kits, written to guard the protocol, found six
real divergences before any browser guest existed to hit them.

## Spikes

Two ran before Phase 0 and settled it; one is proposed before the
browser-served tier is committed to; two remain for Phase 2.

**Done.** nontainer core and `dispatch.py` run unmodified under Pyodide
(the measurements are under *What is already true*). And ranged reads
reach pyarrow through pandas without any handler change: 2 of 20
columns of a 53 MB parquet read 8% of the file in three reads, the same
with `pre_buffer` on or off and the same through plain
`pd.read_parquet(path, columns=[...])` as through `ParquetFile`; on a
fixture with eight row groups and 7 MB gaps between the wanted chunks,
coalescing never bridged into an unselected column. This works because
pandas opens a string path with Python `open()` and hands pyarrow the
file object — which is also why handlers can read parquet under
monkeyfs at all.

**Before the browser-served tier.** The in-tab request interceptor (how
`fetch('api/x')` from the app's page reaches the worker: service
worker, fetch patch, or the postMessage bridge the preview iframe
already uses) and the hostcall endpoint (a sync XHR from the worker
against `/apps/{token}/host/…`, budgeted by the exec's deadline).
Neither has been built anywhere in the family for a published app; a
day on both, against a static publication served by `build_router`, is
what the brief for steps 1 and 4 should follow rather than precede.

**Before Phase 2.**

1. **Does Pyodide's snapshot survive dynamically-linked packages** on
   the pinned version (agex-studio pins 0.27.7)? numpy and pandas ship
   `.so` side modules; early snapshot support refused them. Cloudflare
   snapshots both, so it is at least solvable. Same harness: restore
   twice, `boot()` each, assert `random` and `numpy.random` diverge.
2. **First-visit bytes per profile**, and what a minimal profile
   (core Pyodide, no data stack) costs for a handler that only reads
   a JSON file. This number decides whether the first-byte hybrid is a
   knob or the default.

## Build order

**Phase 0 — the filesystem protocol** (termish, monkeyfs, sandtrap,
nontainer): *shipped 2026-09-20; see the ledger and the section
above.*

**Phase 1 — publications, browser-served** (nontainer, no executor
needed). *Settled first, in 0.7.7: a publication with no `app/api/`
handler opens with no executor at all and is served as files, so the
tier below is for publications that have handlers.*

1. The publish-time manifest in `store.publish`; the manifest and blob
   routes in `serve.py`; `LazyFS` in a `nontainer-guest` wheel (pure
   Python, installable with micropip), ranged from the start.
2. The visitor host-object bridge: the public-method allowlist, the
   JSON boundary refused at the serving open, the token-only endpoint,
   the surface report at that open; the sentence in apps.md's hosting
   half and in the studio's building-apps skill.
3. `AppDriver`: extract the Playwright driver from `testapp.py` behind
   the protocol, move `ws-vitest` onto it, add `Runtime.app_driver`.
   *Shipped in 0.7.7.*
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
   grants and uncovered grants refused at `open`. Closing the server
   rung is a separate, later product decision, not a step here.

**Phase 2′ — the far end** (the alternative to Phase 2, sharing 0 and
1; chosen by whether server-driven turns matter):

5′. nontainer core under Pyodide (the `[apps]` split is already done
    by step 4); `LocalExecutor` as-is over a kvgit store on IndexedDB
    or OPFS; a kvgit remote protocol with a Python twin of agex-ts's
    design and a nontainer server implementing it; a harness in the
    tab — agex-py, or agex v2 over nontainer — behind a JS shell.

**Phase 3 — the studio:**

6. An executor knob, a per-session socket, a rail state for "waiting
   for a browser", the snapshot pipeline, and the first-byte hybrid if
   the measured first visit needs it.

**Next.** Steps 1 and 4 of Phase 1, as one deliverable, after the
interceptor and hostcall spike above; step 2 alongside them, since a
publication served from a tab is what makes the visitor-facing object
a public API in practice. Phase 2 against 2′ is decided after that, on
whether server-driven turns matter.
