"""``isolation="process"``: agent code runs in a forked worker while
the workspace's world — VirtualFS, cache, checkpoints — stays in the
parent, bridged over sandtrap's RPC channel. A worker crash costs the
crashing call, never the host or the workspace."""

import os
import signal

import pytest

from nontainer import PythonConfig, Workspace
from nontainer.providers.kvgit import KvgitProvider

pytest.importorskip("sandtrap.fs.remote", reason="needs sandtrap with RemoteFS")


@pytest.fixture
def ws():
    w = Workspace(
        KvgitProvider.open(None, session="iso"),
        python=PythonConfig(isolation="process"),
    )
    yield w
    w.close()


def test_writes_land_in_workspace_and_checkpoint(ws):
    r = ws.run_python("open('/x.txt', 'w').write('hi from worker')")
    assert r.error is None
    assert ws.fs.read("/x.txt") == b"hi from worker"
    assert r.checkpoint  # the write dirtied the PARENT's fs -> committed


def test_reads_see_parent_state(ws):
    ws.write_file("/seed.txt", "from parent")
    r = ws.run_python("content = open('/seed.txt').read()")
    assert r.error is None
    assert r.namespace["content"] == "from parent"


def test_cache_round_trips_via_rpc(ws):
    assert ws.run_python("cache['n'] = 41").error is None
    r = ws.run_python(
        "m = cache['n'] + 1\nhas = 'n' in cache\nkeys = list(cache)"
    )
    assert r.error is None
    assert r.namespace["m"] == 42
    assert r.namespace["has"] is True
    assert r.namespace["keys"] == ["n"]
    assert ws.cache["n"] == 41  # the PARENT's cache is the store


def test_stdin_and_argv_cross_the_boundary(ws):
    r = ws.exec_python("line = input()", stdin="hello worker")
    assert r.error is None
    assert r.namespace["line"] == "hello worker"


def test_worker_crash_is_contained(ws):
    assert ws.run_python("open('/kept.txt', 'w').write('before')").error is None

    os.kill(ws._sandbox._process.pid, signal.SIGKILL)
    ws._sandbox._process.join(timeout=5.0)

    # next call respawns transparently; nothing already written is lost
    r = ws.run_python("content = open('/kept.txt').read()")
    assert r.error is None
    assert r.namespace["content"] == "before"


def test_close_shuts_down_the_worker():
    w = Workspace(
        KvgitProvider.open(None, session="iso-close"),
        python=PythonConfig(isolation="process"),
    )
    proc = w._sandbox._process
    assert proc.is_alive()
    w.close()
    proc.join(timeout=5.0)
    assert not proc.is_alive()
