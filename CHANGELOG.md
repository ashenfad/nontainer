# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## 0.3.4 - 2026-08-28

### Added

- **`AppsConfig.frontend_notes`** — the block of the apps notes that says
  *which frontend libraries exist and where they come from*, now owned by
  the embedder.

  0.3.3 made vendored libraries possible (`static_assets`) without moving
  the guidance that describes them, so the tool description still told the
  agent — emphatically, and by example — to import Preact from `esm.sh`
  and plotly from `cdn.jsdelivr.net`. For an air-gapped deployment that is
  an instruction to fetch from hosts that do not resolve, sitting in the
  one block introduced with *"copy this known-good pattern exactly"*.
  `apps_primer` could not fix it: it appends, so the correction landed
  below the wrong instruction, which was also the more emphatic one.

  `None` (default) keeps the built-in block, so a plain install renders an
  unchanged prompt; `""` omits it; a string replaces it. Import
  `nontainer.adapters.render.DEFAULT_FRONTEND_NOTES` to extend rather than
  discard.

  The split is supply vs. shape: *where the bytes come from* is the
  embedder's, while relative URLs, "plain DOM is the most reliable
  choice", and the rule against swapping a named import for a
  `<script src>` build or a guessed global stay in the template — they
  are true wherever the bytes come from, and agents get them wrong often
  enough that no embedder should be able to drop them by accident. The
  anti-guessing rule matters *more* on the replaced path: a vendored
  `vendor/mui.js` gives an agent no URL to anchor on, so `window.MUI`
  from memory gets likelier.

  `frontend_notes` is declared last on `AppsConfig`, so 0.3.3's
  positional signature still binds `static_assets` sixth.

### Changed

- **An empty `script_hosts` reads as a rule instead of a bug.** `()` is
  the air-gapped shape, and it used to render "Browser SCRIPTS may only
  load from these hosts:" followed by nothing — a dangling colon that
  reads as a broken prompt. It now states the rule positively: scripts may
  load only from the app itself.

## 0.3.3 - 2026-08-27

### Changed

