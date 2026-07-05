"""Host-side file transfer: put (upload) / get (download)."""

import pytest

from nontainer import Workspace
from nontainer.providers import KvgitProvider


@pytest.fixture
def kv_ws():
    ws = Workspace(KvgitProvider.open(None, session="s1"))
    yield ws
    ws.close()


def test_put_defaults_to_basename(kv_ws, tmp_path):
    src = tmp_path / "report.csv"
    src.write_bytes(b"a,b\n1,2\n")
    path = kv_ws.put(src)
    assert path == "report.csv"
    assert kv_ws.terminal("cat report.csv").stdout == "a,b\n1,2\n"


def test_put_nested_dest_creates_parents(kv_ws, tmp_path):
    src = tmp_path / "x.bin"
    src.write_bytes(b"\x00\x01")
    kv_ws.put(src, "data/raw/x.bin")
    assert kv_ws.fs.read("data/raw/x.bin") == b"\x00\x01"


def test_put_checkpoints(kv_ws, tmp_path):
    src = tmp_path / "f.txt"
    src.write_text("hi")
    kv_ws.put(src)
    assert list(kv_ws.history())[0].info.get("tool") == "put"


def test_get_returns_bytes_and_writes_dest(kv_ws, tmp_path):
    kv_ws.terminal("mkdir -p out; echo done > out/result.txt")
    data = kv_ws.get("out/result.txt", tmp_path / "dl" / "result.txt")
    assert data.strip() == b"done"
    assert (tmp_path / "dl" / "result.txt").read_bytes().strip() == b"done"


def test_get_does_not_checkpoint(kv_ws):
    kv_ws.terminal("echo x > f.txt")
    before = len(list(kv_ws.history()))
    kv_ws.get("f.txt")
    assert len(list(kv_ws.history())) == before


def test_get_missing_raises(kv_ws):
    with pytest.raises(Exception):
        kv_ws.get("nope.txt")
