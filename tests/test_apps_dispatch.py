"""App handlers: request contract, dispatch, transactionality, curl."""

import json

import pytest

from nontainer import Workspace
from nontainer.apps import (
    HttpError,
    Response,
    enable_apps,
    normalize,
    request,
)
from nontainer.providers import KvgitProvider


def make_ws(**kwargs):
    ws = Workspace(KvgitProvider.open(None, session="s1"), **kwargs)
    return ws, enable_apps(ws)


def write_handler(ws, name: str, source: str) -> None:
    ws.fs.makedirs("/app/api", exist_ok=True)
    ws.fs.write(f"/app/api/{name}.py", source.encode())


# -- contract ------------------------------------------------------------


def test_normalize_liberal_returns():
    assert normalize({"a": 1}).content_type == "application/json"
    assert normalize([1, 2]).status == 200
    assert normalize("hi").content_type.startswith("text/plain")
    assert normalize(b"\x00").content_type == "application/octet-stream"
    assert normalize(None).status == 204
    r = normalize(Response(status=201, body={"ok": True}))
    assert r.status == 201 and json.loads(r.text) == {"ok": True}
    with pytest.raises(TypeError):
        normalize(object())


def test_request_require():
    req = request("POST", "/api/x", body=b'{"name": "amy", "n": 3}')
    assert req.require("name") == "amy"
    assert req.require("n", int) == 3
    with pytest.raises(HttpError) as e:
        req.require("missing")
    assert e.value.status == 400

    req2 = request("GET", "/api/x?limit=5")
    assert req2.require("limit", int) == 5  # coerced from query string


# -- dispatch: routing -----------------------------------------------------


def test_404_no_handler():
    ws, rt = make_ws()
    assert rt.dispatch(request("GET", "/api/nope")).status == 404
    ws.close()


def test_underscore_files_not_routable():
    ws, rt = make_ws()
    write_handler(ws, "_lib", "def get(req): return {'leak': True}")
    assert rt.dispatch(request("GET", "/api/_lib")).status == 404
    ws.close()


def test_405_missing_verb():
    ws, rt = make_ws()
    write_handler(ws, "ro", "def get(req): return {}")
    assert rt.dispatch(request("POST", "/api/ro")).status == 405
    ws.close()


# -- dispatch: handler execution ---------------------------------------------


def test_get_returns_json():
    ws, rt = make_ws()
    write_handler(
        ws,
        "scores",
        "def get(req):\n"
        "    limit = int(req.params.get('limit', 2))\n"
        "    return {'scores': [98, 87, 75][:limit]}\n",
    )
    resp = rt.dispatch(request("GET", "/api/scores?limit=2"))
    assert resp.ok, resp.text
    assert json.loads(resp.text) == {"scores": [98, 87]}
    ws.close()


def test_http_error_is_structured():
    ws, rt = make_ws()
    write_handler(
        ws,
        "guarded",
        "def get(req):\n"
        "    raise HttpError(403, 'not yours')\n",
    )
    resp = rt.dispatch(request("GET", "/api/guarded"))
    assert resp.status == 403
    assert "not yours" in resp.text
    ws.close()


def test_crash_is_500_and_logged():
    ws, rt = make_ws()
    write_handler(ws, "boom", "def get(req):\n    return 1 / 0\n")
    resp = rt.dispatch(request("GET", "/api/boom"))
    assert resp.status == 500
    log = ws.fs.read("/app/logs/api.log").decode()
    assert "ZeroDivisionError" in log
    ws.close()


def test_handler_print_lands_in_log():
    ws, rt = make_ws()
    write_handler(ws, "chatty", "def get(req):\n    print('debugging')\n    return {}\n")
    rt.dispatch(request("GET", "/api/chatty"))
    assert "debugging" in ws.fs.read("/app/logs/api.log").decode()
    ws.close()


