"""a2ui layer 1: splice semantics and per-kind component projection.

Pure functions — no workspace, no I/O. ``file_url`` is a trivial stub so the
fragments are fully deterministic.
"""

import json

from nontainer.adapters.a2ui import (
    BASIC_CATALOG,
    component_for,
    splice,
    turn_to_a2ui,
)


def url(path: str) -> str:
    return f"https://host{path}"


# -- splice ------------------------------------------------------------------


def test_splice_inline_ref_replaced_with_note_name():
    # alt-text "chart" but the note names it "revenue" -> note wins.
    segs = splice(
        "before ![chart](/ui/x.plotly.json) after",
        [("revenue", "/ui/x.plotly.json")],
    )
    assert segs == [
        ("md", "before "),
        ("artifact", "revenue", "/ui/x.plotly.json"),
        ("md", " after"),
    ]


def test_splice_unreferenced_artifact_appended():
    segs = splice("just prose", [("k", "/ui/k.cards.json")])
    assert segs == [("md", "just prose"), ("artifact", "k", "/ui/k.cards.json")]


def test_splice_ref_order_vs_note_order():
    # prose references b then a; c is unreferenced -> trailing in NOTE order.
    prose = "![b](/ui/b.png) mid ![a](/ui/a.png)"
    arts = [("a", "/ui/a.png"), ("b", "/ui/b.png"), ("c", "/ui/c.png")]
    segs = splice(prose, arts)
    assert segs == [
        ("artifact", "b", "/ui/b.png"),
        ("md", " mid "),
        ("artifact", "a", "/ui/a.png"),
        ("artifact", "c", "/ui/c.png"),
    ]


def test_splice_prose_only_no_refs():
    assert splice("hello world", []) == [("md", "hello world")]


def test_splice_artifacts_only_empty_prose():
    segs = splice("", [("a", "/ui/a.png"), ("b", "/ui/b.png")])
    assert segs == [
        ("artifact", "a", "/ui/a.png"),
        ("artifact", "b", "/ui/b.png"),
    ]


def test_splice_unknown_path_ref_still_splices():
    # a workspace image the agent embedded by hand, not in the note.
    segs = splice("see ![shot](/workspace/shot.png)", [])
    assert segs == [
        ("md", "see "),
        ("artifact", "shot", "/workspace/shot.png"),
    ]


def test_splice_same_path_twice_splices_each_not_appended():
    prose = "![x](/ui/x.png) and again ![x](/ui/x.png)"
    segs = splice(prose, [("x", "/ui/x.png")])
    assert segs == [
        ("artifact", "x", "/ui/x.png"),
        ("md", " and again "),
        ("artifact", "x", "/ui/x.png"),
    ]


def test_splice_no_empty_md_segments():
    # ref at the very start and very end -> no ("md", "") on either side.
    segs = splice("![a](/ui/a.png)![b](/ui/b.png)", [])
    assert segs == [
        ("artifact", "a", "/ui/a.png"),
        ("artifact", "b", "/ui/b.png"),
    ]


def test_splice_artifact_listed_twice_appends_once():
    segs = splice("", [("a", "/ui/a.png"), ("a2", "/ui/a.png")])
    assert segs == [("artifact", "a", "/ui/a.png")]


# -- component_for -----------------------------------------------------------


def test_component_cards():
    # A mixed row: a stat (with sublabel) and a callout carrying a tone.
    data = json.dumps(
        {
            "items": [
                {"type": "stat", "label": "Revenue", "value": 42,
                 "sublabel": "up 3"},
                {"type": "callout", "title": "Note", "body": "check it",
                 "tone": "warning"},
            ]
        }
    ).encode()
    frag = component_for("kpis", "/ui/kpis.cards.json", data, url)
    assert frag == {
        "component": {
            "componentType": "Row",
            "children": [
                {
                    "componentType": "Card",
                    "children": [
                        {"componentType": "Text", "text": "Revenue", "role": "label"},
                        {"componentType": "Text", "text": "42", "role": "value"},
                        {"componentType": "Text", "text": "up 3", "role": "sublabel"},
                    ],
                },
                {
                    "componentType": "Card",
                    "tone": "warning",
                    "children": [
                        {"componentType": "Text", "text": "Note", "role": "title"},
                        {"componentType": "Text", "text": "check it", "role": "body"},
                    ],
                },
            ],
        },
        "data_model": {},
    }


