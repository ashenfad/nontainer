"""KvgitProvider.apply: one commit's change, three-way against a base.

Revert and cherry-pick are this one primitive with ``base`` and
``theirs`` swapped, so what is pinned here is the primitive: what a
change does to a tree that has moved on since, what it leaves alone,
and what it says when there is nothing to do.
"""

import pytest

from nontainer import NotSupportedError, Workspace, WorkspaceError
from nontainer.planes import CACHE_PREFIX, CONVERSATION_PREFIX
from nontainer.providers import KvgitProvider


@pytest.fixture
def kv_ws():
    """Memory-backed kvgit workspace (autocommit on by default)."""
    provider = KvgitProvider.open(None, session="apply-session")
    ws = Workspace(provider)
    yield ws
    ws.close()


def _provider(ws):
    return ws._provider


def test_a_disjoint_change_applies_clean(kv_ws):
    """The change edits one file, this tree edited another: both stand."""
    p = _provider(kv_ws)
    kv_ws.files.write("/workspace/a.txt", "a one\n")
    kv_ws.files.write("/workspace/b.txt", "b one\n")
    base = p.head
    kv_ws.files.write("/workspace/a.txt", "a two\n")
    theirs = p.head
    kv_ws.files.write("/workspace/b.txt", "b two\n")
    kv_ws.files.write("/workspace/a.txt", "a one\n")  # step a.txt back
    before = p.head

    out = p.apply(base, theirs, info={"tool": "test"})

    assert out.merged and out.conflicts == ()
    assert out.auto_merged == ("/workspace/a.txt",)
    assert kv_ws.files.read("/workspace/a.txt") == b"a two\n"
    assert kv_ws.files.read("/workspace/b.txt") == b"b two\n"
    entry = next(e for e in kv_ws.log() if e.id == out.commit)
    assert entry.parents == (before,)
    assert entry.info["applied_from"] == theirs
    assert entry.info["applied_base"] == base


def test_an_overlapping_edit_comes_back_as_markers(kv_ws):
    p = _provider(kv_ws)
    kv_ws.files.write("/workspace/doc.txt", "one\ntwo\nthree\n")
    base = p.head
    kv_ws.files.write("/workspace/doc.txt", "one\nTHEIRS\nthree\n")
    theirs = p.head
    kv_ws.files.write("/workspace/doc.txt", "one\nOURS\nthree\n")

    out = p.apply(base, theirs)

    assert out.merged
    assert out.conflicts == ("/workspace/doc.txt",)
    assert out.auto_merged == ()
    body = kv_ws.files.read("/workspace/doc.txt")
    assert b"<<<<<<< " in body and b"OURS" in body and b"THEIRS" in body


def test_a_change_already_present_is_a_no_op(kv_ws):
    p = _provider(kv_ws)
    kv_ws.files.write("/workspace/a.txt", "one\n")
    base = p.head
    kv_ws.files.write("/workspace/a.txt", "two\n")
    theirs = p.head
    before = p.head

    out = p.apply(base, theirs)

    assert not out.merged
    assert out.commit is None
    assert out.conflicts == () and out.auto_merged == ()
    assert p.head == before


def test_two_commits_with_nothing_between_them_change_nothing(kv_ws):
    p = _provider(kv_ws)
    kv_ws.files.write("/workspace/a.txt", "one\n")
    base = p.head
    out = p.apply(base, base)
    assert not out.merged and out.commit is None and out.conflicts == ()


def test_a_deletion_in_the_change_deletes(kv_ws):
    p = _provider(kv_ws)
    kv_ws.files.write("/workspace/a.txt", "one\n")
    kv_ws.files.write("/workspace/gone.txt", "gone\n")
    base = p.head
    kv_ws.files.fs.remove("/workspace/gone.txt")
    theirs = kv_ws.commit()
    kv_ws.files.write("/workspace/gone.txt", "gone\n")  # ours holds it again
    kv_ws.files.write("/workspace/new.txt", "new\n")

    out = p.apply(base, theirs)

    assert out.merged and out.conflicts == ()
    assert out.auto_merged == ("/workspace/gone.txt",)
    assert not kv_ws.files.exists("/workspace/gone.txt")
    assert kv_ws.files.read("/workspace/new.txt") == b"new\n"


def test_a_file_the_change_created_is_created(kv_ws):
    p = _provider(kv_ws)
    kv_ws.files.write("/workspace/a.txt", "one\n")
    base = p.head
    kv_ws.files.write("/workspace/made.txt", "made\n")
    theirs = p.head
    kv_ws.files.fs.remove("/workspace/made.txt")
    kv_ws.commit()

    out = p.apply(base, theirs)

    assert out.merged and out.conflicts == ()
    assert kv_ws.files.read("/workspace/made.txt") == b"made\n"
    assert kv_ws.files.fs.stat("/workspace/made.txt").size == 5


