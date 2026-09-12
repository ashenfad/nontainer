"""``from host import db``: the injected objects as an importable module.

A bare injected name is a REPL convenience — right at the top level of
``run_python`` and in an app handler, both of which are the program the
executor runs, and a NameError in a module the program imports. The
import is the contract that holds everywhere, so these tests assert it
at all three call sites, on every rung that runs here.

The dud leg pins ``backend="subprocess"``: the only rung that runs
without a hypervisor, and so the only one a test can reach.
"""

import json
import re

import pytest

from nontainer import PythonConfig, Workspace
from nontainer.apps import enable_apps, request
from nontainer.providers import KvgitProvider


class Db:
    """A live host object: not plain data, so it bridges as a proxy
    under process/kernel isolation and stays a real object in-process."""

    def __init__(self):
        self.rows = ["ann", "bo", "cy"]

    def query(self, limit=10):
        return self.rows[:limit]

    def add(self, name):
        self.rows.append(name)
        return len(self.rows)


def _session(prefix, request_node):
    return f"{prefix}-" + re.sub(r"[^A-Za-z0-9_.-]", "-", request_node.name)[:60]


def _local_ws(session, isolation, **cfg):
    return Workspace(
        KvgitProvider.open(None, session=session),
        python=PythonConfig(isolation=isolation, **cfg),
    )


def _dud_ws(session, **cfg):
    pytest.importorskip("dud")
    from nontainer.executor_dud import DudExecutor

    return Workspace(
        KvgitProvider.open(None, session=session),
        executor=DudExecutor(backend="subprocess"),
        python=PythonConfig(**cfg),
    )


@pytest.fixture(params=["none", "process", "kernel"])
def iso(request):
    """The local rung at each isolation level."""
    return request.param


@pytest.fixture
def db_ws(request, iso):
    """A local-rung workspace with one live host object."""
    w = _local_ws(_session(f"host-{iso}", request.node), iso, host_objects={"db": Db()})
    try:
        yield w
    finally:
        w.close()


# -- the three call sites, both rungs ----------------------------------------

_MODULE_SOURCE = b"""
from host import db


def names(limit):
    return db.query(limit)
"""

_HANDLER_SOURCE = b"""
from app.api._lib import names


def get(req):
    from host import db

    return {"direct": db.query(1), "viamodule": names(2)}
"""


@pytest.fixture(params=["local", "dud"])
def rung_ws(request):
    """One workspace per rung, with apps enabled and a live host object
    — the conformance leg, since the two implementations differ."""
    param = request.param
    session = _session(f"host-{param}", request.node)
    if param == "dud":
        w = _dud_ws(session, host_objects={"db": Db()})
    else:
        w = _local_ws(session, "none", host_objects={"db": Db()})
    rt = enable_apps(w)
    w.files.fs.makedirs("/workspace/app/api", exist_ok=True)
    w.files.fs.write("/workspace/app/api/_lib.py", _MODULE_SOURCE)
    w.files.fs.write("/workspace/app/api/scores.py", _HANDLER_SOURCE)
    try:
        yield w, rt
    finally:
        w.close()


def test_the_import_works_at_all_three_call_sites(rung_ws):
    """Top level, a workspace module, and a handler — the same spelling,
    the same objects, on every rung."""
    ws, rt = rung_ws
    top = ws.run_python("from host import db\nout = db.query(2)")
    assert top.error is None, top.error
    assert top.namespace["out"] == ["ann", "bo"]

    viamodule = ws.run_python(
        "from app.api._lib import names\nout = names(3)",
    )
    assert viamodule.error is None, viamodule.error
    assert viamodule.namespace["out"] == ["ann", "bo", "cy"]

    resp = rt.dispatch(request("GET", "/api/scores"))
    assert resp.status == 200, resp.text
    assert json.loads(resp.text) == {"direct": ["ann"], "viamodule": ["ann", "bo"]}


def test_import_host_then_attribute_is_the_other_spelling(rung_ws):
    ws, _ = rung_ws
    r = ws.run_python("import host\nout = host.db.query(1)")
    assert r.error is None, r.error
    assert r.namespace["out"] == ["ann"]


def test_the_module_is_read_only(rung_ws):
    """A per-exec module nothing may write to: an assignment would be a
    channel from one execution to the next, and there is no such thing."""
    ws, _ = rung_ws
    r = ws.run_python("import host\nhost.db = 1")
    assert r.error is not None
    assert "AttributeError" in r.error


# -- the local rung ----------------------------------------------------------