- **`test_app` page errors name the agent's own code, and quote the line.**
  A stack is mostly somebody else's: with a component library in play the
  top frame is deep inside a bundle and the one line the agent can act on
  is below it — so reporting the *first* frame reported the least useful
  one, and a bare line number still cost a call to go look it up. Errors
  now read:

  ```
  TypeError: svae is not a function (at Dashboard (app.js:42:13), +4 frames above it in library code)
       42 | <Button onClick={svae}>Save</Button>
  ```

  Frames are classified against what is being served: the test_app origin
  and bare `//# sourceURL=` names resolve to workspace files; a declared
  `static_assets` prefix or a third-party host is library code;
  `blob:`/`data:`/eval is generated code with no file to open. An inline
  `<script>` reports the document URL, which now resolves to `index.html`
  rather than going unattributed — the common case in a first app.

  When nothing in the stack is the agent's, it says so (`no frame in your
  own files — all 6 frames are in library code`) instead of printing a
  location from inside a bundle. Same rule as the existing parse-error
  branch: a misleading diagnostic is worse than an absent one.

  Frame selection and rendering are pure functions (`parse_frames`,
  `classify_frame`, `describe_page_error`), so the source read is injected
  and the behavior is testable without a browser.

  The location comes from the *last* parenthesised group in a frame, since
  a function name can contain parentheses of its own; and the quoted line
  is clipped to a glanceable size, windowed on the error column so a
  minified or generated line still shows the fault rather than its first
  200 characters.

### Added

- **`AppsConfig.static_assets`** — a URL prefix → host directory mapping of
  fixed files served *with* an app but absent from the workspace: a vendored
  component library, fonts, a charting bundle. `{"vendor": "/srv/assets"}`
  serves `/srv/assets/mui.js` at `vendor/mui.js`, for `curl`, `test_app`,
  the live preview, and a published snapshot alike — all four go through
  `dispatch`, so one declaration covers them.

  This is what makes an air-gapped app possible (nothing to fetch from a
  CDN) and what a house component library rides on. It is deliberately
  **not** a `Mount`: these bytes are not workspace state but a property of
  the serving environment, so they are to the browser what `host_objects`
  are to handlers. The agent's filesystem never sees them — no commit
  weight, no fork weight, nothing shipped to a remote executor's guest —
  and the apps notes say so in a sentence derived from the config itself,
  because an agent that looks with `ls`, finds nothing, and writes its own
  copy has burned a turn on a file that will not be served. `curl
  vendor/lib.js` still works for a peek.

  Two deliberate exemptions from handler rules: assets skip
  `max_response_bytes` (that cap catches runaway handler output; a charting
  bundle clears the 2MB default on its own), and they take precedence over
  a workspace file at the same path — noted in `api.log` rather than
  shadowed silently.

  Assets are same-origin, so `script_hosts` needs no entry. Declare them on
  the one `AppsConfig` passed to both `enable_apps` and `build_router`:
  present while authoring and missing while serving is the one failure
  `test_app` cannot catch.

- **Static serving knows the types a vendored bundle brings** — `.wasm`,
  `.woff2`, `.woff`, `.ttf`, `.map`. A font survives the octet-stream
  fallback; `.wasm` does not, since `WebAssembly.instantiateStreaming`
  refuses anything but `application/wasm` — and it would have failed with
  nothing in the log to explain it.

### Changed

- **The served CSP allows WebAssembly compilation** (`'wasm-unsafe-eval'`
  in `script-src`). Browsers gate wasm on `script-src`, and `test_app`
  enforces the script allowlist by intercepting requests rather than by
  sending the header — so a vendored library with a wasm core
  (duckdb-wasm, sql.js, pyodide) would verify green and then die only once
  published. It permits wasm compilation only; it does not enable `eval`,
  and it is far narrower than the `'unsafe-inline'` already on that line.

### Fixed

- **`fork()` no longer drops the workspace's mounts.** A fork rebuilt its
  `Workspace` from a hand-listed set of constructor arguments, and `mounts`
  was added after that list was written — so the fork silently lost every
  mount point. Because publishing a snapshot *is* a fork, an embedder who
  mounted a dataset had it work while authoring, verify green under
  `test_app`, and then 404 the moment the app was published: a
  verified-green/published-broken split that verification could not catch.

  Worse than absent, in one case: a write the mount should refuse
  (`echo x > /data/new.txt` under a read-only mount) stopped erroring in
  the fork and silently landed in its own tree instead, because the path
  was no longer a mount point at all.

  The fields a fork replays now come from one record rather than a list at
  the call site, and a test asserts every pass-through parameter of
  `Workspace.__init__` is in it — so the next argument cannot fall out the
  same way. `provider`, `executor`, `commands`, and `autocheckpoint` are
  excluded for stated reasons.

- **Mount sources resolve once, at construction.** A relative `Mount.path`
  (or a symlink retargeted afterwards) used to re-resolve when a fork was
  built, so parent and fork could end up on different directories — which
  the live-view contract says cannot happen. The resolved mapping is what a
  fork replays.

### Changed

- **`Mount`'s docstring, the README, and `docs/api.md` now say what "not
  copied by forks" meant.** The sentence was ambiguous between "the fork
  does not snapshot the mounted data" (true, and intended) and "the fork
  has no mount" (what the code did). Both halves are now explicit: a fork
  **inherits the mount point**, and the data behind it stays a live view of
  the host directory that neither parent nor fork can roll back.

## 0.3.2 - 2026-08-24

### Changed

- **Requires sandtrap >= 0.3.3**, which gates `__import__` against the policy
  instead of refusing it outright. Agents that reach for
  `__import__("numpy")` — a predictable habit — now get the module if it is
  granted, rather than a validation error and a wasted turn.

- **`run_python` no longer reports the namespace.** Every call that bound a
  variable used to end with `[namespace kept for host: cols, df, n, total]`.
  It was the most frequent line in the python observation stream and the least
  useful one: it named bindings the agent had just written, claimed "kept for
  host" for names no host reads (in practice only `ui`, plus the apps loop's
  internal `nt__*`), and described a closed file handle as state the host
  holds. It also went quiet in the one case worth reporting — values dropped
  in transit under process isolation, where it listed what survived and said
  nothing about what vanished.

  Silent calls now render `(no output; success)`, which the note had been
  masking: because parts are appended, a namespace line meant the success
  signal never showed. Consequences are still reported where they exist —
  `[ui artifacts: ...]` for `ui = {...}` bindings, unchanged.

### Removed

- **The `__import__` intent hint.** It told agents "dynamic `__import__` is
  blocked, but ordinary import statements work here". Under sandtrap 0.3.3
  there is nothing to redirect: a *granted* module imports dynamically without
  complaint, and a *blocked* one raises the same `Import of 'x' is not allowed`
  the statement form raises — so `blocked_import_hint` labels it already, and
  `__import__("subprocess")` now inherits the "use the terminal tool" redirect
  for free. The door the hint pointed at is now the door the agent was already
  standing in.

## 0.3.1 - 2026-08-21

### Added

- **`PythonConfig.preload_grants`** — import granted modules once into
  sandtrap's forkserver broker so every worker inherits them copy-on-write
  instead of importing its own copy. Process/kernel isolation only; requires
  sandtrap >= 0.3.2, where the flag first became reachable through the public
  factory.

  It is the large lever on worker cost, and moves time and memory together.
  With the `dataframes()` preset granted, measured here:

  | | worker start | worker RSS |
  |---|---|---|
  | default | ~176 ms | ~77 MB |
  | `preload_grants=True` | ~14 ms | ~33 MB |

  The memory difference is copy-on-write sharing: the stack is paid for once
  in the broker rather than per worker. It applies to **every** worker,
  including the session worker each workspace holds for its life — so in a
  host with many open workspaces it moves more memory than `warm_view_workers`
  does.

  Off by default because preloading runs your grants' *import-time code in the
  broker*, and a grant that starts a thread on import leaves the broker
  multi-threaded — putting every worker forked from it back on the deadlock
  path the forkserver default exists to avoid. Only you can vouch for your
  grants. The stdlib and data-stack presets are fine.

  Note it is **process-wide, not per-workspace**: the preload list is read once
  when the broker starts, so the first workspace to start a worker decides for
  the process. Later ones still work (their modules import per worker) and
  sandtrap warns. Set it uniformly.

### Changed

- **`PythonConfig.view_workers` is renamed `warm_view_workers`**, and defaults
  to 1 rather than 8.

  The old name read as a *limit on workers*. It never was one: it sizes a
  **warm cache**, and nothing in nontainer bounds how many workers a burst can
  create (see below). That misreading is not hypothetical — it produced four
  wrong statements in this project's own docs, written by the author of the
  pool. Renamed while 0.3.0 is a day old and the field has no known users;
  a rename after adoption would not be worth it.

  The default drop is not an API break either — nothing raises, nothing
  changes shape, and a saturated cache falls back to per-call sandboxes
  exactly as before. But it is a tuning change with teeth for one workload,
  so read the last paragraph if you serve app traffic.

  The view-worker pool was introduced as a fork-hazard mitigation: a view
  sandbox was minted per call, so serving an app meant one `fork()` per request
  from a live ASGI server. sandtrap 0.3 creates workers from a forkserver
  broker, so **that hazard is gone at its source** — the pool is now purely a
  cost amortizer, and its documentation said otherwise.

  It still earns its place, because the same change made worker creation *more*
  expensive: a per-call worker used to be a copy-on-write fork of a host that
  already had the stack imported (~5ms, near-zero private memory), where a
  forkserver worker re-imports the granted modules (~18ms stdlib, ~235ms and
  ~113MB with a heavyweight stack).

  The default moved because **residency only rises**. Concurrency — not the
  cap — decides how many workers exist *during* a burst; what stays resident
  afterwards is `min(concurrency, warm_view_workers)`, since calls past the cap run
  in transient sandboxes that are reaped when they finish while pooled workers
  are kept, and nothing expires an idle one. So the cap behaves as a floor that
  fills and stays filled rather than a ceiling you retreat from. At 8, six
  concurrent view calls left six workers holding ~673MB for the executor's
  life. At 1, the same burst leaves one — and the app-iteration loop
  (edit → `test_app` → preview) is sequential enough to stay warm on it.

  **Prefer `preload_grants=True` with `warm_view_workers=0` where your grants
  allow it.** At ~14ms a worker start is cheap enough to give every request a
  pristine one, which removes the warm set entirely: nothing to size, no
  memory floor, and no process state carried between handler calls. The cache
  exists for when preloading isn't safe (a grant that starts threads on
  import) or isn't enabled, where a per-call worker costs ~235ms instead.

  **Raise it if you serve concurrent app traffic.** The new default is sized
  for the build-and-preview loop, not for load, and the trade is not free in
  that direction: under *sustained* concurrency the peak worker count is
  unchanged (concurrency sets it either way), while more of those workers are
  built per call — so a busy app server pays steady-state latency to get the
  retained memory back. Requests past the cap fall back to a per-call sandbox
  rather than queueing, so too-low costs latency (visible, recoverable) while
  too-high costs memory. `0` still gives every call a pristine worker.

- Requires **sandtrap >= 0.3.2**.

## 0.3.0 - 2026-08-20

### Changed

- **Requires sandtrap >= 0.3.0, where process workers no longer fork the
  embedding process.** Workers are created by a `forkserver` broker instead, so
  a multi-threaded host — which any uvicorn/FastAPI server is — can no longer
  hand a worker a lock held by a thread that doesn't exist in it. That failure
  hung the worker rather than crashing it, and surfaced only as
  `"Worker process became unresponsive"`.

  Three consequences for embedders:

  - **A `PythonConfig.policy` you supply must now be serializable.** Module
    grants, module-level functions and classes are fine; lambdas, closures,
    bound methods, and classes defined inside a function are not.
    `Workspace(...)` raises `StPolicyNotPortable` at construction, listing
    every problem at once.
  - **Granted modules are imported once per worker** rather than inherited, so
    a heavyweight grant costs real time at worker start (~126ms for `pandas`,
    against ~18ms for the stdlib preset). Workers are long-lived — one per
    workspace, plus the app-handler pool — so this is paid at construction, not
    per call.
  - **Your program's entry point must be importable.** A worker that doesn't
    inherit memory re-imports `__main__`, so module-level work in your entry
    point belongs behind `if __name__ == "__main__":`. Servers are unaffected
    (an ASGI app is imported, not run as `__main__`) and so is a normal
    `python myserver.py`; what breaks is constructing a `Workspace` from
    `python -c`, from `python -` with a heredoc, or from a bare REPL.

    This is about **your** process, not the agent's code. The terminal's
    `python` builtin runs inside the existing worker rather than starting a
    process, so `python <<'EOF'`, `python -c`, `python file.py`, and
    `cat x.py | python` all keep working from agent code exactly as before.

- **Requires dud >= 0.3.0 for the `[dud]` extra, which now demands an explicit
  allowlist per host object.** Registering one without a grant raises
  `PolicyError` rather than quietly exposing every public method — dud's one
  fail-open path, now closed.

  nontainer grants each host object its **public methods**, which is the same
  surface `LocalExecutor` bridges over RPC (its handler rejects underscored
  names and non-callables). Both rungs therefore reach the same members, and
  `PythonConfig` needs no new knob. `dud.public_methods()` resolves to a
  concrete frozenset rather than a wildcard, so a grant snapshots what exists
  at construction instead of whatever gets added to the object later.

  Two dud 0.3.0 changes land in nontainer's favour without work here. Guest
  processes now boot with their image's environment, so a populated `PATH`
  means agent code can `subprocess` python and anything `packages=[...]`
  installed. And dud's print guards were loosened from an observation budget
  (20 KB transcript / 2 KB entry) to resource guards (1 MiB / 16 KiB) — output
  used to be truncated by dud *before* nontainer could apply its own
  budget-aware rendering, so `max_observation` was competing with a smaller
  cap it couldn't see.

- **Host objects are registered by class, and only in-process.** Under
  process/kernel isolation the agent holds an `RpcProxy`, whose type is not the
  object's, so a registration keyed on that type never matched it and gated
  nothing. It was doing no work while making the policy unserializable — which
  meant a host object whose class is defined inside a function (common in
  tests, and in code that builds adapters in factories) broke the whole policy.
  In-process execution is unchanged: there the real object is what lands in the
  namespace, and the registration carries its member filters.

- **App handler dispatch reuses resident sandbox workers instead of forking
  one per request.** Under `isolation="process"`/`"kernel"`, every handler
  call minted and reaped its own worker, so serving an app meant one
  `fork()` per HTTP request — taken from a live ASGI host, which is
  multi-threaded (the router dispatches through `anyio.to_thread`). Forking
  a multi-threaded process can leave the child holding a lock no surviving
  thread will release, and such a child *hangs* rather than crashing: the
  request stalls until a timeout fires, with nothing in the error naming
  the cause (sandtrap#38). Workers are now kept resident per distinct
  handler view and checked out per call, which turns N forks per N requests
  into at most `PythonConfig.view_workers` forks for the executor's whole
  life, and drops per-request worker start from the latency.

  Only view calls change. `run_python` and plain `exec_python` already ran
  in the session sandbox, forked once at workspace construction and held
  for its life — they never forked per call and are untouched here.

  Requests beyond the cap fall back to a per-call sandbox rather than
  queueing. `PythonConfig.view_workers=0` restores the old per-call
  behavior; the default is 8, and a busy server should raise it toward its
  own concurrency limit (Starlette's default thread limiter is 40).

  The tradeoff a resident worker makes: process state — `sys.modules`,
  module globals, anything a handler mutated through a granted module —
  now outlives the request that created it, where a per-call fork gave
  every request a pristine copy-on-write view. The blast radius is one
  workspace: a pool belongs to one executor, and distinct sessions resolve
  to distinct executors, so this is state shared between handlers of a
  single app.

### Fixed

- **The A2UI artifact fallback no longer emits an invalid `link` key**
  ([#31](https://github.com/ashenfad/nontainer/issues/31)). Any artifact that
  couldn't be mapped to a richer component shipped
  `Text {text, link}` — but the basic catalog's `Text` takes
  `component`/`text`/`variant` and is declared `unevaluatedProperties: false`,
  so a strict consumer rejected the whole fragment:

  ```
  Validation failed for component 'Text' (segN):
    root: Unrecognized key(s) in object: 'link'
  ```

  The link is now markdown inside the text — `[artifact: name](url)` — which
  needs no prop of its own, since `Text` already carries markdown by contract
  (`("md", text)` segments ship verbatim). Affected every fallback artifact:
  html, plain text, non-plotly json, binary, and any bytes-needing kind whose
  payload failed to parse.

### Added

- **Bridged host objects declare their surface to the worker.** sandtrap's
  proxy could not tell a method from a data attribute, so it returned a caller
  for every name: `db.dsn` read as `None`, `db.dsn = x` was silently lost, and
  a typo'd method failed at call time saying nothing about what existed.
  Now:

  ```
  v = db.dsn      -> AttributeError: 'dsn' is a data attribute of the host
                     object, and the bridge carries method calls only
  db.dsn = 'evil' -> AttributeError: cannot set 'dsn' ... would be lost
  v = db.nope()   -> AttributeError: 'nope' is not part of the host object's
                     exposed surface (available: query)
  ```

## [0.2.4] - 2026-08-04

### Changed
- **Requires monkeyfs >= 0.1.6 and sandtrap >= 0.2.14.** The monkeyfs floor
  matters most: 0.1.5 let sandboxed code bypass filesystem interception
  entirely through `bytes` or `os.PathLike` paths — `open(b"/etc/passwd").read()`
  reached the real filesystem — and separately honored `dir_fd` arguments
  against the host and handed out real host directory descriptors. nontainer
  depends on monkeyfs directly for its VFS and `IsolatedFS` providers, so the
  direct pin now states the floor rather than relying on sandtrap to carry it.
  sandtrap 0.2.14 adds `StForkUnsafe`, which names a fork-hostile host instead
  of looping on an unexplained respawn failure — relevant to long-lived
  workspace hosts — and makes worker setup failures report their own traceback.

## [0.2.3] - 2026-07-30

### Added
- **The default safe stdlib now includes common data/type helpers:**
  `heapq`, `bisect`, `difflib`, `struct`, and `binascii`. Narrow grants
  add the capability-neutral core of `functools` (`partial`, `reduce`,
  `lru_cache`, `cache`), string-only `shlex` rendering (`quote`,
  `join`), and pprint representation helpers that return strings or
  booleans. Broader surfaces remain denied and pinned by tests.

### Fixed
- **Process workers no longer retain unrelated host descriptors or orphan
  idle workers after an abrupt host exit.** Nontainer now requires Sandtrap
  0.2.13 and opts process and kernel sandboxes into ambient descriptor
  cleanup. Listening sockets, accepted connections, and other host resources
  therefore stay parent-owned; live `PythonConfig.host_objects` continue to
  cross as RPC proxies rather than fork-inherited handles.
- **Fresh versioned workspaces now commit an explicit initialization
  baseline.** Creating the workspace root and setting its initial cwd
  previously left kvgit staging dirty, so the first read-only tool call
  committed those framework writes under the wrong tool label. Root and
  cwd now land in a one-time `{"tool": "init"}` checkpoint before the
  executor opens, including when tool autocheckpointing is disabled.
  Reopening is idempotent. A provider deliberately pre-seeded by an
  embedder remains staged—initialization joins that pending view without
  silently committing caller-owned work. The init checkpoint is also the
  floor for `Workspace.rollback()`, preventing rollback into a provider's
  pre-workspace seed while leaving explicit `restore()` and legacy
  histories unchanged.
- **The MCP extra is capped below 2.0 until the adapter is migrated.**
  MCP 2 removed `mcp.server.fastmcp`, which made fresh
  `pip install -e ".[mcp]"` environments fail while importing the
  adapter. The declared range now matches the API nontainer supports.

### Security
- **`pickle` is no longer part of the default safe-stdlib grant.**
  `pickle.loads`/`load`/`Unpickler` execute reducer callables outside
  sandtrap's normal call gating, allowing a payload to invoke blocked
  builtins such as `eval` under both in-process and process isolation.
  Trusted embedders may still opt in explicitly with
  `PythonConfig(modules=[ModuleGrant(pickle)])`.
- **Process-global ABC registration remains unavailable by default.**
  `numbers` and `collections.abc` expose `ABCMeta.register`, which
  agent code could use to alter `isinstance` results throughout the
  host interpreter. They remain excluded until grants can constrain
  attributes reached through returned class objects.

## [0.2.2] - 2026-07-27

### Added
- **A `Table` a2ui extension component for dataframes** (issue #18).
  A `.table.json` artifact was flattened into nested `Column`/`Row`/
  `Text` before it reached the wire, so nothing marked it as tabular
  and a consumer had no way to bind a real grid — sorting, alignment,
  virtualized scroll. Under `NONTAINER_CATALOG` a dataframe is now one
  `Table` node whose data rides in the data model, exactly as `Chart`
  does with its plotly spec.

  Gated like `Stat`/`Callout`, NOT unconditional like `Chart`. A plotly
  figure has no basic-catalog approximation worth shipping; a table
  does, and it is what basic consumers already render — making `Table`
  unconditional would turn a readable table into a component they must
  skip.

  The wire shape is normalized to `{columns, rows, total, columnTypes}`
  rather than passing pandas' split orient through. The artifact keeps
  that orient, but a catalog is a public contract: publishing `data`
  next to a row `index` no renderer here uses would freeze a pandas
  implementation detail and force a polars or SQL producer to imitate
  it. Ragged rows are padded and trimmed to the header, since a short
  row shifts a grid's columns silently. No row cap on this path — 50
  was a budget for `Text` nodes an agent would read, and the artifact
  is already head-capped at 200 upstream; `total` still reports the
  true height.
- **`.table.json` artifacts carry `columnTypes`.** Cells cross as JSON
  scalars, so an ISO timestamp is indistinguishable from a string that
  looks like one and a numeric column sorts lexically unless someone
  says otherwise. pandas knows the dtypes, so the artifact now carries
  a coarse kind per column (`number`/`string`/`datetime`/`boolean`),
  read from the dtype `kind` so extension dtypes (`Int64`, tz-aware
  datetimes) classify like their numpy counterparts. Purely additive —
  the existing `{columns, data, total}` keys are untouched, so
  consumers reading the artifact today are unaffected — and omitted
  rather than raised if a frame's dtypes can't be read.

- **`test_app` gained a `select` action.** `{"select": [selector,
  value]}` drives a `<select>`; the option matches by value, then by
  visible label, since agents pass whichever the DOM showed them.
  Previously the only option was `{"type": ...}`, which maps to
  Playwright's `fill()` and raises on a `<select>` — 4 of 11 `test_app`
  failures in an audited session were this one gap, and the agent
  rediscovered the same `dispatchEvent(new Event('change'))` workaround
  three separate times. A `type` aimed at a `<select>` now names the
  `select` action in its error instead of passing Playwright's
  "Element is not an `<input>`" through unhelped.

### Changed
- **`a2ui.component_for(extension_cards=)` is now `extensions=`.**
  Breaking, on a public keyword. The flag was named for cards but now
  gates tables too, and both follow the same rule: emit the nontainer
  catalog's component where a basic-catalog approximation also exists.
  `turn_to_a2ui` sets it from `catalog_id` as before, so callers that
  do not use `component_for` directly are unaffected.
- **`api.log` now records every `/api` request** (`METHOD path ->
  status`), and opens with a header explaining the format. Successful
  requests used to log nothing, so an empty log was ambiguous: it read
  as *logging is broken* rather than *no handler errored*, and sent the
  repair loop chasing a phantom instead of the bug. The header proves
  the mechanism works and the lines prove requests are arriving, so
  silence below the header is now a fact about the app. The header is
  written when the log is first created, NOT at `enable_apps` —
  pre-creating it would materialize `<root>/app` before the agent has
  built anything, and embedders answer "is there an app yet?" with
  `isdir(<root>/app)` (studio's preview probe does). Static assets are
  deliberately not logged — high-volume, low-signal, and they would
  bury the tracebacks the log exists for.

  Request lines from read-only requests buffer until writing them is
  free, because per-request atomicity is gated on `not ws.dirty`: a
  line written during a GET would silently disable handler rollback
  for the next mutating request, and page-GET-then-POST is the common
  order, not a corner case. The runtime cannot claim that dirt as its
  own and roll back regardless — `discard()` is all-or-nothing at the
  provider level and the protocol exposes only a boolean, so its own
  log line is indistinguishable from a screenshot written mid-run,
  which rollback would then destroy. `curl` and `test_app` flush when
  they finish, and `AppRuntime.flush_log()` is public for embedders
  driving dispatch themselves (a live preview route).
- **Repeated `test_app` console lines collapse** to a single entry with
  an `(xN)` count, and the 100-line cap now counts DISTINCT lines. One
  audited session spent 39% of all `test_app` result bytes (7,922 of
  20,279) on 32 copies of the same Tailwind CDN warning, against a
  model working in ~30k of context — repeats crowded the console tail
  the agent actually reads. A genuinely repeating log still reads as
  repeating, via the count.

### Fixed
- **Direct `ws.fs` writes now reach a remote executor.** `ws.fs` writes
  straight into the provider, so behind a remote executor (dud) the
  guest kept serving its stale baseline — a host-written file simply
  was not there. The apps runtime hit this hardest: `AppRuntime._log`
  writes handler tracebacks through `ws.fs`, so `cat
  app/logs/api.log` from the terminal reported "No such file or
  directory" while the traceback sat in the host VFS. It surfaced only
  when some *other* path happened to sync first, which made it
  nondeterministic — and it blinded the documented repair loop exactly
  when an agent was debugging a 500. `ws.fs` now hands back a wrapper
  that marks the executor stale on the mutating protocol methods; the
  workspace syncs before the next execution. `skills.install` wrote
  through the same escape hatch and had the same latent bug.

### Changed
- **Executor syncs are lazy, not eager.** `write_file` / `edit_file` /
  `put` / `restore` / `rollback` / `discard` previously called
  `executor.sync()` inline. A remote sync re-pushes the whole tree, so
  seeding N files cost N wholesale pushes; the workspace now marks the
  view stale and syncs ONCE, before the next execution needs the guest
  current. `Workspace.close()` settles a pending sync first, so a
  parked tree is never tagged with a provider head it doesn't hold.
  No API change — the sync points moved, the guarantees didn't. A sync
  that raises restores the stale mark (so the caller's retry actually
  re-syncs rather than running on the old tree), and a dud guest whose
  push failed parks WITHOUT its affinity tag — an untagged park costs
  one push on the next resume, where a tagged stale tree would have
  been trusted and served.

## [0.2.1] - 2026-07-20

### Added
- **Session deletion is a first-class API.** `delete_workspace(sessions,
  *, store=, backend=)` is the teardown counterpart to `workspace(...)`
  — it dispatches by backend to the same layout the factory built
  (kvgit branches under `store/kvgit`, `dir` session trees, agentfs
  `.db` files) and is plural + idempotent (a name that doesn't exist,
  or a store never created, is a no-op). Each provider gains a matching
  `delete(path, sessions)` classmethod. Kvgit's routes through
  `kvgit.delete_branches` (new in kvgit 0.3.2): an anchor-free admin
  call that opens the raw backend with no current branch, so it can
  drop any branch — including a store's only one, the sole-branch case
  a branch-anchored handle can't reach. This replaces the earlier
  hidden `__void__` anchor branch, which pinned a dead session's entire
  history and silently defeated orphan GC (a data-retention bug); the
  delete path now always folds `__void__` into the doomed set, so that
  stale anchor is purged from legacy stores on their next delete. Dir
  and agentfs validate session ids before touching disk so a hostile
  name can't escape the store root.

## [0.2.0] - 2026-07-20

### Added
- **The workspace root contract.** Agent-visible files now live under
  one configurable absolute path — `/workspace` by default, set with
  `workspace(..., root=)` and readable as `ws.root`. One value per
  session, inherited by forks. The point is cross-executor agreement:
  a dud VM mounts its guest workspace at the same path, so
  `/workspace/data/in.csv` names the same file whether agent code runs
  in the local sandbox or on a real machine. Previously the VM rooted
  the workspace somewhere else entirely, and agents burned turns
  discovering the split.
- **`[dud]` extra documented**, with an Executors section in the README
  covering the second seam — `WorkspaceProvider` decides where state
  lives, `Executor` decides where code runs, and the two are
  independent.
- **`Executor.supports_commands`** — a capability flag for whether
  injected terminal commands reach the shell, readable as
  `ws.supports_commands`. True for `LocalExecutor` (termish takes the
  mapping), false for `DudExecutor` (real bash has no such hook).
  Executors predating the flag default to true, keeping their
  historical behavior.

### Changed
- **BREAKING — agent-visible paths moved under the root.** Skills are
  at `<root>/skills` (was `/skills`), app handlers at `<root>/app`
  (was `/app`), UI artifacts at `<root>/ui` (was `/ui`), and the
  handler log at `<root>/app/logs/api.log`. Sandbox module imports
  resolve from the root too (`Policy.module_root`, requires
  sandtrap >= 0.2.12). Anything holding those paths literally —
  prompts, seeded files, stored sessions — needs repathing.
- **BREAKING — `DudExecutor()` now defaults to a real VM**
  (`backend="vm"`, resolved per platform) instead of the unsandboxed
  `"subprocess"` rung. The old default gave real bash and real files
  with *zero* containment, running as the host user with open egress —
  strictly weaker than the `LocalExecutor` a caller had just left, and
  it was what you got by reaching for a real machine and passing
  nothing. A host without a hypervisor now fails closed
  (`IsolationUnavailable`, missing piece named) rather than silently
  running unsandboxed. `backend="subprocess"` remains available as an
  explicit opt-in: it buys fidelity, not isolation, and is the only
  backend needing no hypervisor.
- **Dependency floors**: `sandtrap >= 0.2.12` (for `Policy.module_root`)
  and `dud >= 0.2.1` (for the guest workspace mounting at the
  configured root).

### Fixed
- **The apps primer no longer teaches `curl` where it doesn't exist.**
  `curl` is an injected terminal builtin, so under `DudExecutor` the
  tool description promised a command that answered `command not
  found` — and the agent then debugged its app instead of its
  environment. The primer gates on `supports_commands`; where `curl`
  is absent it points at `test_app` and explicitly warns against
  importing a handler to call its verb by hand, since that skips
  routing and runs GET without its read-only filesystem, so it can
  pass on code the real request path rejects.
- **`DudExecutor` reaches dud's backends through `dud.session()`**
  instead of importing `dud.backends.*` directly. It had drifted a
  release behind: `backend="firecracker"` raised
  `ValueError("unknown dud backend")`, making dud's Linux/KVM rung
  unreachable from nontainer at all, and `backend="vm"` was hardcoded
  to vfkit, so on Linux it would try to boot a macOS hypervisor rather
  than resolving to firecracker. Routing through the façade fixes both
  and means a new dud rung needs no change here.
- **The workspace root normalizes by segment.** `root="//"` used to
  `rstrip` to `""`, which reads falsy downstream — the local executor
  then composed `/skills` (the flat layout) while a VM guest fell back
  to dud's own `/workspace` default, silently splitting the namespace
  the root exists to unify. Trailing, doubled, and leading-only
  slashes now all collapse the way a guest kernel would collapse them;
  `.`/`..` segments are rejected rather than resolved, since a guest
  would normalize those and the VFS wouldn't.
- **Absolute writes inside the guest land in the diff.** With the
  workspace mounted at the root, a write to `/workspace/x` from VM
  guest code is harvested like any other workspace write; it used to
  land beside the staging internals, invisible to diffs and lost on
  reset.

## [0.1.2] - 2026-07-19

### Added
- **Tracebacks in error results and `/app/logs/api.log`.** Runtime
  errors now render the full traceback — frames, line numbers, the
  raise site — instead of a bare message (under process isolation the
  traceback used to be lost crossing the worker pipe; requires
  sandtrap >= 0.2.10). Sandbox machinery frames (sandtrap/monkeyfs
  plumbing) are dropped, host install prefixes are stripped from
  library frames (`pandas/core/generic.py`, not the absolute venv
  path), and pathological depth is middle-elided.
- **Request context in api.log tags.** Handler log entries read
  `[dashboard:get ?source=filtered&makes=Tesla]` — the query string is
  what lets an agent correlate errors with requests instead of reading
  identical bare lines as a stale log.
- **More intent hints** (`error_hint`, superseding `blocked_import_hint`
  as the entry point, wired into both run_python observations and
  api.log): `shutil` → terminal cp/mv or open(); `__import__` → plain
  import statements work here; plotly's kaleido dead end → `ui = {...}`
  or matplotlib; the tick limit → vectorize, native calls don't tick.
- **Wider `os.path` grant**: `getsize` + `abspath` (monkeyfs-patched,
  VFS-routed) and `split`/`normpath`/`relpath` (pure string math).
  `getmtime`/`getatime`/`getctime` stay out — monkeyfs doesn't patch
  them; `os.stat(p).st_mtime` is the granted route.

### Added (notebook echo)
- **Bare final expressions display in `run_python`** (sandtrap's
  REPL echo, `PythonConfig.echo = "last"` by default): a trailing
  `df.head()` shows its repr, no `print()` needed — and the tool
  description teaches it. Echoed values ride the snapshot-prints
  stream, so a bare expression over a huge object gets reprobate's
  bounded structural render, not a megabyte of repr. Script surfaces
  are exempt by per-exec override (sandtrap >= 0.2.11): the terminal
  `python` builtin keeps `python -c` semantics for pipelines, and app
  handlers never echo into api.log.

### Fixed
- **`dataframes()` pins a fork-safe arrow allocator**
  (`ARROW_DEFAULT_MEMORY_POOL=system`, via `setdefault` before the
  first pandas import). Arrow's default mimalloc pool keeps per-thread
  heaps that don't survive `fork()` — a sandbox worker forked from a
  threaded host segfaulted in `libarrow`'s `mi_thread_init` on its
  first arrow allocation (parquet reads, pandas-3 arrow-backed
  strings), and every respawn re-forked the same hostile parent: a
  permanent "Worker process died during initialisation" loop.
  Embedders that import pandas before building configs should set the
  variable themselves, earlier.

### Changed
- **Tick limits raised**: `PythonConfig.tick_limit` 1M → 50M,
  `AppsConfig.request_tick_limit` 200k → 10M. The same sandbox
  checkpoint enforces the timeout, so that's the real runaway guard;
  the tick limit is a determinism backstop and must never fire on an
  honest loop over a few-hundred-k-row frame.
- sandtrap floor raised to 0.2.11 (worker-rendered tracebacks,
  per-exec echo override).

## [0.1.1] - 2026-07-15

### Added
- **The nontainer a2ui catalog** (`docs/a2ui/catalog.json`, exported as
  `nontainer.adapters.a2ui.NONTAINER_CATALOG`): the idiomatic home for
  extension semantics — re-exports the basic-catalog components the
  egress adapter emits and declares `Stat {label, value, sublabel?}`,
  `Callout {title?, body?, tone}`, and `Chart {spec}`. Passing it as
  `turn_to_a2ui(catalog_id=...)` opts the surface into flat
  one-component-per-item cards that say what they mean, instead of
  Card/Column/Text trees with role-suffixed ids; any other catalog id
  (including a consumer's own) keeps the basic approximation, since we
  can't know what a foreign catalog declares. `Chart` stays
  unconditional — a plotly figure has no basic approximation worth
  shipping.

### Fixed
- **a2ui cards rendered as empty boxes on basic-catalog consumers**
  (#16). The v0.9 basic-catalog `Card` takes a singular required
  `child` id (`unevaluatedProperties: false` — a `children` array
  isn't ignored, it's invalid), so every stat/callout Card shipped
  content the reference renderer never saw. Card content now rides an
  intermediate `Column` behind `child`; the callout's `tone` stays a
  passthrough prop on the basic shape as a documented deviation
  (strictly validating consumers should use `NONTAINER_CATALOG`, where
  `tone` is declared).
- **Card-builder hardening for direct `/ui` writes**, which bypass
  `materialize_ui`'s normalization: an unknown callout `tone` clamps
  to `info` (the catalog declares a closed enum), and explicit nulls
  in stat items read as absent — empty label/value, omitted sublabel —
  never as the literal text `"None"`.

## [0.1.0] - 2026-07-15

### Added
- **`AppsConfig.script_hosts` + `apps_primer`: the script allowlist is
  one declaration.** The hosts browser scripts may load from used to
  live in four hand-synced places — test_app's interception, the served
  CSP, the agent-facing notes, curl's error message — kept honest only
  by a test. All four now derive from `AppsConfig.script_hosts`
  (default unchanged: `DEFAULT_SCRIPT_HOSTS`), so an embedder adding a
  private registry host (e.g. a self-hosted esm.sh over an internal
  npm registry) changes one tuple and the walls, the verifier, and the
  agent's instructions stay in agreement. `apps_primer` appends
  embedder guidance to the apps notes — the place to teach a private
  component lib's known-good import block. `build_router(csp=...)`
  now defaults to deriving from the config (`build_csp`); pass a string
  to override or `""` to disable. Removed: `test_app`'s per-call
  `cdn_allowlist` parameter (set it on the config instead). Agents predictably
  write into `/ui` themselves (`fig.write_json('/ui/x.json')`,
  savefig) instead of assigning objects to `ui = {...}` — and those
  files displayed nowhere. `run_python` now diffs the `/ui` listing
  around the call and appends files the code created to the
  `[ui artifacts: ...]` note (deduped against materialized values),
  extending the existing path-pointer near-miss forgiveness.
- **The walls label their doors.** Three predictable agent collisions
  now redirect instead of dead-ending:
  a 404 on `/api/<name>.py` says endpoints are module names without
  the extension (and suggests the real path when it exists) — agents
  reliably mirror the filename into `fetch()` and then debug the
  backend; blocked imports of `subprocess`/`requests`/`urllib.request`/
  `httpx`/`socket` get a `[hint: ...]` in both run_python observations
  and api.log pointing at the terminal's curl; and `urllib.parse` is
  granted in the STDLIB preset (pure string functions only — `quote`,
  `urlencode`, `parse_qs`, `urlparse`, ... — the network side of
  urllib stays out). The apps primer also states the no-`.py`-in-URL
  rule explicitly.
- **The 8MB `ui` artifact cap explains itself.** An oversize value used
  to silently degrade to a truncated `repr` `.txt` — a 280k-point
  plotly map showed up as a wall of text with no hint why. Now the
  tool result carries a `[ui note: ...]` diagnosis (size vs cap, and
  for plotly the actual usual culprit: per-point customdata/hover
  strings — coordinates are cheap, WebGL traces render 100k+ points
  fine) so the agent self-corrects, and the `.txt` artifact shows the
  same message to the human where the figure would have been.
  `materialize_ui` now returns `(artifacts, problems)`. The tool
  description also teaches the cap + lean-spec guidance up front.
- **`python3` terminal alias.** The reserved `python` bridge now also
  answers to `python3` — the reflex spelling agents type first. Both
  names are reserved against user command injection.
- **`warnings` in the STDLIB preset.** `warn`, `filterwarnings`,
  `simplefilter`, and `catch_warnings` are granted — agents reach for
  `warnings.filterwarnings("ignore")` the moment pandas/sklearn start
  emitting deprecation noise, and the module was imported by the
  presets but never granted.
- **Artifact channels: binary in, images and files out.** Three
  pieces close the "artifacts are stranded in the workspace" gap:
  a `view_image` tool in both adapters (the agent views a saved
  plot/chart — returned as real image content for vision models;
  png/jpeg/gif/webp, 10MB cap); MCP **resources** exposing every
  workspace file as `workspace://{path}` (text as text, binary as
  blob) with a `workspace://-/tree` index — the client-side window
  for extracting what the agent produced; and a `--mount
  POINT=DIR[:rw]` flag on the MCP CLI (read-only by default) — the
  inbound channel for seeding real host files without base64 games.
  `file_write` results additionally carry a ground-truth
  `ResourceLink` to the written file (the link exists because the
  write succeeded), and the MCP tool descriptions coach the agent to
  mention `workspace://` URIs when it produces artifacts.
