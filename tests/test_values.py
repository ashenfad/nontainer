"""nontainer.values: a strict check of a value against its declared type,
and an encoding that decodes only into that type."""

import ast
import collections
import dataclasses
import datetime
import decimal
import enum
import json
import pathlib
import runpy
import sys
import uuid
from collections.abc import Callable, Iterator, Mapping, MutableMapping, Sequence
from typing import Any, Literal, NamedTuple, Optional, TypedDict, Union

import pytest

from nontainer import values
from nontainer.values import (
    FORMAT,
    SAMPLE,
    SAMPLE_ABOVE,
    Encoded,
    Malformed,
    Mismatch,
    Spec,
    TooLarge,
    Unencodable,
    Unsupported,
    encode,
    kinds_of,
)


@dataclasses.dataclass
class Score:
    student: str
    total: int


@dataclasses.dataclass
class Ranking:
    best: str
    scores: list[Score]


@dataclasses.dataclass
class Tree:
    name: str
    children: list["Tree"]


@dataclasses.dataclass
class Note:
    text: str


class Grade(enum.Enum):
    PASS = "pass"
    FAIL = "fail"


class Level(enum.IntEnum):
    LOW = 1
    HIGH = 2


class Point(NamedTuple):
    x: int
    y: int = 0


class Row(TypedDict):
    name: str
    score: float


class Partial(TypedDict, total=False):
    name: str


@dataclasses.dataclass
class Derived:
    base: int
    double: int = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        self.double = self.base * 2


class Client:
    def get(self, key: str) -> str:
        return key


def roundtrip(value: Any, tp: Any) -> Any:
    spec = Spec.of(tp)
    spec.check(value, full=True)
    return spec.decode(Encoded.from_bytes(encode(value).to_bytes()))


# -- kinds ------------------------------------------------------------------------------


def test_kinds_say_what_a_type_needs_carried():
    np = pytest.importorskip("numpy")
    pd = pytest.importorskip("pandas")
    assert kinds_of(list[Score]) == {"data"}
    assert kinds_of(Tree) == {"data"}
    assert kinds_of(dict[str, pd.DataFrame]) == {"data", "table"}
    assert kinds_of(np.ndarray) == {"array"}
    assert kinds_of(bytes) == {"bytes"}
    assert kinds_of(Callable[[int], int]) == {"live"}
    assert kinds_of(Client) == {"live"}
    assert kinds_of(Union[Score, Client]) == {"data", "live"}
    assert kinds_of(Any) == {"any"}
    assert kinds_of(list) == {"data", "any"}
    assert Spec.of(list[Score]).travels and not Spec.of(Iterator[int]).travels


def test_a_spec_names_the_record_and_enum_types_it_uses():
    @dataclasses.dataclass
    class Graded:
        grade: Grade
        ranking: Ranking

    assert set(Spec.of(Optional[Graded]).types) == {Graded, Grade, Ranking, Score}


# -- the strict check ------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, tp",
    [
        (3, int),
        (Level.LOW, int),
        (3, float),
        (2.5, float),
        (True, bool),
        ("x", str),
        (None, None),
        (None, Optional[int]),
        (b"x", bytes),
        ([1, 2], list[int]),
        ((1, "a"), tuple[int, str]),
        ((1, 2, 3), tuple[int, ...]),
        ({1, 2}, set[int]),
        (frozenset({1}), frozenset[int]),
        ({"a": 1}, dict[str, int]),
        (collections.UserDict(a=1), Mapping[str, int]),
        (collections.UserDict(a=1), MutableMapping[str, int]),
        (collections.UserList([1]), Sequence[int]),
        ((1, 2), Sequence[int]),
        (Grade.PASS, Grade),
        ("a", Literal["a", "b"]),
        (Point(1, 2), Point),
        ({"name": "ada", "score": 1.0}, Row),
        ({}, Partial),
        (Ranking("ada", [Score("ada", 7)]), Ranking),
        (Tree("a", [Tree("b", [])]), Tree),
        (Note("x"), Union[Score, Note]),
        (datetime.datetime(2026, 10, 7, 12), datetime.datetime),
        (datetime.date(2026, 10, 7), datetime.date),
        (decimal.Decimal("1.5"), decimal.Decimal),
        (len, Callable[[str], int]),
        (iter([]), Iterator[int]),
        (Client(), Client),
        (object(), Any),
    ],
)
def test_values_that_fit(value, tp):
    Spec.of(tp).check(value)


