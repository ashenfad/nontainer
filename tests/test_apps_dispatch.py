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


def test_require_json_numeric_coercion():
    """JSON has one number type: int passes for float, integral float
    for int; bools are never numbers; strings coerce like params."""
    req = request(
        "POST",
        "/api/x",
        body=b'{"n": 5, "r": 2.0, "half": 2.5, "on": true, "s": "7"}',
    )
    assert req.require("n", float) == 5.0
    assert req.require("r", int) == 2
    assert req.require("s", int) == 7  # JSON string ≙ query-param string
    with pytest.raises(HttpError):
        req.require("half", int)  # non-integral float
    with pytest.raises(HttpError):
        req.require("on", int)  # bool is not a number
    with pytest.raises(HttpError):
        req.require("on", float)
    assert req.require("on", bool) is True
    with pytest.raises(HttpError):
        req.require("n", bool)  # nor is a number a bool


def test_require_param_coercion():
    req = request("GET", "/api/x?n=5&half=2.5&on=true&off=0&word=maybe")
    assert req.require("n", int) == 5
    assert req.require("half", float) == 2.5
    assert req.require("on", bool) is True
    assert req.require("off", bool) is False  # bool("0") would be True
    with pytest.raises(HttpError):
        req.require("word", bool)
    with pytest.raises(HttpError):
        req.require("half", int)


def test_require_json_null_is_missing():
    req = request("POST", "/api/x", body=b'{"n": null}')
    with pytest.raises(HttpError) as e:
        req.require("n")
    assert "missing" in e.value.message


def test_normalize_tolerates_none_headers():
    """Agents write Response(headers=None); it must mean 'no headers',
    not an AttributeError → 500 (PR #7 review)."""
    r = normalize(Response(body={"a": 1}, headers=None))
    assert r.status == 200 and r.headers == {}


def test_normalize_header_casing():
    """Agents type the idiomatic Content-Type — any casing must win
    over the inferred type, and wire keys come out lowercased."""
    r = normalize(Response(body="a,b\n1,2", headers={"Content-Type": "text/csv"}))
    assert r.content_type == "text/csv"
    assert r.headers == {"content-type": "text/csv"}
    r = normalize(Response(body={"a": 1}, headers={"X-Custom": "1"}))
    assert r.content_type == "application/json"  # inferred; keys lowered
    assert r.headers == {"x-custom": "1"}


# -- dispatch: routing -----------------------------------------------------


def test_404_no_handler():
    ws, rt = make_ws()
    assert rt.dispatch(request("GET", "/api/nope")).status == 404
    ws.close()


def test_404_py_suffix_gets_did_you_mean():
    """Agents mirror the FILENAME into the url (fetch('api/explorer.py'))
    and then debug the backend for ages — the 404 must label the door."""
    ws, rt = make_ws()
    write_handler(ws, "explorer", "def get(req): return {'ok': True}")
    r = rt.dispatch(request("GET", "/api/explorer.py"))
    assert r.status == 404
    err = json.loads(r.content)["error"]
    assert "WITHOUT .py" in err and "try /api/explorer" in err
    # no handler either way: convention hint, no bogus suggestion
    r = rt.dispatch(request("GET", "/api/ghost.py"))
    err = json.loads(r.content)["error"]
    assert "WITHOUT the .py extension" in err and "try" not in err
    ws.close()


def test_handler_bare_expressions_do_not_echo():
    """Handlers are scripts: run_python's notebook echo must not leak
    module-level bare-expression reprs into api.log."""
    ws, rt = make_ws()
    write_handler(
        ws,
        "quiet",
        "1 + 1\ndef get(req):\n    return {'ok': True}\n",
    )
    r = rt.dispatch(request("GET", "/api/quiet"))
    assert r.status == 200
    assert not ws.fs.exists("/app/logs/api.log") or (
        "2" not in ws.fs.read("/app/logs/api.log").decode()
    )
    ws.close()


def test_handler_error_logs_traceback_and_request():
    """The two things the observed repair loops starved for: WHERE the
    handler broke (frames + line numbers, not a bare message) and WHICH
    request broke it (the query string in the tag — identical bare
    error lines read as a stale log)."""
    ws, rt = make_ws()
    write_handler(
        ws,
        "boom",
        "def get(req):\n    rows = []\n    return rows[0]\n",
    )
    r = rt.dispatch(request("GET", "/api/boom?source=filtered&makes=Tesla"))
    assert r.status == 500
    log = ws.fs.read("/app/logs/api.log").decode()
    assert "[boom:get ?source=filtered&makes=Tesla] ERROR:" in log
    assert "Traceback (most recent call last)" in log
    assert "line 3" in log and "IndexError" in log
    ws.close()


