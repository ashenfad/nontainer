"""A data-analyst agent over a versioned workspace.

The agent gets terminal + python tools against a kvgit-backed
workspace, analyzes a CSV, writes a report, and stashes stats in the
cache. Afterwards we print the workspace's commit history — every
mutating tool call the agent made is a checkpoint you can roll back.

Run:  ANTHROPIC_API_KEY=... uv run python examples/analyst.py
Deps: pip install nontainer[agno] anthropic
"""

import csv
import statistics
import tempfile

from agno.agent import Agent

from nontainer import PythonConfig, workspace
from nontainer.adapters.agno import WorkspaceTools

from _model import pick_model

SALES = """region,revenue
north,1200
south,3400
north,2100
west,900
south,2800
west,1500
"""

TASK = """\
Analyze data/sales.csv (columns: region, revenue):
1. compute total and mean revenue per region
2. write a short markdown report to report.md
3. store the per-region totals as a dict in cache['totals']
Then show me the report."""


def main() -> None:
    with workspace(
        "analyst-demo",
        store=tempfile.mkdtemp(),
        python=PythonConfig(modules=[csv, statistics]),
    ) as ws:
        ws.fs.makedirs("data", exist_ok=True)
        ws.fs.write("data/sales.csv", SALES.encode())
        ws.checkpoint(info={"seed": "sales.csv"})

        # checkpoint="turn": the whole run lands as ONE commit (the agex
        # model) — contrast with webapp.py's per-call default.
        tk = WorkspaceTools(ws, checkpoint="turn")
        agent = Agent(
            model=pick_model(),
            tools=[tk],
            tool_call_limit=12,
            markdown=False,
            post_hooks=[tk.end_turn],
        )
        run = agent.run(TASK)
        print("=== agent ===\n", run.content)

        print("\n=== report.md (from the workspace) ===")
        print(ws.get("report.md").decode())

        print("=== cache['totals'] ===")
        print(ws.cache["totals"])

        print("\n=== versioned history (newest first) ===")
        for c in ws.history():
            print(f"  {c.id[:10]}  {c.info}")


if __name__ == "__main__":
    main()