def test_the_other_planes_are_untouched(kv_ws):
    """Files three-way; the cache and the stored conversation take
    ours, whole — a key the change added under them does not travel."""
    p = _provider(kv_ws)
    kv_ws.files.write("/workspace/a.txt", "base\n")
    kv_ws.run_python("cache['k'] = 'base'")
    fork = kv_ws.fork("worker")
    try:
        base = p.head
        fork.run_python("cache['k'] = 'theirs'; cache['only'] = 'theirs'")
        fork._provider.kv[f"{CONVERSATION_PREFIX}runs/1"] = b"theirs"
        fork.files.write("/workspace/a.txt", "theirs\n")
        theirs = fork.commit()
        kv_ws.run_python("cache['k'] = 'ours'")

        out = p.apply(base, theirs)

        assert out.merged and out.conflicts == ()
        assert kv_ws.cache["k"] == "ours"
        assert "only" not in kv_ws.cache
        assert f"{CONVERSATION_PREFIX}runs/1" not in p.kv
        assert kv_ws.files.read("/workspace/a.txt") == b"theirs\n"
        assert [key for key in p.kv if key.startswith(CACHE_PREFIX)]
    finally:
        fork.close()


def test_the_empty_tree_is_a_side(kv_ws):
    """``None`` names the tree before anything: a change measured from
    it adds every key it holds, and applied toward it takes them away."""
    p = _provider(kv_ws)
    kv_ws.files.write("/workspace/a.txt", "one\n")
    first = p.head
    kv_ws.files.fs.remove("/workspace/a.txt")
    kv_ws.commit()

    out = p.apply(None, first)
    assert out.merged
    assert kv_ws.files.read("/workspace/a.txt") == b"one\n"

    out = p.apply(first, None)
    assert out.merged
    assert not kv_ws.files.exists("/workspace/a.txt")


def test_the_empty_tree_is_a_side_for_a_file_changed_since(kv_ws):
    """A side that is the empty tree is the empty tree everywhere the
    three-way reads, the metadata row's sizing included."""
    p = _provider(kv_ws)
    kv_ws.files.write("/workspace/a.txt", "one\ntwo\nthree\n")
    first = p.head
    kv_ws.files.write("/workspace/a.txt", "one\nEDITED\nthree\n")

    out = p.apply(first, None)

    assert out.merged
    assert out.conflicts == ("/workspace/a.txt",)
    assert b"<<<<<<< " in kv_ws.files.read("/workspace/a.txt")
    # the file is still a file, described by a row the merge wrote
    assert not kv_ws.files.fs.stat("/workspace/a.txt").is_dir


def test_apply_refuses_uncommitted_work(kv_ws):
    p = _provider(kv_ws)
    kv_ws.files.write("/workspace/a.txt", "one\n")
    base = p.head
    kv_ws.files.write("/workspace/a.txt", "two\n")
    theirs = p.head
    kv_ws.autocommit = False
    kv_ws.files.write("/workspace/a.txt", "three\n")
    assert p.dirty
    with pytest.raises(WorkspaceError, match="uncommitted"):
        p.apply(base, theirs)


def test_apply_refuses_a_commit_that_is_not_there(kv_ws):
    from nontainer import CommitNotFoundError

    p = _provider(kv_ws)
    kv_ws.files.write("/workspace/a.txt", "one\n")
    with pytest.raises(CommitNotFoundError):
        p.apply(p.head, "0" * 40)


def test_commit_at_reads_any_commit_in_the_store(kv_ws):
    p = _provider(kv_ws)
    kv_ws.files.write("/workspace/a.txt", "one\n")
    here = p.head
    fork = kv_ws.fork("worker")
    try:
        marked = fork._provider.head  # the fork mark the child starts at
        fork.files.write("/workspace/b.txt", "two\n")
        there = fork._provider.head
        entry = p.commit_at(there)
        assert entry is not None
        assert entry.id == there
        assert entry.parents == (marked,)
        assert p.commit_at(marked).parents == (here,)
        assert p.commit_at("0" * 40) is None
    finally:
        fork.close()


def test_dir_provider_apply_unsupported(tmp_path):
    from nontainer.providers.dir import DirProvider

    provider = DirProvider(tmp_path / "ws", session="dir")
    with pytest.raises(NotSupportedError, match="apply"):
        provider.apply("a", "b")
    provider.close()
