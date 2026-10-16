"""The 8MB ui cap, applied before a value is fully serialized.

Text is measured before it is encoded, and a plotly figure whose arrays
alone are over the cap is refused before its encoder runs. Both rungs
share the helpers, so the note reads the same wherever it was written.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from nontainer.artifacts import (
    MAX_ARTIFACT_BYTES,
    ArtifactTooLarge,
    capped_bytes,
    plotly_size_floor,
    too_large_note,
)

np = pytest.importorskip("numpy")


# ------------------------------------------------------------------
# Text, before it is encoded
# ------------------------------------------------------------------


def test_ascii_text_over_the_cap_is_refused_at_its_exact_size():
    with pytest.raises(ArtifactTooLarge) as info:
        capped_bytes("x" * 9_000_000)
    assert info.value.size == 9_000_000
    assert not info.value.at_least


def test_non_ascii_text_over_the_cap_by_length_is_a_lower_bound():
    with pytest.raises(ArtifactTooLarge) as info:
        capped_bytes("é" * 8_500_000)
    assert info.value.size == 8_500_000
    assert info.value.at_least


def test_text_under_the_cap_by_length_is_measured_once_encoded():
    """4.5M two-byte characters: under the cap by length, over it encoded."""
    with pytest.raises(ArtifactTooLarge) as info:
        capped_bytes("é" * 4_500_000)
    assert info.value.size == 9_000_000
    assert not info.value.at_least


def test_text_and_bytes_within_the_cap_pass_through():
    assert capped_bytes("abc") == b"abc"
    assert capped_bytes(b"abc") == b"abc"


def test_the_note_says_when_the_size_is_a_lower_bound():
    exact = too_large_note("chart", 9_000_000, "plotly.graph_objs")
    floor = too_large_note("chart", 9_000_000, "plotly.graph_objs", at_least=True)
    assert "(9.0MB > 8MB cap)" in exact
    assert "(at least 9.0MB > 8MB cap)" in floor


# ------------------------------------------------------------------
# A plotly figure's floor, from its arrays
# ------------------------------------------------------------------


def test_a_float_element_counts_two_bytes_and_any_other_one():
    spec = {
        "data": [
            {
                "x": np.zeros(1000, dtype=float),
                "y": np.zeros(1000, dtype=np.int64),
                "text": ["a"] * 10,
                "marker": {"color": np.zeros(100, dtype=float)},
                "name": "trace",  # a string, not an array: no count
            }
        ]
    }
    assert plotly_size_floor(spec) == 2000 + 1000 + 10 + 200


def test_a_figure_object_is_read_through_its_attributes():
    class Trace:
        x = np.zeros(500, dtype=float)
        marker = SimpleNamespace(size=[1, 2, 3])

        def __getattr__(self, name):  # plotly raises for a property a
            raise ValueError(name)  # trace type does not have

    figure = SimpleNamespace(data=(Trace(), Trace()))
    assert plotly_size_floor(figure) == 2 * (1000 + 3)


def test_an_object_without_traces_has_no_floor():
    assert plotly_size_floor(SimpleNamespace()) == 0
    assert plotly_size_floor({"layout": {}}) == 0


# ------------------------------------------------------------------
# Refused before the encoder runs, on both rungs
# ------------------------------------------------------------------


def _figure(points: int, *, serializes: bool):
    """A stand-in for a plotly Figure (named for plotly's module), whose
    to_json fails the test when the cap should have stopped it first."""

    def to_json(self):
        assert serializes, "an oversized figure was serialized anyway"
        return '{"data": []}'

    cls = type(
        "Figure", (), {"__module__": "plotly.graph_objs._figure", "to_json": to_json}
    )
    figure = cls()
    figure.data = (SimpleNamespace(x=np.zeros(points), y=np.zeros(points)),)
    return figure


def test_an_oversized_figure_is_refused_without_serializing(tmp_path):
    from plain_provider import PlainProvider

    from nontainer import Workspace
    from nontainer.ui import materialize_ui

    points = MAX_ARTIFACT_BYTES // 4 + 1  # x and y: 4 bytes a point, at least
    ws = Workspace(PlainProvider(tmp_path / "ws", session="cap"))
    try:
        out, problems = materialize_ui(ws, {"chart": _figure(points, serializes=False)})
    finally:
        ws.close()
    assert out == [("chart", "/workspace/ui/chart.txt")]
    (problem,) = problems
    assert "NOT rendered: too large (at least" in problem


def test_a_figure_under_its_floor_is_serialized(tmp_path):
    from plain_provider import PlainProvider

    from nontainer import Workspace
    from nontainer.ui import materialize_ui

    ws = Workspace(PlainProvider(tmp_path / "ws", session="cap"))
    try:
        out, problems = materialize_ui(ws, {"chart": _figure(1000, serializes=True)})
    finally:
        ws.close()
    assert out == [("chart", "/workspace/ui/chart.plotly.json")]
    assert problems == []


def test_both_rungs_refuse_an_oversized_figure_with_one_note(tmp_path):
    from plain_provider import PlainProvider

    from nontainer import Workspace
    from nontainer.dud_outputs import _materialize
    from nontainer.ui import materialize_ui

    points = MAX_ARTIFACT_BYTES // 4 + 1
    ws = Workspace(PlainProvider(tmp_path / "ws", session="cap"))
    try:
        _, host = materialize_ui(ws, {"chart": _figure(points, serializes=False)})
    finally:
        ws.close()
    guest_root = tmp_path / "guest"
    (guest_root / "ui").mkdir(parents=True)
    _, guest = _materialize("chart", _figure(points, serializes=False), str(guest_root))
    assert host == [guest]


def test_without_plotly_a_specs_arrays_are_bounded_by_their_printed_form():
    """Without plotly a spec's NumPy arrays are written by json.dumps with
    default=str, which abbreviates them; the bound follows that encoder."""
    spec = {"data": [{"x": np.zeros(9_000_000, dtype=np.uint8), "y": [0] * 10}]}
    assert plotly_size_floor(spec) == 9_000_010
    assert plotly_size_floor(spec, arrays=False) == 10


def test_a_spec_with_a_large_array_renders_approximately_without_plotly(tmp_path):
    from plain_provider import PlainProvider

    from nontainer import Workspace
    from nontainer.ui import _has_plotly_encoder, materialize_ui

    if _has_plotly_encoder():
        pytest.skip("plotly is installed: its encoder writes the array in full")
    spec = {
        "data": [{"type": "scatter", "x": np.zeros(9_000_000, dtype=np.uint8)}],
        "layout": {},
    }
    ws = Workspace(PlainProvider(tmp_path / "ws", session="cap"))
    try:
        out, problems = materialize_ui(ws, {"chart": spec})
    finally:
        ws.close()
    assert out == [("chart", "/workspace/ui/chart.plotly.json")]
    (problem,) = problems
    assert "too large" not in problem and "plotly is not importable" in problem
