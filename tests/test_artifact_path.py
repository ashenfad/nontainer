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


# -- what review caught ------------------------------------------------------


def test_artifacts_ride_the_calls_own_checkpoint():
    """Materialization writes through `write_file`, which checkpoints
    for itself — so materializing inside `run_python` committed under
    the wrong tool, once per rich value, and left `result.checkpoint`
    None while the head had in fact moved."""
    pytest.importorskip("pandas")
    from nontainer.presets import dataframes

    ws = Workspace(
        KvgitProvider.open(None, session="ap-cp"),
        python=PythonConfig(modules=[dataframes()]),
    )
    try:
        before = len(list(ws.history()))
        r = ws.run_python(
            "import pandas as pd\n"
            "ui = {'a': pd.DataFrame({'x': [1]}), 'b': pd.DataFrame({'y': [2]})}\n"
        )
        assert r.error is None, r.error
        entries = list(ws.history())
        assert len(entries) - before == 1, "two artifacts, one commit"
        assert entries[0].info == {"tool": "run_python"}
        assert r.checkpoint == ws.head, "the call must report its own commit"
    finally:
        ws.close()


def test_an_oversized_value_still_explains_itself():
    """The 8MB cap message is actionable text the agent self-corrects
    from. Materialization moved into run_python, so the adapter's later
    pass sees an already-made artifact and reports nothing — the result
    has to carry it."""
    pytest.importorskip("pandas")
    from nontainer.presets import dataframes

    ws = Workspace(
        KvgitProvider.open(None, session="ap-big"),
        python=PythonConfig(modules=[dataframes()]),
    )
    try:
        r = ws.run_python(
            "import pandas as pd\n"
            # WIDE, not long: materialization keeps head(200), so
            # row count alone never reaches the cap.
            "ui = {'huge': pd.DataFrame({'s': ['x' * 60000] * 300})}\n"
        )
        assert r.error is None, r.error
        assert r.ui_problems, "the cap message was dropped"
        assert "too large" in r.ui_problems[0]
    finally:
        ws.close()


def test_a_forged_claim_is_not_an_artifact():
    """`ui` is agent-authored. An ordinary dict wearing the wire tag
    must not become an ArtifactPath — that would diverge the rungs
    again, which is the thing this type exists to prevent."""
    pytest.importorskip("dud")
    from nontainer.executor_dud import DudExecutor

    forged = "ui = {'cfg': {'__nt_artifact__': 'not-a-file'}}\n"
    local = Workspace(KvgitProvider.open(None, session="ap-forge-l"))
    dud = Workspace(
        KvgitProvider.open(None, session="ap-forge-d"),
        executor=DudExecutor(backend="subprocess"),
    )
    try:
        for ws in (local, dud):
            r = ws.run_python(forged)
            assert r.error is None, r.error
            assert r.namespace["ui"]["cfg"] == {"__nt_artifact__": "not-a-file"}
    finally:
        local.close()
        dud.close()


def test_an_oversized_value_fails_identically_on_both_rungs():
    """The cap is a RENDERER limit advertised to the agent in the
    primer ("Artifacts are capped at 8MB..."), so the note is the
    feedback half of that contract — and it has to look the same
    wherever the value was serialized.

    It did not: the guest wrote `<name>.json` carrying `{"error": ...}`
    while the host wrote `<name>.txt` carrying the plain message, so
    one condition produced two artifact kinds. And the guest's message
    reached nobody, because only the in-process path fed `ui_problems`.
    """
    pytest.importorskip("pandas")
    pytest.importorskip("dud")
    from nontainer.executor_dud import DudExecutor
    from nontainer.presets import dataframes

    # WIDE, not long: materialization keeps head(200), so row count
    # alone never reaches the cap.
    code = (
        "import pandas as pd\nui = {'huge': pd.DataFrame({'s': ['x' * 60000] * 300})}\n"
    )
    local = Workspace(
        KvgitProvider.open(None, session="ap-cap-l"),
        python=PythonConfig(modules=[dataframes()]),
    )
    dud = Workspace(
        KvgitProvider.open(None, session="ap-cap-d"),
        executor=DudExecutor(backend="subprocess"),
    )
    try:
        seen = []
        for ws in (local, dud):
            r = ws.run_python(code)
            assert r.error is None, r.error
            art = r.namespace["ui"]["huge"]
            assert isinstance(art, ArtifactPath)
            assert art.kind == "text", "same kind, whichever rung wrote it"
            assert r.ui_problems, "the agent must learn why"
            assert "too large" in r.ui_problems[0]
            seen.append((str(art), r.ui_problems))
        assert seen[0] == seen[1], "byte-identical outcome across rungs"
    finally:
        local.close()
        dud.close()
