# A browser executor

> **Status: design proposal, not implemented.** A third rung beside
> `LocalExecutor` and `DudExecutor`: the agent's Python runs in a
> Pyodide worker in a browser tab, while the provider, the agent loop
> and every versioning verb stay where they are. The full picture
> reaches into [nontainer-studio](https://github.com/ashenfad/nontainer-studio);
> the nontainer half is written down here so the studio half has a
> contract to build against. Why the seams make this cheap is in the
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
   else. This one moves a trust boundary and is treated separately
   below.

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

## Phase 0: one filesystem protocol, and a ranged read

Everything below reads workspace files across a wire — a tab pulling a
tree from the server, a visitor's browser pulling a publication, a
worker process pulling from the host over RPC. Today every one of those
reads is whole-file, because the protocol says so: termish's
`FileSystem.read(path) -> bytes`, and monkeyfs's `VirtualFile` is a
`BytesIO` over the whole file, seeked to zero, written back on close.
"Seekable" is a fiction over a full buffer. So a handler that wants two
parquet columns of twenty downloads the parquet. This phase fixes that
at the protocol, in two other repositories, before anything here needs
it — and nothing here *blocks* on it: a whole-file lazy fetch works
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
  monkeyfs provides over it, never what a backend implements.* That
  shrinks monkeyfs's backend contract (`open` leaves it; `readlink`
  becomes optional and probed, the way nontainer probes `refresh()`),
  and makes every termish-shaped filesystem a Python `open()` for free.
  `VirtualFile` is the adapter from bytes-level to file-object-level,
  and the only place that adapter lives — which is already the shape of
  `MountFS.open` and `ReadOnlyFS.open`, both of which delegate.
- **A drift test where both are installed.** The repository's rule for
  conventions is that they should not be conventions —
  `tests/test_layering.py` walks the package with `ast` rather than
  asking anyone to remember. Same here: nontainer is the first place
  termish and monkeyfs meet, so it carries a meta-test that
  `inspect.signature`s every method on both protocols and fails on
  drift, and runs both libraries' conformance kits
  (`termish.fs.check_filesystem`, `monkeyfs.check_filesystem` — each
  shipped so a backend author needs neither the other library nor
  nontainer) against `KvgitProvider.fs`, `MemoryFS`, `VirtualFS` and,
  later, the browser guest's `LazyFS`.

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
   `Range` request against the blob endpoint (below), so
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
- monkeyfs: `open` off the backend protocol; a lazy binary-read
  `VirtualFile` with a block cache; wrappers forward. ~100–150 lines,
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
repositories, it can run in parallel with everything below, and the
whole-file `LazyFS` means nothing waits on it.

## The executor

The inversion from dud is that **the guest dials in**. dud's host
drives a guest it owns; here the guest is a tab that connects to the
server, so the server-side executor is a stub bound to a socket.

| concern | `DudExecutor` | browser executor |
|---|---|---|
| tree | tar push over the dud channel | the same tar over the socket; `ctx.head` affinity so a tab already holding the tree skips it |
| harvest | scan / overlay diff → `StagedDiff` | scan-diff in the worker against a shadow of the pushed tree; whole-file payloads |
| host objects | hostcall proxies behind `dud.public_methods` | the same allowlist; a proxy method is a sync XHR to the server, which dispatches to the live object exactly as `_host_object_rpc_handler` does |
| `ws-*` verbs | bash functions → `ws_verb` hostcall | the guest shell is termish, so each tagged verb is a relay *command* in the guest registry; `supports_commands` is True and the `FerrySpec` path mapping applies unchanged |
| cache | guest pickles, host stores bytes | identical |
| isolation | a VM exceeds any `isolation` | refuse `isolation != "none"` at open, the way `_prepare_config` refuses what a rung cannot honour; the Web Worker is the boundary |
| ticks | gone; wall-clock only | back — sandtrap is in-process in the worker |
| packages | grants → image package list | grants → Pyodide package list, the same `_merge_packages` idea |
| view reentrancy | one channel, serialized | one interpreter per tab, serialized; a worker pool is a memory question the tab answers |

