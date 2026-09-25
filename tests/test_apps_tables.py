"""Table returns, Accept negotiation, and the response-size caps.

A handler may return a table: a pandas DataFrame or Series, a pyarrow
Table, or anything exposing ``__arrow_c_stream__``. As the whole
response it answers as an Arrow IPC stream when the request's Accept
asks for one and as JSON rows otherwise; nested in a dict or list it
encodes as its rows. Every body is held to a text or a binary cap
where it is encoded.
"""

import json
import sys

import pytest

from nontainer import PythonConfig, Workspace
from nontainer.apps import AppsConfig, Response, enable_apps, normalize, request
from nontainer.apps.contract import (
    ARROW_FELL_BACK,
    ARROW_STREAM,
    ARROW_UNAVAILABLE,
    WIRE_NOT_ACCEPTABLE,
    WIRE_NOTED_RESPONSE,
    WIRE_REFUSED,
    WIRE_RESPONSE,
    accepts_arrow,
    is_text_type,
    make_request,
    nt__Encoder,
    size_refusal,
)
from nontainer.providers import KvgitProvider

pd = pytest.importorskip("pandas")
np = pytest.importorskip("numpy")

LOG = "/workspace/app/logs/api.log"


def respond(value, accept=None, **limits):
    return nt__Encoder.respond(value, accept, **limits)


def json_rows(value):
    wire = respond(value)
    assert wire[:4] == (WIRE_RESPONSE, 200, "application/json", {"vary": "Accept"})
    return json.loads(wire[4])


def arrow_table(value):
    pa = pytest.importorskip("pyarrow")
    wire = respond(value, ARROW_STREAM)
    assert wire[:4] == (WIRE_RESPONSE, 200, ARROW_STREAM, {"vary": "Accept"})
    return pa.ipc.open_stream(wire[4]).read_all()


class ArrowStream:
    """The Arrow PyCapsule stream protocol and nothing else, the way a
    polars frame or a DuckDB relation presents itself."""

    def __init__(self, table):
        self._table = table

    def __arrow_c_stream__(self, requested_schema=None):
        return self._table.__arrow_c_stream__(requested_schema)


# -- the table kinds ----------------------------------------------------------


def test_dataframe_answers_json_rows_by_default():
    df = pd.DataFrame(
        {
            "n": np.arange(3),
            "x": [1.5, np.nan, 2.0],
            "when": pd.date_range("2024-01-01", periods=3, tz="UTC"),
        }
    )
    assert json_rows(df) == [
        {"n": 0, "x": 1.5, "when": "2024-01-01T00:00:00+00:00"},
        {"n": 1, "x": None, "when": "2024-01-02T00:00:00+00:00"},
        {"n": 2, "x": 2.0, "when": "2024-01-03T00:00:00+00:00"},
    ]


def test_dataframe_answers_an_arrow_stream_when_asked():
    df = pd.DataFrame({"n": [1, 2], "x": [1.5, np.nan], "s": ["a", "b"]})
    table = arrow_table(df)
    assert table.column_names == ["n", "x", "s"]
    assert table.to_pydict() == {"n": [1, 2], "x": [1.5, None], "s": ["a", "b"]}


def test_series_is_a_one_column_table():
    named = pd.Series([1, 2], name="v")
    assert json_rows(named) == [{"v": 1}, {"v": 2}]
    assert arrow_table(named).column_names == ["v"]
    # Unnamed: the column is named as to_frame() names it, 0.
    unnamed = pd.Series([1, 2])
    assert json_rows(unnamed) == [{"0": 1}, {"0": 2}]
    assert arrow_table(unnamed).column_names == ["0"]


def test_pyarrow_table_both_ways():
    pa = pytest.importorskip("pyarrow")
    table = pa.table({"a": [1, 2], "b": ["x", None]})
    assert json_rows(table) == [{"a": 1, "b": "x"}, {"a": 2, "b": None}]
    assert arrow_table(table).equals(table)
    batch = table.to_batches()[0]
    assert json_rows(batch) == [{"a": 1, "b": "x"}, {"a": 2, "b": None}]


def _stream_objects():
    pa = pytest.importorskip("pyarrow")
    table = pa.table({"a": [1, 2], "b": [0.5, None]})
    objects = [ArrowStream(table)]
    try:
        import polars
    except ImportError:
        pass
    else:
        objects.append(polars.from_arrow(table))
    return table, objects


