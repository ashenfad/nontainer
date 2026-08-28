"""AppsConfig.static_assets: fixed host files served WITH an app.

The plane distinction is the whole point (docs/apps.md): these are to
the browser what host_objects are to handlers. They serve, and they are
NOT workspace state — the agent's filesystem never sees them, so they
cost nothing in commits, forks, or a remote executor's guest tree.
"""

import pytest

from nontainer import Workspace
from nontainer.apps import AppRuntime, AppsConfig, enable_apps, request
from nontainer.providers import KvgitProvider


@pytest.fixture
def assets(tmp_path):
    """A host directory standing in for a vendored bundle."""
    d = tmp_path / "appassets"
    d.mkdir()
    (d / "lib.js").write_text("export const x = 1;\n")
    (d / "core.wasm").write_bytes(b"\x00asm\x01\x00\x00\x00")
    (d / "font.woff2").write_bytes(b"wOF2")
    (d / "nested").mkdir()
    (d / "nested" / "deep.css").write_text("body{}\n")
    return d


def make_ws(assets_dir, **cfg):
    ws = Workspace(KvgitProvider.open(None, session="s1"))
    config = AppsConfig(static_assets={"vendor": assets_dir}, **cfg)
    return ws, enable_apps(ws, config)


def get(runtime, path):
    return runtime.dispatch(request("GET", path))


# -- serving ---------------------------------------------------------------


def test_assets_serve_and_are_typed(assets):
    ws, rt = make_ws(assets)
    try:
        r = get(rt, "/vendor/lib.js")
        assert r.status == 200
        assert r.text == "export const x = 1;\n"
        assert r.content_type.startswith("text/javascript")
        # wasm is the one where the octet-stream fallback is fatal:
        # instantiateStreaming refuses anything else.
        assert get(rt, "/vendor/core.wasm").content_type == "application/wasm"
        assert get(rt, "/vendor/font.woff2").content_type == "font/woff2"
        assert get(rt, "/vendor/nested/deep.css").status == 200
    finally:
        ws.close()


def test_assets_are_not_in_the_workspace(assets):
    """The plane distinction, asserted: serving them puts nothing in the
    agent's filesystem, so they never reach a commit, a fork, or a
    guest tree."""
    ws, rt = make_ws(assets)
    try:
        assert get(rt, "/vendor/lib.js").status == 200
        assert not ws.fs.exists("/workspace/app/vendor")
        assert not ws.fs.exists("/workspace/app/vendor/lib.js")
        r = ws.terminal("ls /workspace/app")
        assert "vendor" not in r.stdout
    finally:
        ws.close()


def test_missing_asset_is_404_not_a_workspace_lookup(assets):
    ws, rt = make_ws(assets)
    try:
        ws.fs.makedirs("/workspace/app", exist_ok=True)
        # A workspace file BELOW the prefix must not answer for a
        # missing asset -- the prefix is claimed wholesale.
        ws.fs.write("/workspace/app/vendor/sneak.js", b"nope")
        assert ws.fs.exists("/workspace/app/vendor/sneak.js")  # not vacuous
        assert get(rt, "/vendor/sneak.js").status == 404
    finally:
        ws.close()


def test_asset_traversal_out_of_the_prefix_is_refused(assets):
    ws, rt = make_ws(assets)
    try:
        ws.fs.makedirs("/workspace/app", exist_ok=True)
        ws.fs.write("/workspace/app/secret.txt", b"s3cret")
        assert get(rt, "/vendor/../secret.txt").status == 404
        assert get(rt, "/vendor/").status == 404
    finally:
        ws.close()


def test_curl_reaches_assets(assets):
    """The agent cannot ls or read an asset, but it can request one --
    enough to confirm a bundle is really there."""
    ws, rt = make_ws(assets)
    try:
        r = ws.terminal("curl /vendor/lib.js")
        assert r, r.stderr
        assert "export const x = 1;" in r.stdout
    finally:
        ws.close()


# -- the two deliberate exemptions -----------------------------------------


