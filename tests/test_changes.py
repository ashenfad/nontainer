"""store.changes: the change a land would apply, described read-only."""

import time

import pytest

from nontainer import (
    CacheChange,
    ChangesTooLarge,
    FileChange,
    Store,
    WorkspaceError,
)
from nontainer.store import Ref


@pytest.fixture
def store(tmp_path):
    st = Store(tmp_path)
    yield st
    st.close()


def test_cache_and_cwd_with_files_unchanged(store):
    """The issue's case: the files are the same, and ws.diff says so,
    while the cache and the cwd moved."""
    with store.open("demo", autocommit=False) as ws:
        ws.terminal("mkdir work")
        ws.commit()
        before = ws.ref
        ws.cache["count"] = 42
        ws.terminal("cd work")
        ws.commit()
        after = ws.ref

    changes = store.changes(before, after)
    assert changes.files == ()
    assert [(c.key, c.change) for c in changes.cache] == [("count", "set")]
    assert changes.cache[0].size > 0
    assert changes.cwd == ("/workspace", "/workspace/work")
    assert changes.bookkeeping == frozenset()
    assert changes.entries == 2
    assert changes and changes.base == before and changes.theirs == after


def test_a_key_set_to_none_is_not_a_deleted_key(store):
    with store.open("demo", autocommit=False) as ws:
        ws.cache["gone"] = 1
        ws.cache["kept"] = 1
        ws.commit()
        before = ws.ref
        del ws.cache["gone"]
        ws.cache["kept"] = None
        ws.commit()
        after = ws.ref
    assert store.changes(before, after).cache == (
        CacheChange("gone", "deleted", None),
        CacheChange("kept", "set", store.changes(before, after).cache[1].size),
    )
    assert store.changes(before, after).cache[1].size is not None


def test_files_directories_and_metadata(store):
    with store.open("demo", autocommit=False) as ws:
        ws.terminal("mkdir old")
        ws.files.write("keep.txt", "k" * 10)
        ws.files.write("touched.txt", "same")
        ws.files.write("gone.txt", "g")
        ws.commit()
        before = ws.ref
        time.sleep(1.1)  # so a touch changes the timestamp
        ws.files.write("keep.txt", "K" * 12)
        ws.files.write("new.txt", "hello")
        ws.files.remove("gone.txt")
        ws.terminal("touch touched.txt && mkdir empty && rm -r old")
        ws.commit()
        after = ws.ref

    assert store.changes(before, after).files == (
        FileChange("/workspace/empty", "added", is_dir=True),
        FileChange("/workspace/gone.txt", "removed", size=None, base_size=1),
        FileChange("/workspace/keep.txt", "modified", size=12, base_size=10),
        FileChange("/workspace/new.txt", "added", size=5, base_size=None),
        FileChange("/workspace/old", "removed", is_dir=True),
        FileChange("/workspace/touched.txt", "metadata", size=4, base_size=4),
    )


def test_a_path_that_changed_type_is_a_replacement(store):
    """A file that became a directory, and a directory that became a
    file: neither is only removed or only added, since the path is
    still there after a land."""
    with store.open("demo", autocommit=False) as ws:
        ws.files.write("was-file", "1234")
        ws.terminal("mkdir was-dir")
        ws.commit()
        before = ws.ref
        ws.files.remove("was-file")
        ws.terminal("mkdir was-file && rm -r was-dir")
        ws.files.write("was-dir", "123456")
        ws.commit()
        after = ws.ref

    assert store.changes(before, after).files == (
        FileChange("/workspace/was-dir", "replaced", size=6, base_size=None),
        FileChange("/workspace/was-file", "replaced", is_dir=True, base_size=4),
    )


def test_bookkeeping_is_named_and_not_a_change(store):
    """A fork changes nothing a land moves: its first commit is the
    agent's git state, and a narrowed one records its view."""
    with store.open("demo", autocommit=False) as ws:
        ws.files.write("a.txt", "a")
        ws.commit()
        base = ws.ref
    with store.fork("demo", "child", at=base.commit, paths=["a.txt"]) as child:
        theirs = child.ref

    changes = store.changes(base, theirs)
    assert changes.files == () and changes.cache == () and changes.cwd is None
    assert changes.bookkeeping == {"ws-git", "view"}
    assert not changes


