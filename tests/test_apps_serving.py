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

# agent-set headers, idiomatically cased — the served response honors
# the content type and REPLACES the app's CSP with the configured one.
HTMLER = """
def get(req):
    return Response(
        body="<html><body>custom</body></html>",
        headers={
            "Content-Type": "text/html",
            "Content-Security-Policy": "default-src 'none'",
        },
    )
"""

# every category of response header an app can reach for: kept
# (representation/caching/custom) and dropped (origin privileges).
HEADERER = """
def get(req):
    return Response(
        body={"ok": True},
        headers={
            "Cache-Control": "max-age=60",
            "Vary": "Authorization",
            "ETag": '"v1"',
            "X-Custom": "1",
            "Set-Cookie": "sid=abc; Path=/",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Credentials": "true",
            "X-Frame-Options": "ALLOWALL",
            "X-Accel-Redirect": "/internal/secrets",
            "X-Sendfile": "/etc/passwd",
        },
    )
"""


def make_served(*, python=None, on_log=None, **router_kwargs):
    ws = Workspace(KvgitProvider.open(None, session="s1"), python=python)
    enable_apps(ws)
    ws.fs.makedirs("/workspace/app/api", exist_ok=True)
    ws.fs.write("/workspace/app/index.html", b"<html><body><h1>hi</h1></body></html>")
    ws.fs.write("/workspace/app/api/scores.py", HANDLER.encode())
    ws.fs.write("/workspace/app/api/writer.py", WRITER.encode())
    ws.fs.write("/workspace/app/api/page.py", HTMLER.encode())
    ws.fs.write("/workspace/app/api/headers.py", HEADERER.encode())
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


def test_agent_content_type_wins_but_not_the_policy():
    """Cased agent headers are honored where they describe the
    RESPONSE: Content-Type overrides the inferred type. A policy is not
    a description — the configured CSP replaces the app's own, once, so
    handler code cannot pick the containment it runs under. (This test
    once asserted the opposite: that an app-set CSP deferred the router
    default. That deference was the bug.)"""
    ws, token, client = make_served(csp="default-src 'self'")
    r = client.get(f"/apps/{token}/api/page")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert r.headers.get_list("content-security-policy") == ["default-src 'self'"]
    ws.close()


def test_response_headers_are_allowlisted():
    """Representation, caching and x-* custom headers reach the wire;
    the ones that grant privileges on the serving origin do not.

    Vary travels with Cache-Control: a handler may vary on an
    allowlisted request header, and a cacheable response that does not
    say so lets a shared cache hand one caller's variant to the next.
    The x-* customs that a server in front would execute rather than
    forward do not reach it — nginx acts on x-accel-redirect from an
    upstream, which would reach an internal location through the
    proxy."""
    ws, token, client = make_served()
    r = client.get(f"/apps/{token}/api/headers")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "max-age=60"
    assert r.headers["vary"] == "Authorization"
    assert r.headers["etag"] == '"v1"'
    assert r.headers["x-custom"] == "1"
    assert "set-cookie" not in r.headers
    assert not r.cookies
    assert "access-control-allow-origin" not in r.headers
    assert "access-control-allow-credentials" not in r.headers
    assert "x-frame-options" not in r.headers
    assert "x-accel-redirect" not in r.headers
    assert "x-sendfile" not in r.headers
    ws.close()


def test_an_app_cannot_disable_its_own_csp():
    """csp="" is the EMBEDDER's opt-out. An app naming its own policy
    on an otherwise-configured router is dropped, not honored."""
    ws, token, client = make_served()
    r = client.get(f"/apps/{token}/api/page")
    assert r.headers["content-security-policy"] != "default-src 'none'"
    assert "'unsafe-inline'" in r.headers["content-security-policy"]
    ws.close()


def test_no_duplicate_content_type():
    """media_type and the allowlisted content-type header name the same
    value, so the wire carries exactly one."""
    ws, token, client = make_served()
    for path in ("/", "api/page", "api/scores"):
        r = client.get(f"/apps/{token}/{path.lstrip('/')}")
        assert len(r.headers.get_list("content-type")) == 1, path
    ws.close()


