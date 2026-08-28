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


def test_asset_traversal_stays_confined(assets):
    """Paths are canonicalized before anything reads them, so the
    confinement that matters is unchanged: nothing escapes the app root,
    and nothing reaches handler source. Note `/vendor/../x.txt`
    canonically MEANS `/x.txt` and serves that app file — the prefix is
    not a jail, the app root is."""
    ws, rt = make_ws(assets)
    try:
        ws.fs.makedirs("/workspace/app", exist_ok=True)
        ws.fs.write("/workspace/app/page.txt", b"an ordinary app file")
        ws.fs.write("/workspace/outside.txt", b"OUTSIDE")
        ws.fs.makedirs("/workspace/app/api", exist_ok=True)
        ws.fs.write("/workspace/app/api/h.py", b"def get(req): return 'x'")

        assert get(rt, "/vendor/../../outside.txt").status == 404
        assert get(rt, "/../outside.txt").status == 404
        assert get(rt, "/vendor/../api/h.py").status == 404
        assert get(rt, "/vendor/").status == 404
        assert get(rt, "/vendor/../page.txt").status == 200  # == /page.txt
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


# -- review regressions ----------------------------------------------------


def test_dot_segments_cannot_bypass_asset_precedence(assets):
    """`/x/../vendor/lib.js` must resolve to the asset, not fall through
    to a workspace file at the same canonical path — otherwise the
    'your file is NOT served' promise is false for any caller that
    preserves dot segments, and the shadow note never fires either."""
    ws, rt = make_ws(assets)
    try:
        ws.fs.makedirs("/workspace/app/vendor", exist_ok=True)
        ws.fs.write("/workspace/app/vendor/lib.js", b"WORKSPACE")
        assert get(rt, "/x/../vendor/lib.js").text == "export const x = 1;\n"
        assert get(rt, "/./vendor/lib.js").text == "export const x = 1;\n"
    finally:
        ws.close()


def test_shadow_note_does_not_dirty_a_clean_workspace(assets):
    """The note fires on a static GET, and a page load is the read-only
    request that most often precedes a POST. Writing it would dirty the
    workspace, and _dispatch_api gates per-request atomicity on
    `not ws.dirty` — so the note would cost the next mutating handler
    its rollback."""
    ws, rt = make_ws(assets)
    try:
        ws.fs.makedirs("/workspace/app/vendor", exist_ok=True)
        ws.fs.write("/workspace/app/vendor/lib.js", b"MINE")
        ws.checkpoint()
        assert not ws.dirty

        assert get(rt, "/vendor/lib.js").text == "export const x = 1;\n"
        assert not ws.dirty  # the note is buffered, not written

        # ... and it still reaches the log where the agent reads it.
        rt.flush_log()
        assert "shadowed" in ws.fs.read("/workspace/app/logs/api.log").decode()
    finally:
        ws.close()


def test_a_get_leaves_a_later_handler_its_rollback(assets):
    """The consequence the buffering protects, end to end: page GET,
    then a POST that raises, whose staged writes must still roll back."""
    ws, rt = make_ws(assets)
    try:
        ws.fs.makedirs("/workspace/app/vendor", exist_ok=True)
        ws.fs.write("/workspace/app/vendor/lib.js", b"MINE")
        ws.fs.makedirs("/workspace/app/api", exist_ok=True)
        ws.fs.write(
            "/workspace/app/api/save.py",
            b"def post(req):\n"
            b"    open('/workspace/partial.txt', 'w').write('half')\n"
            b"    raise ValueError('boom')\n",
        )
        ws.checkpoint()
        assert not ws.dirty

        get(rt, "/vendor/lib.js")  # the shadowing page load
        assert rt.dispatch(request("POST", "/api/save")).status == 500
        assert not ws.fs.exists("/workspace/partial.txt")
    finally:
        ws.close()


def test_served_csp_allows_wasm_compilation():
    """Browsers gate WebAssembly compilation on script-src. test_app
    enforces the allowlist by intercepting requests rather than sending
    this header, so without 'wasm-unsafe-eval' a wasm-backed bundle
    verifies green and dies only once published."""
    from nontainer.apps.serve import build_csp

    csp = build_csp(("esm.sh",))
    assert "'wasm-unsafe-eval'" in csp
    assert "'unsafe-eval'" not in csp.replace("'wasm-unsafe-eval'", "")


# -- who owns the frontend-library guidance --------------------------------
#
# Once an embedder can supply the libraries (static_assets), the prompt
# can no longer hardcode where they come from: the default block names
# esm.sh and jsdelivr, emphatically ("copy this known-good pattern
# exactly"), which is wrong the moment those hosts don't resolve.