def test_component_cards_stat_without_sublabel_and_empty_callout():
    # Stat omits sublabel entirely; callout with empty body emits only title.
    data = json.dumps(
        {
            "items": [
                {"type": "stat", "label": "Users", "value": 1234},
                {"type": "callout", "title": "Just a title", "body": "",
                 "tone": "info"},
                {"type": "mystery"},  # unrecognized -> skipped
            ]
        }
    ).encode()
    frag = component_for("k", "/ui/k.cards.json", data, url)
    cards = frag["component"]["children"]
    assert len(cards) == 2  # the mystery item is dropped
    assert cards[0]["children"] == [
        {"componentType": "Text", "text": "Users", "role": "label"},
        {"componentType": "Text", "text": "1234", "role": "value"},
    ]
    assert cards[1] == {
        "componentType": "Card",
        "tone": "info",
        "children": [
            {"componentType": "Text", "text": "Just a title", "role": "title"},
        ],
    }


def test_component_table_with_cap_and_caption():
    rows = [[i, f"r{i}"] for i in range(60)]
    data = json.dumps(
        {"columns": ["n", "name"], "data": rows, "total": 200}
    ).encode()
    frag = component_for("t", "/ui/t.table.json", data, url)
    children = frag["component"]["children"]
    assert frag["component"]["componentType"] == "Column"
    # header + 50 capped rows + caption
    assert children[0]["children"][0] == {
        "componentType": "Text",
        "text": "n",
        "role": "header",
    }
    assert len(children) == 1 + 50 + 1
    assert children[-1] == {
        "componentType": "Text",
        "text": "showing 50 of 200 rows",
        "role": "caption",
    }


def test_component_table_no_caption_when_all_shown():
    data = json.dumps(
        {"columns": ["a"], "data": [[1], [2]], "total": 2}
    ).encode()
    frag = component_for("t", "/ui/t.table.json", data, url)
    # header + 2 rows, no caption
    assert len(frag["component"]["children"]) == 3
    assert frag["component"]["children"][-1]["children"][0]["role"] == "cell"


def test_component_plotly():
    spec = {"data": [{"x": [1], "y": [2]}], "layout": {"title": "hi"}}
    frag = component_for("fig", "/ui/fig.plotly.json", json.dumps(spec).encode(), url)
    assert frag == {
        "component": {"componentType": "Chart", "spec": {"$ref": "spec"}},
        "data_model": {"spec": spec},
    }


def test_component_json_sniffed_as_plotly():
    spec = {"data": [{"x": [1]}], "layout": {}}
    frag = component_for("fig", "/ui/fig.json", json.dumps(spec).encode(), url)
    assert frag["component"]["componentType"] == "Chart"
    assert frag["data_model"] == {"spec": spec}


def test_component_json_not_plotly_falls_back():
    data = json.dumps({"just": "data"}).encode()
    frag = component_for("blob", "/ui/blob.json", data, url)
    assert frag == {
        "component": {
            "componentType": "Text",
            "text": "artifact: blob",
            "link": "https://host/ui/blob.json",
        },
        "data_model": {},
    }


def test_component_image():
    frag = component_for("pic", "/ui/pic.png", b"\x89PNG", url)
    assert frag == {
        "component": {"componentType": "Image", "url": "https://host/ui/pic.png"},
        "data_model": {},
    }


def test_component_text_fallback():
    frag = component_for("page", "/ui/page.html", b"<h1>hi</h1>", url)
    assert frag == {
        "component": {
            "componentType": "Text",
            "text": "artifact: page",
            "link": "https://host/ui/page.html",
        },
        "data_model": {},
    }


def test_component_data_none_degrades_but_image_still_works():
    # bytes-needing kind with no bytes -> fallback link.
    frag = component_for("fig", "/ui/fig.plotly.json", None, url)
    assert frag["component"]["componentType"] == "Text"
    assert frag["component"]["link"] == "https://host/ui/fig.plotly.json"
    # image is URL-only, so it still renders.
    img = component_for("pic", "/ui/pic.png", None, url)
    assert img["component"]["componentType"] == "Image"


