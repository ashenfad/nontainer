"""Classes load_specs builds are shapes: no module holds them, so they
cross to a worker process or a dud machine as the data they were built
from, and are built again there, the same classes on every rung."""

import dataclasses
import enum
import pickle
import subprocess
import sys

import pytest
from host_types import Row, Unhashable

from nontainer import HostObject, Profile, PythonConfig, Store, values


class Tone(enum.Flag):
    LOUD = 1
    SOFT = 2


@dataclasses.dataclass
class Point2D:
    """A point in the plane."""

    x: float
    y: float
    tone: Tone = Tone.LOUD


def loaded(module="helper"):
    data = values.export_specs({"pts": values.Spec.of(list[Point2D])})
    spec = values.load_specs(data, module=module)["pts"]
    by_name = {t.__name__: t for t in spec.types}
    return spec, by_name["Point2D"], by_name["Tone"]


def test_the_same_data_loads_the_same_classes():
    spec, P, T = loaded()
    again, P2, T2 = loaded()
    assert (P2, T2) == (P, T)
    assert values.is_shape(P) and values.is_shape(T)
    assert not values.is_shape(Point2D)
    assert loaded(module="elsewhere")[1] is not P


def test_shapes_no_one_uses_are_let_go():
    import gc

    from nontainer.values import _SHAPES

    spec, P, T = loaded(module="let_go")
    key = next(k for k in list(_SHAPES.keys()) if k[1] == "let_go")
    assert loaded(module="let_go")[1] is P  # the same, while in use
    del spec, P, T
    gc.collect()
    assert key not in _SHAPES


def test_a_class_with_an_unhashable_metaclass_isnt_a_shape():
    assert not values.is_shape(Unhashable)
    assert not values.is_shape(values._ShapeRecord)  # a base, built from nothing


def test_a_shape_pickles_as_its_data():
    spec, P, T = loaded()
    for obj in (P, T, P(1.0, 2.0, T.LOUD | T.SOFT), [P(0.0, 0.0)]):
        assert pickle.loads(pickle.dumps(obj)) == obj
    assert pickle.loads(pickle.dumps(P)) is P


def test_a_shape_unpickles_where_it_was_never_loaded():
    spec, P, T = loaded()
    blob = pickle.dumps([P(1.0, 2.0, T.SOFT)])
    probe = (
        "import pickle, sys\n"
        "from nontainer import values\n"
        "(p,) = pickle.loads(sys.stdin.buffer.read())\n"
        "print(type(p).__module__, type(p).__name__, p, values.is_shape(type(p)))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe], input=blob, capture_output=True, check=True
    )
    assert out.stdout.decode().split(" ", 2)[:2] == ["helper", "Point2D"]
    assert out.stdout.decode().rstrip().endswith("True")


def open_ws(rung, cfg):
    if rung == "dud":
        if sys.version_info < (3, 11):
            pytest.skip("dud needs Python 3.11+")
        pytest.importorskip("dud")
        from nontainer.executor_dud import DudExecutor

        profile = Profile(
            python=cfg, executor_factory=lambda: DudExecutor(backend="subprocess")
        )
    else:
        profile = Profile(python=dataclasses.replace(cfg, isolation=rung))
    store = Store(memory=True)
    return store.open("s", profile=profile), store


@pytest.fixture(params=["none", "process", "dud"])
def rung(request):
    return request.param


def test_shapes_reach_code_on_every_rung(rung):
    spec, P, T = loaded()
    cfg = PythonConfig(
        host_objects={"pts": HostObject([P(1.0, 2.0)], type=spec)}, classes=(P, T)
    )
    ws, store = open_ws(rung, cfg)
    try:
        r = ws.run_python(
            "from host import Point2D as Imported\n"
            "made = Point2D(3.0, 4.0, Tone.LOUD | Tone.SOFT)\n"
            "print(pts[0].x, made.tone.value, Imported is Point2D,\n"
            "      type(pts[0]) is Point2D, Point2D.__doc__)"
        )
    finally:
        ws.close()
        store.close()
    assert r.error is None, r.error
    assert r.stdout.split() == [
        "1.0",
        "3",
        "True",
        "True",
        "A",
        "point",
        "in",
        "the",
        "plane.",
    ]


def test_a_class_with_an_unhashable_metaclass_still_runs(rung):
    ws, store = open_ws(rung, PythonConfig(classes=(Unhashable,)))
    try:
        r = ws.run_python("print(Unhashable.__name__)")
    finally:
        ws.close()
        store.close()
    assert r.error is None, r.error
    assert r.stdout.strip() == "Unhashable"


def test_a_shape_comes_back_as_itself_where_a_class_would(rung):
    """What a run made of a shape comes back in its namespace as the
    shape, wherever a class of the embedder's own comes back too."""
    spec, P, T = loaded()
    cfg = PythonConfig(classes=(P, T, Row))
    ws, store = open_ws(rung, cfg)
    try:
        r = ws.run_python("made = Point2D(3.0, 4.0)\nown = Row('a', 1, None)")
    finally:
        ws.close()
        store.close()
    assert r.error is None, r.error
    if "own" in r.namespace:
        assert type(r.namespace["made"]) is P
        assert r.namespace["made"] == P(3.0, 4.0)
    else:
        assert "made" not in r.namespace