def test_default_notes_are_unchanged_when_nothing_is_declared():
    """The common case is a plain install with CDNs reachable. It must
    see exactly what it saw before this seam existed."""
    from nontainer.adapters.render import DEFAULT_FRONTEND_NOTES, apps_notes

    notes = apps_notes(AppsConfig())
    for line in DEFAULT_FRONTEND_NOTES.strip().splitlines():
        assert line in notes
    assert "https://esm.sh/preact@10" in notes
    assert "plotly.js-dist-min@2" in notes


def test_frontend_notes_replace_rather_than_append():
    """apps_primer appends, which is right for extra guidance and wrong
    here: a vendoring embedder would be correcting a block that says
    'copy this exactly' and names a CDN — leaving the wrong instruction
    first AND more emphatic."""
    from nontainer.adapters.render import apps_notes

    notes = apps_notes(
        AppsConfig(frontend_notes="Charts: <script src='vendor/plotly.min.js'>")
    )
    assert "vendor/plotly.min.js" in notes
    assert "esm.sh" not in notes.split("Browser SCRIPTS")[0]
    assert "plotly.js-dist-min@2" not in notes


def test_frontend_notes_can_be_omitted_entirely():
    """What goes is SUPPLY: the hosts, the versions, the import block.
    `preactHooks` stays — it is the evidenced example in the
    anti-guessing rule, which lives in the template."""
    from nontainer.adapters.render import apps_notes

    notes = apps_notes(AppsConfig(frontend_notes=""))
    assert "esm.sh/preact" not in notes
    assert "plotly.js-dist-min" not in notes
    assert "scattergeo" not in notes
    assert "MOST RELIABLE" in notes and "RELATIVE urls" in notes


SHAPE_GUIDANCE = (
    "MOST RELIABLE",  # plain DOM first
    "fetch('api/scores')",  # relative urls
    "fetch('/api/x')",  # ... and the absolute form to avoid
    "<script src>",  # don't swap a named import for a script tag
    "never guess a global",  # ... or for an invented global
    "preactHooks",  # the evidenced example
)
"""Every claim the docs make about what survives a frontend_notes
override. Listed once, so the assertion cannot quietly drift to a
convenient subset — which is how the guessed-global rule shipped inside
the REPLACEABLE block while three docs said it was in the template."""


@pytest.mark.parametrize("fn", [None, "", "Use vendor/mui.js.", "x"])
def test_shape_guidance_survives_every_frontend_notes_setting(fn):
    """Shape guidance is true wherever the bytes come from, and agents
    get it wrong often enough that no embedder should be able to lose it
    by accident. The anti-guessing rule matters MOST on the replaced
    path: `vendor/mui.js` gives an agent no URL to anchor on, so
    `window.MUI` from memory is the likely next move."""
    from nontainer.adapters.render import apps_notes

    notes = apps_notes(AppsConfig(frontend_notes=fn))
    missing = [g for g in SHAPE_GUIDANCE if g not in notes]
    assert not missing, f"lost with frontend_notes={fn!r}: {missing}"


def test_an_empty_script_allowlist_reads_as_a_rule_not_a_bug():
    """The air-gapped shape. Listing nothing after 'may only load from
    these hosts:' leaves a dangling colon, which reads as a broken
    prompt rather than a policy."""
    from nontainer.adapters.render import apps_notes

    notes = apps_notes(AppsConfig(script_hosts=()))
    assert "ONLY from this app itself" in notes
    assert "these hosts (enforced by test_app AND published serving):\n\n" not in notes


def test_the_default_block_is_importable_for_extending():
    """An embedder adding a house library shouldn't have to retype the
    Preact block to keep it."""
    from nontainer.adapters.render import DEFAULT_FRONTEND_NOTES, apps_notes

    notes = apps_notes(
        AppsConfig(frontend_notes=DEFAULT_FRONTEND_NOTES + "\nAlso: vendor/house.mjs.")
    )
    assert "https://esm.sh/preact@10" in notes
    assert "vendor/house.mjs" in notes


def test_positional_construction_matches_0_3_3():
    """frontend_notes is declared LAST. Inserting it mid-dataclass would
    silently rebind the sixth positional: static_assets would land in
    frontend_notes, the assets would stop serving, and rendering the
    notes would then AttributeError on a mapping."""
    cfg = AppsConfig(5.0, 10_000_000, 2_000_000, ("esm.sh",), "primer", {"vendor": "."})
    assert cfg.static_assets == {"vendor": "."}
    assert cfg.frontend_notes is None