def test_get_reads_seeded_state():
    ws, token, client = make_served()
    r = client.get(f"/apps/{token}/api/scores?limit=2")
    assert r.status_code == 200
    assert r.json() == {"scores": ["alice", "amy"]}
    ws.close()


def test_post_as_read_takes_a_body():
    ws, token, client = make_served()
    r = client.post(f"/apps/{token}/api/scores", content=json.dumps({"prefix": "a"}))
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
    ws.fs.write("/workspace/app/api/boom.py", b"def get(req):\n    return 1/0\n")
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
        "/workspace/app/api/metric.py",
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


def test_csp_derives_from_script_hosts():
    """What test_app permits headlessly must equal what published
    serving permits live — divergence means apps verify green and
    break in production (the cdn.plot.ly lesson). Both walls now
    derive from the ONE declaration, AppsConfig.script_hosts."""
    import re

    from nontainer.apps import DEFAULT_SCRIPT_HOSTS
    from nontainer.apps.serve import build_csp

    def script_hosts_of(csp):
        script_src = re.search(r"script-src ([^;]+);", csp).group(1)
        return set(re.findall(r"https://([\w.-]+)", script_src))

    assert script_hosts_of(build_csp(DEFAULT_SCRIPT_HOSTS)) == set(DEFAULT_SCRIPT_HOSTS)
    assert script_hosts_of(build_csp(("esm.corp.internal",))) == {"esm.corp.internal"}


def test_served_csp_reflects_config_hosts():
    """A private registry host added via AppsConfig reaches the served
    CSP without touching build_router's csp override."""
    from nontainer.apps import AppsConfig

    cfg = AppsConfig(script_hosts=("esm.corp.internal",))
    ws, token, client = make_served(config=cfg)
    r = client.get(f"/apps/{token}/")
    csp = r.headers["content-security-policy"]
    assert "https://esm.corp.internal" in csp
    assert "esm.sh" not in csp  # replaced, not appended
    ws.close()


def test_csp_override_and_disable():
    """csp= still overrides wholesale; empty string disables."""
    ws, token, client = make_served(csp="default-src 'none'")
    r = client.get(f"/apps/{token}/")
    assert r.headers["content-security-policy"] == "default-src 'none'"
    ws.close()

    ws, token, client = make_served(csp="")
    r = client.get(f"/apps/{token}/")
    assert "content-security-policy" not in r.headers
    # and the app cannot reinstate one on its own behalf
    r = client.get(f"/apps/{token}/api/page")
    assert "content-security-policy" not in r.headers
    ws.close()


def _directives(csp):
    """{name: [sources]} for one policy string."""
    out = {}
    for part in csp.split(";"):
        tokens = part.split()
        if tokens:
            out[tokens[0]] = tokens[1:]
    return out


def test_the_default_policy_allows_blob_images_and_media():
    """A blob URL is bytes the page already holds, displayed rather than
    executed — the same risk class as data:, which img-src already
    allows. plotly rasterizes charts by drawing a Blob-backed <img> onto
    a canvas, so without this the modebar's "download as png" fails in
    a published app. Code from a blob stays refused."""
    from nontainer.apps import DEFAULT_SCRIPT_HOSTS
    from nontainer.apps.serve import build_csp

    d = _directives(build_csp(DEFAULT_SCRIPT_HOSTS))
    assert "blob:" in d["img-src"]
    assert "blob:" in d["media-src"]
    assert "blob:" not in d["script-src"]
    # blob scripts and workers are CODE: no directive is added for them,
    # so they fall through to default-src 'self' and stay blocked.
    assert "worker-src" not in d and "child-src" not in d


