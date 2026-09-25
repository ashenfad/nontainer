"""The response wire: a handler's return is encoded inside the sandbox
it ran in, and only a tuple of primitives crosses back to the host.

Three things are pinned here. Every executor answers a handler with the
same bytes (the parity suite). The host refuses a wire value that is
not one the encoder produces, answering 500 rather than raising. And
the encoder reaches a sandbox with no nontainer installed, as a VM
guest is, by shipping its module's source.
"""

import ast
import json
import re
import subprocess
import sys

import pytest

from nontainer import PythonConfig, Workspace
from nontainer.apps import Response, enable_apps, request
from nontainer.apps import contract as contract_module
from nontainer.apps.contract import (
    HANDLER_CONTRACT,
    WIRE_REFUSED,
    WIRE_RESPONSE,
    HttpError,
    make_request,
    nt__Encoder,
)
from nontainer.apps.dispatch import _TRAILER, _Malformed, _read_wire, _Refused
from nontainer.executor import ViewSpec
from nontainer.providers import KvgitProvider

LOG = "/workspace/app/logs/api.log"

# Non-UTF-8 on purpose: a body that decodes as text would hide an
# executor that round-trips it through str.
BINARY = bytes([0x89, 0x50, 0x4E, 0x47, 0x00, 0xFF, 0xFE, 0x80, 0x0D, 0x0A, 0xC3])

# name -> (handler source, expected (status, content type, headers,
# body)).
CASES = {
    # The regression: a date and a NaN, which the guest's plain
    # json.dumps once refused, so the response never crossed.
    "repro": (
        "import datetime\n"
        "def get(req):\n"
        "    return {'n': float('nan'), 'd': datetime.date(2024, 1, 2)}\n",
        (200, "application/json", {}, b'{"n": null, "d": "2024-01-02"}'),
    ),
    "numpy": (
        "import numpy as np\n"
        "def get(req):\n"
        "    return {'i': np.int64(7), 'f': np.float32(1.5), 'b': np.bool_(True),\n"
        "            'a': np.array([[1.0, np.nan], [np.inf, 4.0]])}\n",
        (
            200,
            "application/json",
            {},
            b'{"i": 7, "f": 1.5, "b": true, "a": [[1.0, null], [null, 4.0]]}',
        ),
    ),
    "decimal": (
        "from decimal import Decimal\n"
        "def get(req):\n"
        "    return {'x': Decimal('1.25'), 'nan': Decimal('NaN')}\n",
        (200, "application/json", {}, b'{"x": 1.25, "nan": null}'),
    ),
    "binary": (
        "def get(req):\n"
        f"    return Response(body={BINARY!r}, headers={{'Content-Type': 'image/png'}})\n",
        (200, "image/png", {"content-type": "image/png"}, BINARY),
    ),
    # A header dict shaped like the dud epilogue's old bytes tag: only
    # the slots the guest marks as bytes are decoded, so it stays a dict.
    "tag_shaped_header": (
        "def get(req):\n    return Response(body='ok', headers={'__nt_b__': 'eA=='})\n",
        (200, "text/plain; charset=utf-8", {"__nt_b__": "eA=="}, b"ok"),
    ),
    "text": (
        "def get(req):\n    return 'héllo'\n",
        (200, "text/plain; charset=utf-8", {}, "héllo".encode()),
    ),
    "none": (
        "def get(req):\n    return None\n",
        (204, "text/plain", {}, b""),
    ),
    "teapot": (
        "def get(req):\n    raise HttpError(418, 'short and stout')\n",
        (418, "application/json", {}, b'{"error": "short and stout"}'),
    ),
    "frame": (
        "import pandas as pd\n"
        "def get(req):\n"
        "    return {'rows': [{'df': pd.DataFrame({'a': [1]})}]}\n",
        (
            500,
            "application/json",
            {},
            json.dumps(
                {
                    "error": "$.rows[0].df: DataFrame is not JSON-encodable "
                    '(return .to_dict("records"), or bytes with a content-type)'
                }
            ).encode(),
        ),
    ),
    "raises": (
        "def get(req):\n    return {'x': 1 / 0}\n",
        (
            500,
            "application/json",
            {},
            json.dumps({"error": "internal error", "log": LOG}).encode(),
        ),
    ),
}


