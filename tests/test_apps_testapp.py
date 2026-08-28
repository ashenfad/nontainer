"""test_app: headless app verification via Playwright, plus its action DSL."""

import pytest

from nontainer import Workspace
from nontainer.apps import enable_apps, render_test_app
from nontainer.providers import KvgitProvider

APP_HTML = """<!doctype html>
<html><body>
<h1 id="title">Scores</h1>
<ul id="list"></ul>
<input id="name" />
<button id="add">add</button>
<div id="status">idle</div>
<script>
const $ = (s) => document.querySelector(s);
async function refresh() {
  const r = await fetch('api/scores');           // RELATIVE url
  const data = await r.json();
  $('#list').innerHTML = data.scores.map(s => `<li>${s}</li>`).join('');
  $('#status').textContent = 'loaded:' + data.scores.length;
}
$('#add').addEventListener('click', async () => {
  await fetch('api/scores', {method: 'POST',
    body: JSON.stringify({name: $('#name').value || 'anon'})});
  await refresh();
});
console.log('app booted');
refresh();
</script>
</body></html>
"""

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


@pytest.fixture
def app_ws(chromium_available):
    ws = Workspace(KvgitProvider.open(None, session="s1"))
    rt = enable_apps(ws)
    ws.fs.makedirs("/workspace/app/api", exist_ok=True)
    ws.fs.write("/workspace/app/index.html", APP_HTML.encode())
    ws.fs.write("/workspace/app/api/scores.py", HANDLER.encode())
    ws.cache["scores"] = ["alice", "bob"]
    ws.checkpoint()
    yield ws, rt
    ws.close()


def test_load_read_and_api_roundtrip(app_ws):
    ws, rt = app_ws
    result = rt.test_app(
        [
            {"read": "#title"},
            {"read": "#status"},
            {"assert": "document.querySelectorAll('#list li').length === 2"},
        ]
    )
    assert result.ok, render_test_app(result)
    assert result.results[0].value == "Scores"
    assert result.results[1].value == "loaded:2"
    assert any("app booted" in line for line in result.console)


def test_click_flow_mutates_backend(app_ws):
    ws, rt = app_ws
    result = rt.test_app(
        [
            {"type": ["#name", "carol"]},
            {"click": "#add"},
            {"read": "#status"},
        ]
    )
    assert result.ok, render_test_app(result)
    assert result.results[2].value == "loaded:3"
    assert ws.cache["scores"] == ["alice", "bob", "carol"]  # real backend mutation


def test_run_flushes_the_request_log(app_ws):
    """Read-only request lines buffer so a page's GET can't cost the
    next POST its rollback; the end of a run is where they go out,
    because reading the log is the agent's next move."""
    ws, rt = app_ws
    result = rt.test_app([{"read": "#status"}])
    assert result.ok, render_test_app(result)
    log = ws.fs.read("/workspace/app/logs/api.log").decode()
    assert "GET /api/scores -> 200" in log


def test_assert_failure_fails_run(app_ws):
    ws, rt = app_ws
    result = rt.test_app([{"assert": "1 === 2"}])
    assert not result.ok
    assert result.results[0].error == "assertion is falsy"


def test_screenshot_written_to_workspace(app_ws):
    ws, rt = app_ws
    result = rt.test_app([{"screenshot": True}])
    assert result.ok, render_test_app(result)
    path = result.screenshots[0]
    png = ws.fs.read(path)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    rendered = render_test_app(result)
    assert path in rendered  # paths in observations, never bytes


def test_screenshot_cap_soft_skips(app_ws):
    """Hitting max_screenshots must not abort the test: the capped
    action is a noted no-op and later actions still run and count."""
    ws, rt = app_ws
    result = rt.test_app(
        [{"screenshot": True}, {"screenshot": True}, {"read": "#title"}],
        max_screenshots=1,
    )
    assert result.ok, render_test_app(result)
    assert len(result.screenshots) == 1
    skipped = result.results[1]
    assert skipped.ok and "cap" in (skipped.error or "")
    assert result.results[2].value == "Scores"  # ran despite the cap
    assert "skipped" in render_test_app(result)


