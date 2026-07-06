"""Apps M3: live serving — build_router, tokens, gating, quiesce."""

import json

import pytest

from nontainer import Workspace
from nontainer.providers import KvgitProvider

pytest.importorskip("starlette")

from starlette.applications import Starlette  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from nontainer.apps import build_router, enable_apps, mint_token  # noqa: E402

HANDLER = """
def get(req):
    return {"scores": cache.get("scores", [])}

def post(req):
    name = req.require("name")
    scores = list(cache.get("scores", []))
    scores.append(name)
    cache["scores"] = scores
    return {"ok": True}
"""


def make_served(**router_kwargs):
    ws = Workspace(KvgitProvider.open(None, session="s1"))
    enable_apps(ws)
    ws.fs.makedirs("/app/api", exist_ok=True)
    ws.fs.write("/app/index.html", b"<html><body><h1>hi</h1></body></html>")
    ws.fs.write("/app/api/scores.py", HANDLER.encode())
    ws.cache["scores"] = ["alice"]
    ws.checkpoint()

    token = mint_token()
    tokens = {token: ws}
    router = build_router(lambda t: tokens.get(t), **router_kwargs)
    app = Starlette()
    app.mount("/apps", router)
    return ws, token, TestClient(app)


def test_mint_token_shape():
    t1, t2 = mint_token(), mint_token()
    assert t1 != t2
    assert len(t1) > 40  # capability-grade


def test_unknown_token_404():
    ws, token, client = make_served()
    assert client.get("/apps/not-a-token/").status_code == 404
    ws.close()


def test_static_and_csp():
    ws, token, client = make_served()
    r = client.get(f"/apps/{token}/")
    assert r.status_code == 200
    assert "<h1>hi</h1>" in r.text
    assert "content-security-policy" in r.headers
    # non-HTML gets no CSP
    r2 = client.get(f"/apps/{token}/api/scores")
    assert "content-security-policy" not in r2.headers
    ws.close()


def test_api_get_and_post_roundtrip():
    ws, token, client = make_served()
    r = client.get(f"/apps/{token}/api/scores")
    assert r.status_code == 200
    assert r.json() == {"scores": ["alice"]}

    r = client.post(f"/apps/{token}/api/scores", content=json.dumps({"name": "bob"}))
    assert r.status_code == 200
    assert client.get(f"/apps/{token}/api/scores").json() == {
        "scores": ["alice", "bob"]
    }
    assert ws.cache["scores"] == ["alice", "bob"]  # real backend state
    ws.close()


def test_handler_errors_stay_500_and_logged():
    ws, token, client = make_served()
    ws.fs.write("/app/api/boom.py", b"def get(req):\n    return 1/0\n")
    r = client.get(f"/apps/{token}/api/boom")
    assert r.status_code == 500
    assert "ZeroDivisionError" in ws.fs.read("/app/logs/api.log").decode()
    ws.close()


def test_rate_limit_429():
    ws, token, client = make_served(rate_limit_per_min=3)
    for _ in range(3):
        assert client.get(f"/apps/{token}/api/scores").status_code == 200
    r = client.get(f"/apps/{token}/api/scores")
    assert r.status_code == 429
    assert "rate limit" in r.text
    ws.close()


def test_quiesce_checkpoint():
    ws, token, client = make_served(quiesce_seconds=0.0)
    before = len(list(ws.history()))
    client.post(f"/apps/{token}/api/scores", content=json.dumps({"name": "bob"}))
    # mutation staged, not committed (requests never mint commits)
    assert len(list(ws.history())) == before
    # next request after quiesce window → lazy checkpoint with source=api
    client.get(f"/apps/{token}/api/scores")
    entries = list(ws.history())
    assert len(entries) == before + 1
    assert entries[0].info == {"source": "api"}
    ws.close()


def test_relative_urls_work_under_mount_prefix():
    """The relocatability payoff: the same app served under
    /apps/{token}/ answers relative API paths correctly."""
    ws, token, client = make_served()
    # a browser resolving fetch('api/scores') from /apps/{token}/ hits:
    r = client.get(f"/apps/{token}/api/scores")
    assert r.status_code == 200
    ws.close()
