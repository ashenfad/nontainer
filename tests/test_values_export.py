"""Specs written as data and read back: types crossing as shapes.

One side compiles a signature against its own classes and writes it
out; the other reads it back as classes of its own, with the same names,
fields and docstrings, makes values of them, and the first side decodes
those into its real classes."""

from __future__ import annotations

import dataclasses
import datetime
import decimal
import enum
import json
import pathlib
import typing
import uuid
from collections.abc import Callable
from typing import Any, Literal, NamedTuple, Optional, TypedDict

import pytest

from nontainer import values as v


class Shape(enum.Enum):
    """A shape we know the corners of."""

    SQUARE = "square"
    HEX = "hex"


@dataclasses.dataclass
class Point2D:
    """A point in the plane."""

    x: float
    y: float

    def norm(self) -> float:
        return (self.x**2 + self.y**2) ** 0.5


@dataclasses.dataclass
class Tree:
    label: str
    kids: list[Tree] = dataclasses.field(default_factory=list)
    shape: Optional[Shape] = None
    when: Literal["now", "later"] = "now"


def round_trip(**specs: Any) -> dict[str, v.Spec]:
    data = v.export_specs({k: v.Spec.of(t) for k, t in specs.items()})
    return v.load_specs(json.loads(json.dumps(data)), module="helper")


def test_a_record_comes_back_as_a_class_of_its_own():
    back = round_trip(result=list[Point2D])
    (P,) = back["result"].types
    assert P is not Point2D
    assert (P.__name__, P.__module__, P.__doc__) == (
        "Point2D",
        "helper",
        "A point in the plane.",
    )
    assert [f.name for f in dataclasses.fields(P)] == ["x", "y"]
    assert not hasattr(P, "norm")  # a shape: no methods
    made = [P(1.0, 2.0), P(x=3.0, y=4.0)]
    back["result"].check(made)
    # the caller decodes the helper's values into its own class
    assert v.Spec.of(list[Point2D]).decode(v.encode(made)) == [
        Point2D(1.0, 2.0),
        Point2D(3.0, 4.0),
    ]


def test_specs_share_their_classes():
    back = round_trip(where=Point2D, result=list[Point2D])
    assert back["where"].types == back["result"].types


def test_nesting_unions_literals_and_a_record_that_holds_itself():
    back = round_trip(tree=Tree)
    T = next(t for t in back["tree"].types if t.__name__ == "Tree")
    S = next(t for t in back["tree"].types if t.__name__ == "Shape")
    assert S.__doc__ == "A shape we know the corners of."
    assert [m.name for m in S] == ["SQUARE", "HEX"]
    made = T(label="root", kids=[T(label="leaf", shape=S.HEX, when="later")])
    back["tree"].check(made)
    with pytest.raises(v.Mismatch):
        back["tree"].check(T(label="x", when="soon"))
    assert v.Spec.of(Tree).decode(v.encode(made)) == Tree(
        "root", [Tree("leaf", shape=Shape.HEX, when="later")]
    )


def test_a_literal_of_an_enum_member():
    back = round_trip(it=Literal[Shape.SQUARE])
    (square,) = typing.get_args(back["it"].annotation)
    assert type(square) is not Shape and square.name == "SQUARE"
    back["it"].check(square)
    with pytest.raises(v.Mismatch):
        back["it"].check(type(square).HEX)


@pytest.mark.parametrize(
    "tp, value",
    [
        (int, 3),
        (bool, True),
        (str, "s"),
        (bytes, b"\x00\x01"),
        (type(None), None),
        (Any, {"a": [1, 2]}),
        (tuple[int, str], (1, "a")),
        (tuple[int, ...], (1, 2, 3)),
        (tuple[()], ()),
        (set[int], {1, 2}),
        (frozenset[str], frozenset({"a"})),
        (dict[str, list[float]], {"a": [1.5]}),
        (datetime.datetime, datetime.datetime(2026, 10, 9, 12, 0)),
        (datetime.date, datetime.date(2026, 10, 9)),
        (datetime.time, datetime.time(12, 30)),
        (datetime.timedelta, datetime.timedelta(seconds=5)),
        (decimal.Decimal, decimal.Decimal("1.25")),
        (uuid.UUID, uuid.UUID(int=7)),
        (pathlib.Path, pathlib.Path("a/b")),
        (int | None, None),
    ],
)
def test_each_kind_round_trips(tp, value):
    back = round_trip(it=tp)
    back["it"].check(value)
    assert v.Spec.of(tp).decode(v.encode(value)) == value


class Pair(NamedTuple):
    left: int
    right: int = 0


