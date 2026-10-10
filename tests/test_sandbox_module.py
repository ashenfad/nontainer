"""Agent code runs in a module of its own on every rung (sandtrap's on
the sandtrap rungs, ``__dud__`` on dud): a dataclass with a quoted or
postponed annotation works, a ``from __future__`` import takes effect,
and the cache still holds data, never a class the code defined."""

import dataclasses
import sys

import pytest

from nontainer import Profile, PythonConfig, Store
from nontainer.executor_dud import _hoist_future


def open_ws(rung):
    if rung == "dud":
        if sys.version_info < (3, 11):
            pytest.skip("dud needs Python 3.11+")
        pytest.importorskip("dud")
        from nontainer.executor_dud import DudExecutor

        profile = Profile(
            python=PythonConfig(),
            executor_factory=lambda: DudExecutor(backend="subprocess"),
        )
    else:
        profile = Profile(python=PythonConfig(isolation=rung))
    store = Store(memory=True)
    return store.open("s", profile=profile), store


@pytest.fixture(params=["none", "process", "dud"])
def ws(request):
    ws, store = open_ws(request.param)
    yield ws
    ws.close()
    store.close()


QUOTED = """\
from dataclasses import dataclass

@dataclass
class Node:
    label: str
    child: "Node | None"

print(Node("a", Node("b", None)).child.label)
"""

POSTPONED = """\
\"\"\"A script.\"\"\"
from __future__ import annotations
from dataclasses import dataclass

@dataclass
class Node:
    label: str
    child: Node | None

print(Node("a", Node("b", None)).child.label)
"""


@pytest.mark.parametrize("code", [QUOTED, POSTPONED], ids=["quoted", "postponed"])
def test_a_recursive_dataclass_works(ws, code):
    r = ws.run_python(code)
    assert r.error is None, r.error
    assert r.stdout.strip() == "b"


def test_a_late_line_keeps_its_number_after_a_future_import(ws):
    r = ws.run_python(
        "from __future__ import annotations\nx = 1\nraise ValueError('here')\n"
    )
    assert r.error is not None
    assert "line 3" in r.error, r.error


def test_a_statement_sharing_the_future_imports_line_still_runs(ws):
    r = ws.run_python(
        "from __future__ import annotations; from host import cache\n"
        "cache['n'] = 1\n"
        "def f(x: Missing) -> None: ...\n"  # postponed, so never looked up
        "print(cache['n'])\n"
    )
    assert r.error is None, r.error
    assert r.stdout.strip() == "1"


def test_the_cache_holds_data_not_a_class_the_code_defined(ws):
    r = ws.run_python(
        "class C:\n"
        "    pass\n"
        "cache['plain'] = {'n': 1}\n"
        "try:\n"
        "    cache['made'] = C()\n"
        "except Exception as error:\n"
        "    print('refused', type(error).__name__)\n"
    )
    # in a process the cache refuses it at the write; on dud, at the flush
    refused = "refused" in r.stdout or (
        r.error and "this session's code defined" in r.error
    )
    assert refused, (r.stdout, r.error)
    assert "made" not in ws.cache
    if "refused" in r.stdout:  # refused at the write; dud's flush is all or nothing
        assert ws.cache["plain"] == {"n": 1}


def test_the_host_still_caches_its_own_classes():
    store = Store(memory=True)
    ws = store.open("s")
    try:

        @dataclasses.dataclass
        class Local:
            n: int

        ws.cache["host"] = {"n": 1}
        assert ws.cache["host"] == {"n": 1}
    finally:
        ws.close()
        store.close()


@pytest.mark.parametrize(
    "code, hoisted",
    [
        ("x = 1\n", ""),
        (
            "from __future__ import annotations\nx = 1\n",
            "from __future__ import annotations\n",
        ),
        (
            '"""doc"""\nfrom __future__ import annotations\n',
            "from __future__ import annotations\n",
        ),
        ("x = 1\nfrom __future__ import annotations\n", ""),
        ("def broken(:\n", ""),
    ],
)
def test_hoisting_takes_only_the_leading_future_imports(code, hoisted):
    future, rest = _hoist_future(code)
    assert future == hoisted
    assert rest.count("\n") == code.count("\n")  # line numbers kept
    if hoisted:
        assert "__future__" not in rest


@pytest.mark.parametrize(
    "code, hoisted, rest",
    [
        (
            "from __future__ import annotations; import os\nx = 1\n",
            "from __future__ import annotations\n",
            "import os\nx = 1\n",
        ),
        (
            "from __future__ import annotations ;  import os  # why\n",
            "from __future__ import annotations\n",
            "import os  # why\n",
        ),
        (
            '"""doc"""; from __future__ import annotations; x = "é"\n',
            "from __future__ import annotations\n",
            '"""doc"""; x = "é"\n',
        ),
        (
            "from __future__ import annotations; from __future__ import division\n",
            "from __future__ import annotations\nfrom __future__ import division\n",
            "\n",
        ),
        (
            "from __future__ import (\n    annotations,\n); x = 1\ny = 2\n",
            "from __future__ import (\n    annotations,\n)\n",
            "\n\nx = 1\ny = 2\n",
        ),
    ],
    ids=["shared", "spaced", "after-docstring", "two", "parenthesized"],
)
def test_hoisting_moves_only_the_import_not_its_line(code, hoisted, rest):
    assert _hoist_future(code) == (hoisted, rest)
