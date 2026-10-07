"""HostObject(obj, stub=...): a live object called from the sandbox
through a class that runs there, its calls typed by the object's own
annotations, the same on every rung."""

import base64
import dataclasses
import json
import pickle
import subprocess
import sys

import pytest
from host_types import BrokenStub, Grade, Ledger, LedgerStub, Report, Row

from nontainer import HostObject, Profile, PythonConfig, Store
from nontainer import values as nt_values
from nontainer.remote import (
    CALL_LIMIT,
    REFUSED,
    RESULT,
    Host,
    Methods,
    Refused,
    Remote,
    proxied,
)


def config(ledger, isolation="none", **kw):
    return PythonConfig(
        isolation=isolation,
        classes=(Row, Grade, Report),
        host_objects={"ledger": HostObject(ledger, stub=LedgerStub)},
        **kw,
    )


def open_ws(rung, cfg):
    if rung == "dud":
        if sys.version_info < (3, 11):
            pytest.skip("dud needs Python 3.11+")
        pytest.importorskip("dud")
        from nontainer import Workspace
        from nontainer.executor_dud import DudExecutor
        from nontainer.providers import KvgitProvider

        ws = Workspace(
            KvgitProvider.open(None, session="host-stub"),
            python=cfg,
            executor=DudExecutor(backend="subprocess"),
        )
        return ws, None
    store = Store(memory=True)
    isolated = dataclasses.replace(cfg, isolation=rung)
    return store.open("h", profile=Profile(python=isolated)), store


@pytest.fixture(params=["none", "process", "dud"])
def rung(request):
    return request.param


@pytest.fixture
def ledger():
    return Ledger()


@pytest.fixture
def ws(rung, ledger):
    ws, store = open_ws(rung, config(ledger))
    yield ws
    ws.close()
    if store is not None:
        store.close()


def run(ws, code):
    r = ws.run_python(code)
    assert r.error is None, r.error
    return r.stdout.strip()


# -- on every rung ---------------------------------------------------------------------


def test_calls_cross_typed_both_ways(ws, ledger):
    out = run(
        ws,
        "print(ledger.add(Row('ada', 3, Grade.PASS)), ledger.add(Row('bo', 5, Grade.FAIL)))\n"
        "best = ledger.best()\n"
        "print(type(best).__name__, best.total, type(ledger).__name__)\n"
        "print(ledger.tally('a', 'b', x=1))",
    )
    assert out.splitlines() == [
        "1 2",
        "Report 8 LedgerStub",
        "{'names': ['a', 'b'], 'weights': {'x': 1}}",
    ]
    assert ledger.rows == [Row("ada", 3, Grade.PASS), Row("bo", 5, Grade.FAIL)]
    assert type(ledger.rows[0]) is Row  # built as the declared type


def test_an_argument_that_does_not_fit_raises_at_the_call(ws, ledger):
    out = run(
        ws,
        "try:\n"
        "    ledger.add(Row('cy', '9', Grade.PASS))\n"
        "except TypeError as e:\n"
        "    print(e)\n"
        "try:\n"
        "    ledger.tally('a', 2)\n"
        "except TypeError as e:\n"
        "    print(e)",
    )
    assert out.splitlines() == [
        "ledger.add(): at row.score: expected int, got str '9'",
        "ledger.tally(): at names[1]: expected str, got int 2",
    ]
    assert ledger.rows == []  # a refused call never reaches the object


def test_a_subclass_reaches_the_host_as_the_declared_type(ws, ledger):
    """Whatever its hooks do: the host gets values built afresh as the
    types it declared, in-process as elsewhere."""
    out = run(
        ws,
        "class Sneaky(list):\n"
        "    def __deepcopy__(self, memo):\n"
        "        return ['not an int']\n"
        "class Mine(Row):\n"
        "    pass\n"
        "print(ledger.keep(Sneaky([1, 2])))\n"
        "n = ledger.add(Mine('ada', 3, Grade.PASS))",
    )
    assert out == "list"
    assert ledger.kept == [[1, 2]]
    assert type(ledger.kept[0]) is list
    assert type(ledger.rows[0]) is Row


