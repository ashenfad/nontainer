"""JSON encoding of handler returns: the data stack's values, NaN, and
refusals that name the path to the offending value."""

import datetime as dt
import json
import subprocess
import sys
import time
from decimal import Decimal

import pytest

from nontainer.apps import Response, contract, enable_apps, normalize, request
from nontainer.apps.contract import encode_json


def strict_loads(data: bytes):
    """``json.loads`` that fails on ``NaN``/``Infinity`` the way a
    browser's ``res.json()`` does."""

    def reject(token):
        raise ValueError(f"non-JSON constant {token}")

    return json.loads(data, parse_constant=reject)


def enc(value):
    return strict_loads(encode_json(value))


# -- plain data -----------------------------------------------------------


def test_plain_data_matches_stdlib_output():
    value = {"s": "x", "i": 1, "f": 1.5, "b": True, "n": None, "l": [1, [2]]}
    assert encode_json(value) == json.dumps(value).encode()


def test_tuple_becomes_list():
    assert enc({"t": (1, (2, 3))}) == {"t": [1, [2, 3]]}


# -- NaN / Inf ------------------------------------------------------------


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_float_is_null(bad):
    assert enc({"x": bad}) == {"x": None}
    assert enc([bad]) == [None]


def test_nan_nested_in_lists_and_dicts():
    value = {"a": [1.0, [float("nan"), {"b": float("inf")}]], "c": {"d": [2.0]}}
    assert enc(value) == {"a": [1.0, [None, {"b": None}]], "c": {"d": [2.0]}}


def test_walk_does_not_mutate_or_copy_untouched_parts():
    clean = {"d": [2.0]}
    value = {"a": [float("nan")], "c": clean}
    cleaned = contract._clean(value, set())
    assert cleaned is not value and cleaned["c"] is clean
    assert value["a"][0] != value["a"][0]  # original NaN left in place