def test_arrow_c_stream_objects_both_ways():
    table, objects = _stream_objects()
    for obj in objects:
        assert json_rows(obj) == [{"a": 1, "b": 0.5}, {"a": 2, "b": None}], obj
        assert arrow_table(obj).equals(table), obj


def test_a_stream_that_is_not_a_table_is_refused():
    pa = pytest.importorskip("pyarrow")
    column = ArrowStream(pa.chunked_array([[1, 2]]))
    wire = respond(column)
    assert wire[0] == WIRE_REFUSED
    assert (
        "ArrowStream exposes __arrow_c_stream__ but does not read as a table"
        in (wire[1])
    )


def test_response_body_table_negotiates_and_keeps_its_headers():
    pa = pytest.importorskip("pyarrow")
    df = pd.DataFrame({"a": [1]})
    wire = respond(
        Response(status=201, body=df, headers={"Vary": "Authorization"}), ARROW_STREAM
    )
    assert wire[1:4] == (201, ARROW_STREAM, {"vary": "Authorization, Accept"})
    assert pa.ipc.open_stream(wire[4]).read_all().column_names == ["a"]
    wire = respond(Response(body=df, headers={"vary": "accept"}))
    assert wire[2:4] == ("application/json", {"vary": "accept"})


# -- the index ----------------------------------------------------------------


def _filtered():
    """What a boolean filter leaves: an unnamed integer index holding the
    surviving rows' old positions, irregular so pandas cannot make it a
    RangeIndex."""
    df = pd.DataFrame({"v": [1, 2, 3, 4]})
    out = df[df.v != 3]
    assert not isinstance(out.index, pd.RangeIndex)
    return out


INDEX_CASES = {
    # Row numbers are dropped: any RangeIndex, and an unnamed integer index.
    "default_range_index": (pd.DataFrame({"v": [1, 2]}), ["v"]),
    "sliced_range_index": (pd.DataFrame({"v": [1, 2, 3]}).iloc[1:], ["v"]),
    "stepped_range_index": (pd.DataFrame({"v": [1, 2, 3]}).iloc[::2], ["v"]),
    "named_range_index": (
        pd.DataFrame({"v": [1, 2]}, index=pd.RangeIndex(0, 2, name="k")),
        ["v"],
    ),
    "filtered_int_index": (_filtered(), ["v"]),
    "unnamed_int_index": (pd.DataFrame({"v": [1, 2]}, index=[10, 20]), ["v"]),
    "unnamed_uint_index": (
        pd.DataFrame({"v": [1, 2]}, index=pd.Index([9, 4], dtype="uint8")),
        ["v"],
    ),
    "unnamed_nullable_int_index": (
        pd.DataFrame({"v": [1, 2, 3]}, index=pd.Index([3, 1, 7], dtype="Int64")),
        ["v"],
    ),
    # Everything else is data, and becomes columns.
    "named_int_index": (
        pd.DataFrame({"v": [1, 2, 3]}, index=pd.Index([10, 30, 20], name="id")),
        ["id", "v"],
    ),
    "named_string_index": (
        pd.DataFrame({"v": [1, 2]}, index=pd.Index(["a", "b"], name="k")),
        ["k", "v"],
    ),
    "unnamed_datetime_index": (
        pd.DataFrame({"v": [1, 2]}, index=pd.date_range("2024-01-01", periods=2)),
        ["index", "v"],
    ),
    "unnamed_string_index": (
        pd.DataFrame({"v": [1, 2]}, index=["a", "b"]),
        ["index", "v"],
    ),
    "unnamed_float_index": (
        pd.DataFrame({"v": [1, 2]}, index=[0.5, 1.5]),
        ["index", "v"],
    ),
    "multi_index": (
        pd.DataFrame(
            {"v": [1, 2]},
            index=pd.MultiIndex.from_tuples([("a", 1), ("b", 2)], names=["k", None]),
        ),
        ["k", "level_1", "v"],
    ),
    "unnamed_int_multi_index": (
        pd.DataFrame({"v": [1, 2]}, index=pd.MultiIndex.from_tuples([(1, 5), (2, 3)])),
        ["level_0", "level_1", "v"],
    ),
    "groupby_series": (
        pd.DataFrame({"year": [2020, 2020, 2023], "v": [1, 2, 3]})
        .groupby("year")["v"]
        .sum(),
        ["year", "v"],
    ),
}


