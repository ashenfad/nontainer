# Unit tests: `ws-pytest` and `ws-vitest`

Two terminal builtins, one per language, for the tier below a request.
`ws-curl` asks one request of the app and `test_app` drives a page; these
two ask a question of a *function* and answer with the assertion that
failed.

```
ws-pytest   a function, in the sandbox your code runs in
ws-vitest   a module, in a browser page with nothing else reachable
ws-curl     one request, through the real dispatch path
test_app    a browser, a page, the app's own fetches
```

This page is the human-readable twin of `ws-pytest --help` and
`ws-vitest --help`. The host's half is `run_pytest(ws)` /
`run_vitest(ws)`, each returning a
[`TestReport`](api.md#unit-tests-ws-pytest-and-ws-vitest) — the record a
merge policy gates on, and the thing the terminal text is a rendering
of. The two verbs produce the same record, with `report.tool` telling
them apart, so one `check=` hook and one UI consume either.

```python
from nontainer.wspytest import register_wspytest
from nontainer.wsvitest import register_wsvitest

register_wspytest(ws)     # now the shell answers `ws-pytest`
register_wsvitest(ws)     # ... and `ws-vitest`
```

`enable_apps(ws)` already calls both, so a workspace with an app has the
verbs with no second step; a workspace without one registers them
directly. Until someone does, the agent gets `command not found`.

Both are the tool's **shape**, not the tool: the discovery rule, the
report, the exit codes, and the flags worth having. Everything else is
refused with a message naming what to do instead.

**Where each one runs.** Python tests run *where your code runs*: in the
sandbox on a local rung, in the guest on a VM rung, under the session's
own python config. JavaScript tests run in a browser **on the host on
every rung**, against the files this workspace holds — the same
asymmetry `test_app` has. The report says so on every run.

---

# `ws-pytest`

`ws-pytest` runs the workspace's Python unit tests through the same
executor its `python` calls run in, so a test imports workspace modules
exactly as the app does.

## Where tests live

```
/workspace/tests/test_scores.py    ← collected
/workspace/tests/util/test_fmt.py  ← collected (any depth under the root)
/workspace/app/api/scores.py       ← a handler; never a test
/workspace/app/test_thing.py       ← NOT collected, and the run says so
```

`test_*.py` anywhere under the workspace root is collected, except
under `app/`. That exception is not taste: `app/` is what publishes, so
a test there ships in every deployment and is fetchable from the served
app. `tests/` sits outside the published subtree, which is the whole
reason to put it there.

Inside a file, every top-level `def test_*()` taking no arguments runs.
A test that takes an argument is asking for a fixture, and there are
none — that is reported as an error rather than skipped, because a test
that silently never runs is worse than one that says why.

## The flags

| ws-pytest | what it does here |
|---|---|
| `<path>` | run one file, or every test file under a directory |
| `<path>::<name>` | run one test |
| `-k EXPR` | run tests whose name matches: a substring, joined with `and` / `or` / `not` and parentheses |
| `-x` | stop after the first failure |
| `--maxfail=N` | stop after the Nth failure |
| `-q` | counts only |
| `-v` | one line per test |
| `--tb=short\|long\|no` | how much of a failure to show; `short` by default |

Exit codes are pytest's: `0` all passed, `1` failures, `2` a file that
would not collect, `4` an argument that makes no sense — a flag, a
path, or a test name nothing defines — `5` nothing collected. So
`ws-pytest && ws-curl $APP_ORIGIN/api/scores` does what it looks like.
A `-k` that matches nothing is `5`, not `4`: a filter is not a name.

Refused, each with the idiom that replaces it: `-s` (there is no
capture to disable — a test's stdout is already in the report), `--lf`
(nothing here outlives the call to remember a last run; name the test),
`-p` (there are no plugins), `--co` (use `-q` with a path), `-m` and
markers (select with `-k`), `--fixtures`, `--pdb`, `--cov`. A
`tests/conftest.py` is reported in the run as unread rather than
silently ignored.

## Writing a test

A test is plain Python. `assert` is the whole assertion API, and
`unittest.mock` is importable for fakes:

```python
# tests/test_lib.py
from unittest.mock import MagicMock

from app.api._lib import load


def test_load_uppercases():
    db = MagicMock()
    db.query.return_value = ["ann"]
    assert load(db) == ["ANN"]
```

That test needs nothing from nontainer, and it is the shape most tests
should have. It works because of the convention the app design already
asks for: **only a handler file may name `db` and `cache` free; a
shared module takes its dependencies as arguments.** A module a handler
imports gets none of the handler's namespace, so a `_lib.py` function
reading `db` as a bare name raises `NameError` when a request reaches
it. Give it what it needs — `def load(db, limit)` — and a test can call
it with a fake. A module that genuinely wants the ambient object
imports it instead: `from host import db` works at the top level, in a
handler and in a module alike.

Mocking is the test's job, in the standard idiom. `patch.object` works
the way it does anywhere — on a workspace module, on a class, on an
instance — and a patched module attribute is what the module itself
reads:

```python
from unittest.mock import patch

import app.api._feed as feed


def test_summary_uses_fetch():
    with patch.object(feed, "fetch", return_value="fake"):
        assert feed.summary() == "fake:3"     # inside the module too
    with patch.object(feed, "LIMIT", 99):
        assert feed.summary() == "real:99"
```

Two things cannot be patched, both for reasons worth knowing:

- **The `host` module is read-only.** `patch.object(host, "db", fake)`
  is refused (`Cannot set attribute 'db' on module 'host'`), because
  the module is rebuilt per execution and an attribute set on it would
  be a channel from one execution to the next. Patch the name your own
  module reads instead: `import host` at the top and `host.db` at the
  call site, since `from host import db` binds once at import time.
- **A handler's injected names are not module attributes.** `db` and
  `cache` are bound into the handler's namespace by dispatch, so there
  is nothing to patch them on — which is why `call` takes them as
  keywords: `call("scores", db=fake)`.

Use `patch.object`, not a string target. `patch("app.api._feed.fetch")`
resolves the dotted name through the real `importlib`, which has never
heard of the workspace tree where imports are virtual — so it raises
`ModuleNotFoundError: No module named 'app'` rather than patching
anything. It is the one spelling whose behaviour is the rung's business
rather than the verb's.

## Calling a handler

A handler is the one unit ordinary Python cannot reach: importing it
and calling `get(req)` by hand fails, because `db` and `cache` are not
module attributes, and it would skip the envelope anyway. So there is
one helper, `call`, already in scope in every test — no import:

```python
# tests/test_scores.py
from unittest.mock import MagicMock


def test_limit_is_honoured():
    db = MagicMock()
    db.query.return_value = ["ann", "bob", "cy"]
    resp = call("scores", params={"limit": "2"}, db=db)
    assert resp.status == 200
    assert resp.json["scores"] == ["ANN", "BOB"]
    db.query.assert_called_once()


def test_missing_name_is_a_400():
    resp = call("scores", "POST", json={}, db=MagicMock())
    assert resp.status == 400
```

```
call(module, method="GET", path=None, *, params=None, body=None,
     json=None, headers=None, **objects) -> TestResponse
```

- `module` is the handler's name under `app/api/`, as a plain string.
  It has to be a literal: the handler is composed into the test program
  before the test runs, so a computed name is refused by name.
- `path` defaults to `/api/<module>`; `params` becomes the query
  string, `json=` a JSON body with its content type, `body=` bytes,
  text, or a value to encode.
- `**objects` substitutes what dispatch would bind: `db=fake`,
  `cache=fake`. Anything not named binds as dispatch would, so a test
  that forgets to fake `db` talks to the real one, loudly. A name the
  session injects nothing under has no real one to fall back to, so
  `call` asks for it; a keyword the handler never reads is an error,
  because a fake nothing reads proves nothing.
- The return is the **response**, not the handler's raw return:
  `resp.status`, `resp.json`, `resp.text`, `resp.headers`, `resp.ok`.
  Liberal returns are normalized, and `raise HttpError(404, ...)` comes
  back as `resp.status == 404` rather than as an exception. Testing the
  envelope is the point of the helper; testing a function is what a
  plain call is for.

Dependencies are substituted by keyword rather than by patching because
a handler's injected names are not module attributes: `db` is bound
into the handler's namespace by dispatch, so there is nothing for a
patch to reach.

One asymmetry, said plainly: **`call` does not reproduce the read-only
filesystem a real GET runs under.** The handler runs in the test's own
sandbox, so a GET that writes passes here and 500s under `ws-curl`.
That is the price of the mock being visible to the handler at all, and
the wire tier is where structural REST is enforced.

A handler that `global`s a module-level name cannot be composed into a
test program, and `ws-pytest` says so instead of running something that
means something else.

## Reading a failure

```
=================================== FAILURES ===================================
_____________________________ test_limit_is_honoured ___________________________
tests/test_scores.py:9: in test_limit_is_honoured
    assert resp.json["scores"] == ["ANN", "BOB"]
E   AssertionError

========================= 1 failed, 2 passed in 0.04s ==========================
```

Every frame names a file you wrote — your test, your `_lib.py`, your
handler — at the line you wrote it on. The sandbox's own frames are
dropped, and the frames of a handler `call` ran name `app/api/<name>.py`
and the handler's own line. `--tb=long` adds the enclosing function's
source with the failing line marked, as pytest's does.

## Both rungs

Python tests run **where your code runs**: in the sandbox on a local
rung, in the guest on a VM rung, under the session's own python config
either way. The report reads identically on both — a conformance corpus
asserts it byte for byte.

---

# `ws-vitest`

`ws-vitest` runs the workspace's JavaScript unit tests. Each test file
is loaded as page JavaScript in a headless Chromium, on a harness page
nontainer serves.

It is vitest's shape, which is jest's surface — and vitest is not
installed, nor is anything else: the harness is nontainer's own
JavaScript, served inline from the workspace's synthetic origin. There
is no `node_modules`, no build step and no config file.

## Where tests live

```
/workspace/tests/util.test.js       ← collected, and the preferred home
/workspace/tests/dom/card.test.js   ← collected (any depth under tests/)
/workspace/app/util.js              ← the module under test
/workspace/app/util.test.js         ← collected, beside its module
/workspace/app/api/scores.py        ← a handler; never a JavaScript test
```

`tests/**/*.test.js` leads the run and `app/**/*.test.js` follows it. A
test beside the module it tests is what the ecosystem does and what an
agent writes without being asked, so it is allowed — and **it ships with
the app**, because `app/` is what a publication carries. The run says so
in a note naming the files; move one to `tests/` if that is not what you
want.

`app/api/` is neither home. A `.test.js` there is reported as **not
runnable**, by name and with the reason: the harness refuses that
subtree the way the served app's static dispatch does, so the page could
not load the file even if it were collected — and handlers are Python,
which `ws-pytest` tests from `tests/`.

## The layout that makes imports work

`app/` and `tests/` are served as **siblings** under one synthetic root:

```
https://nontainer.test/apps/t-unit/app/util.js
https://nontainer.test/apps/t-unit/tests/util.test.js
```

so the import you would write anyway resolves:

```js
// tests/util.test.js
import { add } from '../app/util.js';

describe('add', () => {
  it('adds two numbers', () => {
    expect(add(1, 2)).toBe(3);
  });
});
```

and a test beside its module imports `'./util.js'`. Nothing else has a
URL — not the workspace root, not `helpers/`, not `app/api/`.

## The flags

| ws-vitest | what it does here |
|---|---|
| `run` | accepted and ignored (nothing here watches for edits) |
| `<path>` | run only files whose path contains this word |
| `-t NAME` | run only tests whose name contains NAME |
| `--reporter=default\|verbose` | counts per file / one line per test |
| `--bail[=N]` | stop after the first (or Nth) failing file |

Exit codes are `0` for a green run and `1` for anything else. vitest has
no separate exit code for a usage error, so a refused flag, a filter
that matched nothing and a failing test all exit `1`, and the message is
what tells them apart.

Refused, each with what to do instead: `--coverage` (no instrumentation
here), `--ui` (the report is the answer; `--reporter=verbose` is one
line per test), `--config` (the layout *is* the configuration),
`--watch`, `--browser`, `--environment`.

A positional word is matched as a substring of a file's path, the way
vitest matches one. A word that names an absolute path is resolved
against the shell's cwd first, so `ws-vitest /workspace/tests/util.test.js`
means the file it names on either rung.

## The harness surface

`describe`, `it`/`test`, `beforeEach`/`afterEach`, `expect`, `vi` and
`jest` are globals on the page, and the same names resolve through an
import map:

```js
import { describe, it, expect, vi } from 'vitest';   // or '@jest/globals'
```

`expect` covers `toBe`, `toEqual`, `toStrictEqual`, `toContain`,
`toHaveLength`, `toBeTruthy`/`toBeFalsy`,
`toBeNull`/`toBeUndefined`/`toBeDefined`, `toThrow`, `toMatch`,
`toBeGreaterThan`/`toBeGreaterThanOrEqual`/`toBeLessThan`/`toBeLessThanOrEqual`,
`toBeCloseTo`, `toHaveBeenCalled`/`toHaveBeenCalledTimes`/`toHaveBeenCalledWith`,
`.not`, and `resolves`/`rejects`.

`vi` supplies `fn`, `spyOn`, `stubGlobal`, `stubFetch`,
`unstubAllGlobals` and the `clearAllMocks` family; `jest` is the same
object under its other name. An `async` test is awaited, and
`beforeEach` declared below an `it()` in the same block still applies to
it — the block is collected before anything runs.

Refused with a message naming the replacement:

- **`vi.mock`** — module mocking needs a loader hook a browser does not
  have. Pass the dependency in as an argument, spy on an object your own
  code owns, or stub the boundary.
- **snapshot matchers** (`toMatchSnapshot` and friends) — there is no
  snapshot plane; assert the value with `toEqual`.
- **`expect.extend`** — the matcher set is fixed; write a helper
  function your test calls.
- **`it.only` / `it.skip`** and the `describe` equivalents — choose what
  runs from the command line, with a path filter or `-t NAME`.
- **`beforeAll` / `afterAll`** — each test file is one page of its own,
  so build shared state at module scope.

**A module's exports are read-only in a browser**, so `vi.spyOn(mod,
'fn')` on something you imported cannot replace anything. The harness
says that rather than failing silently. Spy on an object your own code
owns, or take the dependency as an argument — which is the same rule
`_lib.py` follows on the Python side, for the same reason.

## Mock at the fetch boundary

A run is **hermetic**: no api routes come up, no other host is
reachable, and the policy on the wire is stricter than the app's own
(`connect-src 'self'`, where a served app gets `'self' https:`). A unit
test that silently reaches the network passes for the wrong reason and
fails in production, so a forgotten stub fails here — which is the
outcome that teaches.

The boundary a frontend module has is `fetch`, so that is what you stub:

```js
it('renders the scores it fetched', async () => {
  vi.stubFetch({'api/scores': {scores: ['ann', 'bob']}});
  const el = await renderScores();
  expect(el.querySelectorAll('li')).toHaveLength(2);
});
```

`vi.stubFetch` is keyed by **exact path**, not by a pattern: a pattern
invites a mock that matches more than the test meant. A leading `/` and
the page's own prefix are both tolerated, so `'api/scores'` and
`'/api/scores'` are the same key. A fetch to a route the table does not
name throws, naming the route and the keys that *were* stubbed.

The value is the JSON body. For anything else — a status, a header, a
text body — hand it a `Response`:

```js
vi.stubFetch({'api/scores': new Response('', {status: 503})});
```

`vi.stubGlobal('fetch', myFake)` is there too, and is what `stubFetch`
is built on; `vi.unstubAllGlobals()` puts the real one back.

With no stub at all, a `fetch('api/scores')` gets a 404 whose JSON body
says there are no api routes and names `vi.stubFetch`. A fetch to any
other host is refused by the policy before a request is made, and the
run's notes name the directive that refused it.

## Reading a failure

```
 ✓ tests/util.test.js (2 tests) 1ms
 ❯ tests/scores.test.js (2 tests | 1 failed) 3ms
   × renders the scores it fetched

⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯ Failed Tests 1 ⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯

 FAIL  tests/scores.test.js > renders the scores it fetched
AssertionError: expected 1 to be 2 // Object.is equality
 ❯ tests/scores.test.js:7:44
   7|   expect(el.querySelectorAll('li').length).toBe(2);

⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯

 Test Files  1 failed | 1 passed (2)
      Tests  1 failed | 3 passed (4)
   Duration  0.31s
```

Every frame names a file you wrote — `tests/...` or `app/...` — at the
line and column you wrote it on, with that line quoted. The harness's
own frames and the page's entry are dropped. A file that throws before
any test runs (a bad import, a `vi.mock` at module scope) is a **failed
suite** rather than a failed test: it contributes no tests to the
counts, and when the cause was a module that 404'd, the message names
the path nothing was served at.

A run is bounded: a page that will not load and a test that returns a
promise which never settles both end as a failure with a message, not as
a hang.

## Both rungs

`ws-vitest` runs **host-side on every rung**. Chromium lives on the
host; on a VM rung the guest never sees the test file, and the workspace
filesystem is read by the driver. That is the asymmetry `test_app`
already has, and it sits right next to `ws-pytest`'s guest execution, so
every report ends with a note saying which is which. The report reads
identically on both rungs — a conformance corpus asserts it, including
a test file written by real bash in a guest.
