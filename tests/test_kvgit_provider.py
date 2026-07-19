"""KvgitProvider: the versioned substrate — checkpoints, forks, time-travel."""

import pytest

from nontainer import (
    CheckpointNotFoundError,
    NotSupportedError,
    Workspace,
    WorkspaceError,
    workspace,
)
from nontainer.providers import KvgitProvider


@pytest.fixture
def kv_ws():
    """Memory-backed kvgit workspace (autocheckpoint on by default)."""
    provider = KvgitProvider.open(None, session="test-session")
    ws = Workspace(provider)
    yield ws
    ws.close()


# -- provider basics -------------------------------------------------------


def test_caps(kv_ws):
    caps = kv_ws.caps
    assert caps.versioned and caps.staging and caps.cheap_fork and caps.merge
    assert not caps.sql_audit and not caps.fuse_mount


def test_session_validated():
    with pytest.raises(Exception):
        KvgitProvider.open(None, session="../escape")


def test_no_changes_no_commit():
    p = KvgitProvider.open(None, session="s1")
    first = p.checkpoint()
    again = p.checkpoint()
    assert first == again  # empty checkpoint returns current commit


# -- atomic checkpoint: files + cache together ------------------------------


def test_checkpoint_and_restore_files_and_cache(kv_ws):
    kv_ws.terminal("echo v1 > f.txt")
    kv_ws.run_python("cache['gen'] = 1")
    cp1 = kv_ws.checkpoint(info={"label": "v1"})

    kv_ws.terminal("echo v2 > f.txt")
    kv_ws.run_python("cache['gen'] = 2")
    kv_ws.checkpoint(info={"label": "v2"})

    assert kv_ws.terminal("cat f.txt").stdout.strip() == "v2"
    assert kv_ws.cache["gen"] == 2

    kv_ws.restore(cp1)
    # one restore rewinds BOTH planes atomically
    assert kv_ws.terminal("cat f.txt").stdout.strip() == "v1"
    assert kv_ws.cache["gen"] == 1


def test_restore_unknown_id(kv_ws):
    with pytest.raises(CheckpointNotFoundError):
        kv_ws.restore("0" * 40)


# -- autocheckpoint ---------------------------------------------------------


def test_autocheckpoint_records_tool_info(kv_ws):
    kv_ws.terminal("echo hi > a.txt")
    kv_ws.run_python("cache['x'] = 1")
    infos = [c.info.get("tool") for c in kv_ws.history()]
    assert infos[0] == "run_python"
    assert infos[1] == "terminal"


def test_readonly_calls_do_not_commit(kv_ws):
    kv_ws.terminal("echo hi > a.txt")  # one commit
    before = len(list(kv_ws.history()))
    kv_ws.terminal("ls")
    kv_ws.terminal("cat a.txt")
    kv_ws.run_python("v = 1 + 1")
    after = len(list(kv_ws.history()))
    assert after == before  # pure reads / namespace-only runs don't commit


def test_history_limit_and_time(kv_ws):
    kv_ws.terminal("echo a > a.txt")
    kv_ws.terminal("echo b > b.txt")
    entries = list(kv_ws.history(limit=2))
    assert len(entries) == 2
    assert entries[0].time > 0


# -- rollback sugar ----------------------------------------------------------


def test_rollback_steps(kv_ws):
    kv_ws.terminal("echo one > f.txt")
    kv_ws.terminal("echo two > f.txt")
    kv_ws.rollback(1)
    assert kv_ws.terminal("cat f.txt").stdout.strip() == "one"


def test_rollback_restores_cwd(kv_ws):
    kv_ws.terminal("mkdir -p deep/nest; cd deep/nest")
    assert kv_ws.terminal("pwd").stdout.strip().endswith("deep/nest")
    kv_ws.rollback(1)  # back before the cd (mkdir+cd was one call/commit)
    assert kv_ws.terminal("pwd").stdout.strip() == "/"


def test_rollback_past_history_raises(kv_ws):
    kv_ws.terminal("echo x > f.txt")
    with pytest.raises(CheckpointNotFoundError):
        kv_ws.rollback(50)


# -- discard (staging) --------------------------------------------------------


def test_discard_staged_writes():
    p = KvgitProvider.open(None, session="s1")
    ws = Workspace(p, autocheckpoint=False)  # manual checkpointing
    ws.terminal("echo keep > keep.txt")
    ws.checkpoint()
    ws.terminal("echo drop > drop.txt")
    assert ws.terminal("cat drop.txt").stdout.strip() == "drop"
    ws.discard()
    assert not ws.terminal("cat drop.txt")  # gone
    assert ws.terminal("cat keep.txt").stdout.strip() == "keep"
    ws.close()


# -- fork ---------------------------------------------------------------------


def test_fork_sees_state_and_diverges(kv_ws):
    kv_ws.terminal("echo shared > base.txt")
    fork = kv_ws.fork("experiment")

    assert fork.session == "experiment"
    assert fork.terminal("cat base.txt").stdout.strip() == "shared"

    fork.terminal("echo only-fork > fork.txt")
    assert not kv_ws.terminal("cat fork.txt")  # original untouched

    kv_ws.terminal("echo only-main > main.txt")
    assert not fork.terminal("cat main.txt")  # fork untouched
    fork.close()


def test_fork_duplicate_name_rejected(kv_ws):
    kv_ws.fork("dup")
    with pytest.raises(WorkspaceError):
        kv_ws.fork("dup")


def test_fork_checkpoints_pending_changes(kv_ws):
    kv_ws.terminal("echo pending > p.txt")
    # autocheckpoint already committed; add a staged-only change
    kv_ws.fs.write("staged.txt", b"staged")
    fork = kv_ws.fork("snap")
    assert fork.terminal("cat staged.txt").stdout.strip() == "staged"
    fork.close()


def test_mount_not_supported(kv_ws):
    with pytest.raises(NotSupportedError):
        kv_ws.mount()


# -- disk persistence + factory ------------------------------------------------


def test_disk_store_persists_across_instances(tmp_path):
    with workspace("user-1", store=tmp_path, backend="kvgit") as ws:
        ws.terminal("mkdir -p proj; cd proj; echo data > d.txt")
        ws.run_python("cache['n'] = 7")

    with workspace("user-1", store=tmp_path, backend="kvgit") as ws2:
        assert ws2.terminal("pwd").stdout.strip() == "/workspace/proj"
        assert ws2.terminal("cat d.txt").stdout.strip() == "data"
        assert ws2.cache["n"] == 7
        assert len(list(ws2.history())) >= 2


def test_sessions_are_independent_branches(tmp_path):
    with workspace("alice", store=tmp_path, backend="kvgit") as wa:
        wa.terminal("echo alice > who.txt")
    with workspace("bob", store=tmp_path, backend="kvgit") as wb:
        assert not wb.terminal("cat who.txt")  # bob starts empty
        wb.terminal("echo bob > who.txt")
    with workspace("alice", store=tmp_path, backend="kvgit") as wa2:
        assert wa2.terminal("cat who.txt").stdout.strip() == "alice"
