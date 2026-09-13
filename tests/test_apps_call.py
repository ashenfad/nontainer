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
