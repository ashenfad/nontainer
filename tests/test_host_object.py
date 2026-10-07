"""HostObject and PythonConfig.classes: data of a declared type sent into
the sandbox by value, and classes code can name, on every rung."""

import dataclasses
import json
import subprocess
import sys

import pytest
from host_types import Card, Grade, Report, Row

from nontainer import HostObject, Profile, PythonConfig, Store
from nontainer.adapters.render import python_description

ROWS = [Row("ada", 3, Grade.PASS), Row("bo", 1, Grade.FAIL)]

CODE = (
    "best = max(rows, key=lambda r: r.score)\n"
    "rows.append(Row('cy', 9, Grade.PASS))\n"
    "report = Report(best=best.name, total=sum(r.score for r in rows))\n"
    "from host import Report as Imported\n"
    "print(report, type(rows[0]).__name__, Imported is Report, len(rows))\n"
)


def config(isolation="none", **kw):
    return PythonConfig(
        isolation=isolation,
        classes=(Report, Row, Grade),
        host_objects={"rows": HostObject(list(ROWS), type=list[Row]), "limit": 5},
        **kw,
    )


def open_ws(rung, cfg_for):
    if rung == "dud":
        if sys.version_info < (3, 11):
            pytest.skip("dud needs Python 3.11+")
        pytest.importorskip("dud")
        from nontainer import Workspace
        from nontainer.executor_dud import DudExecutor
        from nontainer.providers import KvgitProvider

        ws = Workspace(
            KvgitProvider.open(None, session="host-object"),
            python=cfg_for("none"),
            executor=DudExecutor(backend="subprocess"),
        )
        return ws, None
    store = Store(memory=True)
    return store.open("h", profile=Profile(python=cfg_for(rung))), store


@pytest.fixture(params=["none", "process", "dud"])
def ws(request):
    ws, store = open_ws(request.param, config)
    yield ws
    ws.close()
    if store is not None:
        store.close()


# -- on every rung ---------------------------------------------------------------------


def test_typed_data_and_classes_in_a_run(ws):
    r = ws.run_python(CODE)
    assert r.error is None, r.error
    assert r.stdout.strip() == "Report(best='ada', total=13) Row True 3"
    # what went in is not what the run made
    assert not {"rows", "Row", "Report", "Grade"} & set(r.namespace)


def test_each_run_gets_its_own_copy(ws):
    assert ws.run_python("rows.clear()\nprint(len(rows))").stdout.strip() == "0"
    assert ws.run_python("print(len(rows))").stdout.strip() == "2"
    entry = ws.runtime.python_config.host_objects["rows"]
    assert entry.obj == ROWS  # the host's own, untouched


def test_a_plain_host_object_beside_them_works_as_before(ws):
    assert ws.run_python("print(limit + 1)").stdout.strip() == "6"


def test_an_error_line_is_the_code_own(ws):
    r = ws.run_python("x = 1\n1 / 0\n")
    assert "ZeroDivisionError" in r.error
    assert "line 2" in r.error


def test_typed_data_needs_no_classes_listed_to_be_read():
    """Its fields, methods and properties are open to code whether or not
    its classes are among ``classes``, which are for building values."""

    def bare(isolation):
        return PythonConfig(
            isolation=isolation,
            host_objects={"cards": HostObject([Card("hi")], type=list[Card])},
        )

    for rung in ("none", "process", "dud"):
        try:
            ws, store = open_ws(rung, bare)
        except pytest.skip.Exception:
            continue
        try:
            r = ws.run_python("print(cards[0].front, cards[0].shout(), cards[0].size)")
            assert r.stdout.strip() == "hi HI 2", (rung, r.error)
        finally:
            ws.close()
            if store is not None:
                store.close()


def test_an_array_run_cannot_reach_the_host_array():
    np = pytest.importorskip("numpy")
    from nontainer.presets import dataframes

    array = np.arange(3)
    cfg = PythonConfig(
        modules=[dataframes()],
        host_objects={"values": HostObject(array, type=np.ndarray)},
    )
    with Store(memory=True) as store:
        ws = store.open("h", profile=Profile(python=cfg))
        try:
            r = ws.run_python(
                "values.flags.writeable = True\nvalues[0] = 9\n"
                "if values.base is not None:\n    values.base[1] = 9\n"
                "print(values.tolist())"
            )
            assert r.error is None, r.error
        finally:
            ws.close()
    assert array.tolist() == [0, 1, 2]


# -- the entry -------------------------------------------------------------------------