WIDGET_HTML = """<!doctype html>
<html><body>
<select id="make">
  <option value="t">Tesla</option>
  <option value="f">Ford</option>
</select>
<input id="name" />
<div id="picked">none</div>
<script>
document.querySelector('#make').addEventListener('change', (e) => {
  document.querySelector('#picked').textContent = e.target.value;
});
for (let i = 0; i < 5; i++) {
  console.warn('cdn.tailwindcss.com should not be used in production');
}
console.log('widget booted');
</script>
</body></html>
"""


@pytest.fixture
def widget_ws(chromium_available):
    ws = Workspace(KvgitProvider.open(None, session="s2"))
    rt = enable_apps(ws)
    ws.fs.makedirs("/workspace/app", exist_ok=True)
    ws.fs.write("/workspace/app/index.html", WIDGET_HTML.encode())
    ws.checkpoint()
    yield ws, rt
    ws.close()


def test_select_by_value(widget_ws):
    """`type` maps to page.fill(), which errors on <select> — 4 of one
    audited session's 11 test_app failures were exactly this."""
    ws, rt = widget_ws
    result = rt.test_app(
        [
            {"select": ["#make", "f"]},
            {"read": "#picked"},
        ]
    )
    assert result.ok, render_test_app(result)
    assert result.results[1].value == "f"


def test_select_falls_back_to_visible_label(widget_ws):
    """Agents pass whichever of value/label the DOM showed them;
    matching either keeps that from being another guess-and-retry."""
    ws, rt = widget_ws
    result = rt.test_app(
        [
            {"select": ["#make", "Tesla"]},
            {"read": "#picked"},
        ]
    )
    assert result.ok, render_test_app(result)
    assert result.results[1].value == "t"  # matched by label, set by value


def test_select_reports_an_unmatched_option(widget_ws):
    ws, rt = widget_ws
    result = rt.test_app([{"select": ["#make", "Rivian"]}])
    assert not result.ok
    assert "no option matching 'Rivian'" in result.results[0].error


def test_type_on_a_select_names_the_right_action(widget_ws):
    """Playwright's own message names the problem but not the fix."""
    ws, rt = widget_ws
    result = rt.test_app([{"type": ["#make", "Ford"]}])
    assert not result.ok
    assert "{\"select\": ['#make', value]}" in result.results[0].error


def test_type_still_works_on_a_real_input(widget_ws):
    ws, rt = widget_ws
    result = rt.test_app(
        [
            {"type": ["#name", "carol"]},
            {"eval": "document.querySelector('#name').value"},
        ]
    )
    assert result.ok, render_test_app(result)
    assert "carol" in result.results[1].value


def test_repeated_console_lines_collapse_with_a_count(widget_ws):
    """39% of one session's test_app result bytes were 32 copies of the
    same CDN warning, against a model working in ~30k of context."""
    ws, rt = widget_ws
    result = rt.test_app([])
    assert result.ok, render_test_app(result)
    warnings = [ln for ln in result.console if "cdn.tailwindcss.com" in ln]
    assert len(warnings) == 1
    assert warnings[0].endswith("(x5)")
    # distinct lines are untouched, and still carry no count
    assert any(ln.endswith("widget booted") for ln in result.console)


DEBOUNCED_HTML = """<!doctype html>
<html><body>
<div id="out">stale</div>
<button id="go">go</button>
<script>
document.querySelector('#go').addEventListener('click', () => {
  setTimeout(async () => {                    // fetch STARTS after the
    const r = await fetch('api/scores');      // click-settle gap (300ms)
    const d = await r.json();
    document.querySelector('#out').textContent = 'fresh:' + d.scores.length;
  }, 400);
});
</script>
</body></html>"""


