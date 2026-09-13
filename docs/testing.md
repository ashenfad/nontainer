# ws-pytest

`ws-pytest` runs the workspace's unit tests. It is a terminal builtin,
so an agent types it in the shell next to `cat` and `grep`, and the
tests run through the same executor its `python` calls run in.

This page is the human-readable twin of `ws-pytest --help`. The host's
half is `run_pytest(ws)`, which returns a
[`TestReport`](api.md#unit-tests-ws-pytest) — the record a merge policy
gates on, and the thing the terminal text is a rendering of.

```python
from nontainer.wspytest import register_wspytest

register_wspytest(ws)     # now the shell answers `ws-pytest`
```

`enable_apps(ws)` already calls it, so a workspace with an app has the
verb with no second step; a workspace without one registers it
directly. Until someone does, the agent gets `ws-pytest: command not
found`.

It is pytest's **shape**, not pytest: the discovery rule, the report,
the exit codes, and the flags worth having. Everything else is refused
with a message naming what to do instead.

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
would not collect or an argument that makes no sense, `5` nothing
collected. So `ws-pytest && ws-curl $APP_ORIGIN/api/scores` does what
it looks like.

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

Patch **objects and classes**, never module-level names:

```python
from unittest.mock import patch

with patch.object(Client, "fetch", return_value={"ok": True}):
    ...
```

`patch.object(module, "NAME")` is the one idiom that looks right and
does nothing here: a workspace module is executed into a copy of its
dict, so the patch reads back changed while the module's own functions
go on seeing the original — a silent false pass. `patch("app.api.x.y")`
with a string target raises instead, which is at least loud.

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

Dependencies are substituted by keyword rather than by patching for the
reason above: a handler's collaborators are not module attributes to
patch.

One asymmetry, said plainly: **`call` does not reproduce the read-only
filesystem a real GET runs under.** The handler runs in the test's own
sandbox, so a GET that writes passes here and 500s under `ws-curl`.
That is the price of the mock being visible to the handler at all, and
the wire tier is where structural REST is enforced:

```
ws-pytest   a function, in the sandbox your code runs in
ws-curl     one request, through the real dispatch path
test_app    a browser, a page, the app's own fetches
```

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