@pytest.mark.parametrize("case", list(INDEX_CASES))
def test_index_rule_gives_the_same_columns_both_ways(case):
    value, columns = INDEX_CASES[case]
    rows = json_rows(value)
    assert list(rows[0]) == columns
    assert arrow_table(value).column_names == columns


def test_filtered_rows_keep_their_values_not_their_positions():
    assert json_rows(_filtered()) == [{"v": 1}, {"v": 2}, {"v": 4}]
    assert arrow_table(_filtered()).to_pydict() == {"v": [1, 2, 4]}


def test_groupby_series_values():
    s = pd.DataFrame({"year": [2020, 2020, 2021], "v": [1, 2, 3]}).groupby("year")["v"]
    assert json_rows(s.sum()) == [{"year": 2020, "v": 3}, {"year": 2021, "v": 3}]


def test_frames_with_no_row_form_are_refused():
    dupes = pd.DataFrame([[1, 2]], columns=["a", "a"])
    for accept in (None, ARROW_STREAM):
        wire = respond(dupes, accept)
        assert wire[0] == WIRE_REFUSED
        assert "duplicate column names (a)" in wire[1]
    multi = pd.DataFrame(
        [[1, 2]], columns=pd.MultiIndex.from_tuples([("a", 1), ("a", 2)])
    )
    assert "MultiIndex columns" in respond(multi)[1]
    collides = pd.DataFrame({"k": [1]}, index=pd.Index([5], name="k"))
    assert "index cannot become a column" in respond(collides)[1]


# -- nested tables ------------------------------------------------------------


def test_nested_tables_encode_as_rows():
    pa = pytest.importorskip("pyarrow")
    value = {
        "df": pd.DataFrame({"a": [1, np.nan]}),
        "series": pd.Series([1], index=pd.Index(["x"], name="k"), name="n"),
        "arrow": pa.table({"b": [True]}),
        "stream": ArrowStream(pa.table({"c": ["z"]})),
        "total": 42,
    }
    # A dict is JSON whatever Accept prefers, and does not vary on it.
    wire = respond(value, ARROW_STREAM)
    assert wire[:4] == (WIRE_RESPONSE, 200, "application/json", {})
    assert json.loads(wire[4]) == {
        "df": [{"a": 1.0}, {"a": None}],
        "series": [{"k": "x", "n": 1}],
        "arrow": [{"b": True}],
        "stream": [{"c": "z"}],
        "total": 42,
    }


def test_a_refusal_inside_nested_rows_names_the_path():
    df = pd.DataFrame({"a": [1, 2]})
    df["bad"] = [None, {1}]
    wire = respond({"rows": df})
    assert wire == (
        WIRE_REFUSED,
        "$.rows[1].bad: set is not JSON-encodable "
        "(its order is unstable; return sorted(...))",
    )


def test_non_table_returns_do_not_vary():
    for value in ({"a": 1}, [1], "text", b"bytes", None):
        assert respond(value, ARROW_STREAM)[3] == {}, value


# -- Accept -------------------------------------------------------------------


@pytest.mark.parametrize(
    "accept, arrow",
    [
        (None, False),
        ("", False),
        ("*/*", False),
        ("application/*", False),
        ("application/json", False),
        (ARROW_STREAM, True),
        ("APPLICATION/VND.APACHE.ARROW.STREAM", True),
        (f"{ARROW_STREAM};q=0", False),
        (f"{ARROW_STREAM}; q=0.0", False),
        (f"{ARROW_STREAM};q=0.5", True),
        (f"text/html, {ARROW_STREAM}", True),
        (f"application/json, {ARROW_STREAM}", True),
        (f"{ARROW_STREAM}, */*;q=0.8", True),
        # JSON ranked above Arrow is a preference for JSON.
        (f"application/json, {ARROW_STREAM};q=0.5", False),
        (f"{ARROW_STREAM};q=0.9, */*", False),
        (f"{ARROW_STREAM};q=0.9, application/*;q=0.5", True),
        (f"application/json;q=0, {ARROW_STREAM};q=0.1", True),
        # Malformed ranges and q-values are left out.
        (f"{ARROW_STREAM};q=abc", False),
        (f"{ARROW_STREAM};q=2", False),
        (f"garbage, {ARROW_STREAM}", True),
    ],
)
def test_accept_parsing(accept, arrow):
    assert accepts_arrow(accept) is arrow


def test_request_header_names_are_case_insensitive():
    req = make_request("GET", "/api/t", headers={"Accept": ARROW_STREAM})
    assert req.headers == {"accept": ARROW_STREAM}


