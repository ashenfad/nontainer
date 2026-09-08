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
    out = kv_ws.files.put(src)
    assert out.path == "report.csv"
    assert out.size == 8 and out.created
    assert out.commit  # versioned provider: the upload's commit
    assert kv_ws.terminal("cat report.csv").stdout == "a,b\n1,2\n"
    # overwrite: created flips
    assert not kv_ws.files.put(src).created


def test_put_nested_dest_creates_parents(kv_ws, tmp_path):
    src = tmp_path / "x.bin"
    src.write_bytes(b"\x00\x01")
    kv_ws.files.put(src, "data/raw/x.bin")
    assert kv_ws.files.fs.read("data/raw/x.bin") == b"\x00\x01"


def test_put_commits(kv_ws, tmp_path):
    src = tmp_path / "f.txt"
    src.write_text("hi")
    kv_ws.files.put(src)
    assert list(kv_ws.log())[0].info.get("tool") == "put"


def test_get_returns_bytes_and_writes_dest(kv_ws, tmp_path):
    kv_ws.terminal("mkdir -p out; echo done > out/result.txt")
    data = kv_ws.files.get("out/result.txt", tmp_path / "dl" / "result.txt")
    assert data.strip() == b"done"
    assert (tmp_path / "dl" / "result.txt").read_bytes().strip() == b"done"


def test_get_does_not_commit(kv_ws):
    kv_ws.terminal("echo x > f.txt")
    before = len(list(kv_ws.log()))
    kv_ws.files.get("f.txt")
    assert len(list(kv_ws.log())) == before


def test_get_missing_raises(kv_ws):
    with pytest.raises(Exception):
        kv_ws.files.get("nope.txt")
