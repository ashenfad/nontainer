"""LLM analyst dogfood: a real agno agent, executor selectable.

The fidelity_probe shows what the two executors *can* do with
analyst-style code. This shows what an actual model *reaches for* when
turned loose on a data task — the question the probe can't answer
(does real fidelity change agent behavior, or do agents stay inside
the emulated lane anyway?).

Needs a model key (e.g. ANTHROPIC_API_KEY) and the agno + data extras.
Pick the backend with DUD=1:

    ANTHROPIC_API_KEY=... uv run python dogfood/analyst_agent.py         # sandtrap
    ANTHROPIC_API_KEY=... DUD=1 uv run python dogfood/analyst_agent.py   # dud

Same prompt both ways — read the transcript for whether the agent used
parquet / sqlite / a subprocess, and whether its artifacts survived the
checkpoint (the run prints the committed tree at the end).
"""

from __future__ import annotations

import os

from nontainer import ModuleGrant, PythonConfig, presets, workspace
from nontainer.adapters.agno import WorkspaceTools

TASK = (
    "In /data there's sales.csv (I just wrote it). Load it, compute "
    "monthly revenue totals, save the result as a parquet file AND a "
    "sqlite table under /out, and write a short markdown summary to "
    "/out/summary.md. Use whatever tools you find most natural."
)

SALES_CSV = "month,revenue\n2026-01,1200\n2026-01,800\n2026-02,1500\n2026-03,900\n"


def _config() -> PythonConfig:
    modules = []
    for p in ("dataframes", "plotting"):
        try:
            modules.append(getattr(presets, p)())
        except ImportError:
            pass
    for name in ("sqlite3", "pyarrow", "pyarrow.parquet"):
        try:
            modules.append(ModuleGrant(module=__import__(name, fromlist=["_"]),
                                       recursive=True))
        except ImportError:
            pass
    return PythonConfig(modules=modules)


def main() -> None:
    use_dud = os.getenv("DUD", "") not in ("", "0", "false")
    factory = None
    if use_dud:
        from nontainer.executor_dud import DudExecutor

        factory = lambda: DudExecutor()  # noqa: E731
    label = "dud (real machine)" if use_dud else "Local (sandtrap)"

    from agno.agent import Agent
    from agno.models.anthropic import Claude

    ws = workspace(
        "analyst-dogfood",
        store=None,  # ~/.nontainer — a throwaway session; delete after
        python=_config(),
        executor_factory=factory,
    )
    try:
        ws.terminal("mkdir -p data out")
        ws.run_python(f"open('data/sales.csv','w').write({SALES_CSV!r})")

        agent = Agent(
            model=Claude(id="claude-sonnet-5"),
            tools=[WorkspaceTools(ws)],
            markdown=True,
        )
        print(f"\n=== analyst dogfood on {label} ===\n")
        agent.print_response(TASK, stream=True)

        print("\n=== committed workspace tree (did artifacts survive?) ===")
        for rel in sorted(ws.fs.list("/", recursive=True)):
            if ws.fs.isfile("/" + rel):
                print("  ", rel)
    finally:
        ws.close()


if __name__ == "__main__":
    main()
