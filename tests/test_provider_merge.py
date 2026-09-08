"""KvgitProvider.merge: cross-branch merge at the provider layer."""

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
    """Memory-backed kvgit workspace (autocommit on by default)."""
    provider = KvgitProvider.open(None, session="merge-session")
    ws = Workspace(provider)
    yield ws
    ws.close()


def _provider(ws):
    return ws._provider


def test_fast_forward(kv_ws):
    kv_ws.terminal("echo one > a.txt")
    fork = kv_ws.fork("worker")
    try:
        fork.terminal("echo two > b.txt")

        out = _provider(kv_ws).merge("worker")
        assert out.merged
        assert out.commit == _provider(kv_ws).head
        assert out.conflicts == ()
        # Only what the merge brought in — a.txt was already here.
        assert out.auto_merged == ("/workspace/b.txt",)
        assert kv_ws.terminal("cat b.txt").stdout.strip() == "two"
    finally:
        fork.close()


def test_disjoint_union(kv_ws):
    kv_ws.terminal("echo one > a.txt")
    fork = kv_ws.fork("worker")
    try:
        fork.terminal("echo two > b.txt")
        kv_ws.terminal("echo three > c.txt")

        out = _provider(kv_ws).merge("worker")
        assert out.merged
        assert kv_ws.terminal("cat b.txt").stdout.strip() == "two"
        assert kv_ws.terminal("cat c.txt").stdout.strip() == "three"
    finally:
        fork.close()


def test_overlap_conflicts_with_markers(kv_ws):
    # Exact bytes via fs.write (termish printf does not interpret \n);
    # commit() commits each side since these bypass the terminal.
    kv_ws.files.fs.write("/workspace/doc.txt", b"a\nb\n")
    kv_ws.commit()
    fork = kv_ws.fork("worker")
    try:
        fork.files.fs.write("/workspace/doc.txt", b"a\nFORK\n")
        fork.commit()
        kv_ws.files.fs.write("/workspace/doc.txt", b"a\nMAIN\n")
        kv_ws.commit()

        before = _provider(kv_ws).head
        out = _provider(kv_ws).merge("worker")
        # Markers commit WITH the merge (flagged, never blocking):
        # resolve with ordinary edits and commit.
        assert out.merged
        assert out.commit == _provider(kv_ws).head
        assert out.commit != before
        assert out.conflicts == ("/workspace/doc.txt",)
        assert out.auto_merged == ()
        body = kv_ws.terminal("cat doc.txt").stdout
        assert "<<<<<<< " in body
    finally:
        fork.close()


def test_marker_resolution_roundtrip(kv_ws):
    kv_ws.files.fs.write("/workspace/doc.txt", b"a\nb\n")
    kv_ws.commit()
    fork = kv_ws.fork("worker")
    try:
        fork.files.fs.write("/workspace/doc.txt", b"a\nFORK\n")
        fork.commit()
        kv_ws.files.fs.write("/workspace/doc.txt", b"a\nMAIN\n")
        kv_ws.commit()

        out = _provider(kv_ws).merge("worker")
        assert out.conflicts == ("/workspace/doc.txt",)
        # Resolve like an agent would: edit, commit, verify clean.
        kv_ws.files.fs.write("/workspace/doc.txt", b"a\nBOTH\n")
        resolved = kv_ws.commit()
        assert resolved != out.commit
        assert "<<<<<<< " not in kv_ws.terminal("cat doc.txt").stdout
    finally:
        fork.close()


def test_unknown_branch(kv_ws):
    with pytest.raises(ValueError, match="unknown branch"):
        _provider(kv_ws).merge("nope")


def test_self_merge_refused(kv_ws):
    with pytest.raises(ValueError, match="into itself"):
        _provider(kv_ws).merge("merge-session")


def test_dirty_target_refused(kv_ws):
    # Write AFTER forking: fork commits pending work, so dirt staged
    # before it would already be committed by merge time.
    fork = kv_ws.fork("worker")
    try:
        kv_ws.files.fs.write("/workspace/staged.txt", b"pending")  # uncommitted
        with pytest.raises(WorkspaceError, match="commit or discard"):
            _provider(kv_ws).merge("worker")
    finally:
        kv_ws.discard()
        fork.close()


def test_frozen_refused(kv_ws):
    kv_ws.terminal("echo one > a.txt")
    kv_ws.tags.add("v1")
    snap = kv_ws.tags.at("v1")
    try:
        with pytest.raises(NotSupportedError, match="frozen"):
            snap._provider.merge("worker")
    finally:
        snap.close()


def test_non_file_contested_is_hard_conflict(kv_ws):
    # Both sides must differ from the base to contest: main writes after
    # forking, so LCA holds neither value.
    kv_ws.run_python("cache['k'] = 'base'")
    fork = kv_ws.fork("worker")
    try:
        kv_ws.run_python("cache['k'] = 'main'")
        fork.run_python("cache['k'] = 'worker'")

        before = _provider(kv_ws).head
        out = _provider(kv_ws).merge("worker")
        assert not out.merged
        assert out.commit is None
        assert _provider(kv_ws).head == before
        # Raw store key: the cache is not a file and has no display path.
        assert any("cache" in c for c in out.conflicts)
    finally:
        fork.close()


def test_merge_commit_tagged(kv_ws):
    kv_ws.terminal("echo one > a.txt")
    fork = kv_ws.fork("worker")
    try:
        fork.terminal("echo two > b.txt")
        out = _provider(kv_ws).merge("worker")
        entries = list(kv_ws.log())
        assert entries[0].id == out.commit
        assert entries[0].info.get("tool") == "ws-git.merge"
        assert entries[0].info.get("source") == "worker"
    finally:
        fork.close()


