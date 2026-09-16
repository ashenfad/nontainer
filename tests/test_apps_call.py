"""``call``: running a handler the way a request would, from a test.

Every case here runs through the real path — a test file in a
workspace, executed by ws-pytest's runner — because what is being
tested is exactly what the sandbox does with the composed handler. A
green report means the assertions inside the test file held.
"""

import pytest

from nontainer import PythonConfig, Workspace
from nontainer.apps import enable_apps
from nontainer.providers import KvgitProvider
from nontainer.wspytest import run_pytest

LIB = "def load(db, limit=10):\n    return [s.upper() for s in db.query()][:limit]\n"

SCORES = (
    "from app.api._lib import load\n"
    "\n"
    "\n"
    "def get(req):\n"
    "    limit = int(req.params.get('limit', 10))\n"
    "    return {'scores': load(db, limit)}\n"
    "\n"
    "\n"
    "def post(req):\n"
    "    name = req.require('name')\n"
    "    return {'ok': name}\n"
)


class FakeDb:
    """What the session injects when a test does not substitute one."""

    def query(self):
        return ["real"]


@pytest.fixture
def ws():
    w = Workspace(
        KvgitProvider.open(None, session="apps-call"),
        python=PythonConfig(host_objects={"db": FakeDb()}),
    )
    enable_apps(w)
    w.files.fs.write("/workspace/app/api/_lib.py", LIB.encode())
    w.files.fs.write("/workspace/app/api/scores.py", SCORES.encode())
    try:
        yield w
    finally:
        w.close()


def run(ws, body):
    ws.files.fs.write("/workspace/tests/test_call.py", body.encode())
    return run_pytest(ws)


def test_a_mock_reaches_the_handler_through_its_library(ws):
    report = run(
        ws,
        "from unittest.mock import MagicMock\n"
        "\n"
        "\n"
        "def test_limit_is_honoured():\n"
        "    db = MagicMock()\n"
        "    db.query.return_value = ['ann', 'bob', 'cy']\n"
        "    resp = call('scores', params={'limit': '2'}, db=db)\n"
        "    assert resp.status == 200\n"
        "    assert resp.json['scores'] == ['ANN', 'BOB']\n"
        "    db.query.assert_called_once()\n",
    )
    assert report.ok, report.outcomes


def test_a_dependency_nobody_faked_is_the_real_one(ws):
    """Substitution is opt-in: a test that forgets to fake `db` talks
    to the session's own, loudly, rather than to a helpful stub."""
    report = run(
        ws,
        "def test_real_db():\n    assert call('scores').json['scores'] == ['REAL']\n",
    )
    assert report.ok, report.outcomes


def test_a_required_field_is_a_400(ws):
    report = run(
        ws,
        "from unittest.mock import MagicMock\n"
        "\n"
        "\n"
        "def test_missing_name():\n"
        "    resp = call('scores', 'POST', json={}, db=MagicMock())\n"
        "    assert resp.status == 400\n"
        "    assert 'name' in resp.json['error']\n"
        "\n"
        "\n"
        "def test_present_name():\n"
        "    resp = call('scores', 'POST', json={'name': 'ann'}, db=MagicMock())\n"
        "    assert resp.status == 200\n"
        "    assert resp.json == {'ok': 'ann'}\n",
    )
    assert report.ok, report.outcomes


def test_an_http_error_comes_back_as_a_status(ws):
    ws.files.fs.write(
        "/workspace/app/api/gone.py",
        b"def get(req):\n    raise HttpError(404, 'no such score')\n",
    )
    report = run(
        ws,
        "def test_404():\n"
        "    resp = call('gone')\n"
        "    assert resp.status == 404\n"
        "    assert resp.json['error'] == 'no such score'\n"
        "    assert not resp.ok\n",
    )
    assert report.ok, report.outcomes


def test_liberal_returns_are_normalized(ws):
    ws.files.fs.write(
        "/workspace/app/api/kinds.py",
        b"def get(req):\n"
        b"    kind = req.params.get('kind')\n"
        b"    if kind == 'text':\n"
        b"        return 'plain words'\n"
        b"    if kind == 'none':\n"
        b"        return None\n"
        b"    if kind == 'response':\n"
        b"        return Response(status=201, body={'made': True})\n"
        b"    return [1, 2]\n",
    )
    report = run(
        ws,
        "def test_kinds():\n"
        "    text = call('kinds', params={'kind': 'text'})\n"
        "    assert text.status == 200\n"
        "    assert text.text == 'plain words'\n"
        "    assert text.content_type.startswith('text/plain')\n"
        "    assert call('kinds', params={'kind': 'none'}).status == 204\n"
        "    made = call('kinds', params={'kind': 'response'})\n"
        "    assert made.status == 201\n"
        "    assert made.json == {'made': True}\n"
        "    assert call('kinds').json == [1, 2]\n",
    )
    assert report.ok, report.outcomes