def test_component_malformed_json_degrades():
    frag = component_for("t", "/ui/t.table.json", b"{not json", url)
    assert frag["component"]["componentType"] == "Text"
    assert frag["component"]["link"] == "https://host/ui/t.table.json"


def test_component_table_nonlist_fields_degrade_not_raise():
    """The never-raises contract against agent-written near-misses: table
    payloads are reachable via direct /ui writes (the adoption path), so a
    truthy non-list columns/data must degrade — a header-less table — not
    TypeError out of an egress stream (PR #14 review)."""
    frag = component_for(
        "t", "/ui/t.table.json", b'{"columns": 5, "data": [[1]]}', url
    )
    children = frag["component"]["children"]
    assert children[0] == {"componentType": "Row", "children": []}  # no header
    assert children[1]["children"][0]["text"] == "1"  # rows still render
    # non-list data degrades the same way (empty body, headers intact)
    frag = component_for(
        "t", "/ui/t.table.json", b'{"columns": ["a"], "data": 7}', url
    )
    assert len(frag["component"]["children"]) == 1  # header row only


def test_component_builder_surprise_falls_back_not_raises():
    """Belt and braces: ANY builder exception lands in the Text+link
    fallback — the docstring's never-raises is structural, not by audit.
    A cards payload whose items explode the builder is the probe."""
    frag = component_for(
        "k", "/ui/k.cards.json", b'{"items": [{"type": "stat"}]}', url
    )
    # missing label/value: builder renders empty-string Texts today, but
    # whatever future shape appears, the call must return a fragment
    assert "component" in frag and "data_model" in frag


# -- turn_to_a2ui (layer 2, the v0.9 envelope) -------------------------------


def _reader(files: dict[str, bytes]):
    """A read_bytes callable backed by an in-memory {path: bytes} map."""
    return lambda path: files.get(path)


def test_turn_golden_full_reply():
    # One inline plotly ref + one unreferenced cards artifact (appended).
    spec = {"data": [{"x": [1], "y": [2]}], "layout": {"title": "hi"}}
    cards = {
        "items": [
            {"type": "stat", "label": "Revenue", "value": 42, "sublabel": "up 3"}
        ]
    }
    files = {
        "/ui/fig.plotly.json": json.dumps(spec).encode(),
        "/ui/kpis.cards.json": json.dumps(cards).encode(),
    }
    msgs = turn_to_a2ui(
        "Here is the chart ![fig](/ui/fig.plotly.json) and metrics below.",
        [("fig", "/ui/fig.plotly.json"), ("kpis", "/ui/kpis.cards.json")],
        _reader(files),
        url,
        surface_id="s1",
    )
    assert msgs == [
        {
            "version": "v0.9",
            "createSurface": {"surfaceId": "s1", "catalogId": BASIC_CATALOG},
        },
        {
            "version": "v0.9",
            "updateComponents": {
                "surfaceId": "s1",
                "components": [
                    {
                        "id": "root",
                        "component": "Column",
                        "children": ["seg0", "seg1", "seg2", "seg3"],
                    },
                    {
                        "id": "seg0",
                        "component": "Text",
                        "text": "Here is the chart ",
                    },
                    {
                        "id": "seg1",
                        "component": "Chart",
                        "spec": {"path": "/artifacts/fig/spec"},
                    },
                    {
                        "id": "seg2",
                        "component": "Text",
                        "text": " and metrics below.",
                    },
                    {
                        "id": "seg3",
                        "component": "Row",
                        "children": ["seg3-1"],
                    },
                    {
                        "id": "seg3-1",
                        "component": "Card",
                        "children": [
                            "seg3-1-label-1",
                            "seg3-1-value-1",
                            "seg3-1-sublabel-1",
                        ],
                    },
                    {"id": "seg3-1-label-1", "component": "Text", "text": "Revenue"},
                    {"id": "seg3-1-value-1", "component": "Text", "text": "42"},
                    {"id": "seg3-1-sublabel-1", "component": "Text", "text": "up 3"},
                ],
            },
        },
        {
            "version": "v0.9",
            "updateDataModel": {
                "surfaceId": "s1",
                "path": "/artifacts/fig/spec",
                "value": spec,
            },
        },
    ]


