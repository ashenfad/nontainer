"""The authoring loop's own files stay the author's.

An app's handler log (``app/logs/api.log``: every traceback and print
from development, exception messages included) and ``test_app``'s
captures (``app/screenshots/``) live inside the app tree so the agent
can read them back through the filesystem. Neither is served as static,
in the live preview or from a publication, and a publish leaves both
out — so a version made before they were left out is refused at serve
time too.
"""

import pytest

from nontainer import Store
from nontainer.apps import AppRuntime, AppsConfig, enable_apps, request
from nontainer.apps.dispatch import (
    APP_DIR,
    LOG_DIR,
    LOG_FILE,
    PUBLISH_EXCLUDE,
    SCREENSHOT_DIR,
    UNSERVED_DIRS,
)

pytest.importorskip("starlette")

from starlette.testclient import TestClient  # noqa: E402

from nontainer.apps import build_router  # noqa: E402

BOOM = (
    "SECRET = 'hunter2'\n"
    "def get(req):\n"
    "    raise RuntimeError('db password is ' + SECRET)\n"
)

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080200000090"
    "7753de0000000c49444154089963f8cfc000000301010018dd8db0000000"
    "0049454e44ae426082"
)

LOG = f"/{LOG_DIR}/{LOG_FILE}"
SHOT = f"/{SCREENSHOT_DIR}/shot-1.png"

# Every spelling that canonicalizes into logs/ or screenshots/, plus the
# directories themselves. Case variants are refused too: a workspace
# over a case-insensitive host filesystem opens them as the real path.
REFUSED = (
    LOG,
    "/logs",
    "/logs/",
    "/./logs/api.log",
    "//logs/api.log",
    "/x/../logs/api.log",
    "/x/y/../../logs/api.log",
    "/logs/./api.log",
    "/data/../logs/api.log",
    "/LOGS/api.log",
    "/Logs/api.log",
    SHOT,
    "/screenshots",
    "/screenshots/",
    "/./screenshots/shot-1.png",
    "/x/../screenshots/shot-1.png",
    "/Screenshots/shot-1.png",
)


def author(store: Store, session: str = "author"):
    """A session whose authoring loop has run: a handler raised with a
    secret in its message, test_app saved a capture, and the app has
    ordinary static files beside them."""
    ws = store.open(session)
    enable_apps(ws)
    ws.files.write("/workspace/app/index.html", "<h1>hi</h1>")
    ws.files.write("/workspace/app/data/scores.json", '{"n": 1}')
    ws.files.write("/workspace/app/logsheet.html", "<p>not the log</p>")
    ws.files.write("/workspace/app/api/boom.py", BOOM)
    ws.terminal("ws-curl $APP_ORIGIN/api/boom")
    ws.files.fs.makedirs("/workspace/app/screenshots", exist_ok=True)
    ws.files.fs.write("/workspace/app/screenshots/shot-1.png", PNG)
    ws.commit()
    return ws


def served(pub):
    snapshot = pub.open()
    client = TestClient(build_router(lambda t: snapshot if t == "tok" else None))
    return snapshot, client


def test_the_repro_has_the_secret_in_the_log(tmp_path):
    """The premise: the log really does hold the secret, so a 404 below
    is the refusal and not an empty file."""
    store = Store(tmp_path)
    ws = author(store)
    log = ws.files.read("/workspace/app/logs/api.log").decode()
    assert "db password is hunter2" in log
    assert ws.files.read("/workspace/app/screenshots/shot-1.png") == PNG
    ws.close()


def test_a_publication_serves_neither_the_log_nor_the_screenshots(tmp_path):
    store = Store(tmp_path)
    ws = author(store)
    pub = store.publish(ws, "demo")
    ws.close()

    snapshot, client = served(pub)
    for path in REFUSED:
        resp = client.get(f"/tok{path}")
        assert resp.status_code == 404, f"{path} served: {resp.status_code}"
        assert b"hunter2" not in resp.content
        assert PNG not in resp.content
    # what the app is still comes back
    assert "<h1>hi</h1>" in client.get("/tok/").text
    assert client.get("/tok/data/scores.json").json() == {"n": 1}
    assert client.get("/tok/logsheet.html").status_code == 200
    snapshot.close()


def test_a_new_publication_leaves_them_out_of_its_tree(tmp_path):
    store = Store(tmp_path)
    ws = author(store)
    pub = store.publish(ws, "demo")
    ws.close()

    version = pub.current_version
    assert version.exclude == tuple(sorted(PUBLISH_EXCLUDE))
    snapshot = pub.open()
    try:
        fs = snapshot.files.fs
        assert not fs.exists("/workspace/app/logs")
        assert not fs.exists("/workspace/app/logs/api.log")
        assert not fs.exists("/workspace/app/screenshots")
        assert not fs.exists("/workspace/app/screenshots/shot-1.png")
        # everything else under app/ made it
        assert fs.read("/workspace/app/index.html") == b"<h1>hi</h1>"
        assert fs.read("/workspace/app/data/scores.json") == b'{"n": 1}'
        assert fs.exists("/workspace/app/api/boom.py")
        assert fs.exists("/workspace/app/logsheet.html")
    finally:
        snapshot.close()


