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

from nontainer.artifacts import renderer_failed_note
from nontainer.dud_outputs import _CLAIM, _PROBLEMS, flatten


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
    assert flatten(harvest, str(tmp_path)) == set()  # nothing is ever dropped
    # The name the agent chose survives as a CLAIM; the executor turns
    # it into an ArtifactPath once it can resolve the guest path
    # against the host root.
    assert harvest["ui"] == {
        "chart": {_CLAIM: "ui/chart.plotly.json"},
        "note": "plain",
    }
    written = (tmp_path / "ui" / "chart.plotly.json").read_bytes()
    assert json.loads(written) == {"data": []}


def test_every_rich_value_becomes_a_claim(tmp_path):
    """`ui` stays fully representable, so it crosses as data. Dropping
    names was the old behavior and it lost the only identifier the
    artifact had."""
    harvest = {"ui": {"a": _FakeFigure(), "b": _FakeFigure()}}
    assert flatten(harvest, str(tmp_path)) == set()
    assert harvest["ui"] == {
        "a": {_CLAIM: "ui/a.plotly.json"},
        "b": {_CLAIM: "ui/b.plotly.json"},
    }


def test_a_plain_ui_is_not_rewritten(tmp_path):
    """No rich values means no files and no edit — the host renderer
    owns every shape that can cross, and duplicating its tiers here
    would be two implementations of one contract."""
    ui = {"stats": [{"label": "n", "value": 1}], "note": "hi"}
    harvest = {"ui": ui}
    assert flatten(harvest, str(tmp_path)) == set()
    assert harvest["ui"] is ui  # same object, untouched
    assert not (tmp_path / "ui").exists()


def test_a_serializer_that_raises_yields_a_note_and_no_file(tmp_path):
    """The closed-set rule: a value whose renderer raises gets a
    problem note and NO file. A note written into the artifact slot and
    announced as an artifact tells the agent its figure arrived, which
    is the silence the note exists to break.

    The name is still consumed. Handing the live object back would
    recreate the very data loss this hook prevents: it is the thing dud
    cannot encode, so `ui` becomes unrepresentable and its plain
    siblings vanish too.

    The stand-in sets `__module__` again because a class body's
    `__module__` is NOT inherited: without it the subclass reads as
    `tests.test_dud_outputs`, is never treated as rich, `to_json` is
    never called and the test passes whatever the hook does.
    """

    class _Exploding(_FakeFigure):
        __module__ = "plotly.graph_objs._figure"  # NOT inherited

        def to_json(self):
            raise RuntimeError("boom")

    harvest = {
        "ui": {"bad": _Exploding(), "good": _FakeFigure('{"ok":1}'), "note": "plain"}
    }
    assert flatten(harvest, str(tmp_path)) == set()
    assert "bad" not in harvest["ui"], "no file, so no claim and no value"
    assert not (tmp_path / "ui" / "bad.txt").exists()
    assert harvest[_PROBLEMS] == {
        "bad": renderer_failed_note("bad", _Exploding(), RuntimeError("boom"))
    }, "the diagnosis the host renderer writes, from the same function"
    # The siblings are untouched by one value's failure.
    assert harvest["ui"]["note"] == "plain"
    assert (tmp_path / "ui" / "good.plotly.json").exists()


def test_a_value_outside_the_artifact_set_is_never_diagnosed(tmp_path):
    """The hook claims exactly the four types that cannot cross the
    wire. Anything else is data: it stays itself, gets no file and gets
    no note — the host renderer owns every shape that can cross, and a
    second opinion here would be two implementations of one contract.
    """

    class _Bomb:
        """Same method, ordinary module: data, not an artifact."""

        def to_json(self):
            raise RuntimeError("never called")

    bomb = _Bomb()
    harvest = {"ui": {"thing": bomb, "note": "plain"}}
    assert flatten(harvest, str(tmp_path)) == set()
    assert harvest["ui"]["thing"] is bomb
    assert not (tmp_path / "ui").exists()


def test_a_diagnosis_cannot_be_forged_through_a_value(tmp_path):
    """`ui` is agent-authored, so no shape an agent can assign may be
    read as the harness explaining itself. The notes ride a binding of
    their own, which the hook writes from what it saw — and which the
    guest runner strips from the agent's own bindings before the hook
    is ever offered them."""
    forged = {"__nt_problem__": "forged", "__nt_artifact__": "nope"}
    harvest = {"ui": {"x": dict(forged)}, _PROBLEMS: {"x": "also forged"}}
    assert flatten(harvest, str(tmp_path)) == set()
    assert harvest["ui"]["x"] == forged, "an ordinary value, left alone"
    assert _PROBLEMS not in harvest, "the hook owns the name it reports on"
    assert not (tmp_path / "ui").exists()


