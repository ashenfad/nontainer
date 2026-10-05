"""test_app's ``bind``: a run's handlers read one host object as another.

A browser check drives the app the way a person does, so it wrote into
the live store every published version serves over, and every live run
of an agent ended with a manual DELETE. ``bind={"db": "testdb"}`` hands
the run's handlers the test store under the name they read; the
handler's code is the code that publishes, and only what it is handed
changes. ``AppsConfig.test_bind`` makes that the default.
"""

import pytest

from nontainer import PythonConfig, Workspace
from nontainer.adapters.render import test_app_description as describe_test_app
from nontainer.apps import AppsConfig, enable_apps, render_test_app, request
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


def test_check_bind_takes_the_default_and_the_opt_out(stores):
    ws, rt = _ws(stores, AppsConfig(test_bind={"db": "testdb"}))
    try:
        assert check_bind(rt, None) == (("db", "testdb"),)  # the default
        assert check_bind(rt, {}) == ()  # the real objects
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


def test_the_config_default_is_two_names_per_entry():
    with pytest.raises(ValueError, match="test_bind maps"):
        AppsConfig(test_bind={"db": "db"})
    with pytest.raises(ValueError, match="test_bind maps"):
        AppsConfig(test_bind={"db": "test db"})


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
    assert full.index("db = globals()['testdb']") < full.index(_HOST_PRELUDE)
    with pytest.raises(ValueError, match="two host object names"):
        _view_program("", {}, ViewSpec(bind=(("db; import os", "testdb"),)))


def test_the_description_says_what_a_run_binds():
    plain = describe_test_app(root="/workspace")
    assert 'bind={"name": "other"}' in plain
    defaulted = describe_test_app(
        root="/workspace", config=AppsConfig(test_bind={"db": "testdb"})
    )
    assert "`db` as `testdb`" in defaulted
    assert "bind={} runs against the real objects" in defaulted


PAGE = """<!doctype html><html><body><p id="out">...</p><script>
fetch('api/names', {method: 'POST', headers: {'content-type': 'application/json'},
  body: JSON.stringify({name: 'from the page'})})
  .then(r => r.json()).then(j => { document.getElementById('out').textContent = j.ok; });
</script></body></html>"""


@pytest.fixture
def page_ws(chromium_available, stores):
    ws, rt = _ws(stores, AppsConfig(test_bind={"db": "testdb"}))
    ws.files.fs.write("/workspace/app/index.html", PAGE.encode())
    ws.commit()
    yield ws, rt
    ws.close()


def test_a_browser_run_writes_to_the_test_store_and_says_so(page_ws, stores):
    ws, rt = page_ws
    result = rt.test_app(
        [{"assert": "document.getElementById('out').textContent === 'true'"}]
    )
    assert result.ok, render_test_app(result)
    assert stores["testdb"].rows == ["from the page", "via import"]
    assert stores["db"].rows == []
    assert result.bound == (("db", "testdb"),)
    assert "this run's handlers read db as testdb" in render_test_app(result)


def test_bind_empty_runs_the_page_against_the_real_objects(page_ws, stores):
    ws, rt = page_ws
    result = rt.test_app(
        [{"assert": "document.getElementById('out').textContent === 'true'"}], bind={}
    )
    assert result.ok, render_test_app(result)
    assert stores["db"].rows == ["from the page", "via import"]
    assert stores["testdb"].rows == []
    assert "handlers read" not in render_test_app(result)