def test_a_publication_that_still_holds_them_is_refused_at_serve_time(tmp_path):
    """A version published before publish left these out carries them;
    ``exclude=()`` builds one. Serving refuses by directory, so the
    files being in the tree changes nothing."""
    store = Store(tmp_path)
    ws = author(store)
    pub = store.publish(ws, "demo", exclude=())
    ws.close()

    snapshot, client = served(pub)
    try:
        assert b"hunter2" in snapshot.files.read("/workspace/app/logs/api.log")
        assert snapshot.files.read("/workspace/app/screenshots/shot-1.png") == PNG
        for path in REFUSED:
            resp = client.get(f"/tok{path}")
            assert resp.status_code == 404, f"{path} served: {resp.status_code}"
            assert b"hunter2" not in resp.content
        assert client.get("/tok/data/scores.json").json() == {"n": 1}
    finally:
        snapshot.close()


def test_the_live_preview_refuses_them_too(tmp_path):
    store = Store(tmp_path)
    ws = author(store)
    rt = enable_apps(ws)
    try:
        for path in REFUSED:
            resp = rt.dispatch(request("GET", path))
            assert resp.status == 404, f"{path} served: {resp.status}"
            assert b"hunter2" not in resp.content
        # the curl builtin is the same dispatch
        out = ws.terminal("ws-curl $APP_ORIGIN/logs/api.log")
        assert "not found: /logs/api.log" in out.stdout
        assert "hunter2" not in out.stdout + out.stderr
        # ordinary static files still serve
        assert rt.dispatch(request("GET", "/")).ok
        assert rt.dispatch(request("GET", "/data/scores.json")).ok
        assert rt.dispatch(request("GET", "/./data/scores.json")).ok
        assert rt.dispatch(request("GET", "/logsheet.html")).ok
        # and the agent's own read path is the filesystem, untouched
        assert "hunter2" in ws.terminal("tail app/logs/api.log").stdout
    finally:
        ws.close()


def test_a_file_under_a_case_variant_directory_is_refused(tmp_path):
    """On a case-sensitive store ``app/LOGS`` is its own directory, but
    the same request against a case-insensitive host filesystem reaches
    the real log — so the refusal ignores case."""
    store = Store(tmp_path)
    ws = store.open("s1")
    rt = enable_apps(ws)
    try:
        ws.files.write("/workspace/app/LOGS/api.log", "shadow")
        ws.files.write("/workspace/app/API/h.py", "SECRET = 1")
        assert rt.dispatch(request("GET", "/LOGS/api.log")).status == 404
        assert rt.dispatch(request("GET", "/API/h.py")).status == 404
    finally:
        ws.close()


@pytest.mark.parametrize("prefix", ["logs", "logs/v1", "screenshots", "LOGS", "API"])
def test_a_static_asset_prefix_cannot_claim_them(prefix, tmp_path):
    """Static serving refuses these directories before it looks for an
    asset, so a prefix there could never serve and is refused when
    declared rather than failing quietly at request time."""
    ws = Store(tmp_path / "store").open("s1")
    try:
        with pytest.raises(ValueError, match="unreachable"):
            AppRuntime(ws, AppsConfig(static_assets={prefix: tmp_path}))
    finally:
        ws.close()


def test_the_runtime_writes_where_the_refusal_looks(tmp_path):
    """The log path and the screenshot directory are built from the
    names the refusal and the publish exclusion are built from."""
    ws = Store(tmp_path).open("s1")
    rt = enable_apps(ws)
    try:
        assert rt._log_path == f"/workspace/{APP_DIR}/{LOG_DIR}/{LOG_FILE}"
        assert rt._screenshot_dir == f"/workspace/{APP_DIR}/{SCREENSHOT_DIR}"
        assert {LOG_DIR, SCREENSHOT_DIR} <= set(UNSERVED_DIRS)
        assert set(PUBLISH_EXCLUDE) == {
            f"{APP_DIR}/{LOG_DIR}/",
            f"{APP_DIR}/{SCREENSHOT_DIR}/",
        }
    finally:
        ws.close()


def test_the_store_default_exclusion_is_the_apps_layer_s():
    """Core cannot import the apps layer, so ``Store.publish`` spells its
    default exclusion itself. This is what keeps the two in step: rename
    a directory on one side only and this fails."""
    import inspect

    from nontainer.store import _DEFAULT_PUBLISH_EXCLUDE

    default = inspect.signature(Store.publish).parameters["exclude"].default
    assert default == _DEFAULT_PUBLISH_EXCLUDE
    assert set(_DEFAULT_PUBLISH_EXCLUDE) == set(PUBLISH_EXCLUDE)