**Push-tree for authoring; the lazy path is built first, elsewhere.**
termish's `FileSystem` is a bridgeable surface, and the run-ts note in
the design doc already imagines "bridged over an RPC filesystem". For
the author's rung, push-plus-harvest matches the existing contract and
the cross-rung conformance suites exactly, so it starts there. But the
lazy filesystem gets built *before* this executor, for publications
(below), where it is mandatory rather than nice — and the author's rung
later swaps `sync()` from "push a tar" to "send a fresh manifest of
`working_files()`" against the same `LazyFS` and a per-session
endpoint. Only the host→guest direction changes; the write harvest is
the same scan-diff either way.

**Network honesty.** sandtrap's network denial patches `socket`, which
does not exist under Pyodide; `network=True` would have to mean
allowing `pyfetch` / XHR, and the policy story needs one paragraph
saying so rather than a knob that reads as a policy and does nothing.

**A closed tab is a lost guest.** `HarvestLost` already describes the
torn call and the workspace already surfaces it. What changes is the
product: a turn that runs while the tab is closed cannot exist on this
rung, because the tab is the compute. A studio that keeps delegates on
`LocalExecutor` keeps part of that story — sessions authored on one
executor are readable on another today.

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

## Published apps in the visitor's browser

Serving is frozen and stateless: `resolve(token)` returns a read-only
workspace and each request is one `exec_python(view=)` with
`readonly_fs`. A publication is small by construction — the derived
commit holds `app/` and nothing else. So a visitor's browser needs only
the frozen subset of the executor: a read-only tree, no harvest, no
sync, no verbs, no delegation. Ship `app/` plus a Pyodide runtime,
intercept `api/*` in the page, dispatch into the worker; the server
sees hostcalls and nothing else. This stands alone — authoring can stay
server-side while publications move.

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

**The entitlement of the call is provable.** The server holds the
frozen commit, which the visitor cannot change, and it does not need to
know *who* is calling — only *whether this call is one the app could
make*. That is derivable from the code it already has.

### The entitlement harvest

At publish time, walk the frozen `app/` tree for host-object calls —
nontainer knows the names from `PythonConfig.host_objects` and `from
host import X` — and record, per argument position, either the literal
that was there or *free*:

    db.query("SELECT region, SUM(v) FROM t WHERE year = ?", (year,))
        → (db, query, [lit:"SELECT …", free])
    mail.send(to, "Welcome", body)
        → (mail, send, [free, lit:"Welcome", free])

The bridge check is as dumb as the harvest: find the entitlements for
`(object, method)`, require literal positions to match exactly, let
free positions bind anything. A visitor spoofing a harvested call is
indistinguishable from a visitor using the app, which is the property
wanted. No SQL knowledge anywhere; the harvest is a *shape*, not a
meaning, and the mail example yields exactly the honest entitlement of
that code — whether it is acceptable is the judgment about the host
object the embedder was always making.

The agent sees nothing new. It writes `db.query(literal, params)` as
the building-apps skill already teaches for injection safety; the
publish step does the extraction. A handler whose host calls are not
statically harvestable — string-built SQL, a method chosen at runtime —
is not refused: it stays server-side, with the author's code as the
boundary, as everything is today. So routing is mechanical and
agent-invisible: **a handler runs in the visitor's browser iff every
host call in it is harvestable.**

