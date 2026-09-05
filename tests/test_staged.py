"""KvgitProvider staged mode: index, selective commit, suspension."""

import threading

import pytest

from nontainer import (
    NotSupportedError,
    Workspace,
    WorkspaceError,
    workspace,
)
from nontainer.providers import KvgitProvider


@pytest.fixture
def kv_ws():
    """Memory-backed kvgit workspace (autocheckpoint on by default)."""
    provider = KvgitProvider.open(None, session="stage-session")
    ws = Workspace(provider)
    yield ws
    ws.close()


def _provider(ws):
    return ws._provider


def _history(ws):
    return list(ws.history())


def test_stage_status_split(kv_ws):
    kv_ws.fs.write("/workspace/a.txt", b"one")
    kv_ws.fs.write("/workspace/b.txt", b"two")

    out = _provider(kv_ws).stage(["/workspace/a.txt"])
    assert out.staged == ("/workspace/a.txt",)
    # First stage op suspends autocheckpoint, stated aloud.
    assert out.suspended is True

    st = _provider(kv_ws).status()
    assert st.branch == "stage-session"
    assert st.staged == ("/workspace/a.txt",)
    assert st.unstaged == ("/workspace/b.txt",)
    assert st.merge_source is None
    assert st.merge_unresolved == ()


def test_second_stage_does_not_reannounce(kv_ws):
    kv_ws.fs.write("/workspace/a.txt", b"one")
    kv_ws.fs.write("/workspace/b.txt", b"two")

    first = _provider(kv_ws).stage(["/workspace/a.txt"])
    second = _provider(kv_ws).stage(["/workspace/b.txt"])
    assert first.suspended is True
    assert second.suspended is False
    assert second.staged == ("/workspace/b.txt",)
    # Restaging is a no-op report, not a duplicate.
    assert _provider(kv_ws).stage(["/workspace/a.txt"]).staged == ()


def test_stage_unknown_and_directories_refused(kv_ws):
    kv_ws.fs.write("/workspace/a.txt", b"one")
    kv_ws.checkpoint()

    with pytest.raises(ValueError, match="unknown path"):
        _provider(kv_ws).stage(["/workspace/nope.txt"])
    kv_ws.terminal("mkdir sub")
    with pytest.raises(ValueError, match="not directories"):
        _provider(kv_ws).stage(["/workspace/sub"])
    # Valid-but-unstaged paths are silently ignored by unstage.
    assert _provider(kv_ws).unstage(["/workspace/a.txt"]) == ()
    with pytest.raises(ValueError, match="unknown path"):
        _provider(kv_ws).unstage(["/workspace/nope.txt"])


def test_selective_commit_leaves_unstaged(kv_ws):
    kv_ws.fs.write("/workspace/a.txt", b"one")
    kv_ws.fs.write("/workspace/b.txt", b"two")
    before = _provider(kv_ws).head
    _provider(kv_ws).stage(["/workspace/a.txt"])

    commit = _provider(kv_ws).commit()
    assert commit != before
    assert _provider(kv_ws).head == commit
    entries = _history(kv_ws)
    assert entries[0].id == commit
    assert entries[0].info.get("tool") == "ws-git.commit"

    # a.txt landed, b.txt is still dirty work in progress.
    st = _provider(kv_ws).status()
    assert st.staged == ()
    assert st.unstaged == ("/workspace/b.txt",)
    assert kv_ws.terminal("cat a.txt").stdout.strip() == "one"
    assert kv_ws.terminal("cat b.txt").stdout.strip() == "two"
    # The commit's table describes its own blobs: a's row landed, b's
    # uncommitted row did not ride along.
    import json

    from monkeyfs import VirtualFS

    committed_table = json.loads(
        _provider(kv_ws)._staged.checkout(commit).get(VirtualFS.METADATA_KEY)
    )
    assert committed_table["workspace/a.txt"]["size"] == 3
    assert "workspace/b.txt" not in committed_table
    # Suspension cleared by the landing commit: autocheckpoint works.
    kv_ws.terminal("echo three > c.txt")
    assert _provider(kv_ws).head != commit


def test_commit_nothing_staged_refused(kv_ws):
    kv_ws.terminal("echo one > a.txt")
    with pytest.raises(WorkspaceError, match="nothing staged"):
        _provider(kv_ws).commit()
    # Staging an unmodified file stages nothing committable either.
    _provider(kv_ws).stage(["/workspace/a.txt"])
    with pytest.raises(WorkspaceError, match="nothing staged"):
        _provider(kv_ws).commit()


def test_composition_mints_zero_commits(kv_ws):
    kv_ws.fs.write("/workspace/a.txt", b"one")
    kv_ws.fs.write("/workspace/b.txt", b"two")
    n0 = len(_history(kv_ws))

    _provider(kv_ws).stage(["/workspace/a.txt"])
    kv_ws.terminal("echo three > c.txt")
    kv_ws.run_python("open('/workspace/d.txt', 'w').write('four')")
    assert len(_history(kv_ws)) == n0

    _provider(kv_ws).commit()
    assert len(_history(kv_ws)) == n0 + 1


