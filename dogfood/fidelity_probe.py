"""Fidelity probe: the analyst loop through both executors.

Not an LLM dogfood — this runs the *kind of code* an analyst agent
writes, through LocalExecutor (sandtrap + monkeyfs VFS) and
DudExecutor (a real machine), and reports where they diverge. Two
things are checked per probe:

  ran      — did the code complete without an error result?
  in_ws    — did the artifact it produced land INSIDE the versioned
             workspace (i.e. show up in the commit the call made)?

The second is the subtle one: C-extension I/O (pyarrow, sqlite) can
"succeed" under sandtrap while writing through to the host filesystem,
escaping the workspace silently — it runs, but the artifact isn't
versioned, forked, or restored. Real fs (dud) keeps it in the tree.

Run (from the nontainer worktree, dud extra installed):
    uv run python dogfood/fidelity_probe.py
"""

from __future__ import annotations

from nontainer import ModuleGrant, PythonConfig, Workspace, presets
from nontainer.providers import KvgitProvider

# Mirror studio's data stack grants (opportunistic — skip what's absent).
_MODULES = []
for _p in ("dataframes", "plotting"):
    try:
        _MODULES.append(getattr(presets, _p)())
    except ImportError:
        pass

# Explicitly GRANT the C-extension libraries so sandtrap ALLOWS the
# import — this isolates the fidelity question (does C-level I/O escape
# the VFS?) from the policy question (is the import even permitted?).
# dud ignores these grants entirely: what's installed is the policy.
for _name in ("sqlite3", "pyarrow", "pyarrow.parquet"):
    try:
        _mod = __import__(_name, fromlist=["_"])
        _MODULES.append(ModuleGrant(module=_mod, recursive=True))
    except ImportError:
        pass


# Each probe: (name, setup_code, artifact_path_in_ws_or_None, note)
PROBES = [
    (
        "pandas read_csv (Python-level open)",
        "import pandas as pd\n"
        "open('t.csv','w').write('a,b\\n1,2\\n3,4\\n')\n"
        "df = pd.read_csv('t.csv')\n"
        "total = int(df['b'].sum())\n"
        "assert total == 6, total\n",
        "/t.csv",
        "monkeyfs intercepts pandas' Python open — expected to work both",
    ),
    (
        "pyarrow parquet (C++ file I/O)",
        "import pyarrow as pa, pyarrow.parquet as pq\n"
        "tbl = pa.table({'x': [1,2,3], 'y': ['a','b','c']})\n"
        "pq.write_table(tbl, 'data.parquet')\n"
        "back = pq.read_table('data.parquet')\n"
        "assert back.num_rows == 3\n",
        "/data.parquet",
        "arrow's C++ reader bypasses Python-level VFS patches",
    ),
    (
        "sqlite on a workspace file (C extension)",
        "import sqlite3\n"
        "c = sqlite3.connect('app.db')\n"
        "c.execute('create table t(x int)')\n"
        "c.execute('insert into t values (42)')\n"
        "c.commit(); c.close()\n"
        "c2 = sqlite3.connect('app.db')\n"
        "assert c2.execute('select x from t').fetchone()[0] == 42\n",
        "/app.db",
        "sqlite opens at the C layer — may escape the workspace",
    ),
    (
        "subprocess (shell out to a real tool)",
        "import subprocess\n"
        "out = subprocess.run(['wc','-l'], input=b'a\\nb\\nc\\n',\n"
        "                     capture_output=True).stdout\n"
        "n = int(out.split()[0])\n"
        "assert n == 3, n\n",
        None,
        "sandtrap has no subprocess; real bash/dud does",
    ),
    (
        "matplotlib savefig (Agg C backend + font cache)",
        "import matplotlib; matplotlib.use('Agg')\n"
        "import matplotlib.pyplot as plt\n"
        "plt.plot([1,2,3],[3,1,2]); plt.savefig('ui/plot.png')\n",
        "/ui/plot.png",
        "renders via C, writes PNG bytes to the tree",
    ),
]


def _make(executor_factory, session):
    provider = KvgitProvider.open(None, session=session)
    return Workspace(
        provider,
        python=PythonConfig(modules=_MODULES),
        executor_factory=executor_factory,
    )


def _probe(ws, code, artifact):
    """Returns (ran: bool, in_ws: bool|None, detail: str)."""
    ws.terminal("mkdir -p ui")
    r = ws.run_python(code)
    ran = r.error is None
    detail = "" if ran else (r.error or "").strip().split("\n")[-1][:80]
    in_ws = None
    if artifact is not None:
        in_ws = ws.fs.exists(artifact)
    return ran, in_ws, detail


def main() -> None:
    from nontainer.executor_dud import DudExecutor

    backends = [
        ("Local (sandtrap)", None),
        ("dud (real fs)", lambda: DudExecutor()),
    ]

    print(f"\n{'probe':<44} {'Local':>16}  {'dud':>16}")
    print("-" * 82)
    rows = []
    for name, code, artifact, note in PROBES:
        cells = []
        for i, (_label, factory) in enumerate(backends):
            ws = _make(factory, f"probe-{len(rows)}-{i}")
            try:
                ran, in_ws, detail = _probe(ws, code, artifact)
            except Exception as e:  # a raise (not a result-error) = hard fail
                ran, in_ws, detail = False, None, f"{type(e).__name__}: {e}"[:80]
            finally:
                ws.close()
            if not ran:
                mark = f"✗ {detail}"[:16]
            elif in_ws is False:
                mark = "ran, ESCAPED"
            elif in_ws is True:
                mark = "✓ in-ws"
            else:
                mark = "✓ ran"
            cells.append(mark)
        print(f"{name:<44} {cells[0]:>16}  {cells[1]:>16}")
        print(f"    ↳ {note}")
        rows.append((name, cells))

    print("\nlegend: ✓ in-ws = ran AND artifact versioned in the workspace;")
    print("        ESCAPED = ran but artifact not in the workspace (unversioned);")
    print("        ✗ = errored.\n")


if __name__ == "__main__":
    main()