def test_read_settles_past_delayed_fetch(chromium_available):
    """A debounced/setTimeout'd fetch starts after click's settle
    returns; read's own settle must catch it instead of reading stale
    DOM (the false-green an agent can't catch)."""
    ws = Workspace(KvgitProvider.open(None, session="s-debounce"))
    rt = enable_apps(ws)
    ws.fs.makedirs("/workspace/app/api", exist_ok=True)
    ws.fs.write("/workspace/app/index.html", DEBOUNCED_HTML.encode())
    ws.fs.write("/workspace/app/api/scores.py", HANDLER.encode())
    ws.cache["scores"] = ["alice", "bob"]
    ws.checkpoint()
    try:
        result = rt.test_app([{"click": "#go"}, {"read": "#out"}])
        assert result.ok, render_test_app(result)
        assert result.results[1].value == "fresh:2"  # not "stale"
    finally:
        ws.close()


CHURN_HTML = """<!doctype html>
<html><body>
<div id="x">hi</div>
<script>setInterval(() => fetch('api/scores'), 120);</script>
</body></html>"""


def test_unsettled_cap_attaches_stale_note(chromium_available):
    """Network activity that never goes quiet: settle exits via the cap
    and the action carries a stale-risk note instead of silently
    passing (the run itself still passes — it's a note, not a verdict)."""
    ws = Workspace(KvgitProvider.open(None, session="s-churn"))
    rt = enable_apps(ws)
    ws.fs.makedirs("/workspace/app/api", exist_ok=True)
    ws.fs.write("/workspace/app/index.html", CHURN_HTML.encode())
    ws.fs.write("/workspace/app/api/scores.py", HANDLER.encode())
    ws.checkpoint()
    try:
        result = rt.test_app([{"read": "#x"}], settle_cap=1.0)
        assert result.ok, render_test_app(result)
        read = result.results[0]
        assert read.value == "hi"
        assert "did not settle" in (read.error or "")
        assert "assert" in read.error  # the note points at the robust form
        assert "did not settle" in render_test_app(result)
    finally:
        ws.close()


def test_page_error_captured(app_ws, chromium_available):
    ws = Workspace(KvgitProvider.open(None, session="s2"))
    rt = enable_apps(ws)
    ws.fs.makedirs("/workspace/app", exist_ok=True)
    ws.fs.write(
        "/workspace/app/index.html",
        b"<html><body><script>throw new Error('kaboom')</script></body></html>",
    )
    result = rt.test_app([])
    assert not result.ok
    assert any("kaboom" in e for e in result.page_errors)
    ws.close()


def test_absolute_urls_fail_verification(chromium_available):
    """The relocatability rule, enforced structurally: fetch('/api/...')
    breaks under the synthetic prefix and the agent sees it here."""
    ws = Workspace(KvgitProvider.open(None, session="s3"))
    rt = enable_apps(ws)
    ws.fs.makedirs("/workspace/app/api", exist_ok=True)
    ws.fs.write("/workspace/app/api/data.py", b"def get(req):\n    return {'n': 1}\n")
    ws.fs.write(
        "/workspace/app/index.html",
        b"""<html><body><div id="out">pending</div><script>
        fetch('/api/data')
          .then(r => r.ok ? r.json().then(d => out.textContent = 'ok')
                          : out.textContent = 'failed:' + r.status);
        </script></body></html>""",
    )
    result = rt.test_app([{"read": "#out"}])
    assert result.results[0].value == "failed:404"
    # the harness names the rejection + fix (the console-side hint
    # arrives truncated inside a JSON parse error — see the glm-5.2
    # session post-mortem)
    assert any("/api/data" in r and "relative URLs" in r for r in result.rejected)
    assert "[rejected requests]" in render_test_app(result)
    ws.close()