def test_a_method_the_handler_lacks_is_a_405(ws):
    report = run(
        ws,
        "def test_405():\n"
        "    resp = call('scores', 'DELETE')\n"
        "    assert resp.status == 405\n",
    )
    assert report.ok, report.outcomes


def test_a_get_that_writes_passes_here_and_the_wire_tier_is_where_it_fails(ws):
    """The documented asymmetry: the handler runs in the test's own
    sandbox, which has the session's writable filesystem. Structural
    REST is enforced where a request is, not here."""
    ws.files.fs.write(
        "/workspace/app/api/writes.py",
        b"def get(req):\n"
        b"    with open('/workspace/written.txt', 'w') as f:\n"
        b"        f.write('a get wrote this')\n"
        b"    return {'wrote': True}\n",
    )
    report = run(
        ws,
        "def test_get_that_writes():\n    assert call('writes').json == {'wrote': True}\n",
    )
    assert report.ok, report.outcomes
    assert ws.files.fs.exists("/workspace/written.txt")

    wire = ws.terminal("ws-curl -f $APP_ORIGIN/api/writes")
    assert wire.exit_code == 22
    assert "HTTP 500" in (wire.stdout + wire.stderr)


def test_a_handler_traceback_names_the_handlers_own_line(ws):
    report = run(
        ws,
        "from unittest.mock import MagicMock\n"
        "\n"
        "\n"
        "def test_boom():\n"
        "    call('scores', params={'limit': 'x'}, db=MagicMock())\n",
    )
    assert report.failed == 1
    assert report.outcomes[0].traceback == (
        "tests/test_call.py:5: in test_boom\n"
        "    call('scores', params={'limit': 'x'}, db=MagicMock())\n"
        "app/api/scores.py:5: in get\n"
        "    limit = int(req.params.get('limit', 10))\n"
        "E   ValueError: invalid literal for int() with base 10: 'x'"
    )


def test_faking_what_the_handler_never_reads_is_an_error(ws):
    """A fake nothing reads proves nothing — and a test that believes
    it substituted the database is worse than one that fails."""
    report = run(
        ws,
        "from unittest.mock import MagicMock\n"
        "\n"
        "\n"
        "def test_wrong_name():\n"
        "    call('scores', database=MagicMock())\n",
    )
    assert report.failed == 1
    assert "substitutes what the handler does not use" in (
        report.outcomes[0].message or ""
    )


def test_an_unknown_handler_name_says_what_to_check(ws):
    report = run(ws, "def test_absent():\n    call('nosuch')\n")
    assert report.failed == 1
    assert "app/api/nosuch.py" in (report.outcomes[0].message or "")


def test_a_computed_handler_name_is_refused_by_name(ws):
    report = run(
        ws,
        "def test_computed():\n    name = 'scores'\n    call(name)\n",
    )
    assert report.failed == 1
    assert "name it as a plain string" in (report.outcomes[0].message or "")


def test_a_handler_using_global_is_refused_at_compose_time(ws):
    ws.files.fs.write(
        "/workspace/app/api/counter.py",
        b"total = 0\n\n\ndef get(req):\n    global total\n    total += 1\n    return {'n': total}\n",
    )
    report = run(ws, "def test_counter():\n    call('counter')\n")
    assert report.exit_code == 2
    assert "global total" in (report.collection_error or "")


def test_a_test_file_that_never_calls_a_handler_composes_nothing(ws):
    """The preamble is only what a test named: a file of plain unit
    tests keeps its own line numbers and costs no handler source."""
    report = run(
        ws,
        "from unittest.mock import MagicMock\n"
        "\n"
        "from app.api._lib import load\n"
        "\n"
        "\n"
        "def test_load():\n"
        "    db = MagicMock()\n"
        "    db.query.return_value = ['ann']\n"
        "    assert load(db) == ['ANN']\n"
        "\n"
        "\n"
        "def test_fails_on_its_own_line():\n"
        "    assert False\n",
    )
    assert report.failed == 1
    assert report.outcomes[1].line == 13