@pytest.mark.parametrize(
    "value, tp, message",
    [
        ("42", int, "expected int, got str '42'"),
        (True, int, "expected int, got bool True"),
        (3, bool, "expected bool, got int 3"),
        ("3.5", float, "expected float, got str"),
        (b"x", str, "expected str, got bytes"),
        ([1, "2"], list[int], "at [1]: expected int, got str '2'"),
        ((1,), tuple[int, str], "expected tuple[int, str], got a tuple of 1"),
        ({"a": "1"}, dict[str, int], "at ['a']: expected int"),
        ([1], tuple[int, ...], "expected tuple[int, ...], got list"),
        ({"a": 1}, Score, "expected Score, got dict"),
        (Score("ada", "7"), Score, "at total: expected int, got str '7'"),
        (
            Ranking("ada", [Score("ada", 7), Score("bo", "x")]),
            Ranking,
            "at scores[1].total: expected int",
        ),
        ({"name": "ada"}, Row, "Row is missing key 'score'"),
        ({"name": "a", "score": 1.0, "x": 1}, Row, "Row has no key 'x'"),
        ("PASS", Grade, "expected Grade, got str"),
        (1, Literal[True], "expected Literal[True], got int 1"),
        (Client(), Union[Score, Note], "expected Score | Note, got Client"),
        (datetime.datetime(2026, 10, 7), datetime.date, "expected date, got datetime"),
        ("x", Callable[[int], int], "expected Callable"),
        ("ab", Sequence[str], "expected Sequence[str], got str"),
    ],
)
def test_values_that_do_not_fit(value, tp, message):
    with pytest.raises(Mismatch, match=message.replace("[", r"\[").replace("]", r"\]")):
        Spec.of(tp).check(value)


def test_a_numpy_scalar_is_not_the_python_number_declared():
    np = pytest.importorskip("numpy")
    with pytest.raises(Mismatch, match="expected int, got int64"):
        Spec.of(int).check(np.int64(3))
    Spec.of(float).check(np.float64(2.5))  # a float subclass


# -- sampling --------------------------------------------------------------------------


def test_a_long_container_is_checked_on_a_sample_unless_full():
    n = SAMPLE_ABOVE * 2
    spec = Spec.of(list[int])
    sampled = list(values._indices(n, False))
    assert len(sampled) == 3 * SAMPLE
    assert sampled == list(values._indices(n, False))  # the same every time
    hidden = next(i for i in range(SAMPLE, n - SAMPLE) if i not in sampled)
    items: list[Any] = list(range(n))
    items[hidden] = "bad"
    spec.check(items)  # missed by the sample
    with pytest.raises(Mismatch, match=rf"at \[{hidden}\]"):
        spec.check(items, full=True)
    items[hidden] = hidden
    items[-1] = "bad"  # the end is always looked at
    with pytest.raises(Mismatch, match=rf"at \[{n - 1}\]"):
        spec.check(items)


def test_a_short_container_is_checked_in_full():
    items: list[Any] = list(range(SAMPLE_ABOVE))
    items[SAMPLE_ABOVE // 2] = "bad"
    with pytest.raises(Mismatch):
        Spec.of(list[int]).check(items)


def test_dicts_and_sets_are_sampled_too():
    n = SAMPLE_ABOVE + 1
    Spec.of(dict[int, int]).check({i: i for i in range(n)})
    Spec.of(set[int]).check(set(range(n)))
    with pytest.raises(Mismatch):
        Spec.of(dict[int, int]).check({**{i: i for i in range(n)}, n: "x"}, full=True)


# -- round trips -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, tp",
    [
        (3, int),
        (Level.HIGH, Level),
        (2.5, float),
        (float("inf"), float),
        (True, bool),
        ("x", str),
        (None, Optional[str]),
        (b"\x00\xff", bytes),
        ([1, 2], list[int]),
        ((1, "a"), tuple[int, str]),
        ((1, 2, 3), tuple[int, ...]),
        ({1, 2}, set[int]),
        (frozenset({(1, 2)}), frozenset[tuple[int, int]]),
        ({"a": [1.5]}, dict[str, list[float]]),
        ({1: "one", 2: "two"}, dict[int, str]),
        ({"$nt": "not a tag"}, dict[str, str]),
        (Grade.FAIL, Grade),
        ("b", Literal["a", "b"]),
        (Point(1, 2), Point),
        ({"name": "ada", "score": 1.5}, Row),
        ({}, Partial),
        (Ranking("ada", [Score("ada", 7)]), Ranking),
        (Tree("a", [Tree("b", [])]), Tree),
        (Note("x"), Union[Score, Note]),
        (Score("ada", 7), Union[Note, Score]),
        (Derived(4), Derived),
        (
            datetime.datetime(2026, 10, 7, 12, 30, tzinfo=datetime.timezone.utc),
            datetime.datetime,
        ),
        (datetime.date(2026, 10, 7), datetime.date),
        (datetime.time(12, 30, 1, 5), datetime.time),
        (datetime.timedelta(days=-1, seconds=5, microseconds=7), datetime.timedelta),
        (decimal.Decimal("1.50"), decimal.Decimal),
        (uuid.UUID(int=7), uuid.UUID),
        (pathlib.PurePosixPath("/a/b"), pathlib.PurePosixPath),
    ],
)
def test_values_round_trip(value, tp):
    out = roundtrip(value, tp)
    assert out == value
    assert type(out) is type(value)


