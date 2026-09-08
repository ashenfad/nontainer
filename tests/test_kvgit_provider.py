"""KvgitProvider: the versioned substrate — commits, forks, time-travel."""

import pytest
from monkeyfs import VirtualFS

from nontainer import (
    CommitNotFoundError,
    NotSupportedError,
    Store,
    Workspace,
    WorkspaceError,
    workspace,
)
from nontainer.providers import KvgitProvider


@pytest.fixture
def kv_ws():
    """Memory-backed kvgit workspace (autocommit on by default)."""
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
    first = p.commit()
    again = p.commit()
    assert first == again  # empty commit returns current commit


# -- workspace initialization ------------------------------------------------


def test_fresh_workspace_commits_clean_init_baseline():
    p = KvgitProvider.open(None, session="fresh-init")
    empty_head = p.head

    ws = Workspace(p)
    try:
        entries = list(ws.log())
        assert entries[0].info == {"tool": "init"}
        assert entries[0].id == ws.head
        assert ws.head != empty_head
        assert not ws.dirty
        assert ws.files.fs.isdir("/workspace")
        assert ws.files.fs.getcwd() == "/workspace"
    finally:
        ws.close()


@pytest.mark.parametrize("autocommit", [True, False])
def test_first_readonly_calls_do_not_inherit_initialization(autocommit):
    p = KvgitProvider.open(None, session="readonly-init")
    ws = Workspace(p, autocommit=autocommit)
    try:
        init_head = ws.head
        before = list(ws.log())

        shell = ws.terminal("ls")
        python = ws.run_python("value = 1 + 1")

        assert shell.commit is None
        assert python.commit is None
        assert ws.head == init_head
        assert list(ws.log()) == before
        assert not ws.dirty
    finally:
        ws.close()


def test_reopen_does_not_create_another_init_commit(tmp_path):
    path = tmp_path / "kvgit"
    with Workspace(KvgitProvider.open(path, session="reopen")) as ws:
        init_head = ws.head
        init_history = list(ws.log())

    with Workspace(KvgitProvider.open(path, session="reopen")) as reopened:
        assert reopened.head == init_head
        assert list(reopened.log()) == init_history
        assert list(reopened.log())[0].info == {"tool": "init"}
        assert not reopened.dirty


def test_predirty_provider_preserves_staging_without_init_commit():
    p = KvgitProvider.open(None, session="predirty")
    p.kv["caller-pending"] = {"keep": True}
    head = p.head
    history = list(p.history())

    ws = Workspace(p)
    try:
        assert ws.head == head
        assert list(ws.log()) == history
        assert ws.dirty
        assert p.kv["caller-pending"] == {"keep": True}
        assert ws.files.fs.isdir("/workspace")
        assert ws.files.fs.getcwd() == "/workspace"
        assert p.kv[VirtualFS.CWD_KEY] == "/workspace"
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


def test_fresh_workspace_cannot_rollback_below_init():
    ws = Workspace(KvgitProvider.open(None, session="init-floor"))
    try:
        init_head = ws.head

        with pytest.raises(CommitNotFoundError, match="rollback floor"):
            ws.rollback(1)

        assert ws.head == init_head
        assert not ws.dirty
        assert ws.files.fs.isdir("/workspace")
        assert ws.files.fs.getcwd() == "/workspace"
    finally:
        ws.close()


def test_rollback_can_target_init_but_not_cross_it():
    ws = Workspace(KvgitProvider.open(None, session="rollback-to-init"))
    try:
        init_head = ws.head
        ws.terminal("mkdir -p deep; cd deep; echo changed > state.txt")
        work = ws.head

        landed = ws.rollback(1)
        assert ws.head == landed
        assert landed not in (init_head, work)  # it appended
        assert not ws.files.fs.exists("/workspace/deep")
        assert ws.files.fs.isdir("/workspace")
        assert ws.files.fs.getcwd() == "/workspace"
        # the turn it stepped off is still in the log, one back
        assert [e.id for e in ws.log()][:3] == [landed, work, init_head]

        # The floor counts over the log as it stands, and init sits two
        # back now: reaching past it is what is refused, not going back.
        with pytest.raises(CommitNotFoundError, match="rollback floor"):
            ws.rollback(3)

        assert ws.head == landed
        assert not ws.dirty
        assert ws.files.fs.isdir("/workspace")
        assert ws.files.fs.getcwd() == "/workspace"
    finally:
        ws.close()


