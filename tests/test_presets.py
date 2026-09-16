"""Stdlib-by-default, module flattening, and the preset grant lists."""

import base64
import pickle
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


def test_stdlib_data_helpers():
    ws = make_ws()
    r = ws.run_python(
        "import binascii, bisect, difflib, heapq, struct\n"
        "heap = [3, 1, 2]\n"
        "heapq.heapify(heap)\n"
        "result = (\n"
        "    heapq.heappop(heap),\n"
        "    bisect.bisect([1, 3], 2),\n"
        "    list(difflib.unified_diff(['a'], ['b'])),\n"
        "    struct.unpack('>H', struct.pack('>H', 513))[0],\n"
        "    binascii.hexlify(b'ok'),\n"
        ")"
    )
    assert r, r.error
    assert r.namespace["result"] == (
        1,
        1,
        ["--- \n", "+++ \n", "@@ -1 +1 @@\n", "-a", "+b"],
        513,
        b"6f6b",
    )
    ws.close()


@pytest.mark.parametrize("module", ["numbers", "collections.abc"])
def test_stdlib_excludes_process_global_abc_registration(module):
    """ABCMeta.register mutates a process-global registry. ModuleGrant
    filters do not follow a class object returned from a module, so
    neither ABC module is safe to grant until class-level filters exist."""
    ws = make_ws()
    r = ws.run_python(f"import {module}")
    assert not r and module in (r.error or "")
    ws.close()


def test_stdlib_functools_is_useful_but_narrow():
    ws = make_ws()
    r = ws.run_python(
        "import functools\n"
        "add_two = functools.partial(lambda a, b: a + b, 2)\n"
        "total = functools.reduce(lambda a, b: a + b, [1, 2, 3])\n"
        "@functools.lru_cache(maxsize=4)\n"
        "def fib(n):\n"
        "    return n if n < 2 else fib(n - 1) + fib(n - 2)\n"
        "@functools.cache\n"
        "def square(n):\n"
        "    return n * n\n"
        "result = (add_two(3), total, fib(8), square(6))"
    )
    assert r, r.error
    assert r.namespace["result"] == (5, 6, 21, 36)
    denied = ws.run_python("import functools; functools.singledispatch")
    assert not denied and "singledispatch" in (denied.error or "")
    ws.close()


def test_stdlib_types_is_two_data_shapes_and_nothing_else():
    """``SimpleNamespace`` is the record agents reach for; the rest of
    the module is the interpreter's own machinery."""
    ws = make_ws()
    r = ws.run_python(
        "from types import SimpleNamespace\n"
        "import types\n"
        "row = SimpleNamespace(name='ann', score=3)\n"
        "view = types.MappingProxyType({'a': 1})\n"
        "result = (row.name, row.score, view['a'])"
    )
    assert r, r.error
    assert r.namespace["result"] == ("ann", 3, 1)
    for denied in (
        ws.run_python("import types; types.FunctionType"),
        ws.run_python("from types import CodeType"),
    ):
        assert not denied
    ws.close()


def test_stdlib_typing_is_the_annotation_vocabulary():
    """Annotations are the point; evaluating one is not. An annotation
    a handler writes is inert text until something calls
    ``get_type_hints`` on it, which is why the evaluators stay out."""
    ws = make_ws()
    r = ws.run_python(
        "from typing import Any, Callable, ClassVar, Dict, Final, List, Literal\n"
        "from typing import NamedTuple, Optional, Protocol, TypedDict, TypeVar\n"
        "from typing import Type, Union, cast\n"
        "T = TypeVar('T')\n"
        "class Row(NamedTuple):\n"
        "    name: str\n"
        "    score: int = 0\n"
        "class Shape(Protocol):\n"
        "    def area(self) -> float: ...\n"
        "class Conf(TypedDict):\n"
        "    limit: int\n"
        "def pick(xs: List[T], k: Optional[str] = None) -> Dict[str, Any]:\n"
        "    return {'n': len(xs), 'k': cast(str, k)}\n"
        "result = (Row('ann').score, pick([1, 2], 'x'))"
    )
    assert r, r.error
    assert r.namespace["result"] == (0, {"n": 2, "k": "x"})

    for denied in (
        ws.run_python("import typing; typing.get_type_hints"),
        ws.run_python("from typing import ForwardRef"),
    ):
        assert not denied

    # The escape those two would open, shown not to exist: a string
    # annotation is never evaluated, so calling the function runs no
    # part of what the annotation says.
    inert = ws.run_python(
        "def f(x: \"__import__('os').getcwd()\") -> \"__import__('os')\":\n"
        "    return x + 1\n"
        "result = f(1)"
    )
    assert inert, inert.error
    assert inert.namespace["result"] == 2
    ws.close()


