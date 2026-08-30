"""`ui` artifacts, and the type both executors agree on.

The bug that produced this: whether a rich `ui` value became a file at
all depended on your executor AND your adapter. The dud rung wrote it
during execution and deleted the key; the in-process rung kept the live
object and materialized it only if one particular adapter ran. Same
agent code, two outcomes, neither visible from the API.
"""

from __future__ import annotations

import pytest

from nontainer import ArtifactPath, PythonConfig, Workspace, artifact_kind
from nontainer.artifacts import is_rich
from nontainer.providers.kvgit import KvgitProvider

CODE = (
    "import pandas as pd\n"
    "ui = {'chart': pd.DataFrame({'a': [1, 2, 3]}), 'note': 'plain', 'cfg': {'k': 1}}\n"
)


def test_artifact_path_is_a_string_you_can_ignore():
    """A `str` subclass so knowing the type is optional: an embedder
    who has never heard of it still gets a usable path."""
    a = ArtifactPath("/workspace/ui/chart.table.json")
    assert a == "/workspace/ui/chart.table.json"
    assert isinstance(a, str)
    assert a.startswith("/workspace")
    import json

    assert json.dumps({"p": a}) == '{"p": "/workspace/ui/chart.table.json"}'


def test_kind_is_derived_not_stored():
    """One fact, one place. A stored kind could disagree with the
    suffix — a `.png` labelled "table" would be expressible."""
    assert ArtifactPath("/ui/a.plotly.json").kind == "plotly"
    assert ArtifactPath("/ui/a.table.json").kind == "table"
    assert ArtifactPath("/ui/a.png").kind == "image"
    # ...and it is the SAME function the adapters dispatch on.
    for p in ("/ui/a.plotly.json", "/ui/a.cards.json", "/ui/a.html", "/ui/a.bin"):
        assert ArtifactPath(p).kind == artifact_kind(p)


def test_is_rich_only_claims_what_cannot_cross():
    """Plain data crosses as itself. Replacing an agent's string or
    dict with a path would be a much larger change to `ui` than
    swapping a live object nobody could have used anyway."""
    pd = pytest.importorskip("pandas")
    assert is_rich(pd.DataFrame({"a": [1]}))
    assert not is_rich("plain")
    assert not is_rich({"k": 1})
    assert not is_rich([{"label": "n", "value": 1}])
    assert not is_rich(b"\x89PNG")


def _shape(ws):
    r = ws.run_python(CODE)
    assert r.error is None, r.error
    return r.namespace["ui"]


def test_both_executors_produce_the_same_ui_shape():
    """The property the whole design exists for: identical agent code
    yields identical bindings on either rung."""
    pytest.importorskip("pandas")
    pytest.importorskip("dud")
    from nontainer.executor_dud import DudExecutor
    from nontainer.presets import dataframes

    local = Workspace(
        KvgitProvider.open(None, session="ap-local"),
        python=PythonConfig(modules=[dataframes()]),
    )
    dud = Workspace(
        KvgitProvider.open(None, session="ap-dud"),
        executor=DudExecutor(backend="subprocess"),
    )
    try:
        for ui in (_shape(local), _shape(dud)):
            assert isinstance(ui["chart"], ArtifactPath)
            assert ui["chart"] == "/workspace/ui/chart.table.json"
            assert ui["chart"].kind == "table"
            assert ui["note"] == "plain"  # untouched
            assert ui["cfg"] == {"k": 1}  # untouched
    finally:
        local.close()
        dud.close()


def test_a_rerun_still_names_its_artifact():
    """Issue #46: the artifact is overwritten rather than created, so
    anything keyed on "files that appeared this call" stops seeing it
    after the first run."""
    pytest.importorskip("pandas")
    ws = Workspace(
        KvgitProvider.open(None, session="ap-rerun"),
        python=PythonConfig(
            modules=[
                __import__("nontainer.presets", fromlist=["dataframes"]).dataframes()
            ]
        ),
    )
    try:
        first, second = _shape(ws), _shape(ws)
        assert first["chart"] == second["chart"]
        assert isinstance(second["chart"], ArtifactPath)
    finally:
        ws.close()