def test_rollback_floor_survives_reopen(tmp_path):
    path = tmp_path / "kvgit"
    with Workspace(KvgitProvider.open(path, session="floor-reopen")) as ws:
        init_head = ws.head
        ws.terminal("echo changed > state.txt")

    with Workspace(KvgitProvider.open(path, session="floor-reopen")) as reopened:
        landed = reopened.rollback(1)
        assert init_head in {e.id for e in reopened.log()}
        assert not reopened.files.fs.exists("/workspace/state.txt")
        with pytest.raises(CommitNotFoundError, match="rollback floor"):
            reopened.rollback(3)
        assert reopened.head == landed
        assert reopened.files.fs.isdir("/workspace")
        assert reopened.files.fs.getcwd() == "/workspace"


def test_fork_inherits_rollback_floor():
    parent = Workspace(KvgitProvider.open(None, session="floor-parent"))
    child = None
    try:
        init_head = parent.head
        parent.terminal("echo parent > state.txt")
        child = parent.fork("floor-child")

        landed = child.rollback(1)
        assert init_head in {e.id for e in child.log()}
        assert not child.files.fs.exists("/workspace/state.txt")
        with pytest.raises(CommitNotFoundError, match="rollback floor"):
            child.rollback(3)
        assert child.head == landed
        assert child.files.fs.isdir("/workspace")
        assert child.files.fs.getcwd() == "/workspace"
    finally:
        if child is not None:
            child.close()
        parent.close()


def test_legacy_history_without_init_keeps_provider_rollback_behavior():
    p = KvgitProvider.open(None, session="legacy-rollback")
    p.fs.makedirs("/workspace", exist_ok=True)
    p.fs.chdir("/workspace")
    # Similar-looking caller metadata is not nontainer's exact marker.
    p.commit(info={"tool": "init", "source": "legacy"})
    p.fs.write("/workspace/state.txt", b"legacy")
    p.commit(info={"tool": "legacy-write"})

    ws = Workspace(p)
    try:
        seed = list(ws.log())[2]
        landed = ws.rollback(2)
        assert ws.head == landed
        assert seed.id in {e.id for e in ws.log()}
        assert not ws.files.fs.exists("/workspace")
    finally:
        ws.close()


# -- atomic commit: files + cache together ------------------------------


def test_commit_and_restore_files_and_cache(kv_ws):
    kv_ws.terminal("echo v1 > f.txt")
    kv_ws.run_python("cache['gen'] = 1")
    cp1 = kv_ws.commit(info={"label": "v1"})

    kv_ws.terminal("echo v2 > f.txt")
    kv_ws.run_python("cache['gen'] = 2")
    kv_ws.commit(info={"label": "v2"})

    assert kv_ws.terminal("cat f.txt").stdout.strip() == "v2"
    assert kv_ws.cache["gen"] == 2

    kv_ws.checkout(cp1)
    # one restore rewinds BOTH planes atomically
    assert kv_ws.terminal("cat f.txt").stdout.strip() == "v1"
    assert kv_ws.cache["gen"] == 1


def test_checkout_appends_and_keeps_every_earlier_commit(kv_ws):
    """The head only ever moves forward. Going back is a new commit
    whose state equals the target's, so the turns it steps off are
    still in the log — and still there to come back to."""
    kv_ws.terminal("echo one > f.txt")
    target = kv_ws.head
    kv_ws.terminal("echo two > f.txt")
    kv_ws.terminal("echo three > f.txt")
    before = [e.id for e in kv_ws.log()]

    landed = kv_ws.checkout(target)

    # exactly one commit: nothing raced it, so it converged first try
    assert [e.id for e in kv_ws.log()] == [landed] + before
    assert landed not in before
    assert next(iter(kv_ws.log())).info == {"tool": "checkout", "target": target}
    assert kv_ws.terminal("cat f.txt").stdout.strip() == "one"