One limit decides the rest. The harvested set entitles the *union* of a
handler's calls, not the *sequence*: `if valid(score): db.execute(INSERT
…)` has its validation on the visitor's machine, and the visitor can
issue the INSERT without the check. Entitlement-by-call cannot express
"only after". So reads are entitlement-safe and writes-with-logic are
not — which is the split structural REST already draws. A GET handler
executes against a read-only fs and a read-only cache today; `db` is the
one plane that does not honour it. Make it honour it, on every rung,
and the browser policy is: **GET handlers in the visitor's browser,
mutating verbs on the server.** One rule the agent was already taught,
identical in `ws-curl`, `test_app`, the preview and the published app.

What that needs from the embedder is a marker on the one host object,
the same shape as the public-methods allowlist: which methods are reads
(`AppsConfig(readonly_methods={"db": {"query"}})`, or a decorator). Not
a second object, not a second tier the agent has to understand. The one
agent-visible consequence is that the reference handler's
`CREATE TABLE IF NOT EXISTS` on every request moves into the POST path
or into setup, which is aligning the reference with a rule the skill
already states.

**Enforce the set in preview.** The walk is by receiver name, and the
skill teaches handlers to pass `db` into helpers as an argument; a
helper that renames the parameter would be missed, and a missed call
means an app that works in preview and is refused published — the
lie-class bug. The answer is the one the repo already uses for CSP and
`script_hosts`: `test_app` and `ws-curl` enforce the same set, so the
failure is where the agent can see it. Cheap over-approximation helps:
harvest calls to any marked read-method name regardless of receiver.

**No new seam yet.** The harvest is a function in core over the frozen
tree and the host-object names; the marker is config; the membership
check is the bridge's. Following the `HostObjectFactory` precedent — a
declared seam nothing calls — no `Entitler` protocol is declared until
an embedder has a host object whose entitlements cannot be expressed
as literal-argument matching. That day it becomes a
`Callable[[frozen_tree, host_objects], Entitlements]` they replace.

What is left after all of this is a leak the embedder judges once: a
visitor can `query` tables the app does not display. For a per-app
sqlite file that is the app's own data, a studio can say yes and write
it down. An embedder fronting a shared production db says no and hands
the agent a narrower object *from the start*, in preview and published
alike, so the agent again never sees a difference.

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
  fetches that one blob, when a handler actually needs it. With phase
  0, it fetches the byte ranges it needs.
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
- **Prefetch from the harvest.** The same AST walk that collects
  entitlements can collect string literals that resolve to paths under
  `app/`; the served page emits `<link rel="preload">` for them so the
  parquet's download overlaps the runtime boot instead of following the
  first request.

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
For the tail, the cheap tell is a publish-time report — "this app's
handlers touch 48MB of data," measured from the harvest's path
literals against the manifest — so the author knows before a visitor
does.

## `test_app` becomes a driver protocol

The entitlement set enforced in preview closes one lie; a bigger one is
left open. Under browser-side serving, `test_app` is still Playwright
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

Rung-specific caveats, for the per-rung tool description to carry the
way dud's already says "stderr merges into stdout":

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
session is building a `MemoryFS` from the tar.

None of that is nontainer's. dud owns its VM pool, sandtrap owns the
forkserver, the studio owns `warm_view_workers` and `NONTAINER_STUDIO_VM_WARM`;
whether a tab's warmth comes from a snapshot, a shared worker across
tabs of one apps origin, or a profile per app derived from the same
AST walk as the entitlements is the studio's to decide. nontainer's
contribution is `ctx.head` as the affinity tag and the lazy `open`
above: the tab boots in the background and the first `run_python`
waits, the way `dud-vm`'s first image build already does with the
agent none the wiser.

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

## What it costs

- **Background turns.** "Close the tab mid-turn, the work continues"
  dies by construction on this rung.
- **The Pyodide ceiling.** No native dependency outside Pyodide's
  index, no real bash, iOS Safari's per-tab memory budget. agex-studio
  shipped Python first and made TypeScript primary because Pyodide was
  heavy; nontainer has no TypeScript escape hatch, because Python is
  the point.
- **A third rung to keep honest.** Skills already fork per executor;
  tool descriptions, "what packages exist here" and the conformance
  suites all grow a variant.
- **I/O proportional to workspace size** — tree push and harvest per
  call, mitigated by head affinity and, later, lazy blob fetch.

None of these is a security cost for the author's session. For
publications, risk is traded rather than added: today's surface is
sandbox plus handler plus host object, all on the server; browser-side
it is the entitlement set alone, and the handler and the sandbox stop
being the server's problem. A few hundred lines of AST walk are a
smaller thing to defend than a sandbox.

## When it is worth it

For a single-user local workbench, never: it moves compute from one
process on a laptop to another process on the same laptop and pays
with background turns. The question is whether the studio is going to
be **hosted**. If it is, this is plausibly the cheapest path to
multi-tenancy — cheaper than a microVM pool, with keys server-side —
and the browser-side publication is the piece to build first, alone,
because it stands without the executor and removes the ugliest line in
the current threat model.

"Serverless Python in the browser" is not a change of execution model.
The apps design is already serverless — "there is no resident app
process… requests are dispatched into sandboxed executions on demand"
— and handlers are re-executed per request with no module state. This
relocates a dispatcher that never had a server.

## Spikes before committing

1. **Does Pyodide's snapshot survive dynamically-linked packages** on
   the pinned version (agex-studio pins 0.27.7)? numpy and pandas ship
   `.so` side modules; early snapshot support refused them. Cloudflare
   snapshots both, so it is at least solvable.
2. **First-visit bytes per profile**, and what a minimal profile
   (core Pyodide, no data stack) costs for a handler that only reads
   a JSON file.
3. **The sync hostcall channel**: sync XHR from the worker versus a
   socket with `Atomics.wait` (which needs COOP/COEP on the origin).
4. **Delegates**: a second worker in the author's tab, or
   `LocalExecutor` on the server. Start hybrid.
5. **Ranged reads reach pyarrow.** Confirm that pandas hands pyarrow
   the Python file object (it must — handlers read parquet under
   monkeyfs today) and that pyarrow then does column-selective `seek`
   / `read` against it rather than slurping the file. If it slurps,
   phase 0 still helps every other reader and the RPC bridge, but the
   parquet win needs a `pyarrow.parquet.ParquetFile` in the handler.

## Build order

**Phase 0 — the filesystem protocol** (termish, monkeyfs, sandtrap;
parallel with everything below, blocks nothing):
the ranged `read`, `open` off monkeyfs's backend contract, the lazy
binary `VirtualFile`, both conformance kits, the promise in both
READMEs, and the drift test in nontainer. Four releases in dependency
order.

**Phase 1 — publications, browser-served** (nontainer, no executor
needed):

1. The publish-time manifest in `store.publish`; the manifest and blob
   routes in `serve.py`; `LazyFS` in a `nontainer-guest` wheel (pure
   Python, installable with micropip), whole-file first and `Range`
   once phase 0 lands.
2. The entitlement harvest in core, the read-method marker on
   `AppsConfig`, enforcement in dispatch's GET view on every rung.
3. `AppDriver`: extract the Playwright driver from `testapp.py` behind
   the protocol, move `ws-vitest` onto it, add `Runtime.app_driver`.
4. The frozen guest: the `api/*` interceptor, the in-tab dispatcher
   over `LazyFS`, the hostcall endpoint checking membership, the tab
   driver, and publish-time verification through it.

**Phase 2 — the executor** (nontainer):

5. `nontainer/executor_browser.py` beside `executor_dud.py`, reusing
   the guest wheel and the tab driver; the lazy-`open` note on the
   protocol; the cross-rung suites under a Playwright-driven guest.
   Push-tree for `sync()` first; `LazyFS` over a per-session endpoint
   when the author's rung wants it.

**Phase 3 — the studio:**

6. An executor knob, a per-session socket, a rail state for "waiting
   for a browser", the snapshot pipeline, and the first-byte hybrid if
   the measured first visit needs it.