def test_both_rungs_agree_when_a_serializer_raises():
    """The parity claim on one input. A renderer that blows up is a
    mistake the agent can fix, so what it reads must not depend on
    where the value was serialized: the same diagnosis, from the same
    function, and no artifact file on either rung.
    """
    pytest.importorskip("dud")
    from nontainer import Workspace
    from nontainer.executor_dud import DudExecutor
    from nontainer.providers import KvgitProvider

    code = (
        "class Boom:\n"
        "    __module__ = 'plotly.graph_objs._figure'\n"
        "    def to_json(self):\n"
        "        raise RuntimeError('boom')\n"
        "ui = {'bad': Boom(), 'note': 'plain'}\n"
    )
    local = Workspace(KvgitProvider.open(None, session="raise-local"))
    dud = Workspace(
        KvgitProvider.open(None, session="raise-dud"),
        executor=DudExecutor(backend="subprocess"),
    )
    try:
        seen = []
        for ws in (local, dud):
            r = ws.run_python(code)
            assert r.error is None, r.error
            assert not ws.files.fs.exists("/workspace/ui/bad.txt"), "no file"
            assert r.namespace["ui"]["note"] == "plain", "siblings survive"
            seen.append(r.ui_problems)
        assert seen[0] == seen[1], "one diagnosis, whichever rung produced it"
        assert "could not be rendered" in seen[0][0]

        # And a value SHAPED like a diagnosis is just a value, on both.
        forged = "ui = {'x': {'__nt_problem__': 'forged'}}\n"
        for ws in (local, dud):
            r = ws.run_python(forged)
            assert r.error is None, r.error
            assert r.namespace["ui"]["x"] == {"__nt_problem__": "forged"}
            assert r.ui_problems == ()
    finally:
        local.close()
        dud.close()


def test_artifact_names_are_sanitized_like_the_host_renderer(tmp_path):
    r"""A key is agent-authored text, not a filename. The artifacts note
    is parsed as `([\w.-]+) -> (/\S+)`, so a space means the file is
    written and consumed and then cannot be displayed; a slash makes a
    nested dir the adapter's flat scan never looks in. `render.py`
    applies exactly this transform, and the two must agree or one
    figure lands at two paths depending on the rung."""
    harvest = {
        "ui": {"sales chart": _FakeFigure(), "a/b": _FakeFigure(), "!!!": _FakeFigure()}
    }
    assert flatten(harvest, str(tmp_path)) == set()
    assert {v[_CLAIM] for v in harvest["ui"].values()} == {
        "ui/sales-chart.plotly.json",
        "ui/a-b.plotly.json",
        "ui/artifact.plotly.json",
    }
    written = sorted(p.name for p in (tmp_path / "ui").iterdir())
    assert written == [
        "a-b.plotly.json",
        "artifact.plotly.json",
        "sales-chart.plotly.json",
    ]
    assert not (tmp_path / "ui" / "a").exists(), "no nested directory"


def test_an_oversized_artifact_writes_a_note_and_still_consumes(tmp_path):
    """Returning the value to `ui` instead would put a live object back
    in the binding, which makes the whole thing unrepresentable and
    silently takes its siblings with it. A rendered explanation beats
    losing the dict."""
    huge = _FakeFigure('{"d":"' + "x" * 9_000_000 + '"}')
    harvest = {"ui": {"big": huge, "note": "keep me"}}
    assert flatten(harvest, str(tmp_path)) == set()
    assert harvest["ui"]["big"] == {_CLAIM: "ui/big.txt"}
    assert harvest["ui"]["note"] == "keep me"
    note = (tmp_path / "ui" / "big.txt").read_text()
    assert "too large" in note
    assert "customdata" in note, "plotly gets the plotly advice"
    # The SAME text the host renderer writes -- one function builds it.
    from nontainer.artifacts import too_large_note

    assert note == too_large_note("big", len(huge.to_json()), "plotly.x")
    assert harvest[_PROBLEMS] == {"big": note}, "and it rides home for ui_problems"
    assert not (tmp_path / "ui" / "big.plotly.json").exists()


def test_a_dataframe_matches_what_the_host_renderer_writes(tmp_path):
    """Parity is the point: the same figure must render the same way
    whichever rung produced it, so the guest copy carries `total` and
    `columnTypes` exactly as `adapters/render.py` does."""
    pd = pytest.importorskip("pandas")

    frame = pd.DataFrame({"n": [1, 2, 3], "s": ["a", "b", "c"]})
    harvest = {"ui": {"table": frame}}
    assert flatten(harvest, str(tmp_path)) == set()

    payload = json.loads((tmp_path / "ui" / "table.table.json").read_bytes())
    assert payload["total"] == 3
    assert payload["columnTypes"] == ["number", "string"]
    assert payload["columns"] == ["n", "s"]