def test_dir_provider_merge_unsupported(tmp_path):
    from nontainer.providers.dir import DirProvider

    p = DirProvider(tmp_path / "ws", session="dir")
    with pytest.raises(NotSupportedError, match="merge"):
        p.merge("anything")
    p.close()


def test_workspace_factory_smoke(tmp_path):
    ws = workspace("merge-smoke", store=str(tmp_path / "store"))
    try:
        ws.terminal("echo one > a.txt")
        fork = ws.fork("worker")
        try:
            fork.terminal("echo two > b.txt")
            out = ws._provider.merge("worker")
            assert out.merged
        finally:
            fork.close()
    finally:
        ws.close()


def test_fs_caches_invalidated(kv_ws):
    kv_ws.terminal("echo one > a.txt")
    assert "a.txt" in kv_ws.files.fs.list("/workspace")  # warms the FS caches
    fork = kv_ws.fork("worker")
    try:
        fork.terminal("echo two > b.txt")
        out = _provider(kv_ws).merge("worker")
        assert out.merged
        # Without invalidation these read the pre-merge tree.
        assert "b.txt" in kv_ws.files.fs.list("/workspace")
        assert kv_ws.files.fs.stat("/workspace/b.txt").size == 4
    finally:
        fork.close()


def test_merged_sizes_recomputed(kv_ws):
    # Disjoint edits merge cleanly, but the bytes match neither branch —
    # so a size copied from one branch would be wrong.
    kv_ws.files.fs.write("/workspace/doc.txt", b"a\nb\n")
    kv_ws.commit()
    fork = kv_ws.fork("worker")
    try:
        fork.files.fs.write("/workspace/doc.txt", b"a\nb\nfork\n")
        fork.commit()
        kv_ws.files.fs.write("/workspace/doc.txt", b"main\na\nb\n")
        kv_ws.commit()

        out = _provider(kv_ws).merge("worker")
        assert out.merged
        assert out.conflicts == ()
        body = kv_ws.terminal("cat doc.txt").stdout.encode()
        assert kv_ws.files.fs.stat("/workspace/doc.txt").size == len(body)
    finally:
        fork.close()


def test_file_vs_dir_is_hard_conflict(kv_ws):
    from monkeyfs import VirtualFS

    kv_ws.terminal("echo file > x")
    fork = kv_ws.fork("worker")
    try:
        # Both sides touch the same table row (single-row keys since
        # monkeyfs 0.1.8); the fork then swaps the file for a dir.
        kv_ws.terminal("echo more >> x")
        fork.terminal("rm x && mkdir x && echo hi > x/inner.txt")

        before = _provider(kv_ws).head
        out = _provider(kv_ws).merge("worker")
        assert not out.merged
        assert out.commit is None
        assert _provider(kv_ws).head == before
        assert out.conflicts == (VirtualFS.METADATA_KEY,)
    finally:
        fork.close()


def test_preexisting_markers_not_flagged(kv_ws):
    kv_ws.terminal("echo one > a.txt")
    fork = kv_ws.fork("worker")
    try:
        fork.files.fs.write("/workspace/notes.txt", b"see <<<<<<< HEAD for details\n")
        fork.commit()

        out = _provider(kv_ws).merge("worker")
        assert out.merged
        assert out.conflicts == ()
        assert "/workspace/notes.txt" in out.auto_merged
    finally:
        fork.close()


# -- the facade ----------------------------------------------------------------


def test_workspace_merge_brings_a_forks_work_home(kv_ws):
    """``ws.merge`` is the provider verb under the workspace lock: the
    fork's disjoint file arrives, and the outcome names it."""
    kv_ws.terminal("echo base > a.txt")
    fork = kv_ws.fork("worker")
    try:
        fork.terminal("echo worker > b.txt")

        out = kv_ws.merge("worker")
        assert out.merged
        assert out.conflicts == ()
        assert out.auto_merged == ("/workspace/b.txt",)
        # The outcome names the merge commit; the head is the
        # bookkeeping commit that records it as the agent's, and that
        # record is what leaves a clean workspace behind the merge.
        assert not kv_ws.uncommitted
        assert out.commit == kv_ws.index.head
        assert kv_ws.head != out.commit
        assert kv_ws.terminal("cat b.txt").stdout.strip() == "worker"
    finally:
        fork.close()


def test_workspace_merge_refuses_a_dirty_target(kv_ws):
    """A merge lands against a clean tree, and the refusal says which
    two verbs get there."""
    kv_ws.terminal("echo base > a.txt")
    fork = kv_ws.fork("worker")
    try:
        fork.terminal("echo worker > b.txt")
        kv_ws.files.fs.write("/workspace/pending.txt", b"uncommitted")
        assert kv_ws.uncommitted

        with pytest.raises(WorkspaceError, match="ws.commit"):
            kv_ws.merge("worker")
        assert kv_ws.uncommitted  # nothing was taken from under the caller
        assert not kv_ws.files.fs.exists("/workspace/b.txt")

        kv_ws.commit()
        assert kv_ws.merge("worker").merged
    finally:
        fork.close()


def test_workspace_merge_needs_the_capability(tmp_path):
    """Providers that cannot merge say so from the facade too."""
    with workspace("s1", store=tmp_path, backend="dir") as ws:
        assert ws.caps.merge is False
        with pytest.raises(NotSupportedError, match="merge"):
            ws.merge("other")