def test_unstage_last_resumes(kv_ws):
    kv_ws.fs.write("/workspace/a.txt", b"one")
    _provider(kv_ws).stage(["/workspace/a.txt"])

    assert _provider(kv_ws).unstage(["/workspace/a.txt"]) == ("/workspace/a.txt",)
    assert _provider(kv_ws).status().staged == ()
    # Composition abandoned: the next write autocheckpoints again.
    kv_ws.terminal("echo two > b.txt")
    assert _provider(kv_ws).status().unstaged == ()


def test_discard_staged_abandons_index_not_tree(kv_ws):
    kv_ws.fs.write("/workspace/a.txt", b"one")
    kv_ws.fs.write("/workspace/b.txt", b"two")
    _provider(kv_ws).stage(["/workspace/a.txt", "/workspace/b.txt"])

    _provider(kv_ws).discard_staged()
    st = _provider(kv_ws).status()
    assert st.staged == ()
    assert st.unstaged == ("/workspace/a.txt", "/workspace/b.txt")
    assert kv_ws.terminal("cat a.txt").stdout.strip() == "one"
    # Suspended bit went with the index: autocheckpoint resumes and
    # commits the whole dirty tree (checkpoint commits everything).
    kv_ws.terminal("echo three > c.txt")
    assert _provider(kv_ws).status().unstaged == ()


def test_stage_deleted_file_commits_deletion(kv_ws):
    kv_ws.terminal("echo one > a.txt")
    # Uncommitted deletion: terminal would autocheckpoint the rm.
    kv_ws.fs.remove("/workspace/a.txt")

    _provider(kv_ws).stage(["/workspace/a.txt"])
    assert _provider(kv_ws).status().staged == ("/workspace/a.txt",)
    _provider(kv_ws).commit()
    assert "a.txt" not in kv_ws.fs.list("/workspace")
    assert _provider(kv_ws).status().unstaged == ()


def test_fork_copies_index_then_diverges(kv_ws):
    kv_ws.fs.write("/workspace/a.txt", b"one")
    kv_ws.checkpoint()
    kv_ws.fs.write("/workspace/a.txt", b"one-main")
    _provider(kv_ws).stage(["/workspace/a.txt"])

    fork = kv_ws.fork("worker")
    try:
        # Forking checkpoints, so the fork inherits the staged set —
        # visible once the fork dirties the file again.
        fork.fs.write("/workspace/a.txt", b"one-fork")
        assert fork.status().staged == ("/workspace/a.txt",)
        # The parent landing its commit is invisible to the fork:
        # separate blob copies after the branch point.
        _provider(kv_ws).commit()
        assert _provider(kv_ws).status().staged == ()
        assert fork.status().staged == ("/workspace/a.txt",)
    finally:
        fork.close()


def test_frozen_verbs_refused_status_open(kv_ws):
    kv_ws.terminal("echo one > a.txt")
    kv_ws.tag("v1")
    snap = kv_ws.at_tag("v1")
    try:
        with pytest.raises(NotSupportedError, match="frozen"):
            snap.stage(["/workspace/a.txt"])
        with pytest.raises(NotSupportedError, match="frozen"):
            snap.unstage(["/workspace/a.txt"])
        with pytest.raises(NotSupportedError, match="frozen"):
            snap.commit()
        with pytest.raises(NotSupportedError, match="frozen"):
            snap.discard_staged()
        # Reads are the point of snapshots: status stays open.
        assert snap.status().unstaged == ()
    finally:
        snap.close()


def test_dir_provider_staged_unsupported(tmp_path):
    from nontainer.providers.dir import DirProvider

    p = DirProvider(tmp_path / "ws", session="dir")
    with pytest.raises(NotSupportedError, match="stage"):
        p.stage([])
    with pytest.raises(NotSupportedError, match="unstage"):
        p.unstage([])
    with pytest.raises(NotSupportedError, match="commit"):
        p.commit()
    with pytest.raises(NotSupportedError, match="discard_staged"):
        p.discard_staged()
    with pytest.raises(NotSupportedError, match="status"):
        p.status()
    assert p.stage_suspended() is False
    p.close()