def test_blocked_import_in_handler_logs_hint():
    """subprocess/requests inside a handler: the api.log entry (the
    documented repair loop) redirects to the terminal's curl."""
    ws, rt = make_ws()
    write_handler(ws, "probe", "import requests\ndef get(req): return {}")
    r = rt.dispatch(request("GET", "/api/probe"))
    assert r.status == 500
    log = ws.fs.read("/app/logs/api.log").decode()
    assert "[hint: " in log and "curl" in log
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
        "def get(req):\n    raise HttpError(403, 'not yours')\n",
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
    write_handler(
        ws, "chatty", "def get(req):\n    print('debugging')\n    return {}\n"
    )
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
        ws,
        "sneaky",
        "def get(req):\n    open('/x.txt', 'w').write('no')\n    return {}\n",
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
        "/../private.md",  # workspace-root escape
        "/../../private.md",  # multi-level escape
        "/../apple",  # sibling of /app sharing its prefix
        "/./api/scores.py",  # backend source via `.`
        "/x/../api/_shared.py",  # non-routable shared code via `..`
        "/api/scores.py",  # backend source as a literal static path
        "/app/index.html",  # the /app prefix is not part of the URL space
    ]
    for path in escapes:
        resp = rt.dispatch(request("GET", path))
        assert resp.status == 404, f"{path} leaked: {resp.status} {resp.content!r}"
        assert b"secret" not in resp.content and b"hunter2" not in resp.content
        assert b"workspace-root" not in resp.content
    ws.close()


def test_error_responses_are_json():
    """Model-written frontends call res.json() unconditionally; a
    plain-text error body cascades into a second, misleading
    SyntaxError. Every dispatch error path answers as JSON."""
    ws, rt = make_ws()
    write_handler(ws, "boom", "def get(req):\n    return 1/0\n")
    write_handler(
        ws, "teapot", "def get(req):\n    raise HttpError(418, 'short and stout')\n"
    )

    r = rt.dispatch(request("GET", "/api/boom"))  # handler crash
    assert r.status == 500 and r.content_type == "application/json"
    body = json.loads(r.content)
    assert body["error"] == "internal error" and body["log"] == "/app/logs/api.log"

    r = rt.dispatch(request("GET", "/api/teapot"))  # intentional HttpError
    assert r.status == 418
    assert json.loads(r.content) == {"error": "short and stout"}

    r = rt.dispatch(request("GET", "/api/absent"))  # dispatch-level 404
    assert r.status == 404
    assert "no such endpoint" in json.loads(r.content)["error"]
    ws.close()


def test_nonverb_functions_noted_in_log_once():
    """`def query(req)` is never routed — the gemma lesson: the filter
    logic sat in a dead function while get() ignored the params. The
    log (the documented repair loop) names it, once per module version."""
    ws, rt = make_ws()
    write_handler(
        ws,
        "stats",
        "def get(req):\n    return {'n': 1}\n\n"
        "def query(req):\n    return {'filtered': True}\n\n"
        "def _helper():\n    pass\n",
    )
    rt.dispatch(request("GET", "/api/stats"))
    rt.dispatch(request("GET", "/api/stats"))  # same version: no repeat
    log = ws.fs.read("/app/logs/api.log").decode()
    assert log.count("query() defined but not an HTTP verb") == 1
    assert "_helper" not in log  # underscore-private: fine, unnoted

    # a new module version re-evaluates
    write_handler(
        ws,
        "stats",
        "def get(req):\n    return {'n': 1}\n\ndef search(req):\n    pass\n",
    )
    rt.dispatch(request("GET", "/api/stats"))
    log = ws.fs.read("/app/logs/api.log").decode()
    assert "search() defined but not an HTTP verb" in log
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
    r = ws.terminal('curl -X POST -d \'{"msg": "hi"}\' /api/echo2')
    assert r, r.stderr
    assert json.loads(r.stdout) == {"got": "hi"}
    ws.close()