def test_turn_callout_tone_survives_flattening():
    # A mixed stat + callout row: the callout's `tone` is an unknown prop
    # on the Card, so the flattener must pass it through onto the emitted
    # component (folded id keeps stat and callout cards distinct).
    cards = {
        "items": [
            {"type": "stat", "label": "Revenue", "value": 42},
            {"type": "callout", "title": "Heads up", "body": "check", "tone": "warning"},
        ]
    }
    files = {"/ui/dash.cards.json": json.dumps(cards).encode()}
    msgs = turn_to_a2ui(
        "", [("dash", "/ui/dash.cards.json")], _reader(files), url, surface_id="s1"
    )
    comps = msgs[1]["updateComponents"]["components"]
    by_id = {c["id"]: c for c in comps}
    # The row (seg0) holds two Cards; the callout Card carries tone.
    row = by_id["seg0"]
    stat_card, callout_card = (by_id[cid] for cid in row["children"])
    assert stat_card["component"] == "Card" and "tone" not in stat_card
    assert callout_card["component"] == "Card"
    assert callout_card["tone"] == "warning"


def test_turn_deterministic():
    files = {"/ui/fig.plotly.json": json.dumps({"data": [], "layout": {}}).encode()}
    args = (
        "see ![fig](/ui/fig.plotly.json)",
        [("fig", "/ui/fig.plotly.json")],
        _reader(files),
        url,
    )
    a = turn_to_a2ui(*args, surface_id="s1")
    b = turn_to_a2ui(*args, surface_id="s1")
    assert a == b


def test_turn_empty_reply_is_valid_empty_surface():
    msgs = turn_to_a2ui("", [], lambda _p: None, url, surface_id="s1")
    # createSurface + one updateComponents with an empty root; no data model.
    assert msgs == [
        {
            "version": "v0.9",
            "createSurface": {"surfaceId": "s1", "catalogId": BASIC_CATALOG},
        },
        {
            "version": "v0.9",
            "updateComponents": {
                "surfaceId": "s1",
                "components": [{"id": "root", "component": "Column", "children": []}],
            },
        },
    ]


def test_turn_ids_stable_and_unique():
    # A table's header Row has several role: header cells -> per-role
    # occurrence keeps their ids unique.
    table = {"columns": ["a", "b", "c"], "data": [[1, 2, 3]], "total": 1}
    files = {"/ui/t.table.json": json.dumps(table).encode()}
    msgs = turn_to_a2ui(
        "", [("t", "/ui/t.table.json")], _reader(files), url, surface_id="s1"
    )
    comps = msgs[1]["updateComponents"]["components"]
    ids = [c["id"] for c in comps]
    assert len(ids) == len(set(ids))  # unique
    header = next(c for c in comps if c["id"] == "seg0-1")
    assert header["children"] == ["seg0-1-header-1", "seg0-1-header-2", "seg0-1-header-3"]
    # role prop is dropped from the emitted components.
    assert all("role" not in c for c in comps)


def test_turn_version_on_every_message():
    files = {"/ui/fig.plotly.json": json.dumps({"data": [], "layout": {}}).encode()}
    msgs = turn_to_a2ui(
        "text ![fig](/ui/fig.plotly.json)",
        [("fig", "/ui/fig.plotly.json")],
        _reader(files),
        url,
        surface_id="s1",
    )
    assert all(m["version"] == "v0.9" for m in msgs)
    # createSurface, updateComponents, updateDataModel all present.
    assert [next(k for k in m if k != "version") for m in msgs] == [
        "createSurface",
        "updateComponents",
        "updateDataModel",
    ]


def test_turn_never_raises_on_unreadable():
    def boom(_path):
        raise OSError("disk gone")

    msgs = turn_to_a2ui(
        "![fig](/ui/fig.plotly.json)",
        [("fig", "/ui/fig.plotly.json")],
        boom,
        url,
        surface_id="s1",
    )
    # Degrades to the Text+link fallback, no crash, no data model.
    comp = msgs[1]["updateComponents"]["components"][1]
    assert comp["component"] == "Text"
    assert comp["link"] == "https://host/ui/fig.plotly.json"
    assert len(msgs) == 2
