"""ws-vitest cross-rung conformance: local vs dud-subprocess.

The browser is on the HOST on both rungs — Chromium lives there, and on
a guest rung the test file is read from the workspace by the driver
rather than by anything in the guest. So what the dud case actually
asserts is that the ferry changes nothing: the verb typed in a real bash
guest reaches the same host implementation, with its argv mapped, and
reports byte for byte what the local rung reports.

Requires the ``dud`` extra and the ``[apps]`` browser; skipped when
either is missing. Pins ``backend="subprocess"`` explicitly (the only
rung without a hypervisor); the VM rungs share the guest supervisor
paths.
"""

import re

import pytest

from nontainer import Workspace
from nontainer.providers import KvgitProvider
from nontainer.wsvitest import register_wsvitest

pytest.importorskip("dud")

from nontainer.executor_dud import DudExecutor  # noqa: E402

UTIL = "export const add = (a, b) => a + b;\nexport const LIMIT = 3;\n"

MIXED = """\
import { add, LIMIT } from '../app/util.js';

describe('add', () => {
  it('adds two numbers', () => {
    expect(add(1, 2)).toBe(3);
  });
  it('reads the wrong limit', () => {
    expect(LIMIT).toBe(4);
  });
  it('reaches no api route', async () => {
    const r = await fetch('api/scores');
    expect(r.ok).toBe(true);
  });
});
"""

BROKEN = "throw new Error('this file does not load');\n"

#: The three things two rungs cannot agree on, none of them the verb's:
#: how long a run took, how long a test took, and termish's own
#: nonzero-exit announcement (real bash writes none).
_ELAPSED = re.compile(r"\d+\.\d\ds|\d+ms")
_SHELL_EXIT = re.compile(r"^ws-vitest: exited with code \d+\n?", re.M)

#: The console tail is the browser's, and a 404's resource line arrives
#: whenever the page happens to log it.
_CONSOLE = re.compile(r"^\[(error|warning|log|info)\].*\n?", re.M)


def _normalize(text):
    return _CONSOLE.sub("", _SHELL_EXIT.sub("", _ELAPSED.sub("0ms", text)))


def _ws(rung, name):
    executor = DudExecutor(backend="subprocess") if rung == "dud" else None
    kw = {"executor": executor} if executor is not None else {}
    w = Workspace(KvgitProvider.open(None, session=f"wsvitest-{rung}-{name}"), **kw)
    register_wsvitest(w)
    w.files.fs.write("/workspace/app/util.js", UTIL.encode())
    w.files.fs.write("/workspace/tests/mixed.test.js", MIXED.encode())
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
        "ws-vitest",
        "ws-vitest run",
        "ws-vitest --reporter=verbose",
        "ws-vitest -t 'adds two'",
        "ws-vitest mixed",
        "ws-vitest --bail",
        "ws-vitest --coverage",
        "ws-vitest nosuchfile",
    ],
)
def test_both_rungs_report_identically(script, request, chromium_available):
    name = re.sub(r"[^A-Za-z0-9_.-]", "-", request.node.name)
    local = _run("local", name, script)
    dud = _run("dud", name, script)
    assert local == dud, (local, dud)


def test_a_suite_that_would_not_load_reads_the_same_on_both_rungs(
    request, chromium_available
):
    name = re.sub(r"[^A-Za-z0-9_.-]", "-", request.node.name)
    out = []
    for rung in ("local", "dud"):
        w = _ws(rung, name)
        try:
            w.files.fs.write("/workspace/tests/broken.test.js", BROKEN.encode())
            r = w.terminal("ws-vitest")
            out.append((r.exit_code, _normalize(r.stdout + r.stderr)))
        finally:
            w.close()
    assert out[0] == out[1]
    assert out[0][0] == 1
    assert " FAIL  tests/broken.test.js" in out[0][1]


def _root(w):
    """The absolute path an agent on this rung would type: the guest
    names its own tree, and the ferry is what brings it home."""
    return getattr(w.runtime.executor, "_work", "") or "/workspace"


@pytest.mark.parametrize(
    "template",
    [
        "ws-vitest {root}/tests/mixed.test.js",
        "ws-vitest {root}/tests",
        "cd tests; ws-vitest mixed.test.js",
    ],
)
def test_a_path_filter_resolves_like_every_other_verbs(
    template, request, chromium_available
):
    """A filter that names an absolute path is a path argument: the
    guest spells it in guest coordinates and the ferry rewrites it, so
    both rungs select the same file."""
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
    assert " Test Files  1 failed (1)" in out[0][1], out[0][1]
    assert "no test files matched" not in out[0][1], out[0][1]


def test_a_guest_written_test_file_reaches_the_host_browser(
    request, chromium_available
):
    """The asymmetry, asserted rather than described: the guest writes
    the file with real bash, and the browser that runs it is on the
    host. Sync-on-verb is what carries it across."""
    name = re.sub(r"[^A-Za-z0-9_.-]", "-", request.node.name)
    w = _ws("dud", name)
    try:
        r = w.terminal(
            "printf \"it('written in the guest', () => "
            '{ expect(1).toBe(1); });\\n" > tests/guest.test.js; '
            "ws-vitest tests/guest.test.js"
        )
        assert r.exit_code == 0, r.stdout + r.stderr
        assert " ✓ tests/guest.test.js (1 test)" in r.stdout
    finally:
        w.close()
