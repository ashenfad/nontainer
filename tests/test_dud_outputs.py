"""The guest-side ``ui`` flattener dud calls through ``outputs_hook``.

This file exists because its absence cost a silent regression. dud 0.4.0
moved rich-value flattening out of the guest and into a hook the host
names; nothing in nontainer covered a rich value crossing a dud
boundary, so the whole suite passed against a dud that had stopped
flattening entirely. The symptom was not a missing artifact — it was
``namespace["ui"]`` coming back ``None``, because one DataFrame makes
the whole dict unrepresentable and takes the plain strings beside it
down too.
"""

from __future__ import annotations

import json

import pytest

from nontainer.dud_outputs import flatten


class _FakeFigure:
    """Duck-typed as plotly: detection is module name + method, so a
    stand-in needs no plotly installed."""

    __module__ = "plotly.graph_objs._figure"

    def __init__(self, payload="{}"):
        self._payload = payload

    def to_json(self):
        return self._payload


def test_a_non_dict_ui_is_left_alone(tmp_path):
    """`ui` is a convention, not a requirement. An agent that binds it
    to a string has not asked for anything."""
    harvest = {"ui": "not a dict", "x": 1}
    assert flatten(harvest, str(tmp_path)) == set()
    assert harvest == {"ui": "not a dict", "x": 1}


def test_only_ui_is_touched(tmp_path):
    """The hook is handed EVERY binding. A bare top-level figure is the
    agent's variable, not a declared output — turning it into an
    artifact would be this hook inventing a convention."""
    harvest = {"fig": _FakeFigure(), "n": 3}
    assert flatten(harvest, str(tmp_path)) == set()
    assert not (tmp_path / "ui").exists()


def test_a_rich_value_becomes_a_file_and_the_rest_still_crosses(tmp_path):
    """The regression, pinned. Before the hook was wired, one rich value
    made the entire `ui` binding unrepresentable and it vanished whole."""
    harvest = {"ui": {"chart": _FakeFigure('{"data":[]}'), "note": "plain"}}
    assert flatten(harvest, str(tmp_path)) == set()  # `ui` not fully consumed
    assert harvest["ui"] == {"note": "plain"}, "the representable remainder"
    written = (tmp_path / "ui" / "chart.plotly.json").read_bytes()
    assert json.loads(written) == {"data": []}


def test_a_fully_rich_ui_is_consumed(tmp_path):
    """Nothing left to cross, so the name is dropped rather than
    crossing as an empty dict."""
    harvest = {"ui": {"a": _FakeFigure(), "b": _FakeFigure()}}
    assert flatten(harvest, str(tmp_path)) == {"ui"}


def test_a_plain_ui_is_not_rewritten(tmp_path):
    """No rich values means no files and no edit — the host renderer
    owns every shape that can cross, and duplicating its tiers here
    would be two implementations of one contract."""
    ui = {"stats": [{"label": "n", "value": 1}], "note": "hi"}
    harvest = {"ui": ui}
    assert flatten(harvest, str(tmp_path)) == set()
    assert harvest["ui"] is ui  # same object, untouched
    assert not (tmp_path / "ui").exists()


def test_a_serializer_that_raises_leaves_its_value_alone(tmp_path):
    """Third-party serializers raise anything. One bad value must not
    lose the others."""

    class _Exploding(_FakeFigure):
        def to_json(self):
            raise RuntimeError("boom")

    harvest = {"ui": {"bad": _Exploding(), "good": _FakeFigure('{"ok":1}')}}
    assert flatten(harvest, str(tmp_path)) == set()
    assert set(harvest["ui"]) == {"bad"}
    assert (tmp_path / "ui" / "good.plotly.json").exists()


def test_an_oversized_artifact_writes_a_note_and_still_consumes(tmp_path):
    """Returning the value to `ui` instead would put a live object back
    in the binding, which makes the whole thing unrepresentable and
    silently takes its siblings with it. A rendered explanation beats
    losing the dict."""
    huge = _FakeFigure('{"d":"' + "x" * 9_000_000 + '"}')
    harvest = {"ui": {"big": huge, "note": "keep me"}}
    assert flatten(harvest, str(tmp_path)) == set()
    assert harvest["ui"] == {"note": "keep me"}
    note = json.loads((tmp_path / "ui" / "big.json").read_bytes())
    assert "too large" in note["error"]
    assert "customdata" in note["error"], "plotly gets the plotly advice"
    assert not (tmp_path / "ui" / "big.plotly.json").exists()


def test_a_dataframe_matches_what_the_host_renderer_writes(tmp_path):
    """Parity is the point: the same figure must render the same way
    whichever rung produced it, so the guest copy carries `total` and
    `columnTypes` exactly as `adapters/render.py` does."""
    pd = pytest.importorskip("pandas")

    frame = pd.DataFrame({"n": [1, 2, 3], "s": ["a", "b", "c"]})
    harvest = {"ui": {"table": frame}}
    assert flatten(harvest, str(tmp_path)) == {"ui"}

    payload = json.loads((tmp_path / "ui" / "table.table.json").read_bytes())
    assert payload["total"] == 3
    assert payload["columnTypes"] == ["number", "string"]
    assert payload["columns"] == ["n", "s"]
