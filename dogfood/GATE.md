# Stage 3 gate: does real fidelity earn dud?

Per PLAN.md, stage 3 ends at a go/no-go: *does real-fidelity execution
measurably improve agent behavior enough to justify the VM rungs
(stages 4–6)?* If not, stages 0–2 still leave nontainer cleaner (the
Executor seam) and a working real-fidelity dev mode — and we stop.

This directory holds the two instruments. The empirical half already
ran; the behavioral half needs a model key and is yours to run.

## 1. fidelity_probe.py — ran, no LLM needed

The analyst *kind of code* through both executors. Two checks: did it
run, and did its artifact land INSIDE the versioned workspace (show up
in the commit). C-extension libraries are explicitly granted, so
sandtrap is *allowed* to run them — this isolates fidelity from import
policy.

Result (worktree, pyarrow 25, pandas, matplotlib):

| probe | Local (sandtrap) | dud (real fs) |
|---|---|---|
| pandas read_csv (Python open) | ✓ in-workspace | ✓ in-workspace |
| pyarrow parquet (C++ I/O) | **ran, ESCAPED** | ✓ in-workspace |
| sqlite on a workspace file (C) | **ran, ESCAPED** | ✓ in-workspace |
| subprocess (shell to real tool) | ✗ unavailable | ✓ ran |
| matplotlib savefig (Agg C) | ✓ in-workspace | ✓ in-workspace |

**The finding is ESCAPED, not errored.** Under sandtrap, pyarrow and
sqlite *succeed* — but their C-level I/O bypasses monkeyfs's
Python-level patches and writes to the host, landing OUTSIDE the
workspace. The artifact is real but unversioned: it does not fork, does
not restore, is not in the commit. For a tool whose entire pitch is
versioned/forkable state, an analyst that reaches for parquet or sqlite
silently punches a hole in the guarantee. dud keeps every artifact in
the tree because the tree is a real filesystem. subprocess — an agent
shelling out to real tools — is simply impossible in-process.

Read that as: the fidelity thesis holds *on the exact stack studio
ships as its flagship*. What it does NOT yet tell you is whether models
actually reach for those tools, or stay in the pandas-only lane where
both executors are at parity (rows 1 and 5).

## 2. analyst_agent.py — needs your key

A real agno agent, same prompt both ways, executor via `DUD=1`:

```
ANTHROPIC_API_KEY=... uv run python dogfood/analyst_agent.py         # sandtrap
ANTHROPIC_API_KEY=... DUD=1 uv run python dogfood/analyst_agent.py   # dud
```

The prompt asks for parquet + sqlite + a markdown summary without
prescribing tools. Watch for:

- **Behavior divergence** — does the model reach for parquet/sqlite/
  subprocess, or avoid them? (If it avoids them even under dud, the
  fidelity gain is latent, not realized — weak go.)
- **Silent escape under Local** — the run prints the committed tree at
  the end. Under sandtrap, do the parquet/sqlite artifacts the agent
  "made" actually appear there, or did they escape? (This is the probe
  finding, now via a real agent.)
- **Friction** — error-and-retry loops under sandtrap (import denied,
  file-not-found after a C write) vs. clean runs under dud. Wasted
  turns are a quality cost even when the task eventually completes.

## Reading the gate

- **GO** (proceed to stage 4 libkrun/images): models routinely reach
  for the escaping/unavailable tools, and either lose artifacts or burn
  turns under sandtrap that dud eliminates.
- **NO-GO / defer**: models stay in the pandas-parity lane; the
  fidelity gain never shows up in behavior. Keep dud as the dev-mode
  Executor, don't build the VM rungs yet.

The probe already establishes the *capability* gap is real and lands on
the flagship stack. The agent runs establish whether it's a *behavioral*
gap. Both pointing the same way is the honest bar for spending stage 4.

## Note

`store=None` in analyst_agent.py uses `~/.nontainer` — throwaway
sessions named `analyst-dogfood`; delete the branch after. The probe
uses in-memory providers (no persistence). pyarrow must be installed to
run the parquet probe (`uv pip install pyarrow`); it is not a nontainer
dependency.