def test_it_describes_what_land_applies(store):
    with store.open("demo", autocommit=False) as ws:
        ws.files.write("a.txt", "a")
        ws.commit()
        base = ws.ref
    with store.scratch(base, "work", autocommit=False) as work:
        work.files.write("a.txt", "changed")
        work.files.write("sub/b.txt", "b")
        work.cache["k"] = "v"
        work.commit()
        theirs = work.ref

    changes = store.changes(base, theirs)
    with store.open("demo", autocommit=False) as ws:
        landed = ws.land(base, theirs, expect_head=base.commit)
        diff = ws.diff(base.commit, landed)
        assert ws.cache["k"] == "v"
    described = {f.path for f in changes.files if not f.is_dir}
    assert described == set(diff.added) | set(diff.modified) | set(diff.removed)
    assert [c.key for c in changes.cache] == ["k"]


def test_bounds_refuse_before_reading(store, monkeypatch):
    with store.open("demo", autocommit=False) as ws:
        ws.files.write("big.bin", b"\0" * 50_000)
        ws.commit()
        before = ws.ref
        ws.files.write("small.txt", "s")
        ws.cache["k"] = 1
        ws.commit()
        after = ws.ref

    full = store.changes(before, after)
    assert full.entries == 2
    assert store.changes(before, after, max_entries=2, max_bytes=full.stored_bytes)

    from kvgit.repo import Snapshot

    def no_reads(self, *keys):
        raise AssertionError(f"read {keys}")

    monkeypatch.setattr(Snapshot, "get_many", no_reads)
    with pytest.raises(ChangesTooLarge) as e:
        store.changes(before, after, max_entries=1)
    assert (e.value.entries, e.value.stored_bytes) == (2, full.stored_bytes)
    with pytest.raises(ChangesTooLarge):
        store.changes(before, after, max_bytes=full.stored_bytes - 1)
    with pytest.raises(ValueError):
        store.changes(before, after, max_entries=-1)


def test_an_unchanged_large_file_is_never_read(store, monkeypatch):
    with store.open("demo", autocommit=False) as ws:
        ws.files.write("big.bin", b"\0" * 200_000)
        ws.commit()
        before = ws.ref
        ws.files.write("note.txt", "n")
        ws.commit()
        after = ws.ref

    from kvgit.repo import RawSnapshot, Snapshot
    from monkeyfs import VirtualFS

    from nontainer.providers.kvgit import state_kind

    (big_key,) = [
        k
        for k in store.repo.snapshot(commit=after.commit).keys()
        if state_kind(k) == "file" and VirtualFS._decode_path(k).endswith("big.bin")
    ]

    read: list[str] = []
    for cls in (Snapshot, RawSnapshot):
        for name in ("get", "get_many"):
            real = getattr(cls, name)

            def spy(self, *keys, _real=real, **kwargs):
                read.extend(k for k in keys if isinstance(k, str))
                return _real(self, *keys, **kwargs)

            monkeypatch.setattr(cls, name, spy)
    assert [f.path for f in store.changes(before, after).files] == [
        "/workspace/note.txt"
    ]
    assert big_key not in read
    assert [k for k in read if state_kind(k) == "file"] == []  # no file's bytes


def test_refs_are_validated_as_resolve_validates_them(store):
    with store.open("demo", autocommit=False) as ws:
        ws.files.write("a.txt", "a")
        ws.commit()
        base = ws.ref
        tagged = store.tags.add(ws, "kept")
        ws.files.write("a.txt", "b")
        ws.commit()
        theirs = ws.ref

    short = f"demo@{theirs.commit[:7]}"
    assert store.changes(base, short).theirs == theirs
    assert store.changes(Ref.at_tag("kept", tagged), theirs).files[0].change == (
        "modified"
    )
    with pytest.raises(WorkspaceError, match="No such session"):
        store.changes(Ref("nobody", base.commit), theirs)


def test_it_reads_and_writes_nothing(store):
    with store.open("demo", autocommit=False) as ws:
        ws.files.write("a.txt", "a")
        ws.commit()
        base = ws.ref
        ws.files.write("a.txt", "b")
        ws.commit()
        theirs = ws.ref
    branches = dict(store.repo.branches.items())
    store.changes(base, theirs)
    assert dict(store.repo.branches.items()) == branches


def test_a_store_without_versions_refuses(tmp_path):
    from nontainer.errors import NotSupportedError

    with Store(tmp_path, backend="agentfs") as st:
        with pytest.raises(NotSupportedError):
            st.changes("a@" + "1" * 40, "a@" + "2" * 40)
