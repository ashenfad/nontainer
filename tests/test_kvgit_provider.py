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


# -- workspace initialization ------------------------------------------------


def test_fresh_workspace_commits_clean_init_baseline():
    p = KvgitProvider.open(None, session="fresh-init")
    empty_head = p.head

    ws = Workspace(p)
    try:
        entries = list(ws.history())
        assert entries[0].info == {"tool": "init"}
        assert entries[0].id == ws.head
        assert ws.head != empty_head
        assert not ws.dirty
        assert ws.fs.isdir("/workspace")
        assert ws.fs.getcwd() == "/workspace"
    finally:
        ws.close()


@pytest.mark.parametrize("autocheckpoint", [True, False])
def test_first_readonly_calls_do_not_inherit_initialization(autocheckpoint):
    p = KvgitProvider.open(None, session="readonly-init")
    ws = Workspace(p, autocheckpoint=autocheckpoint)
    try:
        init_head = ws.head
        before = list(ws.history())

        shell = ws.terminal("ls")
        python = ws.run_python("value = 1 + 1")

        assert shell.checkpoint is None
        assert python.checkpoint is None
        assert ws.head == init_head
        assert list(ws.history()) == before
        assert not ws.dirty
    finally:
        ws.close()


def test_reopen_does_not_create_another_init_checkpoint(tmp_path):
    path = tmp_path / "kvgit"
    with Workspace(KvgitProvider.open(path, session="reopen")) as ws:
        init_head = ws.head
        init_history = list(ws.history())

    with Workspace(KvgitProvider.open(path, session="reopen")) as reopened:
        assert reopened.head == init_head
        assert list(reopened.history()) == init_history
        assert list(reopened.history())[0].info == {"tool": "init"}
        assert not reopened.dirty


def test_predirty_provider_preserves_staging_without_init_commit():
    p = KvgitProvider.open(None, session="predirty")
    p.kv["caller-pending"] = {"keep": True}
    head = p.head
    history = list(p.history())

    ws = Workspace(p)
    try:
        assert ws.head == head
        assert list(ws.history()) == history
        assert ws.dirty
        assert p.kv["caller-pending"] == {"keep": True}
        assert ws.fs.isdir("/workspace")
        assert ws.fs.getcwd() == "/workspace"
        assert p.kv["__cwd__"] == "/workspace"
    finally:
        ws.close()


def test_executor_open_failure_leaves_clean_init_baseline():
    class FailingExecutor:
        supports_commands = True

        def open(self, context):
            assert context.head is not None
            assert context.head() == p.head
            raise RuntimeError("executor unavailable")

    p = KvgitProvider.open(None, session="failed-open")
    with pytest.raises(RuntimeError, match="executor unavailable"):
        Workspace(p, executor=FailingExecutor())

    assert not p.dirty
    assert list(p.history())[0].info == {"tool": "init"}
    assert p.fs.isdir("/workspace")
    assert p.fs.getcwd() == "/workspace"
    p.close()


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
    assert kv_ws.terminal("pwd").stdout.strip() == "/workspace"


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


# -- delete ------------------------------------------------------------------


def _kvgit_dir(tmp_path):
    return tmp_path / "kvgit"


def test_delete_removes_branch_and_frees_the_name(tmp_path):
    with workspace("gone", store=tmp_path, backend="kvgit") as ws:
        ws.terminal("echo secret > s.txt")
    KvgitProvider.delete(_kvgit_dir(tmp_path), {"gone"})
    # the name is free again: reopening starts an EMPTY branch, not a
    # resume of the deleted one (files stay deleted)
    with workspace("gone", store=tmp_path, backend="kvgit") as ws2:
        assert not ws2.terminal("cat s.txt")


def test_delete_the_only_branch(tmp_path):
    # the wrinkle this API exists for: deleting the sole branch, with
    # nothing else to anchor a store handle on
    with workspace("solo", store=tmp_path, backend="kvgit") as ws:
        ws.terminal("echo x > x.txt")
    KvgitProvider.delete(_kvgit_dir(tmp_path), {"solo"})
    with workspace("solo", store=tmp_path, backend="kvgit") as ws2:
        assert not ws2.terminal("cat x.txt")


def test_delete_leaves_siblings_untouched(tmp_path):
    with workspace("keep", store=tmp_path, backend="kvgit") as wk:
        wk.terminal("echo alive > k.txt")
    with workspace("drop", store=tmp_path, backend="kvgit") as wd:
        wd.terminal("echo doomed > d.txt")
    KvgitProvider.delete(_kvgit_dir(tmp_path), {"drop"})
    with workspace("keep", store=tmp_path, backend="kvgit") as wk2:
        assert wk2.terminal("cat k.txt").stdout.strip() == "alive"


def test_delete_nonexistent_name_is_noop(tmp_path):
    with workspace("real", store=tmp_path, backend="kvgit") as ws:
        ws.terminal("echo hi > h.txt")
    # mix a live name with a never-existed one: no raise, real one gone
    KvgitProvider.delete(_kvgit_dir(tmp_path), {"real", "never-was"})
    with workspace("real", store=tmp_path, backend="kvgit") as ws2:
        assert not ws2.terminal("cat h.txt")


def test_delete_from_nonexistent_store_is_noop(tmp_path):
    KvgitProvider.delete(tmp_path / "no-such-store", {"whatever"})  # no raise


def test_delete_purges_legacy_void_anchor(tmp_path):
    # Stores written by the OLD code carry a hidden __void__ branch that
    # pins a dead session's whole history (the retention bug). delete now
    # always folds __void__ into the doomed set, so a normal session
    # delete purges the stale anchor from such stores. Forge one the old
    # way (fork __void__ off a session branch), then delete.
    import kvgit

    with workspace("s", store=tmp_path, backend="kvgit") as ws:
        ws.terminal("echo x > x.txt")

    forge = kvgit.store(kind="disk", path=str(_kvgit_dir(tmp_path)), branch="s")
    forge.create_branch("__void__")  # legacy anchor, forks s's commit
    assert "__void__" in forge.list_branches()
    _closer = getattr(forge.versioned.store, "close", None)
    if callable(_closer):
        _closer()

    KvgitProvider.delete(_kvgit_dir(tmp_path), {"s"})

    # Both the session AND the legacy anchor are gone (orphans swept).
    probe = kvgit.store(kind="disk", path=str(_kvgit_dir(tmp_path)), branch="probe")
    branches = set(probe.list_branches())
    assert "__void__" not in branches
    assert "s" not in branches
    _closer2 = getattr(probe.versioned.store, "close", None)
    if callable(_closer2):
        _closer2()

    # And the deleted name stays deleted (no resurrection).
    with workspace("s", store=tmp_path, backend="kvgit") as ws2:
        assert not ws2.terminal("cat x.txt")


def test_delete_empty_set_is_noop(tmp_path):
    with workspace("s", store=tmp_path, backend="kvgit") as ws:
        ws.terminal("echo x > x.txt")
    KvgitProvider.delete(_kvgit_dir(tmp_path), set())  # no store touched
    with workspace("s", store=tmp_path, backend="kvgit") as ws2:
        assert ws2.terminal("cat x.txt").stdout.strip() == "x"


def test_delete_workspace_convenience(tmp_path):
    from nontainer import delete_workspace

    with workspace("via-helper", store=tmp_path, backend="kvgit") as ws:
        ws.terminal("echo bye > b.txt")
    delete_workspace("via-helper", store=tmp_path, backend="kvgit")
    with workspace("via-helper", store=tmp_path, backend="kvgit") as ws2:
        assert not ws2.terminal("cat b.txt")