def test_the_restore_commit_holds_the_targets_whole_world(kv_ws):
    """Files, cache, cwd and the framework's own keys: the host
    restored the whole session, so the appended commit's keyset is the
    target's keyset — not its files alone."""
    provider = kv_ws._provider
    conversation = "__agno__/session"
    kv_ws.terminal("mkdir -p one two; cd one; echo v1 > f.txt")
    kv_ws.run_python("cache['gen'] = 1")
    provider.kv[conversation] = {"run_ids": ["r1"]}
    target = kv_ws.commit(info={"label": "v1"})

    kv_ws.terminal("cd ../two; echo v2 > f.txt")
    kv_ws.run_python("cache['gen'] = 2")
    provider.kv[conversation] = {"run_ids": ["r1", "r2"]}
    kv_ws.commit(info={"label": "v2"})

    landed = kv_ws.checkout(target)

    assert dict(provider.files_at(landed)) == dict(provider.files_at(target))
    for key in (conversation, VirtualFS.CWD_KEY, "__cache__/gen"):
        assert provider.key_at(landed, key) == provider.key_at(target, key)
    # ...and the live workspace reads that way too
    assert kv_ws.terminal("cat f.txt").stdout.strip() == "v1"
    assert kv_ws.terminal("pwd").stdout.strip() == "/workspace/one"
    assert kv_ws.cache["gen"] == 1
    assert provider.kv[conversation] == {"run_ids": ["r1"]}


def test_checkout_onto_state_the_workspace_already_holds_commits_nothing(kv_ws):
    kv_ws.terminal("echo one > f.txt")
    head = kv_ws.head
    before = len(list(kv_ws.log()))

    assert kv_ws.checkout(head) == head
    assert len(list(kv_ws.log())) == before

    # ...and a target the workspace holds the CONTENT of is the same
    # no-op, however it got back there: the restore below lands one
    # commit, and checking the same target out again lands none.
    kv_ws.terminal("echo two > f.txt")
    landed = kv_ws.checkout(head)
    assert len(list(kv_ws.log())) == before + 2
    assert kv_ws.checkout(head) == landed
    assert len(list(kv_ws.log())) == before + 2


def test_a_checkout_replaces_uncommitted_writes(kv_ws):
    """A checkout says what the workspace IS afterwards, so writes that
    never landed are replaced by the restored state. ``ws.discard()``
    is the verb for dropping them on their own."""
    kv_ws.terminal("echo one > f.txt")
    target = kv_ws.head
    kv_ws.terminal("echo two > f.txt")
    kv_ws.autocommit = False
    kv_ws.files.write("/workspace/f.txt", "three\n")
    kv_ws.files.write("/workspace/scratch.txt", "wip\n")
    assert kv_ws.dirty

    kv_ws.checkout(target)

    assert not kv_ws.dirty
    assert kv_ws.files.read("/workspace/f.txt") == b"one\n"
    assert not kv_ws.files.exists("/workspace/scratch.txt")


def test_rollback_after_a_checkout_is_the_redo(kv_ws):
    """Because the checkout appended, the commit before it is the one
    it stepped off — so undo is redo-able with the relative verb."""
    kv_ws.terminal("echo one > f.txt")
    one = kv_ws.head
    kv_ws.terminal("echo two > f.txt")

    kv_ws.checkout(one)
    assert kv_ws.terminal("cat f.txt").stdout.strip() == "one"

    kv_ws.rollback(1)
    assert kv_ws.terminal("cat f.txt").stdout.strip() == "two"


def _keyset(provider, commit):
    """Every key a commit holds, with its value — the whole session."""
    handle = provider._staged.checkout(commit)
    return {key: handle.get(key) for key in handle.keys()}


def _race_commit(ws, other, interfere):
    """Make a second handle commit inside a checkout's window.

    Wraps the provider's ``commit`` so ``interfere`` fires once, in the
    gap between the keyset diff and the commit that follows it — where
    kvgit three-way merges another handle's state into the restore.
    Returns the unwrapped method, to put back afterwards.
    """
    real = ws._provider.commit
    fired: list[bool] = []

    def racing(info=None):
        if not fired:
            fired.append(True)
            other.refresh()
            interfere(other)
            other.commit(info={"tool": "other"})
        return real(info)

    ws._provider.commit = racing
    return real


