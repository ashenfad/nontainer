"""ws-curl cross-rung conformance: local vs dud-subprocess.

The same fetch scripts through ``ws.terminal`` must read identically
on both rungs — the verbs' contract, not the rung's dialect. Covers
the whole local flag surface that fits a frame: GET pipelines, POST,
-i/-w, error shapes (unknown flag, external URL, HTTP status), and
``-o`` captures including same-script use.

Each test gets a fresh session per rung. Shell features stay within
both shells (no ``printf`` — termish lacks it).
"""

import json
import re

import pytest

from nontainer import Workspace
from nontainer.apps import enable_apps
from nontainer.providers import KvgitProvider


def _apps_ws(param, name):
    if param == "dud":
        pytest.importorskip("dud")
        from nontainer.executor_dud import DudExecutor

        executor = DudExecutor(backend="subprocess")
    else:
        executor = None
    kw = {"executor": executor} if executor is not None else {}
    w = Workspace(KvgitProvider.open(None, session=f"wscurl-{param}-{name}"), **kw)
    enable_apps(w)
    w.fs.makedirs("/workspace/app/api", exist_ok=True)
    w.fs.write(
        "/workspace/app/api/nums.py",
        b"def get(req):\n    return {'nums': [3, 1, 2]}\n",
    )
    w.fs.write(
        "/workspace/app/api/echo.py",
        b"def post(req):\n    return {'got': req.require('msg')}\n",
    )
    return w


@pytest.fixture(params=["local", "dud"])
def aws(request):
    """Fresh apps workspace per rung per test."""
    param = request.param
    name = re.sub(r"[^A-Za-z0-9_.-]", "-", request.node.name)
    w = _apps_ws(param, name)
    try:
        yield w
    finally:
        w.close()


@pytest.fixture(params=["dud"])
def daws(request):
    """Dud-only leg: the frame-budget policy lives in the ferry, which
    the local rung never invokes."""
    name = re.sub(r"[^A-Za-z0-9_.-]", "-", request.node.name)
    w = _apps_ws("dud", name)
    try:
        yield w
    finally:
        w.close()


def test_get_in_pipeline(aws):
    r = aws.terminal("ws-curl /api/nums | jq -r '.nums[]' | sort")
    assert r.exit_code == 0, r.stderr
    assert r.stdout.split() == ["1", "2", "3"]


def test_post_with_data(aws):
    r = aws.terminal('ws-curl -X POST -d \'{"msg": "hi"}\' /api/echo')
    assert r.exit_code == 0, r.stderr
    assert json.loads(r.stdout) == {"got": "hi"}


def test_include_and_write_out(aws):
    r = aws.terminal("ws-curl -i /api/nums")
    assert r.exit_code == 0, r.stderr
    assert r.stdout.startswith("HTTP/1.1 200")
    r = aws.terminal("ws-curl -s -w 'code=%{http_code}\\n' /api/nums")
    assert r.exit_code == 0, r.stderr
    assert "code=200" in r.stdout


def test_output_to_file_and_same_script_use(aws):
    """``-o`` captures land where the same script can use them — on the
    dud rung via the answer triple, not a provider write the guest
    can't see."""
    r = aws.terminal("ws-curl -o out.json /api/nums; cat out.json")
    assert r.exit_code == 0, r.stderr
    assert json.loads(r.stdout) == {"nums": [3, 1, 2]}
    assert json.loads(aws.fs.read("/workspace/out.json")) == {"nums": [3, 1, 2]}


def test_same_script_handler_write_then_fetch(aws):
    """Sync-on-verb for fetch: a handler written earlier in the SAME
    script is served, not 404'd (dud) or read stale."""
    r = aws.terminal(
        "cat > app/api/fresh.py <<'EOF'\n"
        "def get(req):\n"
        "    return {'fresh': True}\n"
        "EOF\n"
        "ws-curl /api/fresh"
    )
    assert r.exit_code == 0, r.stderr
    assert json.loads(r.stdout) == {"fresh": True}


def test_oversized_capture_lands_with_http_failure(daws, monkeypatch):
    """Over budget, `-o` captures still land — and the command's own
    HTTP failure rides through (exit code and stderr), so `&&` chains
    stop instead of reading a failed endpoint as successful."""
    import nontainer.wscurl as wscurl

    monkeypatch.setattr(wscurl, "_FRAME_BUDGET", 16)
    r = daws.terminal("ws-curl -o big.json /api/absent")
    assert r.exit_code == 22
    assert "HTTP 404" in r.stdout
    assert "exceeded the guest round-trip frame" in r.stdout
    assert daws._executor_stale is True
    # Landed in the workspace; under the real budget the next call
    # re-syncs the guest and the capture reads back.
    monkeypatch.undo()
    r = daws.terminal("cat big.json; echo; ws-curl /api/nums | jq -r .nums[0]")
    assert r.exit_code == 0, r.stdout
    assert "no such endpoint: /api/absent" in r.stdout
    assert r.stdout.splitlines()[-1] == "3"


def test_oversized_stdout_failure_withholds_body(daws, monkeypatch):
    """Over budget with no captures, an HTTP failure reads exactly as
    it would under budget — plus one clause naming the withheld body."""
    import nontainer.wscurl as wscurl

    monkeypatch.setattr(wscurl, "_FRAME_BUDGET", 16)
    r = daws.terminal("ws-curl /api/absent")
    assert r.exit_code == 22
    assert "HTTP 404" in r.stdout
    assert "withheld (exceeds guest frame)" in r.stdout


def test_errors_read_identically(aws):
    # Error text rides stdout on the dud rung (merged transcript) and
    # stderr locally — identical bytes, different stream.
    def said(r, text):
        return text in (r.stdout + r.stderr)

    r = aws.terminal("ws-curl --frobnicate /api/nums")
    assert r.exit_code == 2
    assert said(r, "ws-curl: unknown flag")
    r = aws.terminal("ws-curl https://example.com/x")
    assert r.exit_code == 6
    assert said(r, "no internet")
    r = aws.terminal("ws-curl /api/absent")
    assert r.exit_code == 22
    assert said(r, "HTTP 404")