def test_a_dependency_the_session_does_not_inject_must_be_faked(ws):
    """A handler reading a name nothing binds would raise NameError in
    a request. The test is told which fake it owes rather than being
    handed a stub that proves nothing."""
    ws.files.fs.write(
        "/workspace/app/api/other.py",
        b"def get(req):\n    return {'rows': store.rows()}\n",
    )
    report = run(ws, "def test_other():\n    call('other')\n")
    assert report.failed == 1
    assert "call('other') needs store=" in (report.outcomes[0].message or "")

    report = run(
        ws,
        "from unittest.mock import MagicMock\n"
        "\n"
        "\n"
        "def test_other():\n"
        "    store = MagicMock()\n"
        "    store.rows.return_value = [1]\n"
        "    assert call('other', store=store).json == {'rows': [1]}\n",
    )
    assert report.ok, report.outcomes


def test_the_contract_classes_are_not_substitutable(ws):
    """A handler naming Response or HttpError is naming the contract,
    not an injection: those resolve from the test program itself."""
    from nontainer.apps.testing import free_names

    source = "def get(req):\n    if db is None:\n        raise HttpError(404, 'x')\n    return Response(status=201, body=db.all())\n"
    assert "HttpError" in free_names(source)
    ws.files.fs.write("/workspace/app/api/mixed.py", source.encode())
    report = run(
        ws,
        "from unittest.mock import MagicMock\n"
        "\n"
        "\n"
        "def test_mixed():\n"
        "    db = MagicMock()\n"
        "    db.all.return_value = {'n': 1}\n"
        "    assert call('mixed', db=db).status == 201\n",
    )
    assert report.ok, report.outcomes


def test_a_mock_survives_process_isolation():
    """The reason the handler is composed rather than dispatched: a
    fresh view sandbox would not hold the mock the test built, which
    works by accident in one process and breaks in two."""
    w = Workspace(
        KvgitProvider.open(None, session="apps-call-iso"),
        python=PythonConfig(isolation="process", host_objects={"db": FakeDb()}),
    )
    enable_apps(w)
    try:
        w.files.fs.write("/workspace/app/api/scores.py", SCORES.encode())
        w.files.fs.write("/workspace/app/api/_lib.py", LIB.encode())
        report = run(
            w,
            "from unittest.mock import MagicMock\n"
            "\n"
            "\n"
            "def test_fake():\n"
            "    db = MagicMock()\n"
            "    db.query.return_value = ['ann']\n"
            "    assert call('scores', db=db).json == {'scores': ['ANN']}\n"
            "\n"
            "\n"
            "def test_real():\n"
            "    assert call('scores').json == {'scores': ['REAL']}\n",
        )
        assert report.ok, report.outcomes
    finally:
        w.close()


def test_a_future_import_still_leads_the_composed_program(ws):
    """The composed program is a valid module whenever the test file
    is: a `from __future__` import (and the docstring that may precede
    it) stays at the top, with the handlers composed in after."""
    report = run(
        ws,
        '"""Tests for the scores endpoint."""\n'
        "\n"
        "from __future__ import annotations\n"
        "\n"
        "from unittest.mock import MagicMock\n"
        "\n"
        "\n"
        "def test_limit() -> None:\n"
        "    db = MagicMock()\n"
        "    db.query.return_value = ['ann', 'bob']\n"
        "    assert call('scores', params={'limit': '1'}, db=db).json == {\n"
        "        'scores': ['ANN']\n"
        "    }\n"
        "\n"
        "\n"
        "def test_names_its_own_line() -> None:\n"
        "    assert False\n",
    )
    assert report.collection_error is None, report.collection_error
    assert report.passed == 1
    assert report.failed == 1
    assert report.outcomes[1].line == 17
    assert report.outcomes[1].frames[-1].path == "tests/test_call.py"


def test_free_names_are_resolved_per_scope():
    """What a handler reads from the injected namespace is a question
    about scopes: a local in one function does not answer for a global
    another function reads."""
    from nontainer.apps.testing import free_names

    # A helper's parameter is its own; the global `get` reads is not.
    assert free_names(
        "def helper(db):\n"
        "    return db.query()\n"
        "\n"
        "\n"
        "def get(req):\n"
        "    return {'rows': helper(db)}\n"
    ) == ("db",)
    # Bound at module level: the module's own, not an injection.
    assert free_names("db = connect()\n\n\ndef get(req):\n    return db.all()\n") == (
        "connect",
    )
    # Bound in an enclosing function scope: a closure, not an injection.
    assert (
        free_names(
            "def get(req):\n"
            "    total = 0\n"
            "\n"
            "    def inner():\n"
            "        return total\n"
            "\n"
            "    return inner()\n"
        )
        == ()
    )
    # A comprehension target is local to the comprehension.
    assert free_names("def get(req):\n    return [x.id for x in db.all()]\n") == ("db",)