def _apps_ws(rung: str) -> Workspace:
    from nontainer.presets import dataframes

    python = PythonConfig(modules=[dataframes()])
    if rung == "dud":
        pytest.importorskip("dud")
        from nontainer.executor_dud import DudExecutor

        ws = Workspace(
            KvgitProvider.open(None, session="wire-dud"),
            executor=DudExecutor(backend="subprocess"),
            python=python,
        )
    else:
        python = PythonConfig(modules=[dataframes()], isolation=rung)
        ws = Workspace(KvgitProvider.open(None, session=f"wire-{rung}"), python=python)
    ws.files.fs.makedirs("/workspace/app/api", exist_ok=True)
    for name, (source, _) in CASES.items():
        ws.files.fs.write(f"/workspace/app/api/{name}.py", source.encode())
    return ws


RUNGS = ("process", "none", "dud")


@pytest.fixture(scope="module")
def rungs():
    pytest.importorskip("pandas")
    built = {}
    try:
        for rung in RUNGS:
            ws = _apps_ws(rung)
            built[rung] = (ws, enable_apps(ws))
        yield built
    finally:
        for ws, _ in built.values():
            ws.close()


def _log(ws: Workspace) -> str:
    return ws.files.fs.read(LOG).decode()


@pytest.mark.parametrize("case", list(CASES))
def test_every_executor_answers_with_the_same_bytes(rungs, case):
    expected = CASES[case][1]
    seen = {}
    for rung, (_, runtime) in rungs.items():
        r = runtime.dispatch(request("GET", f"/api/{case}"))
        seen[rung] = (r.status, r.content_type, r.headers, r.content)
    assert seen["process"] == expected, seen["process"]
    assert seen["none"] == seen["process"], seen
    assert seen["dud"] == seen["process"], seen


def test_a_refusal_names_the_path_in_every_log(rungs):
    for rung, (ws, runtime) in rungs.items():
        runtime.dispatch(request("GET", "/api/frame"))
        assert "BAD RETURN: $.rows[0].df: DataFrame is not JSON-encodable" in _log(
            ws
        ), rung


def test_a_raise_logs_its_traceback_in_every_log(rungs):
    for rung, (ws, runtime) in rungs.items():
        runtime.dispatch(request("GET", "/api/raises"))
        log = _log(ws)
        assert "[raises:get] ERROR:" in log, rung
        assert "ZeroDivisionError" in log, rung
        assert "line 2" in log, (rung, log)  # the handler's own line


# -- host validation ---------------------------------------------------------


def _ok(**over):
    parts = {
        "tag": WIRE_RESPONSE,
        "status": 200,
        "ctype": "text/plain",
        "headers": {},
        "body": b"",
    }
    parts.update(over)
    return (
        parts["tag"],
        parts["status"],
        parts["ctype"],
        parts["headers"],
        parts["body"],
    )


class _Str(str):
    pass


def test_read_wire_accepts_the_encoders_shapes():
    r = _read_wire(_ok(headers={"x-a": "1"}, body=b"hi"))
    assert (r.status, r.content_type, r.headers, r.content) == (
        200,
        "text/plain",
        {"x-a": "1"},
        b"hi",
    )
    with pytest.raises(_Refused, match="nope"):
        _read_wire((WIRE_REFUSED, "nope"))