- **Safe stdlib by default** — `PythonConfig(stdlib=True)` grants a
  curated stdlib set (see `nontainer.presets.STDLIB`), so a plain
  workspace's Python can `import math`/`json`/`csv`/... out of the box.
- **Module-grant presets** — `nontainer.presets.dataframes()` (numpy +
  pandas) and `plotting()` (matplotlib Agg-pinned + font cache warmed;
  plotly optional). `ModuleGrant` gains `include`/`exclude`/`recursive`/
  `name`; `PythonConfig.modules` flattens preset lists one level.
- **Results pin their commit** — `TerminalResult`/`PythonResult`/
  `EditOutcome` carry `checkpoint` (the commit the call produced, or
  `None`); `write_file`/`put` return a `WriteOutcome`; `ws.head` /
  `ws.dirty` pin the state a read-only call observed.
- **Async host facades** — `ws.aterminal` / `ws.arun_python` run the
  sync execution in a thread so event-loop hosts (FastAPI, etc.) stay
  responsive; the agent surface is unchanged.
- **Shared browser for `test_app`** — one Chromium across all calls
  (async Playwright on a dedicated loop-thread), a context per
  concurrent test bounded by a semaphore (`configure_browser`), plus
  `arun_test_app` and `shutdown_browser`. Memory scales with
  concurrency, not sessions.