def test_a_helper_with_a_local_of_the_same_name_still_takes_the_fake(ws):
    ws.files.fs.write(
        "/workspace/app/api/shadowed.py",
        b"def _rows(db):\n"
        b"    return list(db.query())\n"
        b"\n"
        b"\n"
        b"def get(req):\n"
        b"    return {'rows': _rows(db)}\n",
    )
    report = run(
        ws,
        "from unittest.mock import MagicMock\n"
        "\n"
        "\n"
        "def test_shadowed():\n"
        "    db = MagicMock()\n"
        "    db.query.return_value = ['ann']\n"
        "    assert call('shadowed', db=db).json == {'rows': ['ann']}\n",
    )
    assert report.ok, report.outcomes


# -- the contract a directly imported handler carries -------------------------


def test_an_imported_handler_raises_httperror_not_nameerror(ws):
    """``Request``/``Response``/``HttpError`` are dispatch's, bound into
    the handler's globals for a request. A test that imports the module
    instead of calling it gets the same three, so the sad path means a
    status either way."""
    ws.files.fs.write(
        "/workspace/app/api/summary.py",
        b"def get(req):\n"
        b"    key = req.params.get('key')\n"
        b"    if not key:\n"
        b"        raise HttpError(400, 'key is required')\n"
        b"    return Response(status=200, body={'key': key})\n",
    )
    report = run(
        ws,
        "import app.api.summary as summary\n"
        "\n"
        "\n"
        "def test_the_sad_path_is_catchable():\n"
        "    req = Request('GET', '/api/summary', {}, {}, b'')\n"
        "    try:\n"
        "        summary.get(req)\n"
        "    except HttpError as e:\n"
        "        assert isinstance(e, HttpError)\n"
        "        assert e.status == 400\n"
        "    else:\n"
        "        assert False, 'the handler did not raise'\n"
        "\n"
        "\n"
        "def test_the_happy_path_returns_a_response():\n"
        "    req = Request('GET', '/api/summary', {'key': 'k'}, {}, b'')\n"
        "    assert summary.get(req).status == 200\n"
        "\n"
        "\n"
        "def test_call_on_the_same_handler_still_reads_the_envelope():\n"
        "    assert call('summary').status == 400\n",
    )
    assert report.ok, report.outcomes


def test_a_library_under_api_is_not_a_handler_and_gets_nothing(ws):
    """Dispatch runs handlers, not the modules they import: a library
    under app/api/ has no contract in a request, so it has none here."""
    ws.files.fs.write(
        "/workspace/app/api/_raiser.py",
        b"def boom():\n    raise HttpError(400, 'x')\n",
    )
    report = run(
        ws,
        "from app.api._raiser import boom\n"
        "\n"
        "\n"
        "def test_a_library_sees_what_a_request_shows_it():\n"
        "    try:\n"
        "        boom()\n"
        "    except NameError:\n"
        "        return\n"
        "    assert False, 'a library must not be seeded'\n",
    )
    assert report.ok, report.outcomes


def test_a_handler_that_raises_while_importing_reports_at_the_test_line(ws):
    """The seeding imports the module first, so it must not become the
    place a broken module is reported: the test's own import line is
    where the agent looks."""
    ws.files.fs.write(
        "/workspace/app/api/broken.py", b"raise RuntimeError('boom at import')\n"
    )
    report = run(ws, "import app.api.broken as broken\n")
    assert report.exit_code == 2
    assert "boom at import" in (report.collection_error or "")
    assert "tests/test_call.py:1: in <module>" in (report.outcomes[0].traceback or "")


def test_the_seeded_contract_survives_process_isolation():
    w = Workspace(
        KvgitProvider.open(None, session="apps-call-seed-iso"),
        python=PythonConfig(isolation="process", host_objects={"db": FakeDb()}),
    )
    enable_apps(w)
    try:
        w.files.fs.write(
            "/workspace/app/api/summary.py",
            b"def get(req):\n    raise HttpError(418, 'teapot')\n",
        )
        report = run(
            w,
            "from app.api.summary import get\n"
            "\n"
            "\n"
            "def test_an_imported_verb_reads_its_modules_globals():\n"
            "    try:\n"
            "        get(Request('GET', '/api/summary', {}, {}, b''))\n"
            "    except HttpError as e:\n"
            "        assert e.status == 418\n"
            "    else:\n"
            "        assert False, 'the handler did not raise'\n",
        )
        assert report.ok, report.outcomes
    finally:
        w.close()


