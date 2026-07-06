"""Results pin the commit their call created (host-facing state ids)."""

import pytest

from nontainer import Workspace
from nontainer.providers import DirProvider, KvgitProvider


@pytest.fixture
def kv_ws():
    ws = Workspace(KvgitProvider.open(None, session="s1"))
    yield ws
    ws.close()


def test_mutating_calls_carry_their_commit(kv_ws):
    r1 = kv_ws.terminal("echo one > f.txt")
    r2 = kv_ws.run_python("cache['x'] = 1")
    w = kv_ws.write_file("g.txt", "hello")
    e = kv_ws.edit_file("g.txt", "hello", "world")

    ids = [r1.checkpoint, r2.checkpoint, w.checkpoint, e.checkpoint]
    assert all(ids)
    assert len(set(ids)) == 4  # one distinct commit per call
    # newest-first history matches the call order
    assert [c.id for c in kv_ws.history(limit=4)] == list(reversed(ids))


def test_readonly_calls_have_none(kv_ws):
    kv_ws.terminal("echo x > f.txt")
    assert kv_ws.terminal("cat f.txt").checkpoint is None
    assert kv_ws.run_python("v = 1 + 1").checkpoint is None
    # no-op edit (already applied) commits nothing
    kv_ws.write_file("a.py", "x = 2\n")
    out = kv_ws.edit_file("a.py", "x = 1", "x = 2")
    assert out.mode == "already_applied" and out.checkpoint is None


def test_restore_by_result_checkpoint(kv_ws):
    good = kv_ws.terminal("echo good > state.txt")
    kv_ws.terminal("echo bad > state.txt")
    kv_ws.restore(good.checkpoint)  # compensation by identity, not steps
    assert kv_ws.terminal("cat state.txt").stdout.strip() == "good"


def test_turn_mode_defers_ids_to_end_turn(kv_ws):
    pytest.importorskip("agno")
    from nontainer.adapters.agno import WorkspaceTools

    tk = WorkspaceTools(kv_ws, checkpoint="turn")
    tk.functions["file_write"].entrypoint(path="a.py", content="A = 1\n")
    r = kv_ws.terminal("echo hi > b.txt")
    assert r.checkpoint is None  # deferred: the turn commits, not the call

    turn_id = tk.end_turn()
    assert turn_id == next(iter(kv_ws.history())).id
    assert tk.end_turn() is None  # idle turn


def test_unversioned_provider_yields_none(tmp_path):
    ws = Workspace(DirProvider(tmp_path / "ws", session="s1"))
    assert ws.terminal("echo x > f.txt").checkpoint is None
    assert ws.write_file("g.txt", "y").checkpoint is None
    assert ws.head is None and not ws.dirty
    ws.close()


# -- head/dirty: pinning read-only observations ---------------------------------


def test_head_pins_readonly_observations(kv_ws):
    r = kv_ws.terminal("echo one > f.txt")
    assert kv_ws.head == r.checkpoint  # mutating call advanced the head
    ls = kv_ws.terminal("ls")
    assert ls.checkpoint is None  # read-only: no commit...
    assert kv_ws.head == r.checkpoint  # ...and the head is its pin
    assert not kv_ws.dirty  # exact pin: nothing staged


def test_head_with_staged_changes_is_flagged_dirty(kv_ws):
    kv_ws.terminal("echo committed > f.txt")
    pinned = kv_ws.head
    kv_ws.fs.write("staged.txt", b"pending")  # host write, no checkpoint
    assert kv_ws.head == pinned  # head unchanged...
    assert kv_ws.dirty  # ...but flagged: the pin is not exact