def test_the_value_is_checked_against_its_type():
    with pytest.raises(TypeError, match=r"at \[0\].score: expected int, got str"):
        HostObject([Row("ada", "3", Grade.PASS)], type=list[Row])


def test_a_live_object_under_any_is_refused():
    from typing import Any

    class Client:
        pass

    with pytest.raises(TypeError, match=r"allows anything \(a live Client\)"):
        HostObject(Client(), type=Any)
    with pytest.raises(TypeError, match=r"at \['a'\]\[1\]: a live Client"):
        HostObject({"a": [1, Client()]}, type=dict[str, Any])
    assert HostObject({"a": [1, "x"]}, type=dict[str, Any]).by_value


def test_a_type_with_a_live_part_is_refused():
    with pytest.raises(TypeError, match="has a live part"):
        HostObject(len, type=type(len))


def test_a_type_no_spec_can_be_built_for_is_refused():
    @dataclasses.dataclass
    class Lost:
        x: "Nowhere"  # noqa: F821

    with pytest.raises(TypeError, match="can't be resolved"):
        HostObject(None, type=Lost)


def test_an_entry_without_a_type_is_the_object_itself():
    class Client:
        def get(self) -> str:
            return "got"

    for isolation in ("none", "process"):
        cfg = PythonConfig(
            isolation=isolation, host_objects={"client": HostObject(Client())}
        )
        with Store(memory=True) as store:
            ws = store.open("h", profile=Profile(python=cfg))
            try:
                assert ws.run_python("print(client.get())").stdout.strip() == "got"
            finally:
                ws.close()


# -- classes ---------------------------------------------------------------------------


def test_classes_are_classes_with_names_of_their_own():
    with pytest.raises(TypeError, match="holds classes"):
        PythonConfig(classes=(Row("a", 1, Grade.PASS),))  # type: ignore[arg-type]
    other = type("Row", (), {})
    with pytest.raises(ValueError, match="two classes named 'Row'"):
        PythonConfig(classes=(Row, other))
    with pytest.raises(ValueError, match="class named 'Row'"):
        PythonConfig(classes=(Row,), host_objects={"Row": 1})


def test_a_local_class_is_fine_in_process_and_refused_elsewhere():
    @dataclasses.dataclass
    class Local:
        x: int

    with Store(memory=True) as store:
        cfg = PythonConfig(classes=(Local,))
        ws = store.open("a", profile=Profile(python=cfg))
        try:
            assert ws.run_python("print(Local(3).x)").stdout.strip() == "3"
        finally:
            ws.close()
        isolated = dataclasses.replace(cfg, isolation="process")
        with pytest.raises(ValueError, match="defined inside a function"):
            store.open("b", profile=Profile(python=isolated))
        held = PythonConfig(
            isolation="process",
            host_objects={"x": HostObject([Local(1)], type=list[Local])},
        )
        with pytest.raises(ValueError, match="defined inside a function"):
            store.open("c", profile=Profile(python=held))


def test_the_tool_text_says_what_is_data_and_what_is_a_class():
    with Store(memory=True) as store:
        ws = store.open("h", profile=Profile(python=config()))
        try:
            text = python_description(ws)
        finally:
            ws.close()
    assert "injected objects available by name: limit" in text
    assert "data available by name: rows. Each run gets its own copy" in text
    assert "classes available by name: Grade, Report, Row" in text


# -- a guest without nontainer or the types' module -----------------------------------

_GUEST = r"""
import json, sys
inputs = json.loads(sys.stdin.read())
program = inputs.pop("__program")
scope = dict(inputs)
scope["__name__"] = "__main__"
exec(compile(program, "<session>", "exec"), scope)
print(json.dumps(scope["__result"]))
"""


def test_a_bare_guest_rebuilds_the_types_and_unpickles_the_data():
    """What dud ships to a VM guest, run by an interpreter that can import
    neither nontainer nor the module the types live in."""
    from nontainer.executor_dud import _typed_program

    cfg = config()
    program, inputs = _typed_program({"rows": ROWS}, cfg)
    inputs["__program"] = program + (
        "import sys\n"
        "__result = [type(rows[0]).__name__, rows[1].grade.value,\n"
        "            Report('x', 1).total, Grade.PASS.value,\n"
        "            hasattr(sys.modules['host_types'], '__file__')]\n"
    )
    proc = subprocess.run(
        [sys.executable, "-I", "-S", "-c", _GUEST],
        input=json.dumps(inputs),
        capture_output=True,
        text=True,
        cwd="/",
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == ["Row", "fail", 1, "pass", False]