def test_concurrent_stage_no_lost_update(kv_ws):
    kv_ws.fs.write("/workspace/a.txt", b"a")
    kv_ws.fs.write("/workspace/b.txt", b"b")
    kv_ws.checkpoint()
    kv_ws.fs.write("/workspace/a.txt", b"a2")
    kv_ws.fs.write("/workspace/b.txt", b"b2")

    errors: list = []

    def stage_one(path):
        try:
            kv_ws.stage([path])
        except Exception as e:  # noqa: BLE001 - collected, asserted below
            errors.append(e)

    threads = [
        threading.Thread(target=stage_one, args=(path,))
        for path in ("/workspace/a.txt", "/workspace/b.txt")
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert kv_ws.status().staged == ("/workspace/a.txt", "/workspace/b.txt")


def test_merge_unions_staging_blobs(kv_ws):
    import json

    from nontainer.providers.kvgit import _WS_BLOB_KEY, _merge_ws_blob

    # Unit shape: union on both-changed, quiet side wins otherwise.
    def blob(index, suspended=False):
        return json.dumps(
            {"version": 1, "index": sorted(index), "suspended": suspended}
        )

    assert json.loads(_merge_ws_blob(None, blob(["a"]), blob(["b"])))["index"] == [
        "a",
        "b",
    ]
    assert json.loads(_merge_ws_blob(blob(["a"]), blob(["a"]), blob(["a", "b"])))[
        "index"
    ] == [
        "a",
        "b",
    ]

    # Registration: compositions in flight on both sides must not abort
    # the merge. Live verbs always clear on commit, so the contested
    # blobs are hand-staged (white-box, but through the real merge).
    kv_ws.fs.write("/workspace/a.txt", b"base")
    kv_ws.checkpoint()
    fork = kv_ws.fork("worker")
    try:
        p, fp = _provider(kv_ws), fork._provider
        p._write_blob({"k1"}, True)
        p.checkpoint()
        fp._write_blob({"k2"}, False)
        fork.checkpoint()

        out = p.merge("worker")
        assert out.merged
        merged_blob = json.loads(p._staged.get(_WS_BLOB_KEY))
        assert merged_blob["index"] == ["k1", "k2"]
        assert merged_blob["suspended"] is True
    finally:
        fork.close()


def test_selective_commit_preserves_live_table(kv_ws):
    import json

    from monkeyfs import VirtualFS

    p = _provider(kv_ws)
    kv_ws.fs.write("/workspace/a.txt", b"one")
    kv_ws.fs.write("/workspace/b.txt", b"two")
    p.stage(["/workspace/a.txt"])
    p.commit()

    # Live reads still see the uncommitted file: its row survives as
    # unstaged state, not as the fixed-up commit table.
    live = json.loads(p._staged.get(VirtualFS.METADATA_KEY))
    assert live["workspace/b.txt"]["size"] == 3
    # And the next checkpoint persists that truth, not HEAD staleness.
    n0 = len(_history(kv_ws))
    kv_ws.terminal("echo three > c.txt")
    assert len(_history(kv_ws)) == n0 + 1
    committed = json.loads(p._staged.checkout(p.head).get(VirtualFS.METADATA_KEY))
    assert committed["workspace/b.txt"]["size"] == 3


def test_abandoned_clean_index_leaves_no_dirt(kv_ws):
    kv_ws.terminal("echo one > a.txt")
    p = _provider(kv_ws)
    p.stage(["/workspace/a.txt"])  # clean file: index moves, tree doesn't
    assert p.dirty
    p.discard_staged()
    assert not p.dirty
    # A read-only op mints nothing.
    n0 = len(_history(kv_ws))
    kv_ws.terminal("ls a.txt")
    assert len(_history(kv_ws)) == n0


def test_merge_after_selective_commit(kv_ws):
    # Selective commit must leave the tree mergeable: framework keys
    # ride along with the table fixed up, so no metadata staleness
    # blocks the merge.
    kv_ws.fs.write("/workspace/a.txt", b"base")
    kv_ws.checkpoint()
    fork = kv_ws.fork("worker")
    try:
        kv_ws.fs.write("/workspace/c.txt", b"main")
        kv_ws.stage(["/workspace/c.txt"])
        kv_ws.commit()
        fork.terminal("echo worker > b.txt")
        out = _provider(kv_ws).merge("worker")
        assert out.merged
        assert out.auto_merged == ("/workspace/b.txt",)
    finally:
        fork.close()


def test_status_reports_merge_context(kv_ws):
    kv_ws.fs.write("/workspace/doc.txt", b"a\nb\n")
    kv_ws.checkpoint()
    fork = kv_ws.fork("worker")
    try:
        fork.fs.write("/workspace/doc.txt", b"a\nFORK\n")
        fork.checkpoint()
        kv_ws.fs.write("/workspace/doc.txt", b"a\nMAIN\n")
        kv_ws.checkpoint()

        out = _provider(kv_ws).merge("worker")
        assert out.conflicts == ("/workspace/doc.txt",)
        st = _provider(kv_ws).status()
        assert st.merge_source == "worker"
        assert st.merge_unresolved == ("/workspace/doc.txt",)

        # Resolve like an agent would: edit, stage, selective-commit.
        kv_ws.fs.write("/workspace/doc.txt", b"a\nBOTH\n")
        kv_ws.stage(["/workspace/doc.txt"])
        kv_ws.commit()
        st = _provider(kv_ws).status()
        assert st.merge_source is None
        assert st.merge_unresolved == ()
        assert st.unstaged == ()
    finally:
        fork.close()


def test_workspace_wrappers_smoke(tmp_path):
    ws = workspace("stage-smoke", store=str(tmp_path / "store"))
    try:
        ws.terminal("echo one > a.txt")
        out = ws.stage(["/workspace/a.txt"])
        # a.txt already autocheckpointed, but staging records the key
        # regardless (status intersects with modified).
        assert out.staged == ("/workspace/a.txt",)
        assert out.suspended is True
        assert ws.status().unstaged == ()
        ws.discard_staged()
        assert ws.status().unstaged == ()
    finally:
        ws.close()