def test_page_errors_carry_locations(chromium_available):
    """Runtime errors keep their `at url:line:col` (the agent can open
    that line of its own file); parse errors — where the browser
    reports nothing — say so instead of a bare token message. The
    gemma blank-page loop: four full rewrites debugging errors that
    had no location."""
    ws = Workspace(KvgitProvider.open(None, session="s3c"))
    rt = enable_apps(ws)
    ws.fs.makedirs("/workspace/app", exist_ok=True)
    ws.fs.write(
        "/workspace/app/index.html",
        b"<html><body><div id='x'>hi</div>\n"
        b"<script>\nusePreactHooks();\n</script>\n"
        b"<script>\nlet a = );\n</script>\n"
        b"</body></html>",
    )
    result = rt.test_app([{"read": "#x"}])
    runtime_err = next(e for e in result.page_errors if "usePreactHooks" in e)
    # The location names the agent's own FILE (an inline script reports
    # the document url, whose lines are index.html's) and quotes the
    # line, so the repair does not cost a call to go look it up.
    assert "index.html:3:" in runtime_err
    assert "3 | usePreactHooks();" in runtime_err
    parse_err = next(e for e in result.page_errors if "SyntaxError" in e)
    assert "bisect" in parse_err
    ws.close()


def test_blocked_script_named_in_rejections(chromium_available):
    """A non-allowlisted CDN script fails as an anonymous ERR_FAILED in
    the console — the rejection report names the URL and the allowlist."""
    ws = Workspace(KvgitProvider.open(None, session="s3b"))
    rt = enable_apps(ws)
    ws.fs.makedirs("/workspace/app", exist_ok=True)
    ws.fs.write(
        "/workspace/app/index.html",
        b"""<html><body><div id="out">ok</div>
        <script src="https://evil.example.com/lib.js"></script>
        </body></html>""",
    )
    result = rt.test_app([{"read": "#out"}])
    assert result.results[0].value == "ok"
    note = next(r for r in result.rejected if "evil.example.com" in r)
    assert "allowlist" in note and "esm.sh" in note
    ws.close()


def test_external_hosts_denied(chromium_available):
    ws = Workspace(KvgitProvider.open(None, session="s4"))
    rt = enable_apps(ws)
    ws.fs.makedirs("/workspace/app", exist_ok=True)
    ws.fs.write(
        "/workspace/app/index.html",
        b"""<html><body><div id="out">pending</div><script>
        fetch('https://example.com/x')
          .then(() => out.textContent = 'reached')
          .catch(() => out.textContent = 'denied');
        </script></body></html>""",
    )
    result = rt.test_app([{"read": "#out"}])
    assert result.results[0].value == "denied"
    ws.close()


def test_viewport_preset(app_ws):
    ws, rt = app_ws
    result = rt.test_app([{"eval": "window.innerWidth"}], viewport="mobile")
    assert result.ok
    assert result.results[0].value == "390"


# -- action DSL (pure; no browser needed) --------------------------------------


def test_coerce_actions_handles_model_sloppiness():
    from nontainer.apps.testapp import coerce_actions

    assert coerce_actions('[{"click": "#a"}]') == [{"click": "#a"}]
    assert coerce_actions({"screenshot": True}) == [{"screenshot": True}]
    assert coerce_actions(None) == []
    with pytest.raises(ValueError, match="list of objects"):
        coerce_actions([1, 2])
    with pytest.raises(ValueError, match="JSON"):
        coerce_actions("{not json")


# -- adapter exposure: test_app as a tool with image content -------------------


def test_agno_test_app_tool_returns_images(app_ws):
    pytest.importorskip("agno")
    from nontainer.adapters.agno import WorkspaceTools

    ws, rt = app_ws
    tk = WorkspaceTools(ws, apps=rt)
    assert "test_app" in tk.functions

    out = tk.functions["test_app"].entrypoint(
        actions=[{"read": "#title"}, {"screenshot": True}]
    )
    assert "PASS" in out.content
    assert out.images and out.images[0].content[:8] == b"\x89PNG\r\n\x1a\n"
    # and the file artifact persists in the workspace too
    assert ws.fs.exists("/workspace/app/screenshots/shot-1.png")