def test_stdlib_dataclasses_builds_records_but_not_from_strings():
    ws = make_ws()
    r = ws.run_python(
        "from dataclasses import asdict, astuple, dataclass, field, fields, replace\n"
        "@dataclass\n"
        "class Row:\n"
        "    name: str\n"
        "    score: int = 0\n"
        "    tags: list = field(default_factory=list)\n"
        "row = Row('ann')\n"
        "row.tags.append('x')\n"
        "result = (\n"
        "    asdict(row),\n"
        "    astuple(replace(row, score=5))[:2],\n"
        "    [f.name for f in fields(row)],\n"
        ")"
    )
    assert r, r.error
    assert r.namespace["result"] == (
        {"name": "ann", "score": 0, "tags": ["x"]},
        ("ann", 5),
        ["name", "score", "tags"],
    )

    frozen = ws.run_python(
        "from dataclasses import FrozenInstanceError, dataclass\n"
        "@dataclass(frozen=True)\n"
        "class Point:\n"
        "    x: int = 1\n"
        "point = Point()\n"
        "try:\n"
        "    point.x = 2\n"
        "    result = 'assigned'\n"
        "except FrozenInstanceError:\n"
        "    result = 'frozen'"
    )
    assert frozen, frozen.error
    assert frozen.namespace["result"] == "frozen"

    # make_dataclass interpolates field names it was handed as strings
    # into source it exec()s host-side; the decorator reads a class
    # body, where a name is an identifier by construction.
    denied = ws.run_python("from dataclasses import make_dataclass")
    assert not denied and "make_dataclass" in (denied.error or "")
    ws.close()


def test_a_class_annotation_dict_cannot_be_written_from_the_sandbox():
    """What keeps the dataclass grant narrow: field names reach the
    decorator from ``__annotations__``, and sandboxed code cannot put
    anything but a real annotation there."""
    ws = make_ws()
    denied = ws.run_python("class C:\n    pass\nC.__annotations__ = {'x': int}")
    assert not denied and "__annotations__" in (denied.error or "")
    ws.close()


def test_stdlib_shlex_and_pprint_are_string_only():
    ws = make_ws()
    r = ws.run_python(
        "import pprint, shlex\n"
        "result = (\n"
        "    shlex.quote('a b'),\n"
        "    shlex.join(['echo', 'a b']),\n"
        "    pprint.pformat({'b': 2, 'a': 1}),\n"
        "    pprint.saferepr({'a': 1}),\n"
        "    pprint.isrecursive([]),\n"
        "    pprint.isreadable({'a': 1}),\n"
        ")"
    )
    assert r, r.error
    assert r.namespace["result"] == (
        "'a b'",
        "echo 'a b'",
        "{'a': 1, 'b': 2}",
        "{'a': 1}",
        False,
        True,
    )
    for expression, member in (
        ("shlex.split('a b')", "split"),
        ("pprint.pprint({'a': 1})", "pprint"),
        ("pprint.PrettyPrinter()", "PrettyPrinter"),
    ):
        denied = ws.run_python(f"import pprint, shlex; {expression}")
        assert not denied and member in (denied.error or "")
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
    assert r.stdout.splitlines() == [
        "1234",
        "/workspace/f.bin",
        "a/c",
        "/a/b",
        "b/c",
    ]
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


class _EvalPayload:
    """Reducer payload proving why pickle is not a policy-safe data codec."""

    def __reduce__(self):
        return eval, ("40 + 2",)


@pytest.mark.parametrize("isolation", ["none", "process"])
def test_default_stdlib_blocks_pickle_reducer_eval(isolation):
    payload = base64.b64encode(pickle.dumps(_EvalPayload())).decode("ascii")
    ws = make_ws(python=PythonConfig(isolation=isolation))
    try:
        r = ws.run_python(
            "import base64, pickle\n"
            f"escaped = pickle.loads(base64.b64decode({payload!r}))"
        )
        assert not r
        assert "pickle" in (r.error or "") and "not" in (r.error or "").lower()
    finally:
        ws.close()


