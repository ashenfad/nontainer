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
    ws.close()