- **`py.typed`** — the package now ships its PEP 561 marker.
- **Tool primers** — `WorkspaceTools`/`build_server` accept
  `terminal_primer` / `python_primer`: embedder guidance appended to the
  respective tool's description (e.g. "`db` is a SQLite store — use it,
  not `cache`, for shared state"). Strict 1-to-1 with the exposed tools;
  a `python_primer` in terminal-only mode lands in the terminal tool's
  `python` section (with a warning).

- **Faithful `sys` in terminal `python`** — piped input reaches the code
  as `sys.stdin` (`cat data | python script.py`), and `sys.argv` /
  `input()` work, via sandtrap's synthetic safe `sys`. No `import`
  quoting workarounds; dangerous `sys` internals stay unreachable.

### Added
- **Workspace extension surface: `exec_python` / `build_sandbox` /
  `lock`.** A small, documented contract for embedders composing
  execution features on top of the workspace: `exec_python(code, *,
  inputs, sandbox, cache, stdin, argv)` is the raw execution path (no
  checkpoint, no lock; `cache=` overrides the agent-visible cache —
  the old private `_UNSET` sentinel is gone); `build_sandbox(*,
  timeout, tick_limit, extra_classes, filesystem)` mints per-purpose
  sandboxes sharing the frozen config, memoizing the built `Policy`
  per parameter set so a fresh sandbox per request is cheap; `lock`
  exposes the single-writer RLock for host/extension work that must
  serialize with tool calls. The apps extra now talks exclusively to
  this surface (no private attribute access — enforced by a test), so
  it runs unchanged on any `WorkspaceProvider`; frozen serving's
  per-request policy rebuild (a latency + DoS-amplification papercut
  on the anonymous path) is fixed by the memo; mutable (authoring)
  dispatch now serializes under the workspace's own lock, so test_app
  route callbacks and screenshot writes can't race ordinary tool
  calls.