def test_checkout_converges_when_a_concurrent_commit_adds_a_key(tmp_path):
    """A key another handle adds mid-checkout is in neither the diff's
    added nor its removed list — it exists at neither the head the
    checkout started from nor the target — so kvgit's merge would carry
    it into a commit claiming to be the target's state. The checkout
    re-diffs against the head that landed until it holds the target
    exactly."""
    path = tmp_path / "kvgit"
    ws = Workspace(KvgitProvider.open(path, session="race-add"))
    other = KvgitProvider.open(path, session="race-add")
    try:
        provider = ws._provider
        ws.terminal("echo one > f.txt")
        ws.run_python("cache['gen'] = 1")
        target = ws.head
        ws.terminal("echo two > f.txt")
        before = len(list(ws.log()))

        real = _race_commit(
            ws, other, lambda o: o.kv.__setitem__("__cache__/sneak", "late")
        )
        try:
            landed = ws.checkout(target)
        finally:
            provider.commit = real

        assert _keyset(provider, landed) == _keyset(provider, target)
        assert "__cache__/sneak" not in provider.kv
        assert ws.cache["gen"] == 1
        assert ws.terminal("cat f.txt").stdout.strip() == "one"
        # The concurrent commit is not lost, it is superseded: it and
        # both restore commits are in the log.
        tools = [e.info.get("tool") for e in ws.log()]
        assert tools[:3] == ["checkout", "checkout", "other"]
        assert len(list(ws.log())) == before + 3
    finally:
        other.close()
        ws.close()


def test_checkout_converges_when_a_concurrent_commit_changes_a_file(tmp_path):
    """Same window, a file the restore had no reason to touch: it is
    identical at the head and the target, so the merge takes the other
    handle's version and the first restore commit is not the target."""
    path = tmp_path / "kvgit"
    ws = Workspace(KvgitProvider.open(path, session="race-edit"))
    other = KvgitProvider.open(path, session="race-edit")
    try:
        provider = ws._provider
        ws.terminal("echo steady > kept.txt; echo one > f.txt")
        target = ws.head
        ws.terminal("echo two > f.txt")

        real = _race_commit(
            ws, other, lambda o: o.fs.write("/workspace/kept.txt", b"meddled\n")
        )
        try:
            landed = ws.checkout(target)
        finally:
            provider.commit = real

        assert _keyset(provider, landed) == _keyset(provider, target)
        assert ws.terminal("cat kept.txt").stdout.strip() == "steady"
        assert ws.terminal("cat f.txt").stdout.strip() == "one"
    finally:
        other.close()
        ws.close()


def test_checkout_unknown_id(kv_ws):
    with pytest.raises(CommitNotFoundError):
        kv_ws.checkout("0" * 40)


def test_checkout_refuses_a_session_and_says_why(tmp_path):
    """A session is a branch, and a branch is not something to check
    out — the refusal names the two verbs that do reach one."""
    store = Store(tmp_path)
    with store.open("alice") as alice, store.open("bob") as bob:
        alice.terminal("echo alice > who.txt")
        bob.terminal("echo bob > who.txt")

        with pytest.raises(WorkspaceError) as exc:
            alice.checkout("bob")

        message = str(exc.value)
        assert "sessions are branches" in message
        assert 'ws.fork("name")' in message and 'store.open("name")' in message
        # and the session it refused to leave is where it was
        assert alice.terminal("cat who.txt").stdout.strip() == "alice"


def test_checkout_returns_the_commit_it_made(kv_ws):
    """The id that comes back is the RESTORE, not the target: the
    checkout appended, so the head moved forward to reach the past."""
    kv_ws.terminal("echo one > a.txt")
    first = kv_ws.head
    kv_ws.terminal("echo two > a.txt")

    landed = kv_ws.checkout(first)

    assert landed == kv_ws.head
    assert landed != first
    assert kv_ws.terminal("cat a.txt").stdout.strip() == "one"


# -- autocommit ---------------------------------------------------------


def test_autocommit_records_tool_info(kv_ws):
    kv_ws.terminal("echo hi > a.txt")
    kv_ws.run_python("cache['x'] = 1")
    infos = [c.info.get("tool") for c in kv_ws.log()]
    assert infos[0] == "run_python"
    assert infos[1] == "terminal"


