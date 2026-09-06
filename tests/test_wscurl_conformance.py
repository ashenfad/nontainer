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


@pytest.fixture(params=["local", "dud"])
def aws(request):
    """Fresh apps workspace per rung per test."""
    param = request.param
    if param == "dud":
        pytest.importorskip("dud")
        from nontainer.executor_dud import DudExecutor

        executor = DudExecutor(backend="subprocess")
    else:
        executor = None
    name = re.sub(r"[^A-Za-z0-9_.-]", "-", request.node.name)
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