def test_normalize_negotiates_like_the_encoder():
    df = pd.DataFrame({"a": [1]})
    assert normalize(df).content_type == "application/json"
    wire = normalize(df, ARROW_STREAM)
    assert (wire.content_type, wire.headers) == (ARROW_STREAM, {"vary": "Accept"})


# -- pyarrow unavailable ------------------------------------------------------


@pytest.fixture
def no_pyarrow(monkeypatch):
    """An interpreter where ``import pyarrow`` fails, as it does where
    pyarrow is not installed. The encoder's own table checks read
    ``sys.modules`` for it, so they see it absent too."""
    monkeypatch.setitem(sys.modules, "pyarrow", None)
    monkeypatch.setitem(sys.modules, "pyarrow.ipc", None)


def test_arrow_without_pyarrow_is_406(no_pyarrow):
    df = pd.DataFrame({"a": [1]})
    assert respond(df, ARROW_STREAM) == (WIRE_NOT_ACCEPTABLE, ARROW_UNAVAILABLE)
    wire = normalize(df, ARROW_STREAM)
    assert (wire.status, wire.headers) == (406, {"vary": "Accept"})
    assert json.loads(wire.content) == {"error": ARROW_UNAVAILABLE}
    # JSON rows for pandas never need pyarrow.
    assert json_rows(df) == [{"a": 1}]
    assert json_rows(pd.Series([1], name="s")) == [{"s": 1}]


@pytest.mark.parametrize(
    "accept",
    [
        f"{ARROW_STREAM}, application/json;q=0.5",
        f"{ARROW_STREAM}, application/*;q=0.2",
        f"{ARROW_STREAM}, */*;q=0.1",
        f"{ARROW_STREAM}, application/json",
    ],
)
def test_arrow_without_pyarrow_falls_back_when_json_is_accepted(no_pyarrow, accept):
    df = pd.DataFrame({"a": [1]})
    assert respond(df, accept) == (
        WIRE_NOTED_RESPONSE,
        200,
        "application/json",
        {"vary": "Accept"},
        b'[{"a": 1}]',
        ARROW_FELL_BACK,
    )
    wire = normalize(df, accept)
    assert (wire.status, wire.content, wire.headers) == (
        200,
        b'[{"a": 1}]',
        {"vary": "Accept"},
    )
    # The note survives a Response wrapper.
    assert respond(Response(body=df), accept)[5] == ARROW_FELL_BACK


@pytest.mark.parametrize(
    "accept",
    [
        ARROW_STREAM,
        f"{ARROW_STREAM}, text/html",
        f"{ARROW_STREAM}, application/json;q=0",
        # The most specific range covering JSON decides, and it says no.
        f"{ARROW_STREAM}, application/json;q=0, */*",
    ],
)
def test_arrow_without_pyarrow_is_406_when_only_arrow_is_accepted(no_pyarrow, accept):
    df = pd.DataFrame({"a": [1]})
    assert respond(df, accept) == (WIRE_NOT_ACCEPTABLE, ARROW_UNAVAILABLE)


def test_with_pyarrow_arrow_is_sent_and_nothing_is_noted():
    wire = respond(pd.DataFrame({"a": [1]}), f"{ARROW_STREAM}, */*;q=0.1")
    assert wire[0] == WIRE_RESPONSE and wire[2] == ARROW_STREAM


def test_a_stream_without_pyarrow(no_pyarrow):
    """A stream object needs pyarrow to be read at all: as JSON that is
    a bad return naming pyarrow, as Arrow the same 406."""
    stream = ArrowStream(None)
    wire = respond(stream)
    assert wire[0] == WIRE_REFUSED
    assert "ArrowStream is an Arrow stream, and reading one needs pyarrow" in wire[1]
    assert respond({"rows": stream})[1].startswith("$.rows: ArrowStream is an Arrow")
    assert respond(stream, ARROW_STREAM) == (WIRE_NOT_ACCEPTABLE, ARROW_UNAVAILABLE)


def _table_ws(config=None, isolation="none"):
    from nontainer.presets import dataframes

    ws = Workspace(
        KvgitProvider.open(None, session="tables"),
        python=PythonConfig(modules=[dataframes()], isolation=isolation),
    )
    ws.files.fs.makedirs("/workspace/app/api", exist_ok=True)
    ws.files.fs.write(
        "/workspace/app/api/t.py",
        b"import pandas as pd\ndef get(req):\n    return pd.DataFrame({'a': [1, 2]})\n",
    )
    return ws, enable_apps(ws, config)