def test_readonly_calls_do_not_commit(kv_ws):
    kv_ws.terminal("echo hi > a.txt")  # one commit
    before = len(list(kv_ws.log()))
    kv_ws.terminal("ls")
    kv_ws.terminal("cat a.txt")
    kv_ws.run_python("v = 1 + 1")
    after = len(list(kv_ws.log()))
    assert after == before  # pure reads / namespace-only runs don't commit


def test_history_limit_and_time(kv_ws):
    kv_ws.terminal("echo a > a.txt")
    kv_ws.terminal("echo b > b.txt")
    entries = list(kv_ws.log(limit=2))
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
    with pytest.raises(CommitNotFoundError):
        kv_ws.rollback(50)


# -- discard (staging) --------------------------------------------------------


def test_discard_staged_writes():
    p = KvgitProvider.open(None, session="s1")
    ws = Workspace(p, autocommit=False)  # manual committing
    ws.terminal("echo keep > keep.txt")
    ws.commit()
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


def test_fork_commits_pending_changes(kv_ws):
    kv_ws.terminal("echo pending > p.txt")
    # autocommit already committed; add a staged-only change
    kv_ws.files.fs.write("staged.txt", b"staged")
    fork = kv_ws.fork("snap")
    assert fork.terminal("cat staged.txt").stdout.strip() == "staged"
    fork.close()


def test_mount_not_supported(kv_ws):
    with pytest.raises(NotSupportedError):
        kv_ws.files.export()


# -- disk persistence + factory ------------------------------------------------


def test_disk_store_persists_across_instances(tmp_path):
    with workspace("user-1", store=tmp_path, backend="kvgit") as ws:
        ws.terminal("mkdir -p proj; cd proj; echo data > d.txt")
        ws.run_python("cache['n'] = 7")

    with workspace("user-1", store=tmp_path, backend="kvgit") as ws2:
        assert ws2.terminal("pwd").stdout.strip() == "/workspace/proj"
        assert ws2.terminal("cat d.txt").stdout.strip() == "data"
        assert ws2.cache["n"] == 7
        assert len(list(ws2.log())) >= 2


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


def test_store_delete_convenience(tmp_path):
    from nontainer import Store

    with workspace("via-helper", store=tmp_path, backend="kvgit") as ws:
        ws.terminal("echo bye > b.txt")
    Store(tmp_path, backend="kvgit").delete("via-helper")
    with workspace("via-helper", store=tmp_path, backend="kvgit") as ws2:
        assert not ws2.terminal("cat b.txt")


def test_fork_at_an_earlier_commit_leaves_the_parent_alone(tmp_path):
    """A fork at a past commit starts there; the parent, including
    its staged work, is not rewound or committed to get it there."""
    from nontainer import workspace

    ws = workspace("parent", store=tmp_path)
    ws.files.fs.write("/workspace/a.txt", b"A")
    first = ws.commit(info={"tool": "test"})
    ws.files.fs.write("/workspace/b.txt", b"B")
    ws.commit(info={"tool": "test"})
    ws.files.fs.write("/workspace/staged.txt", b"S")  # staged, uncommitted
    head = ws.head

    child = ws.fork("child", at=first)
    try:
        assert child.files.fs.read("/workspace/a.txt") == b"A"
        assert not child.files.fs.exists("/workspace/b.txt")
        assert not child.files.fs.exists("/workspace/staged.txt")
        assert child.head == first
        # the parent kept its head AND its staged work
        assert ws.head == head and ws.dirty
        assert ws.files.fs.read("/workspace/staged.txt") == b"S"
    finally:
        child.close()
        ws.close()


def test_fork_at_an_unknown_commit_raises(tmp_path):
    from nontainer import workspace
    from nontainer.errors import CommitNotFoundError

    ws = workspace("parent", store=tmp_path)
    ws.files.fs.write("/workspace/a.txt", b"A")
    ws.commit()
    try:
        with pytest.raises(CommitNotFoundError):
            ws.fork("child", at="0" * 64)
    finally:
        ws.close()


# -- cwd ---------------------------------------------------------------------


