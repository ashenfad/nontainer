"""test_app's ``bind``: a run's handlers read one host object as another.

A browser check drives the app the way a person does, so it wrote into
the live store every published version serves over, and every live run
of an agent ended with a manual DELETE. ``bind={"db": "testdb"}`` hands
the run's handlers the test store under the name they read; the
handler's code is the code that publishes, and only what it is handed
changes. ``ws-curl --bind db=testdb`` is the same for one request.
Nothing is bound unless asked: which names to bind is the embedder's to
teach.
"""

import pytest

from nontainer import PythonConfig, Workspace
from nontainer.adapters.render import test_app_description as describe_test_app
from nontainer.apps import enable_apps, render_test_app, request
from nontainer.apps.testapp import check_bind, coerce_bind
from nontainer.executor import ViewSpec
from nontainer.executor_dud import _HOST_PRELUDE, _view_program
from nontainer.providers import KvgitProvider


class Rows:
    """A store with one method a handler calls, and a list to read."""

    def __init__(self):
        self.rows = []

    def add(self, value):
        self.rows.append(value)
        return len(self.rows)


HANDLER = """
from host import db as imported

def post(req):
    db.add(req.require("name"))
    imported.add("via import")
    return {"ok": True}
"""


@pytest.fixture
def stores():
    return {"db": Rows(), "testdb": Rows()}


def _ws(stores, config=None):
    ws = Workspace(
        KvgitProvider.open(None, session="b"),
        python=PythonConfig(host_objects=stores),
    )
    rt = enable_apps(ws, config)
    ws.files.fs.makedirs("/workspace/app/api", exist_ok=True)
    ws.files.fs.write("/workspace/app/api/names.py", HANDLER.encode())
    return ws, rt


def test_a_bound_request_hands_the_handler_the_other_object(stores):
    """Under the bare name and through the import alike: both read the
    one binding, so a handler cannot write half to each store."""
    ws, rt = _ws(stores)
    try:
        post = request("POST", "/api/names", body=b'{"name": "amy"}')
        assert rt.dispatch(post, bind={"db": "testdb"}).status == 200
        assert stores["testdb"].rows == ["amy", "via import"]
        assert stores["db"].rows == []

        # the binding is the request's: the next one reads the real db
        assert rt.dispatch(post).status == 200
        assert stores["db"].rows == ["amy", "via import"]
    finally:
        ws.close()


def test_a_swap_swaps(stores):
    """Every right-hand side is read from the names as they were, so two
    bindings that mention each other's names exchange the objects."""
    ws, rt = _ws(stores)
    try:
        ws.files.fs.write(
            "/workspace/app/api/both.py",
            b"def post(req):\n    db.add('as db')\n    testdb.add('as testdb')\n"
            b"    return {}\n",
        )
        rt.dispatch(request("POST", "/api/both"), bind={"db": "testdb", "testdb": "db"})
        assert stores["testdb"].rows == ["as db"]
        assert stores["db"].rows == ["as testdb"]
        full, _, _ = _view_program(
            "", {}, ViewSpec(bind=(("db", "testdb"), ("testdb", "db")))
        )
        ns = {"db": "D", "testdb": "T"}
        exec(full[full.index("db, testdb, =") :].split("\n", 1)[0], ns)
        assert (ns["db"], ns["testdb"]) == ("T", "D")
    finally:
        ws.close()


def test_dispatch_refuses_a_bad_binding_itself(stores):
    """A caller of the public dispatch gets the same refusal test_app
    and ws-curl give, before the handler runs."""
    ws, rt = _ws(stores)
    try:
        post = request("POST", "/api/names", body=b'{"name": "amy"}')
        with pytest.raises(ValueError, match="'nope' is not a host object"):
            rt.dispatch(post, bind={"db": "nope"})
        assert stores["db"].rows == [] and stores["testdb"].rows == []
    finally:
        ws.close()


def test_nothing_is_bound_unless_asked(stores):
    ws, rt = _ws(stores)
    try:
        assert check_bind(rt, None) == ()
        assert check_bind(rt, {}) == ()
        assert check_bind(rt, {"testdb": "db"}) == (("testdb", "db"),)
    finally:
        ws.close()