### Added
- **`--apps` flag on the MCP CLI.** `python -m nontainer.adapters.mcp
  --apps` enables the apps loop without writing an embed script: the
  `curl` terminal builtin plus a `test_app` tool whose screenshots
  return as MCP image content. Previously test_app over MCP required
  calling `build_server(ws, apps=...)` from Python.

### Changed
- **Workspace enforces its single-writer invariant internally.**
  Mutating public calls (`terminal`, `run_python`, `write_file`,
  `edit_file`, `put`, `checkpoint`, `restore`, `rollback`, `discard`,
  `fork`, `close`) hold an internal `RLock`, so a harness that threads
  parallel tool calls onto one session serializes safely — each call
  atomic + checkpointed — instead of corrupting staged state. Custom
  harnesses no longer need to supply their own lock (the adapters keep
  theirs as a fence for adapter-level work). Read-only accessors stay
  lock-free; host-side escape hatches (`ws.fs` writes, `ws.cache`
  mutation) bypass the lock and remain the caller's concurrency
  problem. RLock so a `host_object` that calls back into the public
  API serializes instead of deadlocking.
- **stderr capture is per-execution, not a process-global redirect.**
  `run_python` stderr now comes from sandtrap 0.2.4's ContextVar-routed
  capture (`ExecResult.stderr`): concurrent executions — other sessions
  in the same process, frozen app serving — no longer cross-contaminate
  stderr or risk leaving `sys.stderr` pointing at a dead buffer. The
  internal `capture_stderr` escape hatch is gone; served (frozen) app
  handlers get stderr capture back. Sandboxed `sys.stderr` writes in
  the terminal `python` builtin now surface as stderr instead of
  leaking into pipeline stdout.