def test_one_cwd_key_survives_fork_and_checkout(tmp_path):
    """cwd lives under the filesystem's own key and nowhere else, so it
    travels with the files: a fork starts where its parent stood, and a
    checkout puts the agent back where it was."""
    store = Store(tmp_path)
    with store.open("walker") as ws:
        ws.terminal("mkdir -p one two; cd one")
        here = ws.commit()
        assert ws.terminal("pwd").stdout.strip() == "/workspace/one"

        fork = ws.fork("follower")
        try:
            assert fork.terminal("pwd").stdout.strip() == "/workspace/one"
        finally:
            fork.close()

        ws.terminal("cd ../two")
        assert ws.terminal("pwd").stdout.strip() == "/workspace/two"
        ws.checkout(here)
        assert ws.terminal("pwd").stdout.strip() == "/workspace/one"

        keys = {k for k in ws._provider.kv.keys() if "cwd" in k}
        assert keys == {VirtualFS.CWD_KEY}


def test_a_legacy_cwd_key_is_adopted_and_dropped(tmp_path):
    """Stores written when nontainer kept a cwd of its own carry the
    old key: the value still decides where the session opens, and the
    key goes with the next commit."""
    store = Store(tmp_path)
    with store.open("legacy") as ws:
        ws.terminal("mkdir -p deep")
    # Rewrite the store the way the two-key layout left it.
    provider = KvgitProvider.open(tmp_path / "kvgit", session="legacy")
    try:
        del provider.kv[VirtualFS.CWD_KEY]
        provider.kv["__cwd__"] = "/workspace/deep"
        provider.commit(info={"tool": "legacy"})
    finally:
        provider.close()

    with store.open("legacy") as ws:
        assert ws.terminal("pwd").stdout.strip() == "/workspace/deep"
        assert "__cwd__" not in ws._provider.kv
        ws.commit()
    with store.open("legacy") as ws:
        assert ws.terminal("pwd").stdout.strip() == "/workspace/deep"
        assert "__cwd__" not in ws._provider.kv


def test_the_legacy_cwd_key_goes_even_when_the_new_one_is_there(tmp_path):
    """A store written under the two-key layout carries both. The
    filesystem's key is the one that resolves paths, so the old value
    is not wanted — but the key still has to go, or it stays on the
    branch to be contested by a merge that reads neither side."""
    store = Store(tmp_path)
    with store.open("both") as ws:
        ws.terminal("mkdir -p here; cd here")
    provider = KvgitProvider.open(tmp_path / "kvgit", session="both")
    try:
        provider.kv["__cwd__"] = "/workspace/stale"
        provider.commit(info={"tool": "legacy"})
    finally:
        provider.close()

    with store.open("both") as ws:
        assert ws.terminal("pwd").stdout.strip() == "/workspace/here"
        assert "__cwd__" not in ws._provider.kv
        ws.commit()
    with store.open("both") as ws:
        assert "__cwd__" not in ws._provider.kv


def test_divergent_legacy_cwd_keys_do_not_block_a_merge(tmp_path):
    """Two branches written before the fold hold different values under
    the dead key. Contested state with no rule for it aborts the whole
    merge — over a cwd neither side reads any more. The standing choice
    hands the key to the merger's side, so the merge lands and the
    branches converge on being rid of it."""
    store = Store(tmp_path)
    with store.open("main") as ws:
        ws.terminal("mkdir -p a b; echo base > base.txt")
        ws.fork("worker").close()

    # Both branches as the old layout left them, written through the
    # provider so that opening a workspace is not what put them there
    # (an open drops the key, which is the other half of the fix).
    for session, cwd in (("main", "/workspace/a"), ("worker", "/workspace/b")):
        provider = KvgitProvider.open(tmp_path / "kvgit", session=session)
        try:
            provider.kv["__cwd__"] = cwd
            provider.commit(info={"tool": "legacy"})
        finally:
            provider.close()

    main = KvgitProvider.open(tmp_path / "kvgit", session="main")
    worker = KvgitProvider.open(tmp_path / "kvgit", session="worker")
    try:
        worker.fs.write("/workspace/side.txt", b"worker\n")
        worker.commit()

        out = main.merge("worker")

        assert out.merged
        assert out.conflicts == ()
        assert main.kv.get("__cwd__") == "/workspace/a"  # ours, unread
        assert main.fs.read("/workspace/side.txt") == b"worker\n"
    finally:
        worker.close()
        main.close()

    # and the next open takes the key out for good
    with store.open("main") as ws:
        assert "__cwd__" not in ws._provider.kv
        ws.commit()
    with store.open("main") as ws:
        assert "__cwd__" not in ws._provider.kv
