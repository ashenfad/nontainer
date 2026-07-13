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
    out, _ = materialize_ui(ws, {"trend": fig})
    assert out == [("trend", "/ui/trend.plotly.json")]
    spec = json.loads(ws.fs.read("/ui/trend.plotly.json"))
    assert spec["data"][0]["x"] == [1, 2]  # the SPEC, not baked output


def test_dataframe_becomes_capped_table(ws):
    pd = pytest.importorskip("pandas")
    df = pd.DataFrame({"a": range(500), "b": range(500)})
    out, _ = materialize_ui(ws, {"rows": df})
    assert out == [("rows", "/ui/rows.table.json")]
    table = json.loads(ws.fs.read("/ui/rows.table.json"))
    assert table["columns"] == ["a", "b"]
    assert len(table["data"]) == 200  # capped...
    assert table["total"] == 500  # ...and the cap announces itself


def test_matplotlib_figure_becomes_png(ws):
    plt = pytest.importorskip("matplotlib.pyplot")
    fig, ax = plt.subplots()
    ax.plot([1, 2], [3, 4])
    out, _ = materialize_ui(ws, {"chart": fig})
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
        )[0]
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

    out, _ = materialize_ui(ws, {"x": Cursed()})
    assert out == [("x", "/ui/x.txt")]
    assert ws.fs.read("/ui/x.txt") == b"<Cursed>"


def test_name_sanitization_and_non_dict(ws):
    out, _ = materialize_ui(ws, {"my plot / v2": {"a": 1}})
    assert out == [("my plot / v2", "/ui/my-plot-v2.json")]
    assert materialize_ui(ws, "not a dict") == ([], [])
    assert materialize_ui(ws, None) == ([], [])


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


def test_agno_run_python_adopts_direct_ui_writes():
    """The near-miss one step further out: agents write INTO /ui
    themselves (fig.write_json('/ui/x.json')) instead of assigning
    objects to `ui`. New files the call created join the artifacts
    note — without it they display nowhere; materialized values are
    not double-listed."""
    pytest.importorskip("agno")
    from nontainer.adapters.agno import WorkspaceTools

    ws = Workspace(KvgitProvider.open(None, session="ui-adopt"))
    ws.fs.makedirs("/ui", exist_ok=True)
    tk = WorkspaceTools(ws)
    out = tk.functions["run_python"].entrypoint(
        code=(
            "with open('/ui/chart.json', 'w') as f:\n"
            "    f.write('{\"data\": [], \"layout\": {}}')\n"
            "ui = {'stats': {'n': 3}}"
        )
    )
    assert "[ui artifacts:" in out
    assert "chart.json -> /ui/chart.json" in out
    assert out.count("/ui/stats.json") == 1  # materialized, not re-adopted
    ws.close()


def test_string_path_to_existing_file_passes_through(ws):
    """The near-miss: the agent saved the file itself (savefig) and put
    its PATH in `ui`. A pointer, not content — honor it as-is."""
    ws.fs.makedirs("/ui", exist_ok=True)
    ws.fs.write("/ui/plot.png", b"\x89PNG\r\n\x1a\nfake")
    out, _ = materialize_ui(ws, {"plot": "/ui/plot.png"})
    assert out == [("plot", "/ui/plot.png")]
    # untouched: no re-encode, no sidecar artifact
    assert ws.fs.read("/ui/plot.png") == b"\x89PNG\r\n\x1a\nfake"


def test_string_path_to_missing_file_falls_to_data_tier(ws):
    out, _ = materialize_ui(ws, {"ghost": "/nope/missing.png"})
    assert out == [("ghost", "/ui/ghost.json")]
    assert json.loads(ws.fs.read("/ui/ghost.json")) == "/nope/missing.png"


def test_plain_strings_stay_data(ws):
    """Only rooted paths get the pointer treatment — ordinary prose
    strings still land as json artifacts."""
    out, _ = materialize_ui(ws, {"note": "all done"})
    assert out == [("note", "/ui/note.json")]


def test_oversize_value_reports_why(ws):
    """The 8MB cap must produce a DIAGNOSIS, not a silent repr: the
    problems note reaches the agent (self-correct), and the .txt slot
    shows the human why there's no figure."""
    big = b"\x00" + b"x" * 9_000_000  # non-image magic, over the cap
    out, problems = materialize_ui(ws, {"blob": big})
    assert out == [("blob", "/ui/blob.txt")]
    (problem,) = problems
    assert "NOT rendered: too large" in problem and "9.0MB > 8MB" in problem
    assert ws.fs.read("/ui/blob.txt").decode() == problem


def test_oversize_plotly_gets_the_customdata_hint(ws):
    """The 280k-point map lesson: coordinates are cheap (binary bdata),
    per-point hover strings are what blow the cap — the note must steer
    there, not at the point count."""
    plotly = pytest.importorskip("plotly.graph_objects")
    fig = plotly.Figure(
        data=[
            plotly.Scatter(
                x=[0],
                y=[0],
                customdata=[["y" * 9_000_000]],  # the usual culprit, distilled
            )
        ]
    )
    out, problems = materialize_ui(ws, {"map": fig})
    assert out == [("map", "/ui/map.txt")]
    (problem,) = problems
    assert "customdata" in problem and "scattergl" in problem