def test_agno_vision_false_keeps_screenshots_path_only(app_ws):
    """A text-only model must not receive image media (providers 400
    the NEXT call: 'no endpoints support image input' — the glm-5.2
    lesson). vision=False drops the attachment and the view_image tool;
    screenshots still land in the workspace."""
    pytest.importorskip("agno")
    from nontainer.adapters.agno import WorkspaceTools

    ws, rt = app_ws
    tk = WorkspaceTools(ws, apps=rt, vision=False)
    assert "view_image" not in tk.functions

    out = tk.functions["test_app"].entrypoint(actions=[{"screenshot": True}])
    assert "PASS" in out.content
    assert not out.images
    assert "/workspace/app/screenshots/" in out.content  # the path still rides the text
    assert ws.fs.exists("/workspace/app/screenshots/shot-1.png")


@pytest.mark.asyncio
async def test_mcp_test_app_tool_returns_image_content(app_ws):
    pytest.importorskip("mcp")
    from nontainer.adapters.mcp import build_server

    ws, rt = app_ws
    server = build_server(ws, apps=rt)
    tools = {t.name for t in await server.list_tools()}
    assert "test_app" in tools

    result = await server.call_tool("test_app", {"actions": [{"screenshot": True}]})
    contents = result[0] if isinstance(result, tuple) else result
    types = {type(c).__name__ for c in contents}
    assert "ImageContent" in types
    assert any("PASS" in getattr(c, "text", "") for c in contents)


# -- the served CSP, enforced during verification -----------------------------
#
# Interception reproduces a CSP's ORIGIN rules. A CSP also governs
# BEHAVIOUR -- eval, new Function, blob workers, blob module scripts --
# and none of that involves a request to intercept. Those used to pass
# here and fail only once published, silently: a refused script does not
# throw, so a page-level try/catch sees nothing either.


def _csp_ws(session, body: bytes, **cfg):
    from nontainer.apps import AppsConfig

    ws = Workspace(KvgitProvider.open(None, session=session))
    rt = enable_apps(ws, AppsConfig(**cfg))
    ws.fs.makedirs("/workspace/app", exist_ok=True)
    ws.fs.write("/workspace/app/index.html", body)
    ws.checkpoint()
    return ws, rt


BLOB_SCRIPT = b"""<html><body><div id="out">init</div>
<script>
  const blob = new Blob(["document.getElementById('out').textContent='ran'"],
                        {type: 'text/javascript'});
  const s = document.createElement('script');
  s.src = URL.createObjectURL(blob);
  document.head.appendChild(s);
</script></body></html>"""


def test_a_blob_script_is_refused_and_named(chromium_available):
    """The failure this change exists to surface. `Babel.transformScriptTags`
    -- the obvious JSX entry point -- compiles to a blob, which serves
    fine and is refused once published."""
    ws, rt = _csp_ws("csp1", BLOB_SCRIPT)
    try:
        result = rt.test_app([{"read": "#out"}])
        assert result.results[0].value == "init"  # it did NOT run
        note = next((r for r in result.rejected if "blob:" in r), None)
        assert note, result.rejected
        assert "Content-Security-Policy" in note
        assert "only after publishing" in note  # says WHY it matters
    finally:
        ws.close()


def test_disabling_the_csp_lets_it_through(chromium_available):
    """csp="" is the opt-out, and it must really opt out -- otherwise an
    embedder serving without a policy verifies under one."""
    ws, rt = _csp_ws("csp2", BLOB_SCRIPT, csp="")
    try:
        result = rt.test_app([{"read": "#out"}])
        assert result.results[0].value == "ran"
        assert not any("Content-Security-Policy" in r for r in result.rejected)
    finally:
        ws.close()


def test_an_inline_module_script_still_runs(chromium_available):
    """The recipe the JSX loader depends on: inject the compiled source
    INLINE rather than as a blob. 'unsafe-inline' covers it, so this is
    the shape that survives publishing."""
    body = b"""<html><body><div id="out">init</div>
<script>
  const s = document.createElement('script');
  s.type = 'module';
  s.textContent = "document.getElementById('out').textContent='ran'";
  document.head.appendChild(s);
</script></body></html>"""
    ws, rt = _csp_ws("csp3", body)
    try:
        result = rt.test_app([{"read": "#out"}])
        assert result.results[0].value == "ran"
        assert not any("Content-Security-Policy" in r for r in result.rejected)
    finally:
        ws.close()


