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


def test_stdlib_urllib_parse_is_granted():
    """Handlers/data code reach for query-string helpers reflexively.
    The pure string side is granted; the network side (urllib.request)
    stays out."""
    ws = make_ws()
    r = ws.run_python(
        "import urllib.parse\n"
        "print(urllib.parse.urlencode({'a': 'b c'}))\n"
        "print(urllib.parse.parse_qs('x=1&x=2'))\n"
        "print(urllib.parse.urlparse('http://h/p?q=1').path)"
    )
    assert r, r.error
    assert "a=b+c" in r.stdout and "{'x': ['1', '2']}" in r.stdout
    r = ws.run_python("import urllib.request")
    assert not r and "urllib.request" in (r.error or "")
    ws.close()


def test_blocked_import_renders_redirect_hint():
    """subprocess-to-curl is a predictable collision — the rendered
    observation must label the door (terminal curl), not just say no."""
    from nontainer.adapters.render import render_python

    ws = make_ws()
    text = render_python(ws.run_python("import subprocess"))
    assert "[hint: " in text and "terminal" in text and "curl" in text
    text = render_python(ws.run_python("import requests"))
    assert "[hint: " in text and "curl" in text
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


def test_stdlib_os_path_queries_route_through_vfs():
    """getsize/abspath are monkeyfs-patched, so granting them is safe;
    the string-math helpers need no patching at all."""
    ws = make_ws()
    r = ws.run_python(
        "import os\n"
        "with open('f.bin', 'wb') as f:\n"
        "    f.write(b'x' * 1234)\n"
        "print(os.path.getsize('f.bin'))\n"
        "print(os.path.abspath('f.bin'))\n"
        "print(os.path.normpath('a/b/../c'))\n"
        "print(os.path.split('/a/b/c')[0])\n"
        "print(os.path.relpath('/a/b/c', '/a'))\n"
    )
    assert r, r.error
    assert r.stdout.splitlines() == ["1234", "/f.bin", "a/c", "/a/b", "b/c"]
    ws.close()


def test_stdlib_os_path_timestamps_stay_blocked():
    """getmtime isn't monkeyfs-patched — granting it would leak host-fs
    calls. The VFS-routed door for timestamps is os.stat().st_mtime."""
    ws = make_ws()
    ws.terminal("echo hi > f.txt")
    r = ws.run_python("import os; os.path.getmtime('f.txt')")
    assert not r and "getmtime" in (r.error or "")
    r = ws.run_python("import os; print(os.stat('f.txt').st_mtime >= 0)")
    assert r, r.error
    assert r.stdout.strip() == "True"
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


def test_dataframes_preset_pins_a_fork_safe_arrow_allocator():
    """Arrow's default mimalloc pool segfaults in forked workers (its
    per-thread heaps don't survive fork); the preset pins the system
    allocator before pandas can import pyarrow. setdefault: an embedder
    that chose a pool explicitly keeps it."""
    import os

    from nontainer import presets

    presets.dataframes()
    assert os.environ.get("ARROW_DEFAULT_MEMORY_POOL") == "system"


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