def test_a_dict_with_a_records_fields_is_the_record_on_every_rung(ws, ledger):
    run(ws, "ledger.add({'name': 'ada', 'score': 3, 'grade': 'pass'})")
    assert ledger.rows == [Row("ada", 3, Grade.PASS)]


def test_a_call_the_signature_refuses(ws):
    out = run(
        ws,
        "for call in (lambda: ledger.report(), lambda: ledger.nope()):\n"
        "    try:\n"
        "        call()\n"
        "    except (TypeError, AttributeError) as e:\n"
        "        print(type(e).__name__, e)",
    )
    assert out.splitlines() == [
        "TypeError ledger.report(): missing a required argument: 'value'",
        "AttributeError host object 'ledger' has no method 'nope'",
    ]


def test_a_result_that_does_not_fit_raises(ws):
    out = run(ws, "try:\n    ledger.wrong()\nexcept TypeError as e:\n    print(e)")
    assert out == (
        "ledger.wrong() returned a value that doesn't fit int: "
        "expected int, got str 'not an int'"
    )


def test_the_host_gets_its_own_copy(ws, ledger):
    run(ws, "row = Row('ada', 3, Grade.PASS)\nledger.add(row)\nrow.score = 99")
    assert ledger.rows[0].score == 3


def test_the_stub_can_end_the_run(ws, ledger):
    r = ws.run_python("x = 1\nledger.finish(Report('ada', 3))\nprint('after')")
    assert "Done" in r.error
    assert "line 2" in r.error
    assert "after" not in r.stdout
    assert ledger.reports == [Report("ada", 3)]


def test_the_stub_is_what_host_imports_too(ws):
    assert run(ws, "from host import ledger as same\nprint(type(same).__name__)") == (
        "LedgerStub"
    )


def test_an_unannotated_parameter_takes_plain_data(ws):
    assert run(ws, "print(ledger.loose({'a': [1, 2.5, None]}))") == (
        "{'a': [1, 2.5, None]}"
    )


def test_a_live_object_under_any_crosses_only_in_process(rung, ws):
    r = ws.run_python(
        "class Thing:\n    pass\n"
        "try:\n"
        "    print(type(ledger.loose(Thing())).__name__)\n"
        "except TypeError as e:\n"
        "    print(e)"
    )
    assert r.error is None, r.error
    if rung == "none":
        assert r.stdout.strip() == "Thing"
    else:
        assert r.stdout.strip().startswith(
            "ledger.loose(): argument 1 can't be sent to the host: a Thing"
        )


def test_an_app_handler_holds_the_stub_too(ws, ledger):
    from nontainer.executor import ViewSpec

    r = ws.runtime.exec_python(
        "n = ledger.add(Row('ada', 3, Grade.PASS))\n"
        "try:\n"
        "    ledger.add(Row('bo', 'x', Grade.PASS))\n"
        "except TypeError as e:\n"
        "    said = str(e)\n",
        view=ViewSpec(),
    )
    assert r.error is None, r.error
    assert r.namespace["n"] == 1
    assert (
        r.namespace["said"] == "ledger.add(): at row.score: expected int, got str 'x'"
    )
    assert ledger.rows == [Row("ada", 3, Grade.PASS)]


def test_a_stub_that_cannot_be_built_says_why_on_use(rung):
    cfg = PythonConfig(host_objects={"broken": HostObject(Ledger(), stub=BrokenStub)})
    ws, store = open_ws(rung, cfg)
    try:
        out = run(
            ws,
            "print('runs')\n"
            "try:\n"
            "    broken.add(1)\n"
            "except RuntimeError as e:\n"
            "    print(e)",
        )
    finally:
        ws.close()
        if store is not None:
            store.close()
    assert out.splitlines() == [
        "runs",
        "host object 'broken' is unavailable: its stub couldn't be built "
        "(ValueError: no remote today)",
    ]


def test_code_cannot_reach_past_the_stub(rung, ws):
    if rung == "dud":
        pytest.skip("a VM guest is its own boundary; the host half checks every call")
    for probe in (
        "ledger._remote",
        "getattr(ledger, '_remote')",
        "ledger.__dict__",
        "type(ledger).__init__",
    ):
        r = ws.run_python(f"print({probe})")
        assert "is not accessible" in (r.error or ""), (probe, r.stdout)