# -- the import spelling ------------------------------------------------------

SUMMARY = (
    "def get(req):\n"
    "    key = req.params.get('key')\n"
    "    if not key:\n"
    "        raise HttpError(400, 'key is required')\n"
    "    return {'row': %s.get(key)}\n"
)

FAKE_AND_REAL = (
    "from unittest.mock import MagicMock\n"
    "\n"
    "\n"
    "def test_the_fake_is_what_the_handler_reads():\n"
    "    db = MagicMock()\n"
    "    db.get.return_value = {'id': 7}\n"
    "    resp = call('summary', params={'key': 'k'}, db=db)\n"
    "    assert resp.json == {'row': {'id': 7}}\n"
    "\n"
    "\n"
    "def test_substituting_nothing_reaches_the_session():\n"
    "    resp = call('summary', params={'key': 'k'})\n"
    "    assert resp.json == {'row': {'id': 'real'}}\n"
)


class KeyedDb:
    """A session object with something to read, so the unfaked call has
    an answer of its own."""

    def get(self, key):
        return {"id": "real"}


@pytest.fixture
def keyed():
    w = Workspace(
        KvgitProvider.open(None, session="apps-call-host"),
        python=PythonConfig(host_objects={"db": KeyedDb()}),
    )
    enable_apps(w)
    try:
        yield w
    finally:
        w.close()


@pytest.mark.parametrize(
    ("head", "read"),
    [
        ("", "db"),
        ("from host import db\n\n\n", "db"),
        ("from host import db as store\n\n\n", "store"),
        ("from host import (\n    db,\n)\n\n\n", "db"),
        ("import host\n\n\n", "host.db"),
        ("import host as h\n\n\n", "h.db"),
    ],
    ids=["bare", "from-host", "aliased", "parenthesized", "import-host", "import-as"],
)
def test_a_fake_reaches_the_handler_whichever_way_it_reads_its_db(keyed, head, read):
    """`from host import db` is the spelling a handler should use, so a
    test's fake has to arrive under it: the import is rewritten in the
    composed copy, not executed, or it would reach past the fake to the
    session's real database."""
    keyed.files.fs.write(
        "/workspace/app/api/summary.py", (head + SUMMARY % read).encode()
    )
    keyed.files.fs.write("/workspace/tests/test_host.py", FAKE_AND_REAL.encode())
    report = run_pytest(keyed)
    assert report.ok, report.outcomes


def test_a_rewritten_import_keeps_every_line_number_behind_it(keyed):
    """The rewrite replaces the import statement's own span and nothing
    else, so a frame still names the line the agent wrote."""
    keyed.files.fs.write(
        "/workspace/app/api/summary.py",
        b"from host import (\n"
        b"    db,\n"
        b")\n"
        b"\n"
        b"\n"
        b"def get(req):\n"
        b"    raise ValueError('the seventh line')\n",
    )
    keyed.files.fs.write(
        "/workspace/tests/test_host.py", b"def test_boom():\n    call('summary')\n"
    )
    report = run_pytest(keyed)
    assert report.failed == 1
    assert "app/api/summary.py:7: in get" in (report.outcomes[0].traceback or "")


def test_a_handler_importing_what_the_session_lacks_is_still_asked_for(keyed):
    """`from host import store` cannot reach anything in a request
    either. The test is told which fake it owes, not handed a stub."""
    keyed.files.fs.write(
        "/workspace/app/api/other.py",
        b"from host import store\n\n\ndef get(req):\n    return {'rows': store.rows()}\n",
    )
    keyed.files.fs.write(
        "/workspace/tests/test_host.py", b"def test_other():\n    call('other')\n"
    )
    report = run_pytest(keyed)
    assert report.failed == 1
    assert "call('other') needs store=" in (report.outcomes[0].message or "")


def test_a_handler_reading_nothing_still_refuses_a_fake(keyed):
    keyed.files.fs.write(
        "/workspace/app/api/plain.py", b"def get(req):\n    return {'ok': True}\n"
    )
    keyed.files.fs.write(
        "/workspace/tests/test_host.py",
        b"from unittest.mock import MagicMock\n"
        b"\n"
        b"\n"
        b"def test_plain():\n"
        b"    call('plain', db=MagicMock())\n",
    )
    report = run_pytest(keyed)
    assert report.failed == 1
    assert "substitutes what the handler does not use" in (
        report.outcomes[0].message or ""
    )