def test_assets_are_exempt_from_the_response_cap(tmp_path):
    """A vendored charting bundle clears the 2MB default on its own. The
    cap exists to catch runaway HANDLER output; an asset's size is a
    decision the embedder already made."""
    d = tmp_path / "big"
    d.mkdir()
    (d / "plotly.js").write_bytes(b"x" * 3_500_000)
    ws, rt = make_ws(d)
    try:
        r = get(rt, "/vendor/plotly.js")
        assert r.status == 200
        assert len(r.content) == 3_500_000
    finally:
        ws.close()

    # ... while a handler returning the same volume still trips it.
    ws2 = Workspace(KvgitProvider.open(None, session="s2"))
    rt2 = enable_apps(ws2)
    try:
        ws2.fs.makedirs("/workspace/app/api", exist_ok=True)
        ws2.fs.write(
            "/workspace/app/api/big.py", b"def get(req):\n    return 'x'*3_000_000\n"
        )
        assert get(rt2, "/api/big").status == 500
    finally:
        ws2.close()


def test_asset_shadows_a_workspace_file_and_says_so(assets):
    """Assets win -- predictable, and it stops an agent shadowing the
    design system by accident. But silent shadowing is its own failure
    mode: the agent would edit a file and debug an app that ignores it."""
    ws, rt = make_ws(assets)
    try:
        ws.fs.makedirs("/workspace/app/vendor", exist_ok=True)
        ws.fs.write("/workspace/app/vendor/lib.js", b"MINE")
        r = get(rt, "/vendor/lib.js")
        assert r.text == "export const x = 1;\n"  # the asset, not MINE

        rt.flush_log()
        log = ws.fs.read("/workspace/app/logs/api.log").decode()
        assert "shadowed" in log and "vendor/lib.js" in log
    finally:
        ws.close()


def test_shadow_note_is_written_once(assets):
    ws, rt = make_ws(assets)
    try:
        ws.fs.makedirs("/workspace/app/vendor", exist_ok=True)
        ws.fs.write("/workspace/app/vendor/lib.js", b"MINE")
        for _ in range(3):
            get(rt, "/vendor/lib.js")
        rt.flush_log()
        log = ws.fs.read("/workspace/app/logs/api.log").decode()
        assert log.count("shadowed") == 1
    finally:
        ws.close()


# -- declaration -----------------------------------------------------------


def test_bad_prefixes_rejected(assets, tmp_path):
    ws = Workspace(KvgitProvider.open(None, session="s1"))
    try:
        for bad in ("", "/", "..", "../x", "api", "api/v1"):
            with pytest.raises(ValueError):
                AppRuntime(ws, AppsConfig(static_assets={bad: assets}))
        with pytest.raises(ValueError):
            AppRuntime(ws, AppsConfig(static_assets={"v": tmp_path / "missing"}))
    finally:
        ws.close()


def test_prefix_slashes_are_tolerated(assets):
    ws = Workspace(KvgitProvider.open(None, session="s1"))
    try:
        rt = AppRuntime(ws, AppsConfig(static_assets={"/vendor/": assets}))
        assert get(rt, "/vendor/lib.js").status == 200
    finally:
        ws.close()


def test_frozen_serving_sees_assets(assets):
    """Same config, both runtimes: an asset missing from the serving side
    is an app that verifies green and 404s published."""
    ws = Workspace(KvgitProvider.open(None, session="s1"))
    try:
        config = AppsConfig(static_assets={"vendor": assets})
        frozen = AppRuntime(ws, config, frozen=True, log_sink=lambda m: None)
        assert get(frozen, "/vendor/lib.js").status == 200
    finally:
        ws.close()


# -- what the agent is told ------------------------------------------------


def test_notes_mention_assets_only_when_declared(assets):
    from nontainer.adapters.render import apps_notes

    plain = apps_notes(AppsConfig())
    assert "vendor/" not in plain

    notes = apps_notes(AppsConfig(static_assets={"vendor": assets}))
    assert "vendor/" in notes
    # The negative half is the load-bearing half: an agent that looks
    # with `ls`, finds nothing, and writes its own copy has burned a
    # turn and produced a file that will be shadowed.
    assert "NOT" in notes and "ls" in notes
    assert "curl vendor/" in notes