def test_nan_round_trips():
    import math

    assert math.isnan(roundtrip(float("nan"), float))


def test_any_decodes_plain_data():
    value = {"a": [1, 2.5, None, True], "b": {"c": "d"}}
    assert roundtrip(value, Any) == value
    assert roundtrip((1, 2), Any) == [1, 2]  # JSON has no tuple
    assert roundtrip(b"x", Any) == b"x"


def test_a_table_round_trips_as_its_own_type():
    pd = pytest.importorskip("pandas")
    pytest.importorskip("pyarrow")
    frame = pd.DataFrame({"name": ["ada", "bo"], "score": [1.5, 2.0]})
    out = roundtrip(frame, pd.DataFrame)
    pd.testing.assert_frame_equal(out, frame)
    indexed = frame.set_index("name")
    pd.testing.assert_frame_equal(roundtrip(indexed, pd.DataFrame), indexed)
    series = pd.Series([1, 2], name="n")
    pd.testing.assert_series_equal(roundtrip(series, pd.Series), series)
    unnamed = pd.Series([1.5])
    pd.testing.assert_series_equal(roundtrip(unnamed, pd.Series), unnamed)


def test_tables_inside_data_and_records():
    pd = pytest.importorskip("pandas")
    pa = pytest.importorskip("pyarrow")

    @dataclasses.dataclass
    class Report:
        title: str
        table: pd.DataFrame

    frames = {"a": pd.DataFrame({"x": [1]}), "b": pd.DataFrame({"x": [2]})}
    out = roundtrip(frames, dict[str, pd.DataFrame])
    assert out.keys() == frames.keys()
    pd.testing.assert_frame_equal(out["b"], frames["b"])
    report = roundtrip(Report("t", frames["a"]), Report)
    pd.testing.assert_frame_equal(report.table, frames["a"])
    table = pa.table({"x": [1, 2]})
    assert roundtrip(table, pa.Table).equals(table)


def test_an_array_round_trips_without_pickle():
    np = pytest.importorskip("numpy")
    array = np.arange(6, dtype="float32").reshape(2, 3)
    out = roundtrip(array, np.ndarray)
    assert out.dtype == array.dtype and (out == array).all()
    with pytest.raises(Unencodable, match="Python objects"):
        encode(np.array([object()], dtype=object))


def test_pydantic_models_round_trip_by_their_own_validation():
    pydantic = pytest.importorskip("pydantic")

    class Card(pydantic.BaseModel):
        front: str
        score: Score
        grade: Grade = Grade.PASS

    card = Card(front="q", score=Score("ada", 7))
    assert roundtrip(card, Card) == card
    assert set(Spec.of(Card).types) == {Card, Score, Grade}
    with pytest.raises(Mismatch, match="at grade: expected Grade"):
        Spec.of(Card).decode(
            Encoded({"front": "q", "score": {"student": "a", "total": 1}, "grade": "x"})
        )

    class Short(pydantic.BaseModel):
        text: str

        @pydantic.field_validator("text")
        @classmethod
        def short(cls, text: str) -> str:
            if len(text) > 3:
                raise ValueError("too long")
            return text

    with pytest.raises(Mismatch, match="Short refused its fields.*too long"):
        Spec.of(Short).decode(Encoded({"text": "long text"}))


# -- decoding builds the declared type and nothing else ----------------------------------