def test_a_disallowed_host_keeps_the_allowlist_message(chromium_available):
    """The CSP refuses an external script before interception can, so
    the harness's better-worded message has to come from the violation
    path too -- otherwise enforcing the policy DOWNGRADES a diagnostic."""
    body = b"""<html><body><div id="out">init</div>
<script src="https://evil.example.com/lib.js"></script></body></html>"""
    ws, rt = _csp_ws("csp4", body)
    try:
        result = rt.test_app([{"read": "#out"}])
        note = next(r for r in result.rejected if "evil.example.com" in r)
        assert "allowlist" in note and "esm.sh" in note
    finally:
        ws.close()


def test_a_blocked_script_fails_the_run(chromium_available):
    """Making the violation VISIBLE is not enough: `ok` has to move too,
    or render_test_app still prints PASS over an app whose code never
    ran -- the false green this change exists to remove, one layer up."""
    ws, rt = _csp_ws("csp5", BLOB_SCRIPT)
    try:
        result = rt.test_app([{"read": "#out"}])
        assert result.results[0].ok  # the action itself succeeded ...
        assert not result.ok  # ... but the run did not
        assert "FAIL" in render_test_app(result)
    finally:
        ws.close()


def test_a_blocked_image_is_a_warning_not_a_failure(chromium_available):
    """A refused image is a blemish on a page that otherwise works.
    Failing the run for it would train agents to ignore red."""
    body = b"""<html><body><div id="out">ok</div>
<img src="http://insecure.example.com/x.png" />
</body></html>"""
    ws, rt = _csp_ws("csp6", body, csp="default-src 'self'; img-src 'self'")
    try:
        result = rt.test_app([{"read": "#out"}])
        assert result.ok
        note = next((r for r in result.rejected if "insecure.example" in r), None)
        if note:  # the img may be refused by interception first
            assert "eval" not in note  # script advice on an image is wrong advice
    finally:
        ws.close()


def test_a_custom_policy_extends_the_intercepted_origins(chromium_available):
    """A host the SERVED policy allows must not be aborted here: that is
    the same divergence pointed the other way (a false red), and this
    PR is what made a custom policy possible."""
    from nontainer.apps.testapp import _csp_script_origins

    csp = "default-src 'self'; script-src 'self' 'unsafe-inline' https://esm.corp.internal"
    assert _csp_script_origins(csp) == ("esm.corp.internal",)
    # keywords, scheme-only sources and wildcards are deliberately skipped
    assert _csp_script_origins("script-src 'self' https: *.evil.com") == ()
    # the derived policy yields exactly the declared hosts, so the
    # default path is unchanged
    from nontainer.apps import DEFAULT_SCRIPT_HOSTS
    from nontainer.apps.serve import build_csp

    assert _csp_script_origins(build_csp(DEFAULT_SCRIPT_HOSTS)) == DEFAULT_SCRIPT_HOSTS


def test_an_assert_still_retries_under_the_enforced_csp(chromium_available):
    """The regression 0.3.5 shipped. page.wait_for_function installs its
    poller INTO the page, which needs 'unsafe-eval' -- blocked by the
    policy this harness now sends. Every retry died, so an app that
    merely settles asynchronously (i.e. anything that fetches) failed
    verification, and the failing assert also emitted a CSP rejection
    blaming the app for the harness's own instrumentation."""
    body = b"""<html><body><div id="x">no</div>
<script>setTimeout(() => { document.getElementById('x').textContent = 'yes'; }, 400);</script>
</body></html>"""
    ws, rt = _csp_ws("csp7", body)
    try:
        result = rt.test_app(
            [{"assert": "document.getElementById('x').textContent === 'yes'"}]
        )
        assert result.ok, result  # the app is correct
        assert not result.rejected  # and was not blamed for the harness
    finally:
        ws.close()