def test_dispatch_answers_406_and_logs_it(no_pyarrow):
    # isolation="none" encodes in this interpreter, where pyarrow is
    # made unimportable.
    ws, rt = _table_ws()
    try:
        r = rt.dispatch(request("GET", "/api/t", headers={"accept": ARROW_STREAM}))
        assert (r.status, r.headers) == (406, {"vary": "Accept"})
        assert json.loads(r.content) == {"error": ARROW_UNAVAILABLE}
        log = ws.files.fs.read(LOG).decode()
        assert f"[t:get] NOT ACCEPTABLE: {ARROW_UNAVAILABLE}" in log
        assert "GET /api/t -> 406" in log
        r = rt.dispatch(request("GET", "/api/t"))
        assert (r.status, json.loads(r.content)) == (200, [{"a": 1}, {"a": 2}])
    finally:
        ws.close()


def test_dispatch_falls_back_to_json_and_logs_it(no_pyarrow):
    ws, rt = _table_ws()
    try:
        accept = f"{ARROW_STREAM}, application/json;q=0.5"
        r = rt.dispatch(request("GET", "/api/t?x=1", headers={"accept": accept}))
        assert (r.status, r.content_type, r.headers) == (
            200,
            "application/json",
            {"vary": "Accept"},
        )
        assert json.loads(r.content) == [{"a": 1}, {"a": 2}]
        log = ws.files.fs.read(LOG).decode()
        assert f"[t:get ?x=1] NOTE: {ARROW_FELL_BACK}" in log
        assert "GET /api/t?x=1 -> 200" in log
    finally:
        ws.close()


# -- text or binary -----------------------------------------------------------


@pytest.mark.parametrize(
    "content_type, text",
    [
        ("application/json", True),
        ("application/problem+json", True),
        ("application/atom+xml; charset=utf-8", True),
        ("text/plain; charset=utf-8", True),
        ("TEXT/HTML", True),
        ("text/csv", True),
        ("application/javascript", True),
        ("image/svg+xml", True),
        ("application/octet-stream", False),
        (ARROW_STREAM, False),
        ("image/png", False),
        ("", False),
    ],
)
def test_text_or_binary(content_type, text):
    assert is_text_type(content_type) is text


def test_ws_curl_uses_the_shared_rule():
    from nontainer.apps import wscurl

    assert wscurl.is_text_type is is_text_type


# -- size caps ----------------------------------------------------------------


def test_exactly_at_the_limit_passes():
    body = json.dumps({"s": "x" * 100}).encode()
    at = len(body)
    wire = respond({"s": "x" * 100}, text_limit=at, binary_limit=1)
    assert wire[0] == WIRE_RESPONSE
    wire = respond({"s": "x" * 100}, text_limit=at - 1, binary_limit=10**9)
    assert wire[0] == WIRE_REFUSED
    assert wire[1].startswith(f"JSON response is {at - 1 + 1} bytes, over the")
    assert respond(b"x" * 50, text_limit=1, binary_limit=50)[0] == WIRE_RESPONSE
    assert respond(b"x" * 51, text_limit=1, binary_limit=50)[0] == WIRE_REFUSED


def test_text_cap_message_steers_to_arrow():
    assert size_refusal(12_345_678, "application/json", 10_000_000, 32_000_000) == (
        "JSON response is 12.3 MB, over the 10 MB limit for text: aggregate or "
        "paginate on the server, or return a table and request Arrow (limit 32 MB)"
    )
    # Rounding never makes a body read as the size of its limit.
    assert size_refusal(10_000_001, "application/json", 10_000_000, 32_000_000) == (
        "JSON response is 10,000,001 bytes, over the 10 MB limit for text: "
        "aggregate or paginate on the server, or return a table and request "
        "Arrow (limit 32 MB)"
    )
    assert size_refusal(11_000_000, "text/html", 10_000_000, 32_000_000).startswith(
        "text/html response is 11 MB, over the 10 MB limit for text: "
    )


def test_binary_cap_message():
    assert size_refusal(40_000_000, ARROW_STREAM, 10_000_000, 32_000_000) == (
        "Arrow response is 40 MB, over the 32 MB limit for binary responses: "
        "filter or paginate on the server"
    )
    assert size_refusal(40_000_000, "", 10_000_000, 32_000_000).startswith(
        "Response is 40 MB, over the 32 MB limit for binary"
    )
    assert size_refusal(32_000_000, "image/png", 10_000_000, 32_000_000) is None