class Movie(TypedDict):
    title: str
    year: int


def test_named_tuples_and_typed_dicts_come_back_as_dataclasses():
    back = round_trip(pair=Pair, movie=Movie)
    P = back["pair"].types[0]
    M = back["movie"].types[0]
    assert dataclasses.is_dataclass(P) and dataclasses.is_dataclass(M)
    assert P(left=1).right == 0
    assert v.Spec.of(Pair).decode(v.encode(P(left=1, right=2))) == Pair(1, 2)
    assert v.Spec.of(Movie).decode(v.encode(M(title="Up", year=2009))) == {
        "title": "Up",
        "year": 2009,
    }


def test_a_pydantic_model_comes_back_as_a_dataclass_without_its_validators():
    pydantic = pytest.importorskip("pydantic")

    class Score(pydantic.BaseModel):
        """A score out of ten."""

        value: int
        note: str = "none"

        @pydantic.field_validator("value")
        @classmethod
        def in_range(cls, n: int) -> int:
            if not 0 <= n <= 10:
                raise ValueError("out of range")
            return n

    back = round_trip(score=Score)
    S = back["score"].types[0]
    assert dataclasses.is_dataclass(S) and S.__doc__ == "A score out of ten."
    made = S(value=11)  # the helper can make it; the caller's decode refuses it
    assert made.note == "none"
    with pytest.raises(v.Mismatch, match="out of range"):
        v.Spec.of(Score).decode(v.encode(made))


def test_defaults_are_written_and_never_shared():
    @dataclasses.dataclass
    class Opts:
        name: str
        tags: list[str] = dataclasses.field(default_factory=lambda: ["a"])
        shape: Shape = Shape.HEX
        size: int = dataclasses.field(default=3, kw_only=True)

    back = round_trip(opts=Opts)
    Made = next(t for t in back["opts"].types if t.__name__ == "Opts")
    one, two = Made("x"), Made("y")
    assert one.tags == ["a"] and one.tags is not two.tags
    assert one.shape.name == "HEX" and type(one.shape).__name__ == "Shape"
    assert one.size == 3
    with pytest.raises(TypeError):
        Made("x", ["b"], one.shape, 4)  # size stays keyword-only


def test_a_required_field_after_a_defaulted_one_becomes_keyword_only():
    class Mixed(TypedDict, total=False):
        maybe: int

    class Row(Mixed):
        must: str

    back = round_trip(row=Row)
    R = back["row"].types[0]
    assert R(must="x").maybe is None  # not required, no default written


def test_a_field_the_class_sets_itself_is_not_written():
    @dataclasses.dataclass
    class Area:
        w: float
        h: float
        size: float = dataclasses.field(init=False)

        def __post_init__(self) -> None:
            self.size = self.w * self.h

    back = round_trip(area=Area)
    A = back["area"].types[0]
    assert [f.name for f in dataclasses.fields(A)] == ["w", "h"]
    assert v.Spec.of(Area).decode(v.encode(A(2.0, 3.0))).size == 6.0


def test_a_class_defined_in_a_function_crosses_too():
    @dataclasses.dataclass
    class Local:
        n: int

    back = round_trip(it=Local)
    assert back["it"].types[0](n=1).n == 1


def test_a_docstring_is_only_one_the_class_wrote():
    @dataclasses.dataclass
    class Bare:
        n: int

    data = v.export_specs({"it": v.Spec.of(Bare)})
    assert data["types"]["Bare"]["doc"] is None


def test_tables_and_arrays_round_trip():
    pandas = pytest.importorskip("pandas")
    numpy = pytest.importorskip("numpy")
    back = round_trip(frame=pandas.DataFrame, arr=numpy.ndarray)
    frame = pandas.DataFrame({"a": [1, 2]})
    back["frame"].check(frame)
    back["arr"].check(numpy.arange(3))


def test_a_live_type_is_refused_when_written():
    with pytest.raises(v.Unsupported, match="live"):
        v.export_specs({"fn": v.Spec.of(Callable[[int], int])})


def test_two_types_under_one_name_are_refused():
    def make():
        @dataclasses.dataclass
        class Point2D:
            z: int

        return Point2D

    with pytest.raises(v.Unsupported, match="two different types are named Point2D"):
        v.export_specs({"a": v.Spec.of(Point2D), "b": v.Spec.of(make())})


def good() -> dict[str, Any]:
    return v.export_specs({"it": v.Spec.of(Tree)})


def tamper(path: list[Any], value: Any) -> dict[str, Any]:
    data = json.loads(json.dumps(good()))
    target = data
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    return data


