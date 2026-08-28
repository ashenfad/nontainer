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


# -- mounts across forks ---------------------------------------------------
#
# fork() used to rebuild a Workspace from a hand-listed set of fields,
# and `mounts` was added after that list was written — so a fork (and
# therefore a PUBLISHED snapshot, which is a fork) silently lost every
# mount. These pin the contract the Mount docstring states: the point is
# inherited, the data behind it stays a live view.


def _mounted_kv_ws(src, point="/data"):
    from nontainer.providers import KvgitProvider

    provider = KvgitProvider.open(None, session="parent")
    return Workspace(provider, mounts={point: Mount(src)})


def test_fork_inherits_mount_points(tmp_path):
    src = tmp_path / "datasets"
    src.mkdir()
    (src / "a.csv").write_text("x,y\n1,2\n")

    ws = _mounted_kv_ws(src)
    ws.terminal("echo owned > /workspace/own.txt")
    fork = ws.fork("child")
    try:
        # versioned state carries (it always did) ...
        assert fork.fs.exists("/workspace/own.txt")
        # ... and so does the mount point (it did not)
        assert fork.fs.exists("/data/a.csv")
        r = fork.terminal("cat /data/a.csv")
        assert r, r.stderr
        assert "x,y" in r.stdout
    finally:
        fork.close()
        ws.close()


def test_forked_mount_is_a_live_view_not_a_copy(tmp_path):
    src = tmp_path / "datasets"
    src.mkdir()
    (src / "a.csv").write_text("before\n")

    ws = _mounted_kv_ws(src)
    fork = ws.fork("child")
    try:
        # The host directory is the single source of truth for both.
        (src / "a.csv").write_text("after\n")
        assert fork.fs.read("/data/a.csv").decode() == "after\n"
        assert ws.fs.read("/data/a.csv").decode() == "after\n"
    finally:
        fork.close()
        ws.close()


def test_forked_mount_keeps_readonly(tmp_path):
    src = tmp_path / "datasets"
    src.mkdir()
    (src / "a.csv").write_text("x\n")

    ws = _mounted_kv_ws(src)
    fork = ws.fork("child")
    try:
        r = fork.terminal("echo nope > /data/new.txt")
        assert not r
        assert not (src / "new.txt").exists()
    finally:
        fork.close()
        ws.close()


def test_fork_replays_every_pass_through_setting():
    """Guard: the fields a fork replays are derived from one record, so
    a new Workspace() argument cannot silently fall out of fork() the
    way `mounts` did. When this fails, add the parameter to _Settings —
    or to the exclusions below, with a reason."""
    import inspect

    from nontainer.workspace import _Settings

    # provider: the fork gets its own -- that IS the fork.
    # executor:  a ready instance is bound to one session; forks fall
    #            back to executor_factory.
    # commands:  register_command mutates the built set after
    #            construction, so fork() replays the live set instead.
    excluded = {"self", "provider", "executor", "commands"}
    params = {
        name
        for name, p in inspect.signature(Workspace.__init__).parameters.items()
        if p.kind is not inspect.Parameter.VAR_KEYWORD
    } - excluded
    replayed = set(_Settings.__dataclass_fields__)
    assert params == replayed, (
        f"Workspace.__init__ parameters not replayed by fork(): "
        f"{sorted(params - replayed)}; stale _Settings fields: "
        f"{sorted(replayed - params)}"
    )