def test_plain_data_never_takes_the_walk(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("walked plain data")

    monkeypatch.setattr(contract, "_clean", boom)
    rows = [{"id": i, "name": f"r{i}", "score": i / 2, "ok": True} for i in range(50)]
    assert strict_loads(encode_json({"rows": rows}))["rows"][3]["id"] == 3


def test_slow_path_stays_within_a_constant_factor():
    """Plain records take the stdlib C encoder in one pass; one NaN at
    the very end costs that pass plus a linear walk. On a laptop 100k
    five-field records measure ~40ms plain (same as json.dumps) and
    ~160ms with a trailing NaN; the bound here is loose on purpose."""
    rows = [
        {"id": i, "name": f"row{i}", "score": i * 0.5, "ok": i % 2 == 0, "t": ["a"]}
        for i in range(50_000)
    ]
    with_nan = [dict(r) for r in rows]
    with_nan[-1]["score"] = float("nan")

    def best(fn):
        times = []
        for _ in range(3):
            start = time.perf_counter()
            fn()
            times.append(time.perf_counter() - start)
        return min(times)

    baseline = best(lambda: json.dumps(rows))
    assert best(lambda: encode_json(rows)) < baseline * 3 + 0.05
    assert best(lambda: encode_json(with_nan)) < baseline * 20 + 0.2


# -- stdlib types ---------------------------------------------------------


def test_datetimes_are_iso_8601():
    utc = dt.timezone.utc
    plus2 = dt.timezone(dt.timedelta(hours=2))
    value = {
        "naive": dt.datetime(2024, 1, 2, 3, 4, 5),
        "aware": dt.datetime(2024, 1, 2, 3, 4, 5, tzinfo=plus2),
        "utc": dt.datetime(2024, 1, 2, tzinfo=utc),
        "date": dt.date(2024, 1, 2),
        "time": dt.time(3, 4, 5),
        "time_tz": dt.time(3, 4, tzinfo=plus2),
    }
    assert enc(value) == {
        "naive": "2024-01-02T03:04:05",
        "aware": "2024-01-02T03:04:05+02:00",
        "utc": "2024-01-02T00:00:00+00:00",
        "date": "2024-01-02",
        "time": "03:04:05",
        "time_tz": "03:04:00+02:00",
    }


def test_timedelta_is_total_seconds():
    assert enc({"d": dt.timedelta(days=1, milliseconds=500)}) == {"d": 86400.5}


def test_decimal_is_a_number():
    out = enc({"d": Decimal("12.25"), "n": Decimal("NaN")})
    assert out == {"d": 12.25, "n": None}
    assert isinstance(out["d"], float)


def test_non_str_keys():
    value = {1: "a", 2.5: "b", None: "c", dt.date(2024, 1, 2): "d"}
    assert enc(value) == {"1": "a", "2.5": "b", "null": "c", "2024-01-02": "d"}


def test_non_finite_float_keys_become_null():
    """A Series with a missing numeric index yields a NaN key."""
    assert enc({float("nan"): 1, 2.5: 2}) == {"null": 1, "2.5": 2}
    assert enc({float("inf"): 1}) == {"null": 1}


# -- refusals -------------------------------------------------------------


def refusal(value) -> str:
    with pytest.raises(TypeError) as e:
        encode_json(value)
    return str(e.value)


@pytest.mark.parametrize("value", [{1, 2}, frozenset({1})])
def test_set_refused(value):
    msg = refusal({"tags": value})
    assert msg.startswith("$.tags: ") and "order is unstable" in msg


def test_unknown_type_refused_with_path():
    class Thing:
        pass

    assert refusal({"rows": [{}, {"x": Thing()}]}) == (
        "$.rows[1].x: Thing is not JSON-encodable"
    )
    assert refusal([complex(1, 2)]) == "$[0]: complex is not JSON-encodable"
    assert refusal({"odd key": object()}) == (
        '$["odd key"]: object is not JSON-encodable'
    )


def test_bad_key_refused_with_path():
    msg = refusal({"counts": {(1, 2): 3}})
    assert msg == "$.counts: dict key of type tuple is not JSON-encodable"


def test_circular_reference_refused():
    loop: list = []
    loop.append(loop)
    assert refusal({"z": loop}) == "$.z[0]: circular reference"


def test_response_body_uses_the_same_encoder():
    r = normalize(
        Response(status=201, body={"x": float("nan"), "d": dt.date(2024, 1, 2)})
    )
    assert r.status == 201 and r.content_type == "application/json"
    assert strict_loads(r.content) == {"x": None, "d": "2024-01-02"}
    with pytest.raises(TypeError, match=r"^\$\.s: set"):
        normalize(Response(body={"s": {1}}))


def test_plain_dict_does_not_import_numpy():
    code = (
        "import sys\n"
        "from nontainer.apps.contract import encode_json\n"
        "encode_json({'a': [1, 2.5, float('nan')], 'b': {'c': None}})\n"
        "encode_json({'d': __import__('datetime').date(2024, 1, 1)})\n"
        "print('numpy' in sys.modules, 'pandas' in sys.modules)\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "False False"


# -- numpy ----------------------------------------------------------------


def test_numpy_scalars():
    np = pytest.importorskip("numpy")
    value = {
        "i": np.int64(7),
        "u": np.uint8(3),
        "f": np.float32(1.5),
        "f64": np.float64(2.5),
        "b": np.bool_(True),
        "nan": np.float64("nan"),
        "nan32": np.float32("nan"),
        "inf": np.float64("-inf"),
    }
    out = enc(value)
    assert out == {
        "i": 7,
        "u": 3,
        "f": 1.5,
        "f64": 2.5,
        "b": True,
        "nan": None,
        "nan32": None,
        "inf": None,
    }
    assert type(out["i"]) is int and type(out["b"]) is bool


def test_numpy_arrays():
    np = pytest.importorskip("numpy")
    value = {
        "ints": np.arange(3),
        "grid": np.array([[1.0, np.nan], [np.inf, 4.0]]),
        "objs": np.array([1, "a", None], dtype=object),
    }
    assert enc(value) == {
        "ints": [0, 1, 2],
        "grid": [[1.0, None], [None, 4.0]],
        "objs": [1, "a", None],
    }


def test_numpy_datetime_and_timedelta():
    np = pytest.importorskip("numpy")
    value = {
        "day": np.datetime64("2024-01-02"),
        "nat": np.datetime64("NaT", "D"),
        "days": np.array(["2024-01-02", "NaT"], dtype="datetime64[D]"),
        "span": np.timedelta64(90, "s"),
        "spans": np.array([1500, "NaT"], dtype="timedelta64[ms]"),
    }
    assert enc(value) == {
        "day": "2024-01-02",
        "nat": None,
        "days": ["2024-01-02", None],
        "span": 90.0,
        "spans": [1.5, None],
    }


def test_numpy_keys():
    np = pytest.importorskip("numpy")
    assert enc({np.int64(1): "a", np.str_("k"): "b"}) == {"1": "a", "k": "b"}
    assert enc({np.float64("nan"): "a"}) == {"null": "a"}


def test_numpy_longdouble_is_a_double():
    """Where longdouble is extended precision (x86), ``.item()`` returns
    a longdouble rather than a float; it still encodes as a number."""
    np = pytest.importorskip("numpy")
    value = {
        "x": np.longdouble(1.5),
        "arr": np.array([1.5, np.nan], dtype=np.longdouble),
        "nan": np.longdouble("nan"),
    }
    assert enc(value) == {"x": 1.5, "arr": [1.5, None], "nan": None}


def test_numpy_unsupported_scalar_refused():
    np = pytest.importorskip("numpy")
    assert refusal({"c": np.complex128(1j)}) == (
        "$.c: complex128 is not JSON-encodable"
    )


# -- pandas ---------------------------------------------------------------


def test_pandas_values():
    pd = pytest.importorskip("pandas")
    value = {
        "ts": pd.Timestamp("2024-01-02 03:04:05"),
        "ts_tz": pd.Timestamp("2024-01-02", tz="UTC"),
        "nat": pd.NaT,
        "na": pd.NA,
        "td": pd.Timedelta("1min 30s"),
        "td_nat": pd.Timedelta("NaT"),
    }
    assert enc(value) == {
        "ts": "2024-01-02T03:04:05",
        "ts_tz": "2024-01-02T00:00:00+00:00",
        "nat": None,
        "na": None,
        "td": 90.0,
        "td_nat": None,
    }


def test_pandas_records_and_series_dict():
    pd = pytest.importorskip("pandas")
    np = pytest.importorskip("numpy")
    df = pd.DataFrame(
        {
            "n": np.arange(3),
            "x": [1.5, np.nan, 2.0],
            "k": pd.array([1, None, 3], dtype="Int64"),
            "when": pd.date_range("2024-01-01", periods=3, tz="UTC"),
        }
    )
    out = enc(
        {"rows": df.to_dict("records"), "by_day": df.set_index("when")["x"].to_dict()}
    )
    assert out["rows"][1] == {
        "n": 1,
        "x": None,
        "k": None,
        "when": "2024-01-02T00:00:00+00:00",
    }
    assert out["by_day"] == {
        "2024-01-01T00:00:00+00:00": 1.5,
        "2024-01-02T00:00:00+00:00": None,
        "2024-01-03T00:00:00+00:00": 2.0,
    }


def test_dataframe_refused_with_path_and_hint():
    pd = pytest.importorskip("pandas")
    value = {"rows": [{}, {}, {}, {"when": pd.DataFrame({"a": [1]})}]}
    assert refusal(value) == (
        "$.rows[3].when: DataFrame is not JSON-encodable "
        '(return .to_dict("records"), or bytes with a content-type)'
    )
    with pytest.raises(
        TypeError, match=r"handler returned DataFrame; return \.to_dict"
    ):
        normalize(pd.DataFrame())


# -- end to end through a workspace handler -------------------------------


def make_data_ws():
    pytest.importorskip("pandas")
    from nontainer import PythonConfig, Workspace
    from nontainer.presets import dataframes
    from nontainer.providers import KvgitProvider

    ws = Workspace(
        KvgitProvider.open(None, session="s1"),
        python=PythonConfig(modules=[dataframes()]),
    )
    ws.files.fs.makedirs("/workspace/app/api", exist_ok=True)
    return ws, enable_apps(ws)


def test_handler_returning_data_stack_values_is_valid_json():
    ws, rt = make_data_ws()
    ws.files.fs.write(
        "/workspace/app/api/stats.py",
        b"import numpy as np\n"
        b"import pandas as pd\n"
        b"def get(req):\n"
        b"    df = pd.DataFrame({'n': np.arange(3), 'x': [1.5, np.nan, 2.0],\n"
        b"        'when': pd.date_range('2024-01-01', periods=3, tz='UTC')})\n"
        b"    return {'rows': df.to_dict('records'), 'mean': df['x'].mean(),\n"
        b"            'count': np.int64(len(df)), 'x': df['x'].to_numpy()}\n",
    )
    r = rt.dispatch(request("GET", "/api/stats"))
    assert r.status == 200, r.text
    assert r.content_type == "application/json"
    body = strict_loads(r.content)
    assert body["rows"][1] == {"n": 1, "x": None, "when": "2024-01-02T00:00:00+00:00"}
    assert body["mean"] == 1.75 and body["count"] == 3
    assert body["x"] == [1.5, None, 2.0]
    ws.close()


def test_handler_bad_return_names_the_path():
    ws, rt = make_data_ws()
    ws.files.fs.write(
        "/workspace/app/api/frame.py",
        b"import pandas as pd\n"
        b"def get(req):\n"
        b"    return {'result': {'table': pd.DataFrame({'a': [1]})}}\n",
    )
    r = rt.dispatch(request("GET", "/api/frame"))
    assert r.status == 500
    assert (
        "$.result.table: DataFrame is not JSON-encodable"
        in strict_loads(r.content)["error"]
    )
    log = ws.files.fs.read("/workspace/app/logs/api.log").decode()
    assert "BAD RETURN: $.result.table: DataFrame" in log
    ws.close()
