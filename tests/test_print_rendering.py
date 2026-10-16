"""Prints rendered as they are made, and output cut keeping both ends.

The sandbox renders each print with reprobate when it is made; an
oversized stdout is rebuilt from those renderings. Output that isn't
prints is kept by its start and its end.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest
from plain_provider import PlainProvider

from nontainer import Workspace
from nontainer.executor import _render_prints, _truncate_ends


def test_assembly_shares_the_budget_and_cuts_a_long_rendering():
    out = _render_prints(["a" * 1000, "b" * 10], count=2, budget=500)
    first, second = out.split("\n")
    assert first == "a" * 247 + "..."
    assert second == "b" * 10


def test_assembly_counts_prints_that_were_never_rendered():
    """The sandbox renders only so many prints; the rest are counted."""
    out = _render_prints(["x"] * 3, count=10, budget=10_000)
    assert out.splitlines() == ["x", "x", "x", "[...7 more print(s) elided]"]


def test_assembly_elides_what_the_budget_cannot_show():
    out = _render_prints(["y" * 150] * 10, count=10, budget=600)
    assert out.endswith("more print(s) elided]")
    assert len(out) <= 700


def test_a_head_and_tail_cut_keeps_both_ends():
    text = "".join(f"line {i}\n" for i in range(1000))
    out, cut = _truncate_ends(text, 200)
    assert cut
    assert len(out) <= 200
    assert out.startswith("line 0\n")
    assert out.endswith("line 999\n")
    assert "characters, cut to 200" in out


def test_short_text_is_kept_whole():
    assert _truncate_ends("short", 100) == ("short", False)


def test_prints_past_the_rendering_limit_are_counted(tmp_path):
    ws = Workspace(PlainProvider(tmp_path / "ws", session="s"), max_observation=1_000)
    try:
        r = ws.run_python("for i in range(50):\n    print(i, list(range(500)))\n")
    finally:
        ws.close()
    assert r.truncated
    assert r.stdout.startswith("0 [0, 1, 2")
    assert "more print(s) elided" in r.stdout


def test_a_printed_dataframe_does_not_cross_to_the_host():
    """Printed values used to be deep-copied and, out of a worker, pickled
    to the host for a rendering it mostly never needed. Measured in a fresh
    interpreter, so this process's peak RSS is left alone."""
    pytest.importorskip("pandas")
    script = textwrap.dedent(
        """
        import resource, sys, tempfile
        from pathlib import Path
        sys.path.insert(0, sys.argv[1])
        from plain_provider import PlainProvider
        from nontainer import PythonConfig, Workspace
        from nontainer.presets import dataframes

        def peak_mb():
            v = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            return v / 1024 if sys.platform == "linux" else v / 2**20

        ws = Workspace(
            PlainProvider(Path(tempfile.mkdtemp()) / "ws", session="s"),
            python=PythonConfig(isolation="process", modules=[dataframes()]),
        )
        ws.run_python("x = 1")
        before = peak_mb()
        r = ws.run_python(
            "import pandas as pd\\n"
            "df = pd.DataFrame({'a': range(6_000_000)})\\n"
            "print(df)\\n",
            keep=(),  # the frame stays in the worker; only the print is measured
        )
        assert r.error is None, r.error
        print(peak_mb() - before)
        ws.close()
        """
    )
    tests_dir = str(__import__("pathlib").Path(__file__).parent)
    done = subprocess.run(
        [sys.executable, "-c", script, tests_dir],
        capture_output=True,
        text=True,
        check=True,
    )
    grew = float(done.stdout.strip().splitlines()[-1])
    assert grew < 20, f"the host's peak grew {grew:.0f}MB"