def test_the_import_works_at_every_isolation_level(db_ws):
    r = db_ws.run_python("from host import db\nout = db.query(2)")
    assert r.error is None, r.error
    assert r.namespace["out"] == ["ann", "bo"]


def test_a_workspace_module_reaches_the_host_object_at_every_isolation(db_ws):
    db_ws.files.fs.makedirs("/workspace/helpers", exist_ok=True)
    db_ws.files.fs.write("/workspace/helpers/_data.py", _MODULE_SOURCE)
    r = db_ws.run_python("from helpers._data import names\nout = names(1)")
    assert r.error is None, r.error
    assert r.namespace["out"] == ["ann"]


def test_the_module_carries_the_live_object_not_a_copy():
    """Under process isolation the module's attribute is the same RPC
    proxy the namespace holds, so a parent-side mutation is visible
    through it and a guest-side call moves the parent's object."""
    db = Db()
    w = _local_ws("host-live", "process", host_objects={"db": db})
    try:
        db.rows.append("dee")
        r = w.run_python("from host import db\nout = db.query(10)")
        assert r.error is None, r.error
        assert r.namespace["out"] == ["ann", "bo", "cy", "dee"]

        r = w.run_python("import host\nn = host.db.add('eve')")
        assert r.error is None, r.error
        assert r.namespace["n"] == 5
        assert db.rows[-1] == "eve"  # the PARENT's instance moved
    finally:
        w.close()


def test_plain_data_host_objects_ride_the_module_too():
    w = _local_ws("host-plain", "process", host_objects={"settings": {"n": 3}})
    try:
        r = w.run_python("from host import settings\nout = settings['n']")
        assert r.error is None, r.error
        assert r.namespace["out"] == 3
    finally:
        w.close()


def test_a_get_handler_sees_the_read_only_cache_through_the_module():
    """The module carries the view's own cache, so a GET reaches the
    read-only one it would reach as a bare name."""
    w = _local_ws("host-rocache", "none")
    rt = enable_apps(w)
    w.cache["scores"] = [1, 2]
    w.files.fs.makedirs("/workspace/app/api", exist_ok=True)
    w.files.fs.write(
        "/workspace/app/api/c.py",
        b"import host\n\n\n"
        b"def get(req):\n"
        b"    return {'read': host.cache['scores']}\n\n\n"
        b"def post(req):\n"
        b"    host.cache['scores'] = [9]\n"
        b"    return {'ok': True}\n",
    )
    try:
        ok = rt.dispatch(request("GET", "/api/c"))
        assert ok.status == 200, ok.text
        assert json.loads(ok.text) == {"read": [1, 2]}

        w.files.fs.write(
            "/workspace/app/api/w.py",
            b"import host\n\n\ndef get(req):\n    host.cache['x'] = 1\n    return {}\n",
        )
        refused = rt.dispatch(request("GET", "/api/w"))
        assert refused.status == 500
        log = w.files.fs.read("/workspace/app/logs/api.log").decode()
        assert "read-only" in log

        wrote = rt.dispatch(request("POST", "/api/c"))
        assert wrote.status == 200, wrote.text
        assert w.cache["scores"] == [9]
    finally:
        w.close()


def test_bare_names_still_work_at_top_level_and_in_a_handler():
    """The bare names are sugar over the import where the code is a
    REPL — the import did not replace them."""
    w = _local_ws("host-bare", "none", host_objects={"db": Db()})
    rt = enable_apps(w)
    w.files.fs.makedirs("/workspace/app/api", exist_ok=True)
    w.files.fs.write(
        "/workspace/app/api/bare.py",
        b"def get(req):\n    return {'names': db.query(1)}\n",
    )
    try:
        top = w.run_python("out = db.query(1)")
        assert top.error is None, top.error
        assert top.namespace["out"] == ["ann"]

        resp = rt.dispatch(request("GET", "/api/bare"))
        assert resp.status == 200, resp.text
        assert json.loads(resp.text) == {"names": ["ann"]}
    finally:
        w.close()


# -- the dud rung ------------------------------------------------------------


