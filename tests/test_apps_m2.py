"""Apps M2: test_app — headless verification via Playwright."""

import pytest

from nontainer import Workspace
from nontainer.apps import enable_apps, render_test_app
from nontainer.providers import KvgitProvider

pytest.importorskip("playwright")


@pytest.fixture(scope="module")
def chromium_available():
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as p:
            b = p.chromium.launch()
            b.close()
    except Exception as e:  # pragma: no cover
        pytest.skip(f"chromium unavailable: {e}")


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
    ws.fs.makedirs("/app/api", exist_ok=True)
    ws.fs.write("/app/index.html", APP_HTML.encode())
    ws.fs.write("/app/api/scores.py", HANDLER.encode())
    ws.cache["scores"] = ["alice", "bob"]
    ws.checkpoint()
    yield ws, rt
    ws.close()


def test_load_read_and_api_roundtrip(app_ws):
    ws, rt = app_ws
    result = rt.test_app([
        {"read": "#title"},
        {"read": "#status"},
        {"assert": "document.querySelectorAll('#list li').length === 2"},
    ])
    assert result.ok, render_test_app(result)
    assert result.results[0].value == "Scores"
    assert result.results[1].value == "loaded:2"
    assert any("app booted" in line for line in result.console)


def test_click_flow_mutates_backend(app_ws):
    ws, rt = app_ws
    result = rt.test_app([
        {"type": ["#name", "carol"]},
        {"click": "#add"},
        {"read": "#status"},
    ])
    assert result.ok, render_test_app(result)
    assert result.results[2].value == "loaded:3"
    assert ws.cache["scores"] == ["alice", "bob", "carol"]  # real backend mutation


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


def test_page_error_captured(app_ws, chromium_available):
    ws = Workspace(KvgitProvider.open(None, session="s2"))
    rt = enable_apps(ws)
    ws.fs.makedirs("/app", exist_ok=True)
    ws.fs.write(
        "/app/index.html",
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
    ws.fs.makedirs("/app/api", exist_ok=True)
    ws.fs.write("/app/api/data.py", b"def get(req):\n    return {'n': 1}\n")
    ws.fs.write(
        "/app/index.html",
        b"""<html><body><div id="out">pending</div><script>
        fetch('/api/data')
          .then(r => r.ok ? r.json().then(d => out.textContent = 'ok')
                          : out.textContent = 'failed:' + r.status);
        </script></body></html>""",
    )
    result = rt.test_app([{"read": "#out"}])
    assert result.results[0].value == "failed:404"
    ws.close()


def test_external_hosts_denied(chromium_available):
    ws = Workspace(KvgitProvider.open(None, session="s4"))
    rt = enable_apps(ws)
    ws.fs.makedirs("/app", exist_ok=True)
    ws.fs.write(
        "/app/index.html",
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
    result = rt.test_app(
        [{"eval": "window.innerWidth"}], viewport="mobile"
    )
    assert result.ok
    assert result.results[0].value == "390"


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
    assert ws.fs.exists("/app/screenshots/shot-1.png")


@pytest.mark.asyncio
async def test_mcp_test_app_tool_returns_image_content(app_ws):
    pytest.importorskip("mcp")
    from nontainer.adapters.mcp import build_server

    ws, rt = app_ws
    server = build_server(ws, apps=rt)
    tools = {t.name for t in await server.list_tools()}
    assert "test_app" in tools

    result = await server.call_tool(
        "test_app", {"actions": [{"screenshot": True}]}
    )
    contents = result[0] if isinstance(result, tuple) else result
    types = {type(c).__name__ for c in contents}
    assert "ImageContent" in types
    assert any("PASS" in getattr(c, "text", "") for c in contents)