@pytest.mark.parametrize(
    "data",
    [
        "not a dict",
        {"format": "nt-spec/2", "specs": {}, "types": {}},
        {"format": v.SPEC_FORMAT, "specs": {}, "types": {}, "extra": 1},
        tamper(["types", "Tree", "fields", 0, "name"], "__class__"),
        tamper(["types", "Tree", "fields", 0, "name"], "import os"),
        tamper(["types", "Tree", "fields", 0, "name"], "class"),
        tamper(["types", "Tree", "fields", 0, "type"], {"t": "eval", "code": "1"}),
        tamper(["types", "Tree", "fields", 0, "type"], {"t": "str", "x": 1}),
        tamper(["types", "Tree", "fields", 0, "type"], {"t": "ref", "name": "Nowhere"}),
        tamper(
            ["types", "Tree", "fields", 0, "type"], {"t": "table", "of": "os.system"}
        ),
        tamper(["types", "Tree", "fields", 0, "required"], "yes"),
        tamper(["types", "Tree", "kind"], "module"),
        tamper(["types", "Shape", "members", 0, 0], "_sunder_"),
        tamper(["types", "Shape", "flavor"], "metaclass"),
        tamper(["types", "Shape", "flavor"], "int"),
        tamper(
            ["types", "__Mangled"],
            {"kind": "enum", "doc": None, "flavor": "enum", "members": []},
        ),
        tamper(["types", "Tree", "doc"], 3),
        tamper(["specs", "it"], {"t": "literal", "values": [{"a": 1}]}),
    ],
)
def test_what_is_not_an_export_is_refused(data):
    with pytest.raises(v.Malformed):
        v.load_specs(data)


class Perm(enum.Flag):
    READ = 1
    WRITE = 2
    ALL = 3


class Level(enum.IntEnum):
    LOW = 1
    HIGH = 2


class Tone(str, enum.Enum):
    SOFT = "soft"
    LOUD = "loud"


class Status(enum.Enum):
    OK = 1
    SUCCESS = 1


@pytest.mark.parametrize("cls", [Perm, enum.IntFlag("Bits", ["A", "B"]), Level, Tone])
def test_an_enum_comes_back_as_its_own_kind(cls):
    back = round_trip(it=cls)
    (made,) = back["it"].types
    kind = cls.__mro__[1]
    assert issubclass(made, kind if kind is not str else str)
    for member in cls:
        assert made[member.name].value == member.value
        if isinstance(member, (int, str)):
            assert made[member.name] == member.value


def test_a_flag_keeps_the_combinations_it_had():
    back = round_trip(it=Perm)
    (P,) = back["it"].types
    back["it"].check(P.READ | P.WRITE)
    assert back["it"].decode(v.encode(Perm.READ | Perm.WRITE)) == P.READ | P.WRITE
    no_name = enum.Flag("Sparse", [("A", 1), ("B", 2)])
    back = round_trip(it=no_name)
    (S,) = back["it"].types
    assert back["it"].decode(v.encode(no_name.A | no_name.B)) == S.A | S.B


def test_an_enums_aliases_are_kept():
    back = round_trip(it=Status)
    (S,) = back["it"].types
    assert list(S.__members__) == ["OK", "SUCCESS"]
    assert S.SUCCESS is S.OK


def test_a_private_type_name_round_trips():
    @dataclasses.dataclass
    class _Private:
        x: int

    back = round_trip(it=_Private)
    (P,) = back["it"].types
    assert P.__name__ == "_Private"
    back["it"].check(P(1))


def test_a_name_the_loader_would_refuse_is_refused_when_written():
    Odd = TypedDict("Odd", {"class": int})
    with pytest.raises(v.Unsupported, match="'class'"):
        v.export_specs({"it": v.Spec.of(Odd)})
    Hidden = enum.Enum("Hidden", [("_x", 1)])
    with pytest.raises(v.Unsupported, match="'_x'"):
        v.export_specs({"it": v.Spec.of(Hidden)})


def test_a_type_nested_too_deeply_is_refused():
    tree: dict[str, Any] = {"t": "int"}
    for _ in range(200):
        tree = {"t": "list", "item": tree}
    with pytest.raises(v.Malformed, match="too deeply"):
        v.load_specs({"format": v.SPEC_FORMAT, "specs": {"it": tree}, "types": {}})


def test_a_type_name_can_not_reach_a_real_class():
    data = {
        "format": v.SPEC_FORMAT,
        "specs": {"it": {"t": "ref", "name": "Path"}},
        "types": {},
    }
    with pytest.raises(v.Malformed, match="isn't written"):
        v.load_specs(data)
