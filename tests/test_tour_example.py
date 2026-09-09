"""examples/tour.py runs, and says what it claims to say.

The tour is the one place the whole surface appears in the order an
embedder meets it, so it is also the place a rename or a changed
refusal message goes stale unnoticed. Run it in a subprocess (the way
a reader will) and assert the landmarks of each step.
"""

import subprocess
import sys
from pathlib import Path

TOUR = Path(__file__).resolve().parent.parent / "examples" / "tour.py"


def test_the_tour_runs_and_shows_each_verb():
    out = subprocess.run(
        [sys.executable, str(TOUR)],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert out.returncode == 0, out.stderr
    text = out.stdout

    for landmark in (
        "total = 11 | cache['best'] = south",
        # the index: a partial commit leaves the rest in the tree
        "the scratch file stayed out of it: True",
        # the view, and its write rule
        "what the delegate can see: ['/workspace/report.md']",
        "is outside this session's view",
        # the diff, grouped by what the delegate was sent to do
        "in what I sent it to do: ['/workspace/report.md']",
        "elsewhere: ['/workspace/notes.md']",
        # a merge takes only what has been committed, on both sides
        "refused: uncommitted ws-git work on this session",
        "merged: True | conflicts: ()",
        "North 4, South 7.",
        # delegation as a tool call: the answer, what it changed
        # grouped, and the next step spelled for the terminal
        "Summarized it, and left the working note in summary.md.",
        "changed, in what you sent it to do: /workspace/report.md",
        "changed, elsewhere: /workspace/summary.md",
        "next, in the terminal: ws-git diff analyst.",
        "answered  summarize the report and say how you did it",
        # take, with provenance
        "taken: how the rates were sampled",
        "'tool': 'checkout', 'taken_from': 'colleague@",
        # attach
        "how the rates were sampled",
        # publish: the subtree, not the session
        "published rates v1 from analyst@",
        "['/workspace/app', '/workspace/app/index.html']",
        # two histories over one branch
        "ws-git.merge from polish",
    ):
        assert landmark in text, landmark

    # the agent's log holds only the agent's commits
    agent_log = text.split("the agent's own, which is all ws-git log shows:")[1]
    assert "ws-git.restore" not in agent_log
    assert "file_write" not in agent_log