@pytest.mark.parametrize(
    "value",
    [
        None,
        [WIRE_RESPONSE, 200, "text/plain", {}, b""],  # a list, not a tuple
        (),
        ("nt-response/2", 200, "text/plain", {}, b""),
        (WIRE_RESPONSE, 200, "text/plain", {}),
        _ok(status=True),
        _ok(status=99),
        _ok(status=600),
        _ok(status=200.0),
        _ok(status="200"),
        _ok(ctype=None),
        _ok(ctype=_Str("text/plain")),
        _ok(headers=[("a", "b")]),
        _ok(headers={"a": 1}),
        _ok(headers={1: "a"}),
        _ok(body="text"),
        _ok(body=bytearray(b"x")),
        _ok(tag=_Str(WIRE_RESPONSE)),
        (WIRE_REFUSED, 1),
        (WIRE_REFUSED, "a", "b"),
    ],
)
def test_read_wire_refuses_anything_else(value):
    with pytest.raises(_Malformed):
        _read_wire(value)


FORGED = {
    # Replacing the encoder is the only way a handler's own code decides
    # what the wire holds: the trailer assigns it after the handler ran.
    "forged": (
        "class nt__Encoder:\n"
        "    @staticmethod\n"
        "    def respond(value):\n"
        "        return ('nt-response/1', 200, 'text/plain', {}, 'not bytes')\n"
        "def get(req):\n"
        "    return 'hi'\n"
    ),
    "unwired": (
        "class nt__Encoder:\n"
        "    @staticmethod\n"
        "    def respond(value):\n"
        "        return None\n"
        "def get(req):\n"
        "    return 'hi'\n"
    ),
    "assigns": ("nt__wire = 'forged'\ndef get(req):\n    return 'fine'\n"),
}


@pytest.mark.parametrize("rung", RUNGS)
def test_a_malformed_wire_is_a_500_never_a_crash(rung):
    pytest.importorskip("pandas")
    ws = _apps_ws(rung)
    try:
        for name, source in FORGED.items():
            ws.files.fs.write(f"/workspace/app/api/{name}.py", source.encode())
        runtime = enable_apps(ws)
        r = runtime.dispatch(request("GET", "/api/forged"))
        assert r.status == 500
        assert json.loads(r.content) == {"error": "internal error", "log": LOG}
        assert "malformed: body must be bytes" in _log(ws)

        r = runtime.dispatch(request("GET", "/api/unwired"))
        assert r.status == 500
        assert "malformed: expected a wire tuple, got NoneType" in _log(ws)

        # An assignment in the handler's module body is overwritten by
        # the trailer, which runs last.
        r = runtime.dispatch(request("GET", "/api/assigns"))
        assert (r.status, r.content) == (200, b"fine")
    finally:
        ws.close()


def test_a_status_http_cannot_carry_is_a_bad_return():
    assert nt__Encoder.respond(Response(status=700)) == (
        WIRE_REFUSED,
        "Response status must be an integer from 100 to 599, got 700",
    )
    assert nt__Encoder.respond(Response(status=True))[0] == WIRE_REFUSED
    assert nt__Encoder.error(99, "x") == (
        WIRE_REFUSED,
        "HttpError status must be an integer from 100 to 599, got 99",
    )
    np = pytest.importorskip("numpy")
    assert nt__Encoder.respond(Response(status=np.int64(201)))[1] == 201


def test_an_http_error_must_carry_an_error_status():
    """HttpError coerces with int(), so a success status could slip in
    as a float or a string; the encoder refuses anything below 400."""
    for raised in (HttpError(201.9, "x"), HttpError("201", "x"), HttpError(302)):
        wire = nt__Encoder.error(raised.status, raised.message)
        assert wire[0] == WIRE_REFUSED, raised
        assert "HttpError status must be 400 to 599" in wire[1]
    lenient = HttpError("404", "gone")
    assert nt__Encoder.error(lenient.status, lenient.message)[:2] == (
        WIRE_RESPONSE,
        404,
    )