@pytest.mark.parametrize(
    "tree, tp, message",
    [
        ("42", int, "expected int, got str '42'"),
        (1.5, int, "expected int, got float"),
        ([1, 2], tuple[int], "expected tuple[int], got 2 items"),
        ({"student": "a"}, Score, "Score is missing field 'total'"),
        ({"student": "a", "total": 1, "x": 0}, Score, "Score has no field 'x'"),
        ({"student": "a", "total": "1"}, Score, "at total: expected int, got str"),
        ({"$nt": "bytes", "part": 0}, str, "expected str, got an encoded bytes"),
        ({"$nt": "table", "part": 0, "of": "pandas.DataFrame"}, int, "expected int"),
        (
            {"$nt": "number", "dtype": "int64", "v": 3},
            int,
            "expected int, got an encoded number",
        ),
        ({"$nt": "bytes", "part": 3}, bytes, "pointing at a part it doesn't have"),
        ({"$nt": "who knows"}, Any, "unknown encoded value"),
        (
            {"$nt": "table", "part": 0, "of": "pandas.DataFrame"},
            Any,
            "declare its type",
        ),
        ("7", Grade, "not one of its values"),
        ("2026-13-01", datetime.date, "expected date"),
        ({"$nt": "float", "v": "1e9"}, float, "isn't nan or infinite"),
        ([1], Callable[[int], int], "can't cross into this process"),
        ({"a": 1}, Union[Score, Note], "expected Score | Note"),
    ],
)
def test_decoding_refuses_what_the_type_does_not_allow(tree, tp, message):
    with pytest.raises(Mismatch, match=message.replace("[", r"\[").replace("]", r"\]")):
        Spec.of(tp).decode(Encoded(tree, (b"x",)))


def test_a_table_comes_back_only_as_the_table_type_declared():
    pd = pytest.importorskip("pandas")
    pa = pytest.importorskip("pyarrow")
    encoded = encode(pa.table({"x": [1]}))
    with pytest.raises(
        Mismatch, match="expected DataFrame, got an encoded pyarrow.Table"
    ):
        Spec.of(pd.DataFrame).decode(encoded)


def test_check_and_decode_agree():
    """What the check accepts in this process, the decoder accepts off
    it, and what it refuses, the decoder refuses (a value that can't be
    encoded aside)."""
    cases = [
        (3, int),
        (Level.LOW, int),
        (True, int),
        (3, float),
        ("3", float),
        (None, Optional[int]),
        ([1, "2"], list[int]),
        (Score("a", 1), Score),
        (Score("a", "1"), Score),
        (Note("x"), Score),
        (Note("x"), Union[Score, Note]),
        ({"name": "a", "score": 1.0}, Row),
        ({"name": "a"}, Row),
        ({1: "a"}, dict[int, str]),
        ({"1": "a"}, dict[int, str]),
        (Grade.PASS, Grade),
        ("PASS", Grade),
        ("a", Literal["a"]),
        (1, Literal[True]),
        (datetime.datetime(2026, 1, 1), datetime.date),
        (Point(1, 2), Point),
        (Point(1, 2), tuple[int, int]),
        (b"x", bytes),
        ("x", bytes),
    ]
    for value, tp in cases:
        spec = Spec.of(tp)
        try:
            spec.check(value, full=True)
            checked = True
        except Mismatch:
            checked = False
        try:
            spec.decode(encode(value))
            decoded = True
        except Mismatch:
            decoded = False
        assert checked == decoded, (value, tp, checked, decoded)


def test_what_json_cannot_tell_apart_decodes_either_way():
    """Where decoding is looser than the check: JSON has arrays only, so
    a tuple or a set sent where a list is declared arrives as one, and an
    enum travels as its value, so the value decodes as the member. The
    wire stays plain JSON, which other languages read."""
    with pytest.raises(Mismatch):
        Spec.of(list[int]).check((1, 2))
    assert Spec.of(list[int]).decode(encode((1, 2))) == [1, 2]
    assert Spec.of(tuple[int, ...]).decode(encode([1, 2])) == (1, 2)
    assert Spec.of(set[int]).decode(encode([1, 2])) == {1, 2}
    with pytest.raises(Mismatch):
        Spec.of(Grade).check("pass")
    assert Spec.of(Grade).decode(encode("pass")) is Grade.PASS


def test_a_named_tuple_travels_as_an_array():
    assert encode(Point(1, 2)).tree == [1, 2]
    assert Spec.of(Point).decode(Encoded([3])) == Point(3, 0)
    assert Spec.of(Point).decode(Encoded({"x": 3, "y": 4})) == Point(3, 4)
    assert Spec.of(tuple[int, int]).decode(encode(Point(1, 2))) == (1, 2)
    with pytest.raises(Mismatch, match="got 3 items"):
        Spec.of(Point).decode(Encoded([1, 2, 3]))