def test_a_failing_assert_does_not_blame_the_app_for_csp(chromium_available):
    ws, rt = _csp_ws("csp8", b"<html><body><div id='x'>hi</div></body></html>")
    try:
        result = rt.test_app([{"assert": "1 === 2"}])
        assert not result.ok
        assert result.results[0].error == "assertion is falsy"
        assert not result.rejected
    finally:
        ws.close()


def test_an_assert_retries_an_expression_that_throws(chromium_available):
    """A predicate reaching into a node the app has not rendered yet
    throws on the first pass and succeeds on the third -- the ordinary
    shape of asserting against an app that fetches."""
    body = b"""<html><body><div id="host"></div>
<script>setTimeout(() => {
  document.getElementById('host').innerHTML = '<span id="late">ready</span>';
}, 300);</script></body></html>"""
    ws, rt = _csp_ws("csp9", body)
    try:
        result = rt.test_app(
            [{"assert": "document.querySelector('#late').textContent === 'ready'"}]
        )
        assert result.ok, result
    finally:
        ws.close()


def test_an_assert_returning_a_dead_promise_times_out(chromium_available):
    """page.evaluate AWAITS a promise the expression returns, so a poll
    loop that only checks the clock between evaluations would hang here
    with no output. The deadline has to bound each evaluation."""
    import time

    ws, rt = _csp_ws("csp10", b"<html><body><div id='x'>hi</div></body></html>")
    try:
        started = time.monotonic()
        result = rt.test_app(
            [{"assert": "new Promise(() => {})"}], assert_timeout_ms=600
        )
        elapsed = time.monotonic() - started
        assert not result.ok
        assert elapsed < 20, f"took {elapsed:.1f}s -- did it hang?"
        assert "never resolved" in result.results[0].error
    finally:
        ws.close()


def test_a_slow_promise_that_resolves_in_time_still_passes(chromium_available):
    """The bound must not turn every promise-returning assert red."""
    ws, rt = _csp_ws("csp11", b"<html><body><div id='x'>hi</div></body></html>")
    try:
        result = rt.test_app(
            [{"assert": "new Promise(r => setTimeout(() => r(true), 200))"}],
            assert_timeout_ms=3_000,
        )
        assert result.ok, result
    finally:
        ws.close()


# -- reliability + ergonomics audit -------------------------------------------


def test_an_absolute_url_fails_the_run(chromium_available):
    """apps.md promises relocatability violations "fail during
    verification, not at delivery". They warned. And the 404 carries a
    JSON body, so an app calling .json() without checking .ok renders as
    if fine -- PASS on an app that 404s in production."""
    body = b"""<html><body><div id="s">idle</div>
<script>fetch('/api/data').then(r => r.json())
  .then(() => document.getElementById('s').textContent = 'got');</script>
</body></html>"""
    ws, rt = _csp_ws("audit1", body)
    try:
        result = rt.test_app([{"read": "#s"}])
        assert result.results[0].value == "got"  # the app looked fine ...
        assert not result.ok  # ... and the run does not
        assert any("absolute path" in r for r in result.rejected)
    finally:
        ws.close()


DELAYED = b"""<html><body><div id="n">0</div>
<script>setTimeout(() => { document.getElementById('n').textContent = '42'; }, 350);</script>
</body></html>"""


def test_eval_settles_like_read(chromium_available):
    """`eval` is where agents ask what `read` cannot express, so stale
    answers there are worse, not better. It used to return '0' on this
    page while `read` returned 42."""
    ws, rt = _csp_ws("audit2", DELAYED)
    try:
        result = rt.test_app([{"eval": "document.getElementById('n').textContent"}])
        assert result.results[0].value == "'42'"
    finally:
        ws.close()


def test_a_failed_action_captures_the_page(chromium_available):
    """The run stops at a failure, so a trailing screenshot action never
    happens -- the agent re-runs the whole test just to look."""
    ws, rt = _csp_ws("audit3", b"<html><body><div id='x'>hi</div></body></html>")
    try:
        result = rt.test_app([{"click": "#nope"}, {"screenshot": True}])
        assert not result.ok
        assert result.screenshots, "no screenshot at the moment of failure"
        assert "page at failure:" in result.results[0].error
    finally:
        ws.close()