def test_the_wire_carries_primitives_only():
    """What an executor has to carry back: exact built-in types, so the
    host never calls a method of the handler's making."""
    np = pytest.importorskip("numpy")
    wire = nt__Encoder.respond(
        Response(status=np.int64(201), body={"n": np.int64(1)}, headers={"X-A": 1})
    )
    assert [type(v) for v in wire] == [str, int, str, dict, bytes]
    assert wire[3] == {"x-a": "1"}


# -- shipping the encoder to a guest with no nontainer ------------------------


def _module_imports(tree: ast.Module) -> tuple[set[str], set[str], int]:
    """(module-level import roots, function-level import roots, relative
    imports anywhere)."""
    top, nested, relative = set(), set(), 0
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level:
            relative += 1
            continue
        if isinstance(node, ast.Import):
            roots = {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            roots = {node.module.split(".")[0]}
        else:
            continue
        (top if node in tree.body else nested).update(roots)
    return top, nested, relative


def test_the_contract_module_imports_nothing_from_nontainer():
    """Its source is what a VM guest builds the encoder and the contract
    classes from, and that guest has no nontainer to import: an import
    of this package, relative or absolute, breaks every handler there.
    The standard library at module level; numpy and pandas only inside
    the functions that encode their values."""
    source = open(contract_module.__file__).read()
    top, nested, relative = _module_imports(ast.parse(source))
    assert relative == 0
    stdlib = sys.stdlib_module_names | {"__future__"}
    assert top <= stdlib, top - stdlib
    assert nested <= stdlib | {"numpy", "pandas"}, nested - stdlib


def test_every_class_dispatch_ships_lives_in_the_contract_module():
    """The guest bootstrap ships one source file per defining module, so
    a contract class defined elsewhere drags that module (and its
    imports) into a guest that cannot resolve them."""
    from nontainer.apps.dispatch import AppRuntime

    ws = Workspace(KvgitProvider.open(None, session="wire-ship"))
    try:
        shipped = AppRuntime(ws)._contract
    finally:
        ws.close()
    assert nt__Encoder in shipped
    assert {c.__module__ for c in shipped} == {contract_module.__name__}


# The guest half of dud's runner, reduced to what a view exec needs:
# bind the inputs, run the program, and carry back each public binding
# that JSON can encode, dropping the rest without a word.
_GUEST_RUNNER = """
import json, sys
g = {"__name__": "__main__"}
g.update(json.loads(sys.stdin.read()))
exec(compile(g.pop("__program"), "<session>", "exec"), g)
out = {}
for k, v in g.items():
    if k.startswith("_"):
        continue
    try:
        json.dumps(v)
    except (TypeError, ValueError):
        continue
    out[k] = v
out["__synthesized"] = not hasattr(sys.modules["nontainer.apps.contract"], "__file__")
print(json.dumps(out))
"""


def _run_in_bare_guest(handler: str, verb: str, req) -> dict:
    """Run a handler as dud ships it to a VM guest, in an interpreter
    that cannot import nontainer (``-I -S``: no site-packages, no cwd),
    and return the rebuilt outputs."""
    from nontainer.executor_dud import (
        _HOST_NAMES_INPUT,
        _rebuild_view_value,
        _view_program,
    )

    extra = (*HANDLER_CONTRACT, nt__Encoder)
    program, inputs, _ = _view_program(
        handler + _TRAILER.format(verb=verb),
        {"nt__req": req},
        ViewSpec(extra_classes=extra),
    )
    inputs[_HOST_NAMES_INPUT] = []
    inputs["__program"] = program
    proc = subprocess.run(
        [sys.executable, "-I", "-S", "-c", _GUEST_RUNNER],
        input=json.dumps(inputs),
        capture_output=True,
        text=True,
        cwd="/",
    )
    assert proc.returncode == 0, proc.stderr
    outputs = json.loads(proc.stdout)
    assert outputs.pop("__synthesized") is True  # built from source
    return {k: _rebuild_view_value(v, extra) for k, v in outputs.items()}


@pytest.mark.parametrize(
    "case", ["repro", "decimal", "binary", "text", "none", "teapot"]
)
def test_the_encoder_runs_in_a_guest_without_nontainer(case):
    source, expected = CASES[case]
    out = _run_in_bare_guest(source, "get", make_request("GET", f"/api/{case}"))
    r = _read_wire(out["nt__wire"])
    assert (r.status, r.content_type, r.headers, r.content) == expected


def test_a_refusal_crosses_from_a_guest_without_nontainer():
    out = _run_in_bare_guest(
        "def post(req):\n    return {'s': {1}, 'who': req.require('who')}\n",
        "post",
        make_request("POST", "/api/x", body=b'{"who": "amy"}'),
    )
    with pytest.raises(_Refused, match=re.escape("$.s: set is not JSON-encodable")):
        _read_wire(out["nt__wire"])


def test_http_error_body_matches_dispatchs_own_errors():
    """A handler's HttpError and an error dispatch raises itself read
    the same on the wire, so a client parses one shape."""
    from nontainer.apps.dispatch import _error_response

    wire = nt__Encoder.error(404, "gone")
    assert wire[4] == _error_response(404, "gone").content
    assert HttpError(404, "gone").status == wire[1]


def test_dud_rebuilds_a_tuple_and_leaves_forged_tags_alone():
    """A guest binds whatever it likes, including dicts shaped like the
    epilogue's tags; one that does not decode stays a dict for the
    host's own validation to refuse, and never raises out of the result
    mapping."""
    pytest.importorskip("dud")
    import base64

    from nontainer.executor_dud import _rebuild_view_value

    body = base64.b64encode(BINARY).decode()
    tagged = {"__nt_tuple__": [WIRE_RESPONSE, 200, "x", {}, body], "bin": [4]}
    assert _rebuild_view_value(tagged, ()) == (WIRE_RESPONSE, 200, "x", {}, BINARY)
    # Only the named slots decode: a dict that looks like a tag is data.
    lookalike = {"__nt_b__": body}
    value = {"__nt_tuple__": [lookalike, body], "bin": [1]}
    assert _rebuild_view_value(value, ()) == (lookalike, BINARY)
    # A forged bin list never raises: bad indexes, non-text, bad base64.
    for forged_bin in ([9], [-9], ["x"], "0", None):
        value = {"__nt_tuple__": [body], "bin": forged_bin}
        assert _rebuild_view_value(value, ()) == (body,)
    for forged in (5, "not base64!", None):
        value = {"__nt_tuple__": [forged], "bin": [0]}
        assert _rebuild_view_value(value, ()) == (forged,)
    with pytest.raises(_Malformed):
        _read_wire(
            _rebuild_view_value({"__nt_tuple__": [WIRE_RESPONSE], "bin": []}, ())
        )


@pytest.mark.parametrize("rung", RUNGS)
def test_handler_tuples_cross_as_tuples(rung):
    """A view exec's tuple binding comes back a tuple on every rung, bytes
    elements intact: the shape the response wire relies on."""
    if rung == "dud":
        pytest.importorskip("dud")
        from nontainer.executor_dud import DudExecutor

        ws = Workspace(
            KvgitProvider.open(None, session="wire-tuple"),
            executor=DudExecutor(backend="subprocess"),
        )
    else:
        ws = Workspace(
            KvgitProvider.open(None, session="wire-tuple"),
            python=PythonConfig(isolation=rung),
        )
    try:
        r = ws.runtime.exec_python(
            f"t = ('a', 1, {{'k': 'v'}}, {BINARY!r})", view=ViewSpec()
        )
        assert r.error is None, r.error
        assert r.namespace["t"] == ("a", 1, {"k": "v"}, BINARY)
        assert type(r.namespace["t"]) is tuple
    finally:
        ws.close()