def test_the_carry_limit_is_named_when_it_binds():
    carry = 6_291_456
    assert size_refusal(8_000_000, "image/png", 10_000_000, 32_000_000, carry) == (
        "image/png response is 8 MB, over the 6.3 MB this executor can carry: "
        "filter or paginate on the server"
    )
    assert size_refusal(
        8_000_000, "application/json", 10_000_000, 32_000_000, carry
    ) == (
        "JSON response is 8 MB, over the 6.3 MB this executor can carry: "
        "aggregate or paginate on the server, or return a table and request "
        "Arrow (limit 6.3 MB)"
    )
    # A configured cap below the carry limit is the one named.
    assert "limit for text" in size_refusal(
        11_000_000, "application/json", 10_000_000, 32_000_000, 64 << 20
    )


def test_no_limit_is_no_limit():
    assert size_refusal(10**12, "application/json") is None
    assert respond(b"x" * 1000)[0] == WIRE_RESPONSE


def _capped_ws(isolation="none"):
    ws, rt = _table_ws(
        AppsConfig(max_response_bytes=100, max_binary_response_bytes=200),
        isolation=isolation,
    )
    ws.files.fs.write(
        "/workspace/app/api/big.py",
        b"def get(req):\n"
        b"    if req.params.get('kind') == 'bin':\n"
        b"        return b'x' * 201\n"
        b"    return {'s': 'x' * 200}\n",
    )
    # An encoder that ignores the limits: the host holds it to them.
    ws.files.fs.write(
        "/workspace/app/api/forged.py",
        b"class nt__Encoder:\n"
        b"    @staticmethod\n"
        b"    def respond(value, *args, **kwargs):\n"
        b"        return ('nt-response/1', 200, 'image/png', {}, b'x' * 300)\n"
        b"def get(req):\n"
        b"    return 'hi'\n",
    )
    return ws, rt


@pytest.mark.parametrize("isolation", ["none", "process"])
def test_dispatch_enforces_both_caps_and_logs(isolation):
    ws, rt = _capped_ws(isolation)
    try:
        r = rt.dispatch(request("GET", "/api/big"))
        text_msg = (
            "JSON response is 209 bytes, over the 100 bytes limit for text: "
            "aggregate or paginate on the server, or return a table and request "
            "Arrow (limit 200 bytes)"
        )
        assert (r.status, json.loads(r.content)) == (500, {"error": text_msg})
        r = rt.dispatch(request("GET", "/api/big?kind=bin"))
        bin_msg = (
            "application/octet-stream response is 201 bytes, over the 200 bytes "
            "limit for binary responses: filter or paginate on the server"
        )
        assert (r.status, json.loads(r.content)) == (500, {"error": bin_msg})
        r = rt.dispatch(request("GET", "/api/forged"))
        forged_msg = (
            "image/png response is 300 bytes, over the 200 bytes limit for "
            "binary responses: filter or paginate on the server"
        )
        assert (r.status, json.loads(r.content)) == (500, {"error": forged_msg})
        log = ws.files.fs.read(LOG).decode()
        assert f"[big:get] BAD RETURN: {text_msg}" in log
        assert f"[big:get ?kind=bin] BAD RETURN: {bin_msg}" in log
        assert f"[forged:get] BAD RETURN: {forged_msg}" in log
    finally:
        ws.close()


def test_static_files_are_capped_and_declared_assets_are_not(tmp_path):
    (tmp_path / "big.js").write_bytes(b"x" * 500)
    ws, rt = _table_ws(
        AppsConfig(
            max_response_bytes=100,
            max_binary_response_bytes=200,
            static_assets={"vendor": tmp_path},
        )
    )
    try:
        ws.files.fs.write("/workspace/app/data.json", b"[" + b"1," * 100 + b"1]")
        r = rt.dispatch(request("GET", "/data.json"))
        assert r.status == 500
        assert json.loads(r.content) == {
            "error": "JSON response is 203 bytes, over the 100 bytes limit for text"
        }
        r = rt.dispatch(request("GET", "/vendor/big.js"))
        assert (r.status, len(r.content)) == (200, 500)
    finally:
        ws.close()


def test_config_defaults():
    config = AppsConfig()
    assert config.max_response_bytes == 10_000_000
    assert config.max_binary_response_bytes == 32_000_000