def test_pydantic_agrees_on_json_where_it_should():
    """pydantic, strict on JSON, as an oracle for the scalar, container
    and record rules. The deliberate differences: a record refuses
    fields it doesn't have (pydantic ignores them)."""
    pydantic = pytest.importorskip("pydantic")
    cases = [
        (int, 3),
        (int, "3"),
        (int, 3.0),
        (int, True),
        (float, 3),
        (float, "3.5"),
        (str, "x"),
        (str, 3),
        (bool, True),
        (bool, 1),
        (list[int], [1, 2]),
        (list[int], [1, "2"]),
        (tuple[int, str], [1, "a"]),
        (tuple[int, str], [1]),
        (dict[str, int], {"a": 1}),
        (dict[str, int], {"a": "1"}),
        (Optional[int], None),
        (Score, {"student": "a", "total": 1}),
        (Score, {"student": "a"}),
        (Score, {"student": "a", "total": "1"}),
        (Ranking, {"best": "a", "scores": [{"student": "a", "total": 1}]}),
        (Grade, "pass"),
        (Grade, "nope"),
        (Literal["a", "b"], "b"),
        (Literal["a"], "c"),
        (datetime.date, "2026-10-07"),
        (datetime.date, "2026-13-07"),
        (decimal.Decimal, "1.5"),
        (uuid.UUID, str(uuid.UUID(int=3))),
    ]
    for tp, tree in cases:
        try:
            pydantic.TypeAdapter(tp).validate_json(json.dumps(tree), strict=True)
            expected = True
        except pydantic.ValidationError:
            expected = False
        try:
            Spec.of(tp).decode(Encoded(tree))
            got = True
        except Mismatch:
            got = False
        assert got == expected, (tp, tree, got, expected)


# -- encoding --------------------------------------------------------------------------


def test_live_values_and_cycles_have_no_encoding():
    with pytest.raises(Unencodable, match="Client can't be encoded"):
        encode(Client())
    loop: list[Any] = []
    loop.append(loop)
    with pytest.raises(Unencodable, match="holding itself"):
        encode(loop)


def test_an_encoding_over_its_limit_is_refused():
    encode(b"x" * 100, limit=1_000)
    with pytest.raises(TooLarge, match="over the 1,000 allowed"):
        encode(b"x" * 2_000, limit=1_000)


def test_a_blob_names_its_format_and_reads_back():
    encoded = encode({"a": b"\x01", "b": [b"\x02\x03"]})
    blob = encoded.to_bytes()
    assert blob.startswith(FORMAT.encode())
    assert Encoded.from_bytes(blob) == encoded
    assert encoded.size == len(blob)
    for broken in (
        b"",
        b"pickle",
        blob[:-1],
        blob + b"x",
        FORMAT.encode() + b"\n\x00\x00\x00\x09not json!",
    ):
        with pytest.raises(Malformed):
            Encoded.from_bytes(broken)


# -- compiling -------------------------------------------------------------------------


def test_annotations_that_cannot_be_resolved_are_refused():
    @dataclasses.dataclass
    class Lost:
        x: "Nowhere"  # noqa: F821

    with pytest.raises(Unsupported, match="Lost's annotations can't be resolved"):
        Spec.of(Lost)


def test_a_function_local_type_resolves_with_the_names_given():
    @dataclasses.dataclass
    class Inner:
        x: int

    @dataclasses.dataclass
    class Outer:
        inner: "Inner"
        many: list["Inner"]

    with pytest.raises(Unsupported):
        Spec.of(Outer)
    spec = Spec.of(Outer, names={"Inner": Inner})
    assert spec.decode(encode(Outer(Inner(1), [Inner(2)]))) == Outer(
        Inner(1), [Inner(2)]
    )


def test_a_protocol_that_cannot_be_checked_is_refused():
    from typing import Protocol, runtime_checkable

    class Opaque(Protocol):
        def go(self) -> None: ...

    @runtime_checkable
    class Checked(Protocol):
        def go(self) -> None: ...

    with pytest.raises(Unsupported, match="runtime_checkable"):
        Spec.of(Opaque)
    Spec.of(Checked)


# -- the module runs from its source alone ---------------------------------------------


def test_the_module_imports_only_the_standard_library_at_module_level():
    source = pathlib.Path(values.__file__).read_text()
    lazy = {"numpy", "pandas", "pyarrow", "polars"}
    for node in ast.parse(source).body:
        if isinstance(node, ast.Import):
            roots = {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            roots = {(node.module or "").split(".")[0]}
        else:
            continue
        assert roots <= set(sys.stdlib_module_names) | {"__future__"}, roots
    nested = {
        alias.name.split(".")[0]
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert nested - set(sys.stdlib_module_names) <= lazy


def test_the_module_runs_from_its_source():
    namespace = runpy.run_path(values.__file__, run_name="nt_values_from_source")
    blob = namespace["encode"]({"a": [1, b"x"]}).to_bytes()
    spec = namespace["Spec"].of(dict[str, list[Union[int, bytes]]])
    assert spec.decode(blob) == {"a": [1, b"x"]}