def test_dud_carries_the_proxy_plain_data_and_nothing_of_the_ferries():
    """The guest module holds what the config declared — the hostcall
    proxy, plain data, the cache — and not the ws-git/ws-curl handlers
    the dud rung registers for its own terminal verbs, which exist on
    one rung only and so are not part of the import's contract."""
    w = _dud_ws("host-dud-names", host_objects={"db": Db(), "settings": {"n": 3}})
    try:
        r = w.run_python(
            "import host\n"
            "rows = host.db.query(2)\n"
            "n = host.settings['n']\n"
            "names = sorted(k for k in vars(host) if not k.startswith('__'))\n"
        )
        assert r.error is None, r.error
        assert r.namespace["rows"] == ["ann", "bo"]
        assert r.namespace["n"] == 3
        assert r.namespace["names"] == ["cache", "db", "settings"]
    finally:
        w.close()


def test_dud_get_handler_sees_the_read_only_cache_through_the_module():
    """A GET reaches the guest's read-only cache through the module,
    exactly as it does through the bare name."""
    w = _dud_ws("host-dud-rocache")
    rt = enable_apps(w)
    w.cache["scores"] = [1, 2]
    w.files.fs.makedirs("/workspace/app/api", exist_ok=True)
    w.files.fs.write(
        "/workspace/app/api/c.py",
        b"import host\n\n\ndef get(req):\n    return {'read': host.cache['scores']}\n",
    )
    w.files.fs.write(
        "/workspace/app/api/w.py",
        b"import host\n\n\ndef get(req):\n    host.cache['x'] = 1\n    return {}\n",
    )
    w.commit()
    try:
        ok = rt.dispatch(request("GET", "/api/c"))
        assert ok.status == 200, ok.content
        assert json.loads(ok.content) == {"read": [1, 2]}

        refused = rt.dispatch(request("GET", "/api/w"))
        assert refused.status == 500
        assert "x" not in w.cache
    finally:
        w.close()


# -- the reserved name -------------------------------------------------------


def test_a_host_object_named_host_is_refused():
    with pytest.raises(ValueError, match="host"):
        Workspace(
            KvgitProvider.open(None, session="host-collide"),
            python=PythonConfig(host_objects={"host": Db()}),
        )


def test_a_module_grant_named_host_is_refused():
    import math

    from nontainer import ModuleGrant

    with pytest.raises(ValueError, match="host"):
        Workspace(
            KvgitProvider.open(None, session="host-grant"),
            python=PythonConfig(modules=[ModuleGrant(math, name="host")]),
        )


@pytest.mark.parametrize("path", ["/workspace/host.py", "/workspace/host/__init__.py"])
def test_a_workspace_module_named_host_is_refused(path):
    """The module resolves ahead of the workspace tree, so a `host.py`
    would be shadowed with nothing said. Say it instead."""
    w = _local_ws("host-shadow", "none", host_objects={"db": Db()})
    try:
        w.files.fs.makedirs(path.rsplit("/", 1)[0], exist_ok=True)
        w.files.fs.write(path, b"X = 1\n")
        r = w.run_python("out = 1")
        assert r.error is not None
        assert "host" in r.error
    finally:
        w.close()


def test_a_host_object_named_host_is_refused_on_the_dud_rung():
    """The name is reserved on both rungs: the module is the answer to
    `import host` wherever the code runs."""
    with pytest.raises(ValueError, match="host"):
        _dud_ws("host-dud-collide", host_objects={"host": Db()})


def test_a_workspace_module_named_host_is_refused_on_the_dud_rung():
    w = _dud_ws("host-dud-shadow", host_objects={"db": Db()})
    try:
        w.files.fs.write("/workspace/host.py", b"X = 1\n")
        r = w.run_python("out = 1")
        assert r.error is not None
        assert "host" in r.error
    finally:
        w.close()


def test_dud_tracebacks_still_name_the_line_the_agent_wrote():
    """The guest compiles the host prelude and the submitted code as one
    unit, so the numbers the guest reports run ahead of the numbers the
    agent counted. The agent's own coordinates are what comes back."""
    w = _dud_ws("host-dud-lines")
    try:
        r = w.run_python("a = 1\nb = 2\nrows = []\nrows[0]\n")
        assert r.error is not None
        assert "line 4" in r.error, r.error
    finally:
        w.close()


def test_the_module_refuses_patch_object():
    """`patch.object(host, 'db', fake)` is a write to the module, and
    the module takes no writes — a test patches the name its own module
    reads, not the one the executor rebuilds each exec."""
    w = _local_ws("host-patch", "none", host_objects={"db": Db()})
    try:
        r = w.run_python(
            "import host\n"
            "from unittest.mock import patch\n"
            "with patch.object(host, 'db'):\n"
            "    pass\n"
        )
        assert r.error is not None
        assert "AttributeError" in r.error
    finally:
        w.close()