def test_curl_absorbs_real_curl_reflexes():
    """The glm-5.2 lesson: agents type -v/--max-time/-w from habit, and
    a rejected flag mid-`;`-sequence fails invisibly. Network-only
    flags are accepted no-ops; -w/-i/-o do what they say."""
    ws, rt = make_ws()
    write_handler(ws, "nums", "def get(req):\n    return {'nums': [1]}\n")

    # no-op flags don't break the call
    r = ws.terminal("curl -s -v -L --max-time 10 --connect-timeout 5 /api/nums")
    assert r, r.stderr
    assert json.loads(r.stdout) == {"nums": [1]}

    # -w substitutes %{http_code} (with \n escapes, curl-style)
    r = ws.terminal("curl -s -w 'code=%{http_code}\\n' /api/nums")
    assert r, r.stderr
    assert "code=200" in r.stdout

    # -i prepends status + headers
    r = ws.terminal("curl -i /api/nums")
    assert r.stdout.startswith("HTTP/1.1 200")
    assert "content-type:" in r.stdout.lower()

    # -o writes the body to the workspace fs (cwd-relative)
    r = ws.terminal("cd /app && curl -o out.json /api/nums && cat out.json")
    assert r, r.stderr
    assert json.loads(ws.fs.read("/app/out.json")) == {"nums": [1]}

    # repeated -d concatenates with '&', like real curl
    write_handler(
        ws, "echoraw", "def post(req):\n    return {'raw': req.body.decode()}\n"
    )
    r = ws.terminal("curl -d a=1 -d b=2 /api/echoraw")
    assert r, r.stderr
    assert json.loads(r.stdout)["raw"] == "a=1&b=2"

    # --json sets the content type
    write_handler(
        ws, "echo3", "def post(req):\n    return {'ct': req.headers.get('content-type')}\n"
    )
    r = ws.terminal("curl --json '{\"a\": 1}' /api/echo3")
    assert r, r.stderr
    assert json.loads(r.stdout)["ct"] == "application/json"

    # unknown flags still fail LOUDLY, now with the supported list
    r = ws.terminal("curl --resolve foo:80:1.2.3.4 /api/nums")
    assert not r
    assert "unknown flag --resolve" in r.stderr and "supported:" in r.stderr
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
    write_handler(
        ws, "cmt", "# def get(req): old idea\ndef post(req):\n    return {}\n"
    )
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


def test_log_failure_warns_once():
    """A broken log path must not break dispatch, but must not go
    silently blind either: one RuntimeWarning per runtime, then quiet."""
    import warnings

    from nontainer.apps import AppRuntime

    ws, _ = make_ws()

    def broken_sink(message: str) -> None:
        raise OSError("disk full")

    rt = AppRuntime(ws, log_sink=broken_sink)
    with pytest.warns(RuntimeWarning, match="log write failed"):
        rt._log("first")
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # a second warning would raise
        rt._log("second")
    ws.close()


def test_log_failure_does_not_break_dispatch():
    import warnings

    from nontainer.apps import AppRuntime

    ws, _ = make_ws()

    def broken_sink(message: str) -> None:
        raise OSError("disk full")

    rt = AppRuntime(ws, log_sink=broken_sink)
    write_handler(ws, "boom", "def get(req):\n    raise ValueError('x')\n")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        resp = rt.dispatch(request("GET", "/api/boom"))
    assert resp.status == 500  # handler error surfaced despite dead logs
    ws.close()


def test_curl_external_url_says_offline():
    """The most expensive trial-and-error discovery, said outright:
    absolute URLs get an explicit no-internet error, not a confusing
    404 from dispatching the URL as a path."""
    ws, rt = make_ws()
    r = ws.terminal("curl https://cdn.plot.ly/plotly.min.js")
    assert r.exit_code == 6
    assert "no internet" in r.stderr and "cdn.jsdelivr.net" in r.stderr
    ws.close()


def test_curl_external_url_error_names_configured_hosts():
    """The offline message quotes AppsConfig.script_hosts, not a
    hardcoded list — a private-registry embedder's agents are pointed
    at the hosts that actually work."""
    from nontainer.apps import AppsConfig

    ws = Workspace(KvgitProvider.open(None, session="apps"))
    enable_apps(ws, AppsConfig(script_hosts=("esm.corp.internal",)))
    r = ws.terminal("curl https://esm.sh/preact@10")
    assert r.exit_code == 6
    assert "esm.corp.internal" in r.stderr
    assert "unpkg.com" not in r.stderr
    ws.close()