def test_handler_sees_cache_and_files():
    ws, rt = make_ws()
    ws.cache["greeting"] = "hello"
    ws.fs.write("/data.txt", b"file-data")
    write_handler(
        ws,
        "combo",
        "def get(req):\n"
        "    return {'c': cache['greeting'], 'f': open('/data.txt').read()}\n",
    )
    resp = rt.dispatch(request("GET", "/api/combo"))
    assert json.loads(resp.text) == {"c": "hello", "f": "file-data"}
    ws.close()


# -- structural REST -----------------------------------------------------------


def test_get_cannot_write_files():
    ws, rt = make_ws()
    write_handler(
        ws, "sneaky", "def get(req):\n    open('/x.txt', 'w').write('no')\n    return {}\n"
    )
    resp = rt.dispatch(request("GET", "/api/sneaky"))
    assert resp.status == 500
    assert not ws.fs.exists("/x.txt")
    ws.close()


def test_get_cannot_write_cache():
    ws, rt = make_ws()
    write_handler(ws, "sneaky2", "def get(req):\n    cache['x'] = 1\n    return {}\n")
    resp = rt.dispatch(request("GET", "/api/sneaky2"))
    assert resp.status == 500
    assert "x" not in ws.cache
    ws.close()


def test_post_can_write():
    ws, rt = make_ws()
    write_handler(
        ws,
        "save",
        # NOTE: `with` is required on VirtualFS — an unclosed write handle
        # loses data (monkeyfs VirtualFile lacks __del__; upstream TODO).
        "def post(req):\n"
        "    name = req.require('name')\n"
        "    with open('/saved.txt', 'w') as f:\n"
        "        f.write(name)\n"
        "    cache['last'] = name\n"
        "    return Response(status=201, body={'saved': name})\n",
    )
    resp = rt.dispatch(request("POST", "/api/save", body=b'{"name": "amy"}'))
    assert resp.status == 201, resp.text
    assert ws.fs.read("/saved.txt") == b"amy"
    assert ws.cache["last"] == "amy"
    ws.close()


def test_failed_post_discards_staged_writes():
    """Atomic rollback happens only when the provider is clean at
    dispatch — otherwise discard would nuke unrelated pending work
    (the design's 'atomic when clean' rule)."""
    ws, rt = make_ws()
    ws.terminal("echo keep > keep.txt")  # autocheckpointed → provider clean
    write_handler(
        ws,
        "partial",
        "def post(req):\n"
        "    with open('/half.txt', 'w') as f:\n"
        "        f.write('written-then-crash')\n"
        "    assert open('/half.txt').read()  # really staged before the crash\n"
        "    raise ValueError('midway')\n",
    )
    ws.checkpoint()  # atomicity requires a clean provider at dispatch
    resp = rt.dispatch(request("POST", "/api/partial", body=b"{}"))
    assert resp.status == 500
    assert not ws.fs.exists("/half.txt")  # atomic: nothing left behind
    assert ws.fs.exists("/app/api/partial.py")  # committed work untouched
    assert ws.terminal("cat keep.txt").stdout.strip() == "keep"
    ws.close()


# -- static ---------------------------------------------------------------------


def test_static_serving():
    ws, rt = make_ws()
    ws.fs.makedirs("/app", exist_ok=True)
    ws.fs.write("/app/index.html", b"<h1>app</h1>")
    ws.fs.write("/app/app.js", b"export const x = 1")
    ws.fs.write("/app/sub/page.html", b"<p>sub</p>")
    assert rt.dispatch(request("GET", "/")).content_type.startswith("text/html")
    assert rt.dispatch(request("GET", "/app.js")).ok
    # `.` segments and redundant slashes normalize, stay inside /app
    assert rt.dispatch(request("GET", "/./app.js")).ok
    assert rt.dispatch(request("GET", "//app.js")).ok
    assert rt.dispatch(request("GET", "/sub/../app.js")).ok
    assert rt.dispatch(request("GET", "/sub/page.html")).ok
    assert rt.dispatch(request("GET", "/nope.css")).status == 404
    assert rt.dispatch(request("POST", "/index.html")).status == 405
    ws.close()