# -- the entry -------------------------------------------------------------------------


def test_type_and_stub_are_one_or_the_other():
    with pytest.raises(TypeError, match="not both"):
        HostObject([1], type=list[int], stub=LedgerStub)
    with pytest.raises(TypeError, match="stub is a class"):
        HostObject(Ledger(), stub=LedgerStub(None))


def test_a_signature_no_spec_can_be_built_for_is_refused():
    class Lost:
        def go(self, x: "Nowhere") -> None:  # noqa: F821
            pass

    with pytest.raises(TypeError, match=r"go\(\): its 'x': annotation 'Nowhere'"):
        HostObject(Lost(), stub=LedgerStub)


def test_a_live_type_in_a_signature_is_refused_off_in_process():
    class Client:
        pass

    class Pool:
        def use(self, client: Client) -> None:
            pass

    cfg = PythonConfig(host_objects={"pool": HostObject(Pool(), stub=LedgerStub)})
    with Store(memory=True) as store:
        store.open("a", profile=Profile(python=cfg)).close()  # in-process: fine
        isolated = dataclasses.replace(cfg, isolation="process")
        with pytest.raises(
            ValueError, match=r"use\(\)'s 'client' \(Client\) has a live"
        ):
            store.open("b", profile=Profile(python=isolated))


def test_a_local_stub_is_fine_in_process_and_refused_elsewhere():
    class Local:
        def __init__(self, remote):
            self._remote = remote

        def add(self, row):
            return self._remote.add(row)

    cfg = PythonConfig(
        classes=(Row, Grade),
        host_objects={"ledger": HostObject(Ledger(), stub=Local)},
    )
    with Store(memory=True) as store:
        ws = store.open("a", profile=Profile(python=cfg))
        try:
            assert run(ws, "print(ledger.add(Row('a', 1, Grade.PASS)))") == "1"
        finally:
            ws.close()
        isolated = dataclasses.replace(cfg, isolation="process")
        with pytest.raises(ValueError, match="defined inside a function"):
            store.open("b", profile=Profile(python=isolated))


def test_the_contract_is_the_public_methods():
    class Thing:
        Nested = Row

        def __init__(self):
            self.value = 1

        def call(self, *items: int, **named: str) -> list:
            return [items, named]

        @staticmethod
        def static(x: int) -> int:
            return x

        @property
        def prop(self) -> int:
            return 1

        def _private(self):
            pass

    methods = Methods(Thing())
    assert sorted(methods) == ["call", "static"]
    assert methods["call"].params["items"].annotation is int


def test_the_tool_text_names_a_stubbed_object_as_injected():
    from nontainer.adapters.render import python_description

    with Store(memory=True) as store:
        ws = store.open("h", profile=Profile(python=config(Ledger())))
        try:
            text = python_description(ws)
        finally:
            ws.close()
    assert "injected objects available by name: ledger" in text


# -- the host half ---------------------------------------------------------------------


def _call(host, method, *args, **kwargs):
    blob = nt_values.encode([list(args), kwargs]).to_bytes()
    return host.call(method, blob)


def _refusal(reply):
    assert reply.startswith(REFUSED), reply[:40]
    return json.loads(reply[len(REFUSED) :])


def test_the_host_half_refuses_what_is_not_a_call():
    ledger = Ledger()
    host = Host("ledger", ledger, Methods(ledger))
    row = pickle.dumps([[Row("a", 1, Grade.PASS)], {}])
    for method, blob in [
        ("add", row),  # a pickle is never read
        ("add", b"nt-value/1\n" + b"\0" * 8),
        ("add", nt_values.encode({"not": "a call"}).to_bytes()),
        ("add", "text"),
    ]:
        said = _refusal(host.call(method, blob))
        assert said["error"] == "TypeError"
        assert "malformed call" in said["message"]
    for method in ("_private", "__init__", "nope", 3):
        said = _refusal(host.call(method, b""))  # type: ignore[arg-type]
        assert said["error"] == "AttributeError"
    assert ledger.rows == []