def test_a_directly_imported_handler_reads_the_sessions_own_db(keyed):
    """The seeded contract is not a substitution: an import runs the
    module, and the module's `from host import db` reaches the session
    exactly as it does in a request."""
    keyed.files.fs.write(
        "/workspace/app/api/summary.py",
        b"from host import db\n\n\ndef get(req):\n    return {'row': db.get('k')}\n",
    )
    keyed.files.fs.write(
        "/workspace/tests/test_host.py",
        b"import app.api.summary as summary\n"
        b"\n"
        b"\n"
        b"def test_direct():\n"
        b"    assert summary.get(None) == {'row': {'id': 'real'}}\n",
    )
    report = run_pytest(keyed)
    assert report.ok, report.outcomes


# -- asking for the helper ----------------------------------------------------


def test_a_test_imports_the_helper_the_way_a_handler_imports_its_db(keyed):
    keyed.files.fs.write("/workspace/app/api/summary.py", (SUMMARY % "db").encode())
    keyed.files.fs.write(
        "/workspace/tests/test_host.py",
        b"from host import call\n"
        b"\n"
        b"\n"
        b"def test_imported():\n"
        b"    assert call('summary', params={'key': 'k'}).json == {'row': {'id': 'real'}}\n",
    )
    report = run_pytest(keyed)
    assert report.ok, report.outcomes


def test_the_helper_rides_in_a_list_with_what_the_session_binds(keyed):
    """`from host import db, call` is one statement about two different
    things: the session's database, and the test tier's own helper."""
    keyed.files.fs.write("/workspace/app/api/summary.py", (SUMMARY % "db").encode())
    keyed.files.fs.write(
        "/workspace/tests/test_host.py",
        b"from host import db, call\n"
        b"from unittest.mock import MagicMock\n"
        b"\n"
        b"\n"
        b"def test_the_session_is_reachable_too():\n"
        b"    assert db.get('k') == {'id': 'real'}\n"
        b"    assert call('summary', params={'key': 'k'}).json == {'row': {'id': 'real'}}\n"
        b"\n"
        b"\n"
        b"def test_a_fake_still_substitutes():\n"
        b"    fake = MagicMock()\n"
        b"    fake.get.return_value = {'id': 7}\n"
        b"    resp = call('summary', params={'key': 'k'}, db=fake)\n"
        b"    assert resp.json == {'row': {'id': 7}}\n",
    )
    report = run_pytest(keyed)
    assert report.ok, report.outcomes


def test_the_bare_name_still_reaches_the_helper(keyed):
    """Tests written before the import existed keep running."""
    keyed.files.fs.write("/workspace/app/api/summary.py", (SUMMARY % "db").encode())
    keyed.files.fs.write(
        "/workspace/tests/test_host.py",
        b"def test_bare():\n    assert call('summary').status == 400\n",
    )
    report = run_pytest(keyed)
    assert report.ok, report.outcomes


def test_the_rewritten_import_keeps_the_lines_after_it(keyed):
    keyed.files.fs.write("/workspace/app/api/summary.py", (SUMMARY % "db").encode())
    keyed.files.fs.write(
        "/workspace/tests/test_host.py",
        b"from host import db, call\n"
        b"\n"
        b"\n"
        b"def test_line_five():\n"
        b"    assert call('summary').status == 200\n",
    )
    report = run_pytest(keyed)
    assert report.failed == 1
    assert report.outcomes[0].line == 5
    assert "tests/test_host.py:5: in test_line_five" in (
        report.outcomes[0].traceback or ""
    )


def test_a_handler_importing_the_helper_fails_as_a_request_would(keyed):
    """`call` is the test tier's, and a handler is not run by it in
    production: the import has to fail here the way it fails there."""
    keyed.files.fs.write(
        "/workspace/app/api/bad.py",
        b"from host import call\n\n\ndef get(req):\n    return {'x': call('summary')}\n",
    )
    keyed.files.fs.write(
        "/workspace/tests/test_host.py",
        b"from host import call\n\n\ndef test_bad():\n    call('bad')\n",
    )
    report = run_pytest(keyed)
    assert report.failed == 1
    assert "cannot import name 'call' from 'host'" in (report.outcomes[0].message or "")