def test_csp_extend_appends_to_a_derived_directive():
    """Extension is additive and de-duplicated, derived sources first —
    an added source can never displace one the derived policy relies on.
    """
    from nontainer.apps.serve import build_csp

    base = _directives(build_csp(("esm.sh",)))
    extended = _directives(
        build_csp(
            ("esm.sh",),
            extend={"connect-src": ("http://tiles.internal", "wss:", "'self'")},
        )
    )
    assert extended["connect-src"] == [
        *base["connect-src"],
        "http://tiles.internal",
        "wss:",
    ]  # 'self' was already there, and is not repeated
    assert extended["img-src"] == base["img-src"]  # nothing else moved


def test_csp_extend_adds_a_missing_directive():
    """A directive the derived policy doesn't name (frame-src,
    worker-src) is appended whole, so an embedder can permit framing or
    a same-origin worker without restating the policy."""
    from nontainer.apps.serve import build_csp

    csp = build_csp(("esm.sh",), extend={"frame-src": ("https://maps.internal",)})
    assert _directives(csp)["frame-src"] == ["https://maps.internal"]
    assert csp.startswith("default-src 'self';")  # appended, not prepended


def test_csp_extend_matches_directives_case_insensitively():
    """Directive names are case-insensitive to the browser, so a
    mixed-case key must extend img-src rather than add a second,
    shadowed copy of it."""
    from nontainer.apps.serve import build_csp

    d = _directives(build_csp(("esm.sh",), extend={"IMG-SRC": ("http://t.internal",)}))
    assert "IMG-SRC" not in d
    assert d["img-src"][-1] == "http://t.internal"


def test_an_empty_extension_is_the_derived_policy():
    from nontainer.apps.serve import build_csp

    assert build_csp(("esm.sh",), extend={}) == build_csp(("esm.sh",))
    assert build_csp(("esm.sh",), extend=None) == build_csp(("esm.sh",))


def test_csp_and_csp_extend_together_are_refused():
    """Silently ignoring the extension would serve a policy the embedder
    believes they widened. A verbatim policy has nothing to extend, and
    "" — no header at all — has even less."""
    from nontainer.apps import AppsConfig

    with pytest.raises(ValueError, match="nothing to extend"):
        AppsConfig(csp="default-src 'self'", csp_extend={"img-src": ("blob:",)})
    with pytest.raises(ValueError, match="nothing to extend"):
        AppsConfig(csp="", csp_extend={"img-src": ("blob:",)})


def test_csp_extend_rejects_values_that_would_splice_the_policy():
    """A directive name or source carrying ';' or a space would inject a
    whole directive into the served header, which is the one way an
    extension could TIGHTEN something by accident."""
    from nontainer.apps import AppsConfig

    for bad in (
        {"img-src blob:": ("x",)},
        {"IMG-SRC": ("blob:",)},
        {"": ("blob:",)},
        {"img-src": ("blob:; script-src *",)},
        {"img-src": ("",)},
        {"img-src": "blob:"},  # a bare string is not a source list
    ):
        with pytest.raises(ValueError):
            AppsConfig(csp_extend=bad)


def test_resolve_csp_applies_the_extension():
    from nontainer.apps import AppsConfig
    from nontainer.apps.serve import build_csp, resolve_csp

    cfg = AppsConfig(csp_extend={"connect-src": ("http://api.internal",)})
    assert resolve_csp(cfg) == build_csp(
        cfg.script_hosts, extend={"connect-src": ("http://api.internal",)}
    )
    # a verbatim policy is still verbatim
    assert resolve_csp(AppsConfig(csp="default-src 'none'")) == "default-src 'none'"


def test_served_html_carries_the_extended_policy():
    """The extension has to reach the wire, not just resolve_csp: it
    exists so an intranet app can load plain-http tiles in a browser."""
    from nontainer.apps import AppsConfig

    cfg = AppsConfig(csp_extend={"img-src": ("http://tiles.internal",)})
    ws, token, client = make_served(config=cfg)
    r = client.get(f"/apps/{token}/")
    d = _directives(r.headers["content-security-policy"])
    assert d["img-src"] == [
        "'self'",
        "https:",
        "data:",
        "blob:",
        "http://tiles.internal",
    ]
    ws.close()
