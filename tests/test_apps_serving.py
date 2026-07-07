"""Frozen app serving: read-only snapshots, concurrency, no mutation."""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from nontainer import PythonConfig, Workspace
from nontainer.providers import KvgitProvider

pytest.importorskip("starlette")

from starlette.applications import Starlette  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from nontainer.apps import build_router, enable_apps, mint_token  # noqa: E402

# read-only handlers: GET reads seeded state; POST is a read that takes a
# body (a filter), not a mutation.
HANDLER = """
def get(req):
    limit = int(req.params.get("limit", 10))
    return {"scores": cache.get("scores", [])[:limit]}

def post(req):
    prefix = req.require("prefix")
    return {"matches": [s for s in cache.get("scores", []) if s.startswith(prefix)]}
"""

WRITER = """
def post(req):
    cache["x"] = 1        # mutation — rejected under frozen serving
    return {"ok": True}
"""


def make_served(*, python=None, on_log=None, **router_kwargs):
    ws = Workspace(KvgitProvider.open(None, session="s1"), python=python)
    enable_apps(ws)
    ws.fs.makedirs("/app/api", exist_ok=True)
    ws.fs.write("/app/index.html", b"<html><body><h1>hi</h1></body></html>")
    ws.fs.write("/app/api/scores.py", HANDLER.encode())
    ws.fs.write("/app/api/writer.py", WRITER.encode())
    ws.cache["scores"] = ["alice", "amy", "bob"]
    ws.checkpoint()

    token = mint_token()
    tokens = {token: ws}
    router = build_router(lambda t: tokens.get(t), on_log=on_log, **router_kwargs)
    app = Starlette()
    app.mount("/apps", router)
    return ws, token, TestClient(app)


def test_mint_token_shape():
    assert mint_token() != mint_token()
    assert len(mint_token()) > 40


def test_unknown_token_404():
    ws, token, client = make_served()
    assert client.get("/apps/not-a-token/").status_code == 404
    ws.close()


def test_static_and_csp():
    ws, token, client = make_served()
    r = client.get(f"/apps/{token}/")
    assert r.status_code == 200 and "<h1>hi</h1>" in r.text
    assert "content-security-policy" in r.headers
    r2 = client.get(f"/apps/{token}/api/scores")
    assert "content-security-policy" not in r2.headers  # non-HTML
    ws.close()


def test_get_reads_seeded_state():
    ws, token, client = make_served()
    r = client.get(f"/apps/{token}/api/scores?limit=2")
    assert r.status_code == 200
    assert r.json() == {"scores": ["alice", "amy"]}
    ws.close()


def test_post_as_read_takes_a_body():
    ws, token, client = make_served()
    r = client.post(
        f"/apps/{token}/api/scores", content=json.dumps({"prefix": "a"})
    )
    assert r.status_code == 200
    assert r.json() == {"matches": ["alice", "amy"]}
    ws.close()


def test_mutation_is_rejected():
    logs: list[str] = []
    ws, token, client = make_served(on_log=logs.append)
    r = client.post(f"/apps/{token}/api/writer", content="{}")
    assert r.status_code == 500  # read-only VFS → PermissionError
    assert any("PermissionError" in m or "read-only" in m.lower() for m in logs)
    ws.close()


def test_handler_error_is_500_and_logged_to_sink():
    logs: list[str] = []
    ws, token, client = make_served(on_log=logs.append)
    ws.fs.write("/app/api/boom.py", b"def get(req):\n    return 1/0\n")
    ws.checkpoint()
    r = client.get(f"/apps/{token}/api/boom")
    assert r.status_code == 500
    assert any("ZeroDivisionError" in m for m in logs)  # off-VFS log
    ws.close()


def test_concurrent_requests_to_one_snapshot():
    """The frozen payoff: many requests to one snapshot run without a
    per-session lock — fresh read-only sandbox each, no corruption."""
    ws, token, client = make_served()
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(
                lambda _: client.get(f"/apps/{token}/api/scores?limit=3"),
                range(40),
            )
        )
    assert all(r.status_code == 200 for r in results)
    assert all(r.json() == {"scores": ["alice", "amy", "bob"]} for r in results)
    ws.close()


def test_served_handler_can_call_host_objects():
    """The dashboard shape: a read-only telemetry client injected via
    host_objects, queried from a served handler."""

    class Telemetry:
        def series(self, metric):
            return [1, 2, 3] if metric == "cpu" else []

    ws, token, client = make_served(
        python=PythonConfig(host_objects={"db": Telemetry()})
    )
    ws.fs.write(
        "/app/api/metric.py",
        b"def get(req):\n    return {'points': db.series(req.params['m'])}\n",
    )
    ws.checkpoint()
    r = client.get(f"/apps/{token}/api/metric?m=cpu")
    assert r.status_code == 200
    assert r.json() == {"points": [1, 2, 3]}
    ws.close()


def test_serving_is_stateless():
    """resolve is called per request (no snapshot cache), and the router
    does not close the returned workspace (embedder owns lifecycle)."""
    ws, token, _ = make_served()
    calls = {"n": 0}

    def resolve(t):
        calls["n"] += 1
        return ws if t == token else None

    app = Starlette()
    app.mount("/apps", build_router(resolve))
    client = TestClient(app)

    for _ in range(3):
        assert client.get(f"/apps/{token}/api/scores").status_code == 200
    assert calls["n"] == 3  # resolve called every request, no cache
    # the workspace is still open — the router never closed it
    assert ws.terminal("echo alive").stdout.strip() == "alive"
    ws.close()
