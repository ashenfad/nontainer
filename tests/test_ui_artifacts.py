"""The `ui = {...}` rich-reply convention: namespace values become
workspace artifacts under /ui/, sniffed down a theming hierarchy
(spec formats > pixels > html > data), never silently dropped.
"""

import json

import pytest

from nontainer import Workspace
from nontainer.adapters.render import materialize_ui
from nontainer.providers import KvgitProvider


@pytest.fixture
def ws():
    w = Workspace(KvgitProvider.open(None, session="ui"))
    yield w
    w.close()


def test_plotly_figure_becomes_spec(ws):
    plotly = pytest.importorskip("plotly.graph_objects")
    fig = plotly.Figure(data=[plotly.Scatter(x=[1, 2], y=[3, 4])])
    out = materialize_ui(ws, {"trend": fig})
    assert out == [("trend", "/ui/trend.plotly.json")]
    spec = json.loads(ws.fs.read("/ui/trend.plotly.json"))
    assert spec["data"][0]["x"] == [1, 2]  # the SPEC, not baked output


def test_dataframe_becomes_capped_table(ws):
    pd = pytest.importorskip("pandas")
    df = pd.DataFrame({"a": range(500), "b": range(500)})
    out = materialize_ui(ws, {"rows": df})
    assert out == [("rows", "/ui/rows.table.json")]
    table = json.loads(ws.fs.read("/ui/rows.table.json"))
    assert table["columns"] == ["a", "b"]
    assert len(table["data"]) == 200  # capped...
    assert table["total"] == 500  # ...and the cap announces itself


def test_matplotlib_figure_becomes_png(ws):
    plt = pytest.importorskip("matplotlib.pyplot")
    fig, ax = plt.subplots()
    ax.plot([1, 2], [3, 4])
    out = materialize_ui(ws, {"chart": fig})
    plt.close(fig)
    assert out == [("chart", "/ui/chart.png")]
    assert ws.fs.read("/ui/chart.png")[:8] == b"\x89PNG\r\n\x1a\n"


def test_bytes_and_html_and_json_tiers(ws):
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d494844520000000100000001080200000090"
        "7753de0000000c49444154089963f8cfc000000301010018dd8db0000000"
        "0049454e44ae426082"
    )

    class Widget:
        def _repr_html_(self):
            return "<b>hi</b>"

    out = dict(
        materialize_ui(
            ws,
            {
                "shot": png,
                "widget": Widget(),
                "stats": {"mean": 2.5},
                "blob": b"\x00\x01\x02",
            },
        )
    )
    assert out["shot"] == "/ui/shot.png"
    assert out["widget"] == "/ui/widget.html"
    assert ws.fs.read("/ui/widget.html") == b"<b>hi</b>"
    assert json.loads(ws.fs.read("/ui/stats.json")) == {"mean": 2.5}
    assert out["blob"] == "/ui/blob.bin"


def test_unrenderable_lands_as_repr_not_silence(ws):
    class Cursed:
        def _repr_html_(self):
            raise RuntimeError("nope")

        def __repr__(self):
            return "<Cursed>"

    out = materialize_ui(ws, {"x": Cursed()})
    assert out == [("x", "/ui/x.txt")]
    assert ws.fs.read("/ui/x.txt") == b"<Cursed>"


def test_name_sanitization_and_non_dict(ws):
    out = materialize_ui(ws, {"my plot / v2": {"a": 1}})
    assert out == [("my plot / v2", "/ui/my-plot-v2.json")]
    assert materialize_ui(ws, "not a dict") == []
    assert materialize_ui(ws, None) == []


def test_agno_run_python_notes_ui_artifacts():
    pytest.importorskip("agno")
    from nontainer.adapters.agno import WorkspaceTools

    ws = Workspace(KvgitProvider.open(None, session="ui-agno"))
    tk = WorkspaceTools(ws)
    out = tk.functions["run_python"].entrypoint(code="ui = {'stats': {'n': 3}}")
    assert "[ui artifacts: stats -> /ui/stats.json]" in out
    assert json.loads(ws.fs.read("/ui/stats.json")) == {"n": 3}
    # and the tool description teaches the convention
    assert "ui = " in (tk.functions["run_python"].entrypoint.__doc__ or "")
    ws.close()
