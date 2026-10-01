"""Byte ranges on static paths: what lets a media element seek.

A browser seeks an audio or video clip by asking for part of the file.
A server that ignores the ask leaves the clip unseekable, and a player
that starts the clip part-way through (a scrubbed video) plays it from
its beginning, or not at all.
"""

import io
import wave

import pytest

from nontainer import Workspace
from nontainer.apps import AppsConfig, enable_apps, request
from nontainer.apps.dispatch import _byte_range
from nontainer.providers import KvgitProvider

BODY = bytes(range(256)) * 4  # 1024 bytes, each position recognizable


def _silence(seconds: float) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(b"\0\0" * int(24000 * seconds))
    return buf.getvalue()


def _ranged(spec: str | None, size: int = 1024, **extra: str):
    headers = {"range": spec} if spec is not None else {}
    headers.update(extra)
    return _byte_range(request("GET", "/x", headers=headers), size)


# -- what a Range header asks for --------------------------------------------


def test_the_range_forms_a_browser_sends():
    assert _ranged("bytes=10-19") == (10, 19)
    assert _ranged("bytes=1000-") == (1000, 1023)  # to the end
    assert _ranged("bytes=-24") == (1000, 1023)  # the last 24
    assert _ranged("bytes=1000-5000") == (1000, 1023)  # clamped to the file
    assert _ranged("bytes=-5000") == (0, 1023)  # a suffix longer than the file


def test_what_is_answered_with_the_whole_file():
    """A server may ignore a Range it does not support; these responses
    carry no validator, so an If-Range cannot be confirmed either."""
    for spec in (None, "", "bytes=0-1,5-6", "items=0-1", "bytes=x-y", "bytes=5-2"):
        assert _ranged(spec) is None, spec
    assert _ranged("bytes=0-1", **{"if-range": '"v1"'}) is None
    assert _byte_range(request("GET", "/x", headers={"Range": "bytes=0-1"}), 10) == (
        0,
        1,
    )


def test_a_range_outside_the_file_is_unsatisfiable():
    assert _ranged("bytes=1024-") == "unsatisfiable"
    assert _ranged("bytes=-0") == "unsatisfiable"
    assert _ranged("bytes=0-", size=0) == "unsatisfiable"


# -- served, from the workspace and from asset directories --------------------


@pytest.fixture
def served(tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "song.mp3").write_bytes(BODY)
    ws = Workspace(KvgitProvider.open(None, session="s1"))
    rt = enable_apps(
        ws,
        AppsConfig(static_assets={"vendor": assets}, max_binary_response_bytes=2000),
    )
    ws.files.fs.makedirs("/workspace/app/audio", exist_ok=True)
    ws.files.fs.write("/workspace/app/audio/voice.wav", BODY)
    ws.files.fs.write("/workspace/app/audio/huge.wav", b"\0" * 4000)
    ws.commit()
    yield rt
    ws.close()


def _get(rt, path, **headers):
    return rt.dispatch(request("GET", path, headers=headers))


@pytest.mark.parametrize("path", ["/audio/voice.wav", "/vendor/song.mp3"])
def test_a_static_file_answers_the_range_it_is_asked_for(served, path):
    whole = _get(served, path)
    assert whole.status == 200 and whole.content == BODY
    assert whole.headers["accept-ranges"] == "bytes"
    assert whole.content_type in ("audio/wav", "audio/mpeg")

    part = _get(served, path, range="bytes=100-199")
    assert part.status == 206
    assert part.content == BODY[100:200]
    assert part.headers["content-range"] == "bytes 100-199/1024"
    assert part.content_type == whole.content_type

    tail = _get(served, path, range="bytes=-24")
    assert tail.status == 206 and tail.content == BODY[-24:]

    beyond = _get(served, path, range="bytes=5000-")
    assert beyond.status == 416
    assert beyond.headers["content-range"] == "bytes */1024"


def test_the_size_caps_judge_the_file_not_the_part(served):
    """A file too big to serve whole is not served a piece at a time."""
    assert _get(served, "/audio/huge.wav").status == 500
    assert _get(served, "/audio/huge.wav", range="bytes=0-9").status == 500


def test_the_router_lets_ranges_through_both_ways(tmp_path):
    """The Range header reaches dispatch, and the range headers reach
    the client: both directions run through an allowlist."""
    pytest.importorskip("starlette")
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from nontainer.apps import build_router, mint_token

    ws = Workspace(KvgitProvider.open(None, session="s1"))
    enable_apps(ws)
    ws.files.fs.makedirs("/workspace/app", exist_ok=True)
    ws.files.fs.write("/workspace/app/clip.wav", BODY)
    ws.commit()
    token = mint_token()
    app = Starlette()
    app.mount("/apps", build_router(lambda t: ws if t == token else None))
    client = TestClient(app)
    r = client.get(f"/apps/{token}/clip.wav", headers={"Range": "bytes=10-19"})
    assert r.status_code == 206
    assert r.content == BODY[10:20]
    assert r.headers["content-range"] == "bytes 10-19/1024"
    assert r.headers["accept-ranges"] == "bytes"
    assert r.headers["content-type"] == "audio/wav"
    ws.close()


# -- in a browser -------------------------------------------------------------


def test_a_browser_can_seek_into_a_served_clip(chromium_available):
    """The symptom this fixes, end to end through test_app: a clip the
    browser could not seek, so a player scrubbed into its middle played
    from its beginning. Now it reports itself seekable, and playing
    from part-way through starts there."""
    ws = Workspace(KvgitProvider.open(None, session="s1"))
    rt = enable_apps(ws)
    ws.files.fs.makedirs("/workspace/app", exist_ok=True)
    ws.files.fs.write("/workspace/app/clip.wav", _silence(3.0))
    ws.files.fs.write(
        "/workspace/app/index.html",
        b'<!doctype html><audio id="a" src="clip.wav" preload="auto"></audio>',
    )
    ws.commit()
    result = rt.test_app(
        [
            {"assert": "document.getElementById('a').readyState >= 1"},
            {
                "eval": "(() => { const s = document.getElementById('a').seekable; "
                "return s.length ? s.end(s.length - 1) : 0; })()"
            },
            {
                "eval": "(() => { const a = document.getElementById('a'); "
                "a.currentTime = 1.5; a.play(); return true; })()"
            },
            {"wait": 500},
            {"eval": "document.getElementById('a').currentTime"},
        ]
    )
    ws.close()
    assert result.ok, result
    evals = [r.value for r in result.results if "eval" in r.action]
    assert float(evals[0]) == pytest.approx(3.0, abs=0.05)  # the whole clip
    assert 1.5 <= float(evals[2]) < 2.5  # played on from where it was put