- **Live app serving is now frozen (read-only) snapshots.** `build_router`
  serves a Workspace pinned to a published commit: handlers read the VFS
  and call `host_objects` but can't mutate it (write → 500). This makes
  serving **concurrent** (fresh read-only sandbox per request, no
  per-session lock, no staged buffer, no checkpointing) and lossless to
  evict. Mutable app state belongs in an external store via
  `host_objects`. Removed: per-session serialization, quiesce
  checkpointing, `queue_depth`/`quiesce_seconds`. Added: `max_snapshots`,
  `on_log` (handler logs route off the read-only VFS; default: the
  `nontainer.apps` logger). `AppRuntime(..., frozen=True, log_sink=...)`.
  The router is **stateless** — `resolve → dispatch`, no snapshot cache,
  no residency/lifecycle (cache inside `resolve` if it's expensive; the
  router doesn't close its result). Rate limiting is an edge concern;
  `rate_limit_per_min`/`max_snapshots`/`queue_depth` are gone.

### Fixed
- **`test_app` accepts a stringified actions list.** Models routinely
  send the nested list as a JSON string; the pydantic layer agno wraps
  entrypoints in rejected it on the annotation before the existing
  `coerce_actions` tolerance could run. The annotation is loosened so
  coercion gets its chance.
- **Agent-set response headers are matched case-insensitively.**
  `normalize()` lowercases `Response.headers` keys on the way to the
  wire, so the idiomatic `"Content-Type": "text/csv"` overrides the
  inferred content type instead of being silently ignored, and an
  agent-set `Content-Security-Policy` makes the served router defer
  its default instead of emitting a duplicate header (browsers apply
  the intersection). `WireResponse.headers` keys are now canonical
  lowercase.
- **`Request.require()` coerces symmetrically across sources.** JSON
  has one number type, so `require("x", float)` accepts JSON `5` and
  `require("n", int)` accepts `2.0` (non-integral floats still 400);
  bools are never numbers (JSON `true` no longer passes an `int`
  check); JSON strings coerce like query params; and query-param bools
  parse `true/1/false/0` instead of Python's `bool("false") is True`.
- **Screenshot cap no longer aborts the test.** A `test_app` action
  hitting `max_screenshots` is a noted soft skip (`ok`, with a
  "skipped: screenshot cap reached" note) instead of a hard failure
  that discarded every later action — asserts after the cap now run
  and count.
- **Handler-log failures warn instead of going silently blind.**
  `_log` still never breaks dispatch, but a broken/full fs (or a
  raising `on_log` sink) now emits one `RuntimeWarning` per runtime —
  previously every handler diagnostic vanished while the agent's
  documented repair loop ("tail `/app/logs/api.log`") debugged blind.
- **`test_app` false-PASS window closed (as far as heuristics can).**
  `read` now settles before observing, so a fetch that *starts* after
  the previous action's settle returned (debounce, `setTimeout`) is
  waited for instead of read as stale DOM. And a settle that exits via
  its cap (`settle_cap`, default 5s — now a `test_app` parameter)
  attaches a stale-risk note to the action's result instead of
  silently passing, pointing the agent at `{"assert": ...}` — the
  retrying form no heuristic can replace, since nothing can wait for a
  fetch that hasn't started yet.
- **Browser shutdown no longer stalls interpreter exit.** The shared
  test_app browser's atexit teardown deadlines dropped from 10s+5s to
  3s+2s — a healthy Chromium closes in milliseconds, and a wedged one
  isn't worth holding process exit for. `configure_browser` now
  documents its process-global, first-caller-wins contract.
- **App static serving path traversal** — `.`/`..` segments can no
  longer escape `/app/`, and backend source under `/app/api/` is never
  served as a static file.

### Changed
- Requires **sandtrap ≥ 0.2.4** (per-execution stderr capture;
  recursive-registration filter propagation, dotted patterns,
  synthetic `sys`/stdin) and **monkeyfs ≥ 0.1.5**
  (`VirtualFS.invalidate()`).