def test_a_name_the_session_does_not_bind_is_refused_with_the_names_it_does(stores):
    ws, rt = _ws(stores)
    try:
        with pytest.raises(ValueError, match="'cache_db' is not a host object") as e:
            check_bind(rt, {"db": "cache_db"})
        assert "it binds: db, testdb" in str(e.value)
        with pytest.raises(ValueError, match="binds a name to itself"):
            check_bind(rt, {"db": "db"})
    finally:
        ws.close()


def test_a_tools_bind_argument_may_arrive_as_a_json_string():
    assert coerce_bind(None) is None
    assert coerce_bind("") is None
    assert coerce_bind('{"db": "testdb"}') == {"db": "testdb"}
    assert coerce_bind({}) == {}
    with pytest.raises(ValueError, match="bind must be an object"):
        coerce_bind("[1]")


def test_the_guest_program_rebinds_before_the_host_module_is_built():
    """On the dud rung the host module is built from the guest's
    globals, so the rebinding line has to come first for the import to
    see it."""
    full, _, _ = _view_program("x = 1\n", {}, ViewSpec(bind=(("db", "testdb"),)))
    assert full.index("db, = globals()['testdb'],") < full.index(_HOST_PRELUDE)
    with pytest.raises(ValueError, match="two host object names"):
        _view_program("", {}, ViewSpec(bind=(("db; import os", "testdb"),)))


def test_the_description_names_the_parameter():
    assert 'bind={"name": "other"}' in describe_test_app(root="/workspace")


def test_ws_curl_binds_one_request_and_says_so(stores):
    ws, rt = _ws(stores)
    try:
        r = ws.terminal(
            'ws-curl --bind db=testdb -X POST --json \'{"name": "amy"}\' '
            "$APP_ORIGIN/api/names"
        )
        assert r.exit_code == 0, r.stderr
        assert "handlers read db as testdb" in r.stdout + r.stderr
        assert stores["testdb"].rows == ["amy", "via import"]
        assert stores["db"].rows == []

        # unbound, as ever: what the session binds
        r = ws.terminal(
            'ws-curl -X POST --json \'{"name": "bo"}\' $APP_ORIGIN/api/names'
        )
        assert r.exit_code == 0 and "handlers read" not in r.stdout + r.stderr
        assert stores["db"].rows == ["bo", "via import"]
    finally:
        ws.close()


def test_ws_curl_refuses_a_bind_it_cannot_honour(stores):
    ws, rt = _ws(stores)
    try:
        r = ws.terminal("ws-curl --bind db $APP_ORIGIN/api/names")
        assert r.exit_code == 2 and "--bind takes NAME=OTHER" in r.stderr
        r = ws.terminal("ws-curl --bind db=nope -X POST $APP_ORIGIN/api/names")
        assert r.exit_code == 2 and "it binds: db, testdb" in r.stderr
        assert stores["db"].rows == [] and stores["testdb"].rows == []
    finally:
        ws.close()


PAGE = """<!doctype html><html><body><p id="out">...</p><script>
fetch('api/names', {method: 'POST', headers: {'content-type': 'application/json'},
  body: JSON.stringify({name: 'from the page'})})
  .then(r => r.json()).then(j => { document.getElementById('out').textContent = j.ok; });
</script></body></html>"""


@pytest.fixture
def page_ws(chromium_available, stores):
    ws, rt = _ws(stores)
    ws.files.fs.write("/workspace/app/index.html", PAGE.encode())
    ws.commit()
    yield ws, rt
    ws.close()


def test_a_bound_browser_run_writes_to_the_other_store_and_says_so(page_ws, stores):
    ws, rt = page_ws
    result = rt.test_app(
        [{"assert": "document.getElementById('out').textContent === 'true'"}],
        bind={"db": "testdb"},
    )
    assert result.ok, render_test_app(result)
    assert stores["testdb"].rows == ["from the page", "via import"]
    assert stores["db"].rows == []
    assert result.bound == (("db", "testdb"),)
    assert "this run's handlers read db as testdb" in render_test_app(result)


def test_an_unbound_run_uses_what_the_session_binds(page_ws, stores):
    ws, rt = page_ws
    result = rt.test_app(
        [{"assert": "document.getElementById('out').textContent === 'true'"}]
    )
    assert result.ok, render_test_app(result)
    assert stores["db"].rows == ["from the page", "via import"]
    assert stores["testdb"].rows == []
    assert "handlers read" not in render_test_app(result)
