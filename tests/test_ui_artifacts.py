"""The `ui = {...}` rich-reply convention: namespace values become
workspace artifacts under /ui/, sniffed down a theming hierarchy
(spec formats > pixels > html > data), never silently dropped.
"""

import json

import pytest

from nontainer import Workspace
from nontainer.adapters.render import (
    artifact_kind,
    artifacts_note,
    materialize_ui,
    parse_artifacts_note,
)
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
    # the returned name is the SANITIZED one — it rides the artifacts
    # note verbatim, so a raw name with ", " or " -> " must never leak
    out, _ = materialize_ui(ws, {"my plot / v2": {"a": 1}})
    assert out == [("my-plot-v2", "/ui/my-plot-v2.json")]
    assert materialize_ui(ws, "not a dict") == ([], [])
    assert materialize_ui(ws, None) == ([], [])


def test_cards_stats_and_callouts_normalize(ws):
    """A mixed row of stats (tagged and untagged) and a tagged callout
    materializes to /ui/<name>.cards.json as {"items": [...]}, each item
    normalized to its canonical shape."""
    cards = [
        {"type": "stat", "label": "Revenue", "value": 42000, "sublabel": "up 8%"},
        {"label": "Users", "value": 1234},  # untagged stat
        {"type": "callout", "title": "Heads up", "body": "check inputs",
         "tone": "warning"},
    ]
    out, _ = materialize_ui(ws, {"dash": cards})
    assert out == [("dash", "/ui/dash.cards.json")]
    payload = json.loads(ws.fs.read("/ui/dash.cards.json"))
    assert payload == {
        "items": [
            {"type": "stat", "label": "Revenue", "value": 42000, "sublabel": "up 8%"},
            {"type": "stat", "label": "Users", "value": 1234},
            {"type": "callout", "title": "Heads up", "body": "check inputs",
             "tone": "warning"},
        ]
    }


def test_cards_callout_tone_clamps_and_defaults(ws):
    """Unknown/absent tones clamp to "info" — the renderer must never
    invent sentiment. Missing title/body become empty strings."""
    cards = [
        {"type": "callout", "title": "A", "body": "b", "tone": "chartreuse"},
        {"type": "callout", "body": "no title"},  # tone absent, title missing
    ]
    materialize_ui(ws, {"notes": cards})
    payload = json.loads(ws.fs.read("/ui/notes.cards.json"))
    assert payload == {
        "items": [
            {"type": "callout", "title": "A", "body": "b", "tone": "info"},
            {"type": "callout", "title": "", "body": "no title", "tone": "info"},
        ]
    }


def test_cards_normalization_drops_unknown_and_coerces_label(ws):
    """Unknown keys are dropped and the label is stringified — the shape
    that reaches the renderer is exactly {type, label, value, sublabel?}."""
    cards = [{"label": 2024, "value": 10, "color": "red", "footnote": "x"}]
    materialize_ui(ws, {"stat": cards})
    payload = json.loads(ws.fs.read("/ui/stat.cards.json"))
    assert payload == {"items": [{"type": "stat", "label": "2024", "value": 10}]}


def test_cards_legacy_delta_and_unit_fold(ws):
    """Legacy forgiveness: a stat with no sublabel folds `delta` into
    sublabel, and `unit` appends onto the value."""
    cards = [{"label": "Revenue", "value": 42000, "delta": "+8%", "unit": " USD"}]
    materialize_ui(ws, {"kpis": cards})
    payload = json.loads(ws.fs.read("/ui/kpis.cards.json"))
    assert payload == {
        "items": [
            {"type": "stat", "label": "Revenue", "value": "42000 USD",
             "sublabel": "+8%"},
        ]
    }


def test_cards_explicit_sublabel_beats_legacy_delta(ws):
    """When both are present, the explicit sublabel wins and delta is
    dropped as an unknown key."""
    cards = [{"label": "R", "value": 1, "sublabel": "real", "delta": "+9%"}]
    materialize_ui(ws, {"k": cards})
    payload = json.loads(ws.fs.read("/ui/k.cards.json"))
    assert payload == {"items": [{"type": "stat", "label": "R", "value": 1,
                                  "sublabel": "real"}]}


def test_cards_survive_non_json_scalars(ws):
    """Stat values are routinely numpy scalars (df.sum()) — anything
    json.dumps rejects must degrade to a string tile via default=str,
    never bounce the whole row to the repr fallback."""
    from decimal import Decimal

    cards = [{"label": "revenue", "value": Decimal("12.5")}]
    out, _ = materialize_ui(ws, {"kpi": cards})
    assert out == [("kpi", "/ui/kpi.cards.json")]
    payload = json.loads(ws.fs.read("/ui/kpi.cards.json"))
    assert payload["items"] == [{"type": "stat", "label": "revenue", "value": "12.5"}]


def test_bare_card_list_adopts_default_envelope(ws):
    """Envelope forgiveness (the newstuff lesson): `ui = [<stats>]` —
    perfect items, missing dict wrapper — adopts as {"cards": ...}
    instead of rendering nothing."""
    out, problems = materialize_ui(
        ws,
        [
            {"label": "Revenue", "value": "$1.2M", "sublabel": "+8% MoM"},
            {"type": "callout", "title": "Note", "body": "n=42"},
        ],
    )
    assert out == [("cards", "/ui/cards.cards.json")]
    assert problems == []
    payload = json.loads(ws.fs.read("/ui/cards.cards.json"))
    assert [i["type"] for i in payload["items"]] == ["stat", "callout"]


def test_bare_non_card_list_still_renders_nothing(ws):
    assert materialize_ui(ws, [1, 2, 3]) == ([], [])
    assert materialize_ui(ws, []) == ([], [])


