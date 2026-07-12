"""Stdlib-by-default, module flattening, and the preset grant lists."""

import statistics

import pytest

from nontainer import ModuleGrant, PythonConfig, Workspace
from nontainer.providers import KvgitProvider


def make_ws(**kwargs) -> Workspace:
    return Workspace(KvgitProvider.open(None, session="s1"), **kwargs)


# -- stdlib by default --------------------------------------------------------


def test_default_workspace_has_working_stdlib():
    ws = make_ws()
    r = ws.run_python(
        "import json, math, csv, datetime, re\n"
        "print(json.dumps({'pi': round(math.pi, 2)}))"
    )
    assert r, r.error
    assert '"pi": 3.14' in r.stdout
    ws.close()


def test_stdlib_io_routes_through_vfs():
    ws = make_ws()
    ws.terminal("mkdir -p data; echo 'a,b' > data/x.csv")
    r = ws.run_python(
        "import os\n"
        "print(sorted(os.listdir('data')))\n"
        "print(os.path.exists('data/x.csv'))\n"
        "print(os.stat('data/x.csv').st_size)"
    )
    assert r, r.error
    assert "['x.csv']" in r.stdout and "True" in r.stdout
    ws.close()


def test_stdlib_warnings_can_quiet_library_noise():
    """Agents reach for warnings.filterwarnings('ignore') the moment
    pandas/sklearn start shouting deprecations — the module is granted
    (warn/filterwarnings/simplefilter/catch_warnings)."""
    ws = make_ws()
    r = ws.run_python(
        "import warnings\n"
        "warnings.filterwarnings('ignore')\n"
        "with warnings.catch_warnings():\n"
        "    warnings.simplefilter('ignore')\n"
        "    warnings.warn('noise')\n"
        "print('quiet')"
    )
    assert r, r.error
    assert "quiet" in r.stdout
    ws.close()


def test_stdlib_excludes_global_random_state():
    ws = make_ws()
    r = ws.run_python("import random; random.seed(1)")
    assert not r and "seed" in (r.error or "")
    r = ws.run_python("import random; print(random.randint(1, 6) in range(1, 7))")
    assert r, r.error
    ws.close()


def test_stdlib_pathlib_is_vfs_contained():
    ws = make_ws()
    ws.terminal("echo hi > f.txt")
    # monkeyfs patches pathlib: reads route to the workspace...
    r = ws.run_python("import pathlib; print(pathlib.Path('f.txt').read_text())")
    assert r, r.error
    assert r.stdout.strip() == "hi"
    # ...and absolute host paths do NOT escape to the real fs
    r = ws.run_python("import pathlib; pathlib.Path('/etc/hosts').read_text()")
    assert not r and "FileNotFoundError" in (r.error or "")
    ws.close()


def test_stdlib_false_gives_bare_cell():
    ws = make_ws(python=PythonConfig(stdlib=False))
    r = ws.run_python("import math")
    assert not r and "not allowed" in (r.error or "")
    ws.close()


# -- modules flattening --------------------------------------------------------


def test_modules_flatten_one_level():
    grants = [ModuleGrant(statistics)]
    ws = make_ws(python=PythonConfig(stdlib=False, modules=[grants]))
    r = ws.run_python("import statistics; print(statistics.mean([1, 2, 3]))")
    assert r, r.error
    assert r.stdout.strip() == "2"
    ws.close()


def test_modules_bad_entry_raises():
    with pytest.raises(TypeError, match="not a module"):
        make_ws(python=PythonConfig(modules=["statistics"]))  # type: ignore[list-item]


def test_explicit_grant_overrides_stdlib_entry():
    # user re-grants random WITHOUT the seed exclusion; later wins
    ws = make_ws(python=PythonConfig(modules=[ModuleGrant(__import__("random"))]))
    r = ws.run_python("import random; random.seed(1); print('reseeded')")
    assert r, r.error
    ws.close()


def test_module_grant_include_exclude():
    ws = make_ws(
        python=PythonConfig(
            stdlib=False,
            modules=[ModuleGrant(statistics, include=("mean",))],
        )
    )
    assert ws.run_python("import statistics; print(statistics.mean([2, 4]))")
    assert not ws.run_python("import statistics; statistics.median([1])")
    ws.close()


# -- presets -------------------------------------------------------------------


def test_dataframes_preset():
    pytest.importorskip("pandas")
    from nontainer.presets import dataframes

    ws = make_ws(python=PythonConfig(modules=[dataframes()]))
    r = ws.run_python(
        "import pandas as pd\n"
        "import numpy as np\n"
        "df = pd.DataFrame({'x': np.arange(3)})\n"
        "print(int(df['x'].sum()))"
    )
    assert r, r.error
    assert r.stdout.strip() == "3"
    # the exclude lists hold
    assert not ws.run_python("import numpy as np; np.random.seed(0)")
    ws.close()


def test_dataframes_io_via_vfs():
    pytest.importorskip("pandas")
    from nontainer.presets import dataframes

    ws = make_ws(python=PythonConfig(modules=[dataframes()]))
    ws.terminal("echo 'a,b' > in.csv; echo '1,2' >> in.csv; echo '3,4' >> in.csv")
    r = ws.run_python(
        "import pandas as pd\n"
        "df = pd.read_csv(open('in.csv'))\n"
        "print(int(df['a'].sum()))"
    )
    assert r, r.error
    assert r.stdout.strip() == "4"
    ws.close()


def test_plotting_preset_savefig_in_sandbox():
    pytest.importorskip("matplotlib")
    from nontainer.presets import plotting

    grants = plotting(plotly=False)
    import matplotlib

    assert matplotlib.get_backend().lower() == "agg"  # pinned at preset time

    ws = make_ws(python=PythonConfig(modules=[grants]))
    r = ws.run_python(
        "import matplotlib.pyplot as plt\n"
        "fig, ax = plt.subplots()\n"
        "ax.text(0.5, 0.5, 'labelled')\n"  # text → needs the font cache
        "fig.savefig('plot.png')\n"
        "plt.close(fig)"
    )
    assert r, r.error
    assert ws.fs.read("plot.png")[:8] == b"\x89PNG\r\n\x1a\n"
    # display/backend calls are excluded
    assert not ws.run_python("import matplotlib.pyplot as plt; plt.show()")
    ws.close()


def test_plotting_requires_plotly_when_asked():
    pytest.importorskip("matplotlib")
    try:
        import plotly  # noqa: F401

        pytest.skip("plotly installed; the require path can't fail here")
    except ImportError:
        pass
    from nontainer.presets import plotting

    with pytest.raises(ImportError):
        plotting(plotly=True)