def test_pickle_remains_available_as_an_explicit_unsafe_grant():
    ws = make_ws(python=PythonConfig(modules=[ModuleGrant(pickle)]))
    try:
        r = ws.run_python(
            "import pickle\nroundtrip = pickle.loads(pickle.dumps({'answer': 42}))"
        )
        assert r, r.error
        assert r.namespace["roundtrip"] == {"answer": 42}
    finally:
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
    that chose a pool explicitly keeps it.

    Do not drop this as obsolete under the forkserver default. The
    tempting argument — "the broker never imports your grants, so no
    arrow state exists to inherit" — is exactly what
    ``PythonConfig.preload_grants=True`` invalidates: it imports the
    granted stack, pyarrow included, into the broker that every worker
    is forked from. The pin works there because the ordering holds
    (this runs while the config is built, which is before any worker
    and therefore before the broker starts, and the broker inherits the
    environment).
    """
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
    assert ws.files.fs.read("plot.png")[:8] == b"\x89PNG\r\n\x1a\n"
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


# -- unittest.mock ------------------------------------------------------------


def test_stdlib_grants_unittest_mock():
    """The workspace module a handler imports is the unit worth testing,
    and patching its collaborators is how that test is written."""
    ws = make_ws()
    r = ws.run_python(
        "from unittest.mock import MagicMock, patch\n"
        "m = MagicMock(return_value=7)\n"
        "out = m('ignored')\n"
    )
    assert r, r.error
    assert r.namespace["out"] == 7
    ws.close()


def test_patch_object_replaces_what_a_workspace_module_calls():
    """A patch on the module's own name is the name the module's code
    reads — the facade a workspace module is loaded behind resolves to
    the module itself."""
    ws = make_ws()
    ws.files.fs.write(
        "/workspace/_lib.py",
        b"def fetch():\n    return 'real'\n\n\ndef headline():\n    return fetch().upper()\n",
    )
    r = ws.run_python(
        "import _lib\n"
        "from unittest.mock import patch\n"
        "with patch.object(_lib, 'fetch', return_value='fake'):\n"
        "    patched = _lib.headline()\n"
        "restored = _lib.headline()\n"
    )
    assert r, r.error
    assert r.namespace["patched"] == "FAKE"
    assert r.namespace["restored"] == "REAL"  # the patch came back off
    ws.close()


def test_patch_object_replaces_a_method_on_a_workspace_class():
    ws = make_ws()
    ws.files.fs.write(
        "/workspace/_svc.py",
        b"class Svc:\n"
        b"    def rate(self):\n"
        b"        return 1.0\n\n"
        b"    def total(self, n):\n"
        b"        return n * self.rate()\n",
    )
    r = ws.run_python(
        "from _svc import Svc\n"
        "from unittest.mock import patch\n"
        "with patch.object(Svc, 'rate', return_value=2.0):\n"
        "    patched = Svc().total(3)\n"
        "restored = Svc().total(3)\n"
    )
    assert r, r.error
    assert r.namespace["patched"] == 6.0
    assert r.namespace["restored"] == 3.0
    ws.close()


def test_mock_internals_stay_behind_the_default_exclude():
    """The grant is the public mock API; `_patch` and friends are the
    implementation and stay where the default exclude leaves them."""
    ws = make_ws()
    r = ws.run_python("import unittest.mock as m\nout = m._patch\n")
    assert not r
    ws.close()


# -- what a config's modules flatten to --------------------------------------


def test_flatten_grants_is_public_and_includes_the_stdlib_set():
    """The one honest answer to "what may this code import": a primer
    that names the importable modules asks this rather than re-deriving
    the rules."""
    import json as json_mod

    from nontainer.executor import flatten_grants

    names = {
        g.name or g.module.__name__
        for g in flatten_grants(PythonConfig(stdlib=True, modules=[json_mod]))
    }
    assert "json" in names and "math" in names

    bare = flatten_grants(PythonConfig(stdlib=False, modules=[json_mod]))
    assert [g.module for g in bare] == [json_mod]


def test_flatten_grants_flattens_a_preset_list_one_level():
    """A preset (`presets.dataframes()`) is a LIST of grants and a
    config holds it as one entry; the flat list is what an executor
    registers."""
    import json as json_mod
    import math as math_mod

    from nontainer.executor import flatten_grants

    preset = [json_mod, math_mod]
    grants = flatten_grants(PythonConfig(stdlib=False, modules=[preset]))
    assert [g.module for g in grants] == [json_mod, math_mod]
