"""ws-pytest cross-rung conformance: local vs dud-subprocess.

The same tests through ``ws.terminal("ws-pytest")`` must read
identically on both rungs — the verb's contract, not the rung's
dialect. The test code runs in the guest on the dud rung and in
sandtrap's sandbox locally, so the report is the only thing that can
say so.

Requires the ``dud`` extra; skipped when it isn't installed. Pins
``backend="subprocess"`` explicitly (the only rung without a
hypervisor); the VM rungs share the guest supervisor paths.
"""

import re

import pytest

from nontainer import Workspace
from nontainer.providers import KvgitProvider
from nontainer.wspytest import register_wspytest

pytest.importorskip("dud")

from nontainer.executor_dud import DudExecutor  # noqa: E402

LIB = "def load(db):\n    return [s.upper() for s in db.query()]\n"

MIXED = (
    "from unittest.mock import MagicMock\n"
    "\n"
    "from app.api._lib import load\n"
    "\n"
    "\n"
    "def test_upper():\n"
    "    db = MagicMock()\n"
    "    db.query.return_value = ['ann']\n"
    "    assert load(db) == ['ANN']\n"
    "\n"
    "\n"
    "def test_wrong_expectation():\n"
    "    db = MagicMock()\n"
    "    db.query.return_value = ['ann']\n"
    "    assert load(db) == ['ann'], 'case is not preserved'\n"
    "\n"
    "\n"
    "def test_reaches_a_helper():\n"
    "    load(None)\n"
)

BROKEN = "raise RuntimeError('this file does not import')\n"

#: The one thing two rungs cannot agree on: how long the run took.
_ELAPSED = re.compile(r"in \d+\.\d\ds")

#: termish announces a nonzero exit; real bash does not. That line is
#: the shell's, not the verb's, so it is normalized away — everything
#: the verb itself writes must match byte for byte.
_SHELL_EXIT = re.compile(r"^ws-pytest: exited with code \d+\n?", re.M)


def _normalize(text):
    return _SHELL_EXIT.sub("", _ELAPSED.sub("in 0.00s", text))


def _ws(rung, name):
    executor = DudExecutor(backend="subprocess") if rung == "dud" else None
    kw = {"executor": executor} if executor is not None else {}
    w = Workspace(KvgitProvider.open(None, session=f"wspytest-{rung}-{name}"), **kw)
    register_wspytest(w)
    w.files.fs.write("/workspace/app/api/_lib.py", LIB.encode())
    w.files.fs.write("/workspace/tests/test_mixed.py", MIXED.encode())
    return w


def _run(rung, name, script):
    w = _ws(rung, name)
    try:
        r = w.terminal(script)
        return r.exit_code, _normalize(r.stdout + r.stderr)
    finally:
        w.close()


@pytest.mark.parametrize(
    "script",
    [
        "ws-pytest",
        "ws-pytest -q",
        "ws-pytest -v",
        "ws-pytest --tb=long",
        "ws-pytest tests/test_mixed.py::test_upper",
        "ws-pytest -k upper",
        "ws-pytest -x",
        "ws-pytest -s",
    ],
)
def test_both_rungs_report_identically(script, request):
    name = re.sub(r"[^A-Za-z0-9_.-]", "-", request.node.name)
    local = _run("local", name, script)
    dud = _run("dud", name, script)
    assert local == dud


def test_a_collection_error_reads_the_same_on_both_rungs(request):
    name = re.sub(r"[^A-Za-z0-9_.-]", "-", request.node.name)
    out = []
    for rung in ("local", "dud"):
        w = _ws(rung, name)
        try:
            w.files.fs.write("/workspace/tests/test_broken.py", BROKEN.encode())
            r = w.terminal("ws-pytest")
            out.append((r.exit_code, _normalize(r.stdout + r.stderr)))
        finally:
            w.close()
    assert out[0] == out[1]
    assert out[0][0] == 2
    assert "ERROR collecting tests/test_broken.py" in out[0][1]


def test_a_test_reaching_a_workspace_module_names_that_file_on_both_rungs(request):
    """The frame that raised is in app/api/_lib.py on either rung —
    sandtrap's VFS module locally, a real file in the guest."""
    name = re.sub(r"[^A-Za-z0-9_.-]", "-", request.node.name)
    for rung in ("local", "dud"):
        code, text = _run(rung, name, "ws-pytest -k helper")
        assert code == 1, text
        assert "app/api/_lib.py:2: in load" in text, (rung, text)


def _root(w):
    """The absolute path an agent on this rung would type: the guest
    names its own tree, and the ferry is what brings it home."""
    return getattr(w.runtime.executor, "_work", "") or "/workspace"


@pytest.mark.parametrize(
    "template",
    [
        "ws-pytest {root}/tests/test_mixed.py",
        "ws-pytest {root}/tests/test_mixed.py::test_upper",
        "ws-pytest {root}/tests",
        "cd tests; ws-pytest test_mixed.py::test_upper",
    ],
)
def test_a_path_selector_resolves_like_every_other_verbs(template, request):
    """A selector is a path argument: absolute kept, relative resolved
    against the cwd — the rule every ws-* verb reads its paths by."""
    name = re.sub(r"[^A-Za-z0-9_.-]", "-", request.node.name)
    out = []
    for rung in ("local", "dud"):
        w = _ws(rung, name)
        try:
            r = w.terminal(template.format(root=_root(w)))
            out.append((r.exit_code, _normalize(r.stdout + r.stderr)))
        finally:
            w.close()
    assert out[0] == out[1], template
    assert "collected" in out[0][1], out[0][1]
    assert "not found" not in out[0][1], out[0][1]
