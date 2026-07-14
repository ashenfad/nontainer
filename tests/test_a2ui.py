"""a2ui layer 1: splice semantics and per-kind component projection.

Pure functions — no workspace, no I/O. ``file_url`` is a trivial stub so the
fragments are fully deterministic.
"""

import json

from nontainer.adapters.a2ui import component_for, splice


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
    data = json.dumps(
        {"items": [{"label": "Revenue", "value": 42, "delta": "+3", "unit": "$"}]}
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
                        {"componentType": "Text", "text": "+3", "role": "delta"},
                        {"componentType": "Text", "text": "$", "role": "unit"},
                    ],
                }
            ],
        },
        "data_model": {},
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
