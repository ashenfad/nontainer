"""Page-error frames: which one is the agent's, and what is on that line.

The selection and rendering are pure functions over the stack text (the
fs read is injected), so most of this needs no browser. One end-to-end
case pins the whole path through a real Chromium.
"""

from nontainer import Workspace
from nontainer.apps import AppsConfig, enable_apps
from nontainer.apps.testapp import (
    classify_frame,
    describe_page_error,
    parse_frames,
)
from nontainer.providers import KvgitProvider

BASE = "https://nontainer.test/apps/t-test/"


def kinds(stack, prefixes=()):
    return [classify_frame(f, prefixes).kind for f in parse_frames(stack)]


# -- parsing ---------------------------------------------------------------


def test_parses_both_v8_frame_shapes():
    frames = parse_frames(
        f"TypeError: boom\n"
        f"    at App ({BASE}app.js:42:13)\n"
        f"    at {BASE}index.html:7:1\n"
    )
    assert [(f.fn, f.line, f.col) for f in frames] == [("App", 42, 13), (None, 7, 1)]
    assert frames[0].url == f"{BASE}app.js"


def test_non_frame_lines_are_skipped():
    assert parse_frames("TypeError: boom\n  <anonymous>\n") == []
    assert parse_frames("") == []


# -- classification --------------------------------------------------------


def test_classifies_agent_vendor_and_opaque():
    stack = (
        f"    at App ({BASE}app.js:42:13)\n"  # agent
        f"    at r ({BASE}vendor/mui.js:1:5000)\n"  # declared asset
        "    at h (https://esm.sh/preact@10:1:200)\n"  # third-party host
        "    at eval (blob:https://nontainer.test/9f2a:88:9)\n"  # generated
    )
    assert kinds(stack, ("vendor",)) == ["agent", "vendor", "vendor", "opaque"]


def test_an_asset_prefix_is_only_vendor_when_declared():
    """Without the declaration those bytes are just a file the agent
    wrote, and blaming a 'library' for it would misdirect the repair."""
    stack = f"    at r ({BASE}vendor/mui.js:1:5000)\n"
    assert kinds(stack, ()) == ["agent"]
    assert kinds(stack, ("vendor",)) == ["vendor"]


def test_document_url_resolves_to_index_html():
    """An inline <script> reports the DOCUMENT url, and its line numbers
    are index.html's — the most common case in an agent's first app."""
    for url in (BASE, BASE.rstrip("/")):
        (frame,) = [classify_frame(f) for f in parse_frames(f"    at {url}:3:5")]
        assert frame.kind == "agent"
        assert frame.rel == "index.html"


def test_source_url_names_resolve():
    """`//# sourceURL=app.jsx` is how a browser-side transpiler keeps its
    output attributable; it surfaces as a bare name with no scheme."""
    (frame,) = [classify_frame(f) for f in parse_frames("    at App (app.jsx:42:13)")]
    assert frame.kind == "agent" and frame.rel == "app.jsx"


def test_traversal_in_a_frame_url_is_not_agent_code():
    (frame,) = [classify_frame(f) for f in parse_frames(f"    at {BASE}../../x.js:1:1")]
    assert frame.rel is None


# -- rendering -------------------------------------------------------------


def test_picks_the_agent_frame_under_a_pile_of_vendor_frames():
    stack = "\n".join(
        [f"    at f{i} (https://esm.sh/mui@6:1:{i})" for i in range(12)]
        + [f"    at App ({BASE}app.js:42:13)"]
    )
    out = describe_page_error("TypeError", "svae is not a function", stack)
    assert "app.js:42:13" in out
    assert "+12 frames above it in library code" in out
    assert "esm.sh" not in out  # the twelve are counted, not printed


def test_quotes_the_offending_source_line():
    stack = f"    at App ({BASE}app.js:42:13)"
    out = describe_page_error(
        "TypeError",
        "svae is not a function",
        stack,
        read_line=lambda rel, n: (
            "onClick={svae}" if (rel, n) == ("app.js", 42) else None
        ),
    )
    assert "\n     42 | onClick={svae}" in out


def test_a_single_agent_frame_reports_no_skip_count():
    out = describe_page_error("TypeError", "x", f"    at App ({BASE}app.js:9:1)")
    assert "app.js:9:1" in out and "frames above" not in out


def test_says_so_when_nothing_is_the_agents():
    """A location the agent cannot open is worse than none: it sends the
    repair loop into a bundle. Same rule as the parse-error branch."""
    vendor = "\n".join(f"    at f{i} (https://esm.sh/mui@6:1:{i})" for i in range(3))
    out = describe_page_error("TypeError", "boom", vendor)
    assert "no frame in your own files" in out
    assert "all 3 frames are in library code" in out

    blob = "    at eval (blob:https://nontainer.test/9f2a:88:9)"
    out = describe_page_error("TypeError", "boom", blob)
    assert "generated code" in out and "no file to open" in out

    mixed = vendor + "\n" + blob
    out = describe_page_error("TypeError", "boom", mixed)
    assert "3 frames in library code, 1 in generated code" in out


def test_parse_errors_keep_their_own_message():
    out = describe_page_error("SyntaxError", "Unexpected token )", "")
    assert "bisect" in out


def test_a_stackless_error_renders_plainly():
    assert describe_page_error("TypeError", "boom", "") == "TypeError: boom"


def test_a_raising_reader_still_yields_the_location():
    """A diagnostic must never break the run: if the source cannot be
    read, the agent still gets the frame it can go open."""

    def boom(rel, n):
        raise OSError("fs is gone")

    out = describe_page_error(
        "TypeError", "x", f"    at App ({BASE}app.js:42:13)", read_line=boom
    )
    assert "app.js:42:13" in out
    assert " | " not in out


# -- end to end ------------------------------------------------------------


def test_vendor_frames_are_skipped_end_to_end(chromium_available, tmp_path):
    """The whole path through a real browser: a vendored bundle throws,
    and the frame the agent is pointed at is the line in its own file
    that called it."""
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "lib.js").write_text("export function boom() { throw new Error('x'); }\n")

    ws = Workspace(KvgitProvider.open(None, session="frames-e2e"))
    rt = enable_apps(ws, AppsConfig(static_assets={"vendor": assets}))
    try:
        ws.fs.makedirs("/workspace/app", exist_ok=True)
        ws.fs.write(
            "/workspace/app/index.html",
            b"<html><body><div id='x'>hi</div>\n"
            b"<script type='module'>\n"
            b"import { boom } from './vendor/lib.js';\n"
            b"boom();\n"
            b"</script>\n</body></html>",
        )
        result = rt.test_app([{"read": "#x"}])
        err = next(e for e in result.page_errors if "Error" in e)
        # blamed on the agent's call site, not the bundle that threw
        assert "index.html:" in err
        assert "boom();" in err
        assert "vendor/lib.js" not in err
        assert "above it in library code" in err
    finally:
        ws.close()
