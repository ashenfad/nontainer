"""AgentFSProvider spike: fs + kv over one SQLite file, tools unchanged."""

import sqlite3

import pytest

from nontainer import NotSupportedError, Workspace, workspace

pytest.importorskip("agentfs_sdk")

from nontainer.providers import AgentFSProvider  # noqa: E402


@pytest.fixture
def afs_ws(tmp_path):
    provider = AgentFSProvider(tmp_path / "s1.db", session="s1")
    ws = Workspace(provider)
    yield ws
    ws.close()


# -- provider basics ---------------------------------------------------------


def test_caps(afs_ws):
    caps = afs_ws.caps
    assert not caps.versioned
    assert caps.sql_audit


def test_versioning_raises(afs_ws):
    with pytest.raises(NotSupportedError):
        afs_ws.checkpoint()
    with pytest.raises(NotSupportedError):
        afs_ws.fork("x")


# -- terminal over agentfs -----------------------------------------------------


def test_terminal_pipeline(afs_ws):
    r = afs_ws.terminal("mkdir -p src; echo 'b\na' > src/f.txt; cat src/f.txt | sort")
    assert r, r.stderr
    assert r.stdout.splitlines() == ["a", "b"]


def test_cwd_stateful(afs_ws):
    afs_ws.terminal("mkdir -p deep/nest")
    afs_ws.terminal("cd deep/nest")
    # cwd starts at the workspace root (/workspace by default), so the
    # relative cd lands under it. This assertion went stale when the
    # root contract landed (0.2.0) — masked because neither local dev
    # nor CI installs the [agentfs] extra.
    assert afs_ws.terminal("pwd").stdout.strip() == "/workspace/deep/nest"


def test_terminal_failure(afs_ws):
    r = afs_ws.terminal("cat /missing.txt")
    assert not r
    assert r.stderr


def test_append_redirect(afs_ws):
    afs_ws.terminal("echo one > log.txt; echo two >> log.txt")
    assert afs_ws.terminal("cat log.txt").stdout.splitlines() == ["one", "two"]


def test_glob_and_find(afs_ws):
    afs_ws.terminal("mkdir -p a b; touch a/x.py b/y.py b/z.txt")
    r = afs_ws.terminal("find . -name '*.py' | sort")
    assert r, r.stderr
    assert "x.py" in r.stdout and "y.py" in r.stdout and "z.txt" not in r.stdout


# -- python over agentfs --------------------------------------------------------


def test_python_reads_writes_files(afs_ws):
    afs_ws.terminal("echo hello > in.txt")
    r = afs_ws.run_python(
        "content = open('in.txt').read().strip()\n"
        "open('out.txt', 'w').write(content.upper())"
    )
    assert r, r.error
    assert afs_ws.terminal("cat out.txt").stdout.strip() == "HELLO"


def test_python_bridge_in_terminal(afs_ws):
    r = afs_ws.terminal("python -c 'print(2**10)'")
    assert r, r.stderr
    assert r.stdout.strip() == "1024"


# -- cache over agentfs kv --------------------------------------------------------


def test_cache_json_values_and_pickle_fallback(afs_ws):
    afs_ws.run_python("cache['plain'] = {'a': [1, 2]}")
    afs_ws.run_python("cache['exotic'] = (1, 2, 3)")  # tuple: not JSON-native
    r = afs_ws.run_python("t = cache['exotic']; d = cache['plain']")
    assert r, r.error
    assert r.namespace["t"] == (1, 2, 3)
    assert r.namespace["d"] == {"a": [1, 2]}


def test_cache_persists_across_reopen(tmp_path):
    p1 = AgentFSProvider(tmp_path / "s1.db", session="s1")
    ws1 = Workspace(p1)
    ws1.run_python("cache['n'] = 7")
    ws1.terminal("echo stay > f.txt")
    ws1.close()

    p2 = AgentFSProvider(tmp_path / "s1.db", session="s1")
    ws2 = Workspace(p2)
    assert ws2.cache["n"] == 7
    assert ws2.terminal("cat f.txt").stdout.strip() == "stay"
    ws2.close()


# -- the artifact story ------------------------------------------------------------


def test_workspace_is_one_inspectable_sqlite_file(tmp_path):
    p = AgentFSProvider(tmp_path / "s1.db", session="s1")
    ws = Workspace(p)
    ws.terminal("echo audit-me > f.txt")
    ws.close()

    # the workspace is a plain sqlite file, openable by standard tooling
    con = sqlite3.connect(tmp_path / "s1.db")
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    con.close()
    assert tables  # schema is visible to any sqlite client


# -- factory ------------------------------------------------------------------------


def test_factory_agentfs_backend(tmp_path):
    with workspace("user-1", store=tmp_path, backend="agentfs") as ws:
        ws.terminal("echo hi > f.txt")
        assert ws.terminal("cat f.txt").stdout.strip() == "hi"
    assert (tmp_path / "user-1.db").exists()


def test_open_plus_modes_round_trip(tmp_path):
    """PR#1 review: r+/w+/a+ must preserve/truncate correctly."""
    p = AgentFSProvider(tmp_path / "s1.db", session="s1")
    fs = p.fs
    fs.write("/f.txt", b"hello world")

    with fs.open("/f.txt", "r+") as f:  # preserves, writes back
        f.write("HELLO")
    assert fs.read("/f.txt") == b"HELLO world"

    with fs.open("/f.txt", "a") as f:  # appends
        f.write("+more")
    assert fs.read("/f.txt") == b"HELLO world+more"

    with fs.open("/f.txt", "w+") as f:  # TRUNCATES
        f.write("fresh")
    assert fs.read("/f.txt") == b"fresh"

    try:
        fs.open("/missing.txt", "r+")
        raise AssertionError("r+ on missing file should raise")
    except FileNotFoundError:
        pass
    p.close()


# -- delete -------------------------------------------------------------------------


def test_delete_unlinks_db_files(tmp_path):
    a = AgentFSProvider(tmp_path / "a.db", session="a")
    a.close()
    b = AgentFSProvider(tmp_path / "b.db", session="b")
    b.close()
    AgentFSProvider.delete(tmp_path, {"a"})
    assert not (tmp_path / "a.db").exists()
    assert (tmp_path / "b.db").exists()  # sibling untouched


def test_delete_nonexistent_and_empty_store_are_noops(tmp_path):
    a = AgentFSProvider(tmp_path / "a.db", session="a")
    a.close()
    AgentFSProvider.delete(tmp_path, {"a", "never"})  # missing name: no raise
    assert not (tmp_path / "a.db").exists()
    AgentFSProvider.delete(tmp_path / "no-store", {"whatever"})  # no store: no raise


def test_delete_rejects_bad_session_id(tmp_path):
    from nontainer import SessionIdError

    with pytest.raises(SessionIdError):
        AgentFSProvider.delete(tmp_path, {"../escape"})


def test_delete_workspace_convenience(tmp_path):
    from nontainer import delete_workspace

    with workspace("s", store=tmp_path, backend="agentfs"):
        pass
    assert (tmp_path / "s.db").exists()
    delete_workspace("s", store=tmp_path, backend="agentfs")
    assert not (tmp_path / "s.db").exists()