def test_static_path_traversal_is_contained():
    """The static server must not escape /app/ nor serve backend source
    — normalized `.`/`..` segments were the hole."""
    ws, rt = make_ws()
    ws.fs.makedirs("/app/api", exist_ok=True)
    ws.fs.write("/app/index.html", b"<h1>app</h1>")
    ws.fs.write("/app/api/scores.py", b"API_KEY = 'sk-secret'")
    ws.fs.write("/app/api/_shared.py", b"DB_PASSWORD = 'hunter2'")
    ws.fs.write("/private.md", b"workspace-root file")
    ws.fs.write("/apple", b"sibling-prefix file")  # must not slip a prefix check

    escapes = [
        "/../private.md",         # workspace-root escape
        "/../../private.md",      # multi-level escape
        "/../apple",              # sibling of /app sharing its prefix
        "/./api/scores.py",       # backend source via `.`
        "/x/../api/_shared.py",   # non-routable shared code via `..`
        "/api/scores.py",         # backend source as a literal static path
        "/app/index.html",        # the /app prefix is not part of the URL space
    ]
    for path in escapes:
        resp = rt.dispatch(request("GET", path))
        assert resp.status == 404, f"{path} leaked: {resp.status} {resp.content!r}"
        assert b"secret" not in resp.content and b"hunter2" not in resp.content
        assert b"workspace-root" not in resp.content
    ws.close()


# -- curl --------------------------------------------------------------------------


def test_curl_get_in_pipeline():
    ws, rt = make_ws()
    write_handler(ws, "nums", "def get(req):\n    return {'nums': [3, 1, 2]}\n")
    r = ws.terminal("curl /api/nums | jq -r '.nums[]' | sort")
    assert r, r.stderr
    assert r.stdout.split() == ["1", "2", "3"]
    ws.close()


def test_curl_post_with_data():
    ws, rt = make_ws()
    write_handler(
        ws,
        "echo2",
        "def post(req):\n    return {'got': req.require('msg')}\n",
    )
    r = ws.terminal("curl -X POST -d '{\"msg\": \"hi\"}' /api/echo2")
    assert r, r.stderr
    assert json.loads(r.stdout) == {"got": "hi"}
    ws.close()


def test_curl_failure_exit_code():
    ws, rt = make_ws()
    r = ws.terminal("curl /api/absent")
    assert not r
    assert r.exit_code == 22  # curl --fail convention, preserved by termish>=0.1.7
    assert "HTTP 404" in r.stderr
    ws.close()


def test_curl_agent_loop_write_then_test():
    """The full M1 story: agent writes a handler in the terminal,
    then verifies it with curl — one tool, no server."""
    ws, rt = make_ws()
    script = """mkdir -p app/api
echo 'def get(req):
    return {"status": "alive"}' > app/api/health.py
curl /api/health"""
    r = ws.terminal(script)
    assert r, r.stderr
    assert json.loads(r.stdout) == {"status": "alive"}
    ws.close()


def test_verb_in_comment_is_405():
    """PR#1 review: 'def get(' in a comment must not pass the verb check."""
    ws, rt = make_ws()
    write_handler(ws, "cmt", "# def get(req): old idea\ndef post(req):\n    return {}\n")
    assert rt.dispatch(request("GET", "/api/cmt")).status == 405
    ws.close()


def test_get_cache_pop_is_permission_error_not_attribute_error():
    """PR#1 review: derived mutators must raise PermissionError."""
    ws, rt = make_ws()
    ws.cache["k"] = 1
    ws.checkpoint()
    write_handler(ws, "popper", "def get(req):\n    cache.pop('k')\n    return {}\n")
    resp = rt.dispatch(request("GET", "/api/popper"))
    assert resp.status == 500
    log = ws.fs.read("/app/logs/api.log").decode()
    assert "PermissionError" in log and "AttributeError" not in log
    assert ws.cache["k"] == 1
    ws.close()