def test_bare_near_miss_list_gets_a_problem_note(ws):
    """A bare list where MOST items are cards but one isn't: nothing
    materializes (adoption stays strict), but the problems channel names
    the offending item — the dict-native constructor error."""
    out, problems = materialize_ui(
        ws,
        [
            {"label": "A", "value": 1},
            {"label": "B", "value": 2},
            {"label": "C"},  # no value: breaks the row
        ],
    )
    assert out == []
    assert len(problems) == 1
    assert "looks like a card row" in problems[0]
    assert "'label': 'C'" in problems[0]


def test_named_near_miss_list_notes_and_lands_on_json_floor(ws):
    """A NAMED almost-cards value still materializes (JSON floor) so the
    human sees something, and the note tells the agent which item to fix."""
    out, problems = materialize_ui(
        ws, {"kpis": [{"label": "A", "value": 1}, {"labl": "B", "value": 2}]}
    )
    assert out == [("kpis", "/ui/kpis.json")]
    assert len(problems) == 1
    assert "'kpis' looks like a card row" in problems[0]
    assert "'labl': 'B'" in problems[0]


def test_near_miss_note_is_bounded_for_huge_items(ws):
    """The offending-item preview rides reprobate's hard budget, so a
    pathological value (a huge string, a dict holding one) can't balloon
    the problems note — or the memory it takes to build it."""
    _, problems = materialize_ui(
        ws,
        {"kpis": [{"label": "A", "value": 1}, {"labl": "x" * 1_000_000}]},
    )
    assert len(problems) == 1
    assert len(problems[0]) < 600


def test_minority_match_list_is_not_diagnosed(ws):
    """A list where card-shaped dicts are the MINORITY isn't plausibly a
    card row — no note, plain JSON floor."""
    out, problems = materialize_ui(
        ws, {"stuff": [{"label": "A", "value": 1}, 2, 3]}
    )
    assert out == [("stuff", "/ui/stuff.json")]
    assert problems == []


def test_ui_note_shows_the_envelope():
    """The teaching example must carry the dict wrapper — the misread
    that caused the newstuff miss."""
    from nontainer.adapters.render import PYTHON_UI_NOTE

    assert 'ui = {"kpis": [{"label"' in PYTHON_UI_NOTE


def test_cards_cap_at_24(ws):
    """The row is capped: 25 tiles in, 24 out."""
    cards = [{"label": f"m{n}", "value": n} for n in range(25)]
    materialize_ui(ws, {"wall": cards})
    payload = json.loads(ws.fs.read("/ui/wall.cards.json"))
    assert len(payload["items"]) == 24
    assert payload["items"][-1] == {"type": "stat", "label": "m23", "value": 23}


@pytest.mark.parametrize(
    "value",
    [
        [],  # empty list: no cards to render
        [{"label": "a", "value": 1}, {"not": "a card"}],  # one bad element
        [{"label": "a"}],  # stat missing "value"
        [{"type": "callout"}],  # callout with neither title nor body
        [{"label": "a", "value": 1}, "plain"],  # a non-dict element
    ],
)
def test_cards_near_miss_falls_to_json_floor(ws, value):
    """A single element that is neither a stat nor a tagged callout sends
    the WHOLE list through to the generic JSON data tier — the convention
    never half-renders."""
    out, _ = materialize_ui(ws, {"x": value})
    assert out == [("x", "/ui/x.json")]
    assert json.loads(ws.fs.read("/ui/x.json")) == value


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


@pytest.mark.parametrize(
    "path,kind",
    [
        ("/ui/x.plotly.json", "plotly"),  # compound suffix beats bare .json
        ("/ui/x.json", "json"),
        ("/ui/x.table.json", "table"),
        ("/ui/x.cards.json", "cards"),  # phase 2 mapping, blessed early
        ("/ui/x.png", "image"),
        ("/ui/x.JPG", "image"),  # suffix match is case-insensitive
        ("/ui/x.jpeg", "image"),
        ("/ui/x.gif", "image"),
        ("/ui/x.webp", "image"),
        ("/ui/x.html", "html"),
        ("/ui/x.txt", "text"),
        ("/ui/x.bin", "binary"),
        ("/ui/x", "binary"),
    ],
)
def test_artifact_kind_dispatch(path, kind):
    assert artifact_kind(path) == kind


def test_artifacts_note_round_trip():
    """The note is a blessed contract: builder -> parser is lossless,
    even mid-string and even for names that needed sanitizing."""
    pairs = [
        ("trend", "/ui/trend.plotly.json"),
        ("my-plot-v2", "/ui/my-plot-v2.json"),  # already sanitized upstream
        ("shot", "/ui/shot.png"),
    ]
    base = "ok\nstdout: done"
    assert parse_artifacts_note(base + artifacts_note(pairs)) == pairs

    # single artifact
    one = [("stats", "/ui/stats.json")]
    assert parse_artifacts_note(artifacts_note(one)) == one


def test_artifacts_note_absent_returns_empty():
    assert artifacts_note([]) == ""
    assert parse_artifacts_note("just some tool output, no note here") == []
    assert parse_artifacts_note("") == []


def test_parse_tolerates_trailing_ui_note_lines():
    """The artifacts note is appended before the [ui note: ...] problem
    lines — the parser must not swallow those into the last path."""
    pairs = [("map", "/ui/map.txt")]
    text = (
        "render output"
        + artifacts_note(pairs)
        + "\n[ui note: 'map' NOT rendered: too large (9.0MB > 8MB cap).]"
    )
    assert parse_artifacts_note(text) == pairs


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
