"""Mounts (workspace volumes) and Cache key rules."""

import pytest

from nontainer import Cache, CacheError, Mount, Workspace
from nontainer.providers import DirProvider


def test_readonly_mount_visible_to_both_tools(tmp_path):
    src = tmp_path / "datasets"
    src.mkdir()
    (src / "a.csv").write_text("x,y\n1,2\n")

    p = DirProvider(tmp_path / "ws", session="s1")
    ws = Workspace(p, mounts={"/data": Mount(src)})

    r = ws.terminal("cat /data/a.csv")
    assert r, r.stderr
    assert "x,y" in r.stdout

    r = ws.run_python("content = open('/data/a.csv').read()")
    assert r, r.error
    assert "1,2" in r.namespace["content"]
    ws.close()


def test_readonly_mount_blocks_writes(tmp_path):
    src = tmp_path / "datasets"
    src.mkdir()
    p = DirProvider(tmp_path / "ws", session="s1")
    ws = Workspace(p, mounts={"/data": Mount(src)})
    r = ws.terminal("echo nope > /data/new.txt")
    assert not r
    assert not (src / "new.txt").exists()
    ws.close()


def test_writable_mount(tmp_path):
    src = tmp_path / "scratch"
    src.mkdir()
    p = DirProvider(tmp_path / "ws", session="s1")
    ws = Workspace(p, mounts={"/scratch": Mount(src, readonly=False)})
    r = ws.terminal("echo hi > /scratch/out.txt")
    assert r, r.stderr
    assert (src / "out.txt").read_text().strip() == "hi"
    ws.close()


def test_bad_mount_points_rejected(tmp_path):
    src = tmp_path / "d"
    src.mkdir()
    p = DirProvider(tmp_path / "ws", session="s1")
    with pytest.raises(ValueError):
        Workspace(p, mounts={"/": Mount(src)})
    with pytest.raises(ValueError):
        Workspace(p, mounts={"relative": Mount(src)})
    with pytest.raises(ValueError):
        Workspace(p, mounts={"/x": Mount(src / "missing")})


# -- Cache key rules -------------------------------------------------------


def test_cache_key_rules():
    cache = Cache({})
    with pytest.raises(ValueError, match="__"):
        cache["__reserved"] = 1
    with pytest.raises(ValueError, match="/"):
        cache["a/b"] = 1
    with pytest.raises(TypeError):
        cache[42] = 1  # type: ignore[index]


def test_cache_rejects_unpicklable():
    cache = Cache({})
    with pytest.raises(CacheError, match="not picklable"):
        cache["gen"] = (x for x in range(3))


def test_cache_mapping_behavior():
    backing: dict = {"unrelated": 1}
    cache = Cache(backing)
    cache["a"] = 1
    cache["b"] = 2
    assert set(cache) == {"a", "b"}
    assert len(cache) == 2
    assert "a" in cache
    assert "unrelated" not in cache
    del cache["a"]
    assert "a" not in cache
    assert backing["__cache__/b"] == 2
