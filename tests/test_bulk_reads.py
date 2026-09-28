"""Bulk reads: what changed, and the values of many files, cost the change.

The agent's git, the conversation store and the tree push each read
many values at once. Over a networked store every separate read is a
round trip, so these pin both the answers and the number of calls the
store sees — a count that must not grow with the size of the tree.
"""

import pytest
from kvgit.kv.memory import Memory
from monkeyfs import ReadOnlyFS, VirtualFS

from nontainer import CommitNotFoundError, Store
from nontainer.providers import KvgitProvider
from nontainer.views import ViewFS, read_many
from nontainer.wsgit import register_wsgit


class CountingMemory(Memory):
    """An in-memory backend that counts the reads it answers."""

    def __init__(self):
        super().__init__()
        self.calls = 0

    def get(self, key):
        self.calls += 1
        return super().get(key)

    def get_many(self, *args):
        self.calls += 1
        return super().get_many(*args)


def _provider():
    return KvgitProvider.open(None, session="bulk")


def _write(provider, files):
    for path, data in files.items():
        provider.fs.write(path, data)


# -- working_diff ------------------------------------------------------------


def test_working_diff_names_what_the_tree_changed_since_a_commit():
    p = _provider()
    _write(p, {"/w/keep.txt": b"k", "/w/edit.txt": b"old", "/w/drop.txt": b"d"})
    base = p.commit()

    p.fs.write("/w/edit.txt", b"new")
    p.fs.write("/w/keep.txt", b"k")  # the same bytes again: not a change
    p.fs.remove("/w/drop.txt")
    p.fs.write("/w/add.txt", b"a")

    diff = p.working_diff(base)
    assert diff.added == {"/w/add.txt"}
    assert diff.removed == {"/w/drop.txt"}
    assert diff.modified == {"/w/edit.txt"}
    p.close()


def test_working_diff_spans_commits_made_since():
    p = _provider()
    _write(p, {"/w/a.txt": b"1", "/w/b.txt": b"1"})
    base = p.commit()
    p.fs.write("/w/a.txt", b"2")
    p.commit()  # a framework commit between the base and the tree
    p.fs.write("/w/b.txt", b"2")

    assert p.working_diff(base).modified == {"/w/a.txt", "/w/b.txt"}
    assert p.working_diff(p.head).modified == {"/w/b.txt"}
    p.close()


def test_working_diff_forgets_a_change_undone_before_the_commit():
    p = _provider()
    _write(p, {"/w/a.txt": b"1"})
    base = p.commit()
    p.fs.write("/w/a.txt", b"2")
    p.commit()
    p.fs.write("/w/a.txt", b"1")  # back to what the base holds

    assert not p.working_diff(base).paths
    p.close()


def test_working_diff_on_a_frozen_view_reads_its_overlay():
    p = _provider()
    _write(p, {"/w/a.txt": b"1"})
    p.commit()
    p.tag("v1")
    frozen = p.at_tag("v1")
    frozen.fs.write("/w/a.txt", b"scratch")
    frozen.fs.write("/w/new.txt", b"n")

    diff = frozen.working_diff(frozen.head)
    assert diff.modified == {"/w/a.txt"} and diff.added == {"/w/new.txt"}
    frozen.close()
    p.close()


def test_working_diff_refuses_an_unknown_commit():
    p = _provider()
    p.fs.write("/w/a.txt", b"1")
    p.commit()
    with pytest.raises(CommitNotFoundError):
        p.working_diff("0" * 40)
    p.close()


# -- tree views read in batches -----------------------------------------------


def test_a_tree_view_reads_many_paths_in_one_call(tmp_path):
    kv = CountingMemory()
    st = Store(tmp_path, kv=kv)
    ws = st.open("bulk")
    for i in range(50):
        ws.files.fs.write(f"/workspace/f{i}.txt", f"{i}".encode())
    head = ws.commit()
    view = ws._provider.files_at(head)

    kv.calls = 0
    found = view.get_many(["/workspace/f1.txt", "/workspace/f2.txt", "/nope.txt"])

    assert found == {"/workspace/f1.txt": b"1", "/workspace/f2.txt": b"2"}
    # Neither the tree listing nor a read per path: the few keyset
    # nodes on the way to two entries, then one read for the blobs.
    assert kv.calls <= 6
    ws.close()
    st.close()


def _status_calls(tmp_path, n):
    kv = CountingMemory()
    st = Store(tmp_path, kv=kv)
    ws = st.open("bulk")
    register_wsgit(ws)
    for i in range(n):
        ws.files.fs.write(f"/workspace/f{i:04d}.txt", b"x" * 64)
    ws.terminal("ws-git commit -m base")
    ws.files.fs.write("/workspace/f0001.txt", b"changed")
    ws.commit()  # the agent's head is now behind the store's

    kv.calls = 0
    out = ws.terminal("ws-git status").stdout
    calls = kv.calls
    ws.close()
    st.close()
    return out, calls


def test_status_costs_the_change_not_the_tree(tmp_path):
    small_out, small = _status_calls(tmp_path / "small", 20)
    large_out, large = _status_calls(tmp_path / "large", 400)

    assert "f0001.txt" in small_out and "f0001.txt" in large_out
    # Twenty times the files, and a keyset a level deeper at most.
    assert large <= small + 4


# -- the conversation --------------------------------------------------------


def test_a_conversation_loads_its_runs_in_one_read(tmp_path):
    pytest.importorskip("agno")
    from nontainer.adapters.agno_db import RUN_PREFIX, SESSION_KEY, KvgitSessionDb

    kv = CountingMemory()
    st = Store(tmp_path, kv=kv)
    ws = st.open("chat")
    run_ids = [f"r{i}" for i in range(40)]
    store = ws._provider.kv
    for rid in run_ids:
        store[RUN_PREFIX + rid] = {"run_id": rid, "session_id": "chat"}
    store[SESSION_KEY] = {
        "session_id": "chat",
        "session_type": "agent",
        "run_ids": run_ids,
        "created_at": 1,
        "updated_at": 1,
    }
    ws.commit()
    db = KvgitSessionDb(ws, db_path=str(tmp_path / "agno"))

    kv.calls = 0
    session = db.get_session("chat", deserialize=False)

    assert [run["run_id"] for run in session["runs"]] == run_ids
    # The record and the runs, each through a few keyset nodes: not
    # forty reads, one per run.
    assert kv.calls <= 12
    ws.close()
    st.close()


# -- whole files through a filesystem ----------------------------------------


class _NoBatch:
    """A filesystem with ``read`` and nothing to batch with."""

    def __init__(self, files):
        self.files = files

    def read(self, path):
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]


def test_read_many_falls_back_to_one_read_per_path():
    fs = _NoBatch({"/a": b"a"})
    assert read_many(fs, ["/a", "/b"]) == {"/a": b"a"}


def test_read_many_through_a_read_only_wrapper():
    vfs = VirtualFS({})
    vfs.write("/a", b"a")
    assert read_many(ReadOnlyFS(vfs), ["/a", "/missing"]) == {"/a": b"a"}


def test_a_view_leaves_out_what_it_hides():
    vfs = VirtualFS({})
    vfs.write("/w/seen.txt", b"s")
    vfs.write("/w/hidden.txt", b"h")
    view = ViewFS(vfs, ["/w/seen.txt"])

    assert read_many(view, ["/w/seen.txt", "/w/hidden.txt"]) == {"/w/seen.txt": b"s"}