def test_the_host_half_builds_only_the_declared_types():
    np = pytest.importorskip("numpy")
    ledger = Ledger()
    host = Host("ledger", ledger, Methods(ledger))
    said = _refusal(_call(host, "add", np.arange(3)))
    assert said["message"].startswith("ledger.add(): at row: expected Row")
    reply = _call(host, "add", {"name": "a", "score": 1, "grade": "pass"})
    assert reply.startswith(RESULT)
    assert pickle.loads(reply[len(RESULT) :]) == 1
    assert ledger.rows == [Row("a", 1, Grade.PASS)]


def test_the_host_half_refuses_a_live_result():
    class Leaky:
        def get(self):
            return object()

    host = Host("leaky", Leaky(), Methods(Leaky()))
    said = _refusal(_call(host, "get"))
    assert "returned a live object" in said["message"]


# -- the sandbox half ------------------------------------------------------------------


def test_a_call_too_large_to_send_is_refused_where_it_is_made():
    remote = Remote("ledger", lambda method, blob: pytest.fail("sent"))
    with pytest.raises(ValueError, match="too large to send to the host"):
        remote.add(b"x" * (CALL_LIMIT + 1))


def test_a_refusal_comes_back_as_the_error_it_names():
    def send(method, blob):
        return Refused(AttributeError, "nope").reply()

    with pytest.raises(AttributeError, match="nope"):
        Remote("ledger", send).add(1)
    with pytest.raises(RuntimeError, match="can't be read"):
        Remote("ledger", lambda m, b: b"garbage").add(1)


def test_a_stub_that_cannot_be_built_says_so_on_use():
    class Proxy:
        def call(self, method, blob):
            raise AssertionError("not reached")

    stub = proxied(Proxy(), "ledger", "no_such_module", "Stub")
    with pytest.raises(RuntimeError, match="'ledger' is unavailable.*no_such_module"):
        stub.add(1)


# -- a guest without nontainer or the types' module -----------------------------------

_GUEST = r"""
import base64, json, sys
inputs = json.loads(sys.stdin.read())
program = inputs.pop("__program")
replies = {k: base64.b64decode(v) for k, v in inputs.pop("__replies").items()}
sent = []

class Proxy:
    def call(self, method, blob):
        sent.append([method, base64.b64encode(blob).decode()])
        return replies[method]

scope = dict(inputs, ledger=Proxy(), __name__="__main__")
exec(compile(program, "<session>", "exec"), scope)
print(json.dumps({"result": scope["__result"], "sent": sent}))
"""


def test_a_bare_guest_builds_the_stub_and_speaks_the_wire():
    """What dud ships to a VM guest, run by an interpreter that can import
    neither nontainer nor the module the stub and types live in."""
    from nontainer.executor_dud import _typed_program

    ledger = Ledger()
    host = Host("ledger", ledger, Methods(ledger))
    replies = {
        "best": RESULT + pickle.dumps(Report("ada", 3)),
        "add": RESULT + pickle.dumps(1),
        "report": Refused(TypeError, "no thanks").reply(),
    }
    cfg = config(ledger)
    program, inputs = _typed_program({}, cfg)
    inputs["__replies"] = {k: base64.b64encode(v).decode() for k, v in replies.items()}
    inputs["__program"] = program + (
        "import sys\n"
        "__result = [type(ledger).__name__, repr(ledger.best()),\n"
        "            ledger.add(Row('ada', 3, Grade.PASS))]\n"
        "try:\n"
        "    ledger.finish(Report('x', 1))\n"
        "except TypeError as e:\n"
        "    __result.append(str(e))\n"
        "__result.append(hasattr(sys.modules['nontainer'], '__file__'))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-I", "-S", "-c", _GUEST],
        input=json.dumps(inputs),
        capture_output=True,
        text=True,
        cwd="/",
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["result"] == [
        "LedgerStub",
        "Report(best='ada', total=3)",
        1,
        "no thanks",
        False,  # nontainer itself was rebuilt from source
    ]
    # what the guest sent, the host half reads
    (method, blob) = next(s for s in out["sent"] if s[0] == "add")
    assert host.call(method, base64.b64decode(blob)).startswith(RESULT)
    assert ledger.rows == [Row("ada", 3, Grade.PASS)]