def test_a_selector_miss_names_what_is_present(chromium_available):
    """Playwright says only what it waited for, so an agent re-guesses
    blind. Everywhere else this module names the door."""
    body = b"""<html><body><div id="title">Runs</div>
<button id="refresh">go</button>
<table><tr data-key="42"><td>x</td></tr></table></body></html>"""
    ws, rt = _csp_ws("audit4", body)
    try:
        result = rt.test_app([{"click": "#reefresh"}])
        error = result.results[0].error
        assert "#refresh" in error and "#title" in error
        assert '[data-key="42"]' in error
    finally:
        ws.close()


def test_goto_navigates_within_the_app(chromium_available):
    """A multi-page app could only ever be verified at its entry."""
    ws, rt = _csp_ws("audit5", b"<html><body><h1 id='home'>Home</h1></body></html>")
    try:
        ws.fs.write(
            "/workspace/app/about.html",
            b"<html><body><h1 id='ab'>About</h1></body></html>",
        )
        ws.checkpoint()
        result = rt.test_app(
            [
                {"goto": "about.html"},
                {"read": "#ab"},
                {"goto": "index.html"},
                {"read": "#home"},
            ]
        )
        assert result.ok, render_test_app(result)
        assert result.results[1].value == "About"
        assert result.results[3].value == "Home"
    finally:
        ws.close()


def test_an_early_error_is_not_buried_by_a_chatty_tail():
    """Pure function: console noise is exactly what a page with a
    problem produces, so a plain tail drops the line that explains it."""
    from nontainer.apps.testapp import _console_excerpt

    console = ("[error] boom", *(f"[log] noise {i}" for i in range(14)))
    excerpt = _console_excerpt(console)
    assert "[error] boom" in excerpt
    assert excerpt[-1] == "[log] noise 13"  # the tail is still the tail
    assert any("more lines" in line for line in excerpt)  # and says what it cut

    short = ("[log] a", "[log] b")
    assert _console_excerpt(short) == list(short)  # nothing to elide


def test_goto_a_missing_page_fails(chromium_available):
    """page.goto RESOLVES on 4xx/5xx -- it only raises for transport
    failures -- so a navigation to a missing page was a passing action
    on a 404 document. The exact failure this PR is about, in the action
    it added."""
    ws, rt = _csp_ws("audit7", b"<html><body><h1 id='home'>Home</h1></body></html>")
    try:
        result = rt.test_app([{"goto": "nope.html"}])
        assert not result.ok
        assert "HTTP 404" in result.results[0].error
    finally:
        ws.close()


def test_a_failed_screenshot_is_not_reported_as_a_cap_skip(chromium_available):
    """`None` from _capture means the CAP, a benign skip. A genuine
    failure raises -- collapsing the two let a requested screenshot fail
    and be reported ok=True with "cap reached"."""
    ws, rt = _csp_ws("audit8", b"<html><body><div id='x'>hi</div></body></html>")
    try:
        # the cap path stays a soft skip, and later actions still run
        result = rt.test_app(
            [{"screenshot": True}, {"screenshot": True}, {"read": "#x"}],
            max_screenshots=1,
        )
        assert result.ok, render_test_app(result)
        assert "cap" in (result.results[1].error or "")
        assert result.results[2].value == "hi"
    finally:
        ws.close()


def test_the_elision_count_describes_what_was_cut():
    """A diagnostic that overstates its own elision is one an agent
    cannot reason from: promoted lines are ON SCREEN, so they are not
    omitted."""
    from nontainer.apps.testapp import _console_excerpt

    console = tuple(f"[error] e{i}" for i in range(5)) + tuple(
        f"[log] n{i}" for i in range(15)
    )
    excerpt = _console_excerpt(console)
    shown = [line for line in excerpt if "more lines" not in line]
    claimed = next(line for line in excerpt if "more lines" in line)
    assert f"{len(console) - len(shown)} more lines" in claimed
