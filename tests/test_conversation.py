"""The harness-neutral conversation plane: the index core owns, the
legacy ``__agno__/`` plane it still reads, and the migration a write
makes."""

import pytest

from nontainer import Store, conversation
from nontainer.conversation import Index
from nontainer.planes import (
    COMPACTION_PREFIX,
    CONVERSATION_INDEX_KEY,
    CONVERSATION_PREFIX,
    CONVERSATION_RECORD_KEY,
    CONVERSATION_RUN_PREFIX,
    LEGACY_CONVERSATION_PREFIX,
    LEGACY_RUN_PREFIX,
    LEGACY_SESSION_KEY,
)

LEGACY_RECORD = {
    "session_id": "chat",
    "session_type": "agent",
    "run_ids": ["r1", "r2"],
    "session_data": {"session_name": "rates", "forked_from_session_id": "origin"},
    "created_at": 1,
}


@pytest.fixture
def store(tmp_path):
    st = Store(tmp_path)
    yield st
    st.close()


def _seed_legacy(ws):
    """A conversation as nontainer wrote it before the plane was
    harness-neutral: the agno adapter's ``__agno__/`` keys."""
    kv = ws.provider.kv
    kv[LEGACY_SESSION_KEY] = dict(LEGACY_RECORD)
    kv[LEGACY_RUN_PREFIX + "r1"] = {"run_id": "r1", "said": "one"}
    kv[LEGACY_RUN_PREFIX + "r2"] = {"run_id": "r2", "said": "two"}
    return ws.commit(info={"tool": "test"})


def _plane(ws, prefix):
    return sorted(k for k in ws.provider.kv.keys() if k.startswith(prefix))


# -- reading -------------------------------------------------------------------


def test_nothing_stored_reads_as_none(store):
    ws = store.open("chat")
    assert conversation.index_of(ws) is None
    assert conversation.read_record(ws.provider.kv) is None
    assert conversation.read_runs(ws.provider.kv, ["r1"]) == {}
    ws.close()


def test_the_legacy_plane_reads_through_the_index(store):
    ws = store.open("chat")
    _seed_legacy(ws)
    index = conversation.index_of(ws)
    assert index == Index(
        harness="agno", session="chat", runs=("r1", "r2"), forked_from="origin"
    )
    assert index.legacy
    record = conversation.read_record(ws.provider.kv)
    assert "run_ids" not in record and record["session_type"] == "agent"
    assert conversation.read_runs(ws.provider.kv, ["r2", "r1", "gone"]) == {
        "r2": {"run_id": "r2", "said": "two"},
        "r1": {"run_id": "r1", "said": "one"},
    }
    ws.close()


def test_index_at_reads_either_plane_at_a_commit(store):
    ws = store.open("chat")
    legacy_head = _seed_legacy(ws)
    conversation.write(
        ws.provider.kv, Index(harness="agno", session="chat", runs=("r1",))
    )
    current_head = ws.commit(info={"tool": "test"})
    at_legacy = conversation.index_at(ws.provider, legacy_head)
    at_current = conversation.index_at(ws.provider, current_head)
    assert at_legacy.legacy and at_legacy.runs == ("r1", "r2")
    assert not at_current.legacy and at_current.runs == ("r1",)
    ws.close()


# -- the migration a write makes -----------------------------------------------


def test_a_write_moves_the_legacy_plane_in_the_same_commit(store):
    ws = store.open("chat")
    _seed_legacy(ws)
    index = conversation.index_of(ws)
    conversation.write(
        ws.provider.kv,
        Index(harness="agno", session="chat", runs=(*index.runs, "r3")),
        runs={"r3": {"run_id": "r3", "said": "three"}},
    )
    landed = ws.commit(info={"tool": "turn"})

    assert _plane(ws, LEGACY_CONVERSATION_PREFIX) == []
    assert ws.provider.key_at(landed, LEGACY_SESSION_KEY) is None
    after = conversation.index_of(ws)
    assert after.runs == ("r1", "r2", "r3") and not after.legacy
    # carried runs, and the record without run_ids
    assert conversation.read_runs(ws.provider.kv, after.runs) == {
        "r1": {"run_id": "r1", "said": "one"},
        "r2": {"run_id": "r2", "said": "two"},
        "r3": {"run_id": "r3", "said": "three"},
    }
    record = ws.provider.kv[CONVERSATION_RECORD_KEY]
    assert record["session_data"]["session_name"] == "rates"
    assert "run_ids" not in record
    ws.close()


def test_a_dropped_run_is_not_carried(store):
    ws = store.open("chat")
    _seed_legacy(ws)
    conversation.write(
        ws.provider.kv,
        Index(harness="agno", session="chat", runs=("r2",)),
        drop=["r1"],
    )
    assert _plane(ws, CONVERSATION_RUN_PREFIX) == [CONVERSATION_RUN_PREFIX + "r2"]
    assert _plane(ws, LEGACY_CONVERSATION_PREFIX) == []
    ws.close()


def test_a_checkout_of_an_older_commit_reads_legacy_then_migrates_again(store):
    """History is append-only: a commit from before the migration still
    holds the legacy plane, and a checkout brings it back into a live
    head. It reads the same, and the next write moves it again."""
    ws = store.open("chat")
    legacy_head = _seed_legacy(ws)
    conversation.write(ws.provider.kv, Index(harness="agno", session="chat", runs=()))
    ws.commit(info={"tool": "test"})

    ws.checkout(legacy_head)
    assert conversation.index_of(ws).legacy
    assert conversation.index_of(ws).runs == ("r1", "r2")
    conversation.write(
        ws.provider.kv, Index(harness="agno", session="chat", runs=("r1", "r2"))
    )
    ws.commit(info={"tool": "turn"})
    assert not conversation.index_of(ws).legacy
    assert _plane(ws, LEGACY_CONVERSATION_PREFIX) == []
    ws.close()


# -- forks ---------------------------------------------------------------------


def test_a_full_fork_rebinds_the_index_and_leaves_the_record(store):
    ws = store.open("chat")
    record = {"anything": "the harness keeps", "session_id": "chat"}
    conversation.write(
        ws.provider.kv,
        Index(harness="agex", session="chat", runs=("a",)),
        record=record,
        runs={"a": {"run": "a"}},
    )
    ws.commit(info={"tool": "turn"})
    child = ws.fork("chat.child")
    try:
        index = conversation.index_of(child)
        assert index == Index(
            harness="agex", session="chat.child", runs=("a",), forked_from="chat"
        )
        assert child.provider.kv[CONVERSATION_RECORD_KEY] == record
        assert not child.uncommitted
    finally:
        child.close()
    assert conversation.index_of(ws).session == "chat"
    ws.close()


def test_a_fresh_fork_wipes_both_planes_and_compaction(store):
    ws = store.open("chat")
    _seed_legacy(ws)
    kv = ws.provider.kv
    kv[CONVERSATION_RUN_PREFIX + "stray"] = {"x": 1}
    kv[COMPACTION_PREFIX + "fold/000001"] = {"summary": "s"}
    ws.commit(info={"tool": "test"})
    fresh = ws.fork("chat.fresh", inherit="fresh")
    try:
        for prefix in (
            CONVERSATION_PREFIX,
            LEGACY_CONVERSATION_PREFIX,
            COMPACTION_PREFIX,
        ):
            assert _plane(fresh, prefix) == []
    finally:
        fresh.close()
    ws.close()


def _turn(ws, run):
    """One more run on the conversation, committed as a turn would be."""
    index = conversation.index_of(ws) or Index(harness="agex", session=ws.session)
    conversation.write(
        ws.provider.kv,
        Index(
            harness=index.harness,
            session=ws.session,
            runs=(*index.runs, run),
            forked_from=index.forked_from,
        ),
        runs={run: {"run": run}},
    )
    return ws.commit(info={"tool": "turn"})


def test_a_fork_that_checks_out_a_pre_fork_commit_stays_itself(store):
    """The fork's history begins with its parent's commits, whose index
    names the parent. Restoring one rebinds it in the restore's own
    commit, so the branch never claims to hold the parent."""
    ws = store.open("chat")
    before = _turn(ws, "a")
    _turn(ws, "b")
    child = ws.fork("chat.child")
    try:
        stepped_off = child.head
        landed = child.checkout(before)
        assert conversation.index_of(child) == Index(
            harness="agex", session="chat.child", runs=("a",), forked_from="chat"
        )
        assert not child.uncommitted
        log = [c.id for c in child.provider.history(limit=2)]
        assert log == [landed, stepped_off]
        assert conversation.index_at(child.provider, landed).session == "chat.child"
        # the commit itself is untouched, and a turn on top is the fork's
        assert conversation.index_at(child.provider, before).session == "chat"
        _turn(child, "c")
        assert conversation.index_of(child).runs == ("a", "c")
        # and so is the redo, back to where the checkout stepped off
        child.checkout(stepped_off)
        assert conversation.index_of(child).runs == ("a", "b")
        assert conversation.index_of(child).session == "chat.child"
    finally:
        child.close()
    assert conversation.index_of(ws).runs == ("a", "b")
    ws.close()


def test_a_fork_of_a_fork_keeps_its_own_lineage_on_a_rewind(store):
    """Rewinding past both forks restores an index naming the
    grandparent; the lineage stays the one this session records."""
    ws = store.open("chat")
    before = _turn(ws, "a")
    _turn(ws, "b")
    child = ws.fork("chat.child")
    grand = child.fork("chat.grand")
    try:
        grand.checkout(before)
        index = conversation.index_of(grand)
        assert index.session == "chat.grand"
        assert index.forked_from == "chat.child"
        assert index.runs == ("a",)
    finally:
        grand.close()
        child.close()
    ws.close()


def test_a_fork_checking_out_a_legacy_pre_fork_commit_migrates_it_as_its_own(
    store,
):
    ws = store.open("chat")
    legacy_head = _seed_legacy(ws)
    child = ws.fork("chat.child")
    try:
        landed = child.checkout(legacy_head)
        index = conversation.index_of(child)
        assert index == Index(
            harness="agno", session="chat.child", runs=("r1", "r2"), forked_from="chat"
        )
        assert not index.legacy
        assert _plane(child, LEGACY_CONVERSATION_PREFIX) == []
        assert child.provider.key_at(landed, LEGACY_SESSION_KEY) is None
        assert conversation.read_runs(child.provider.kv, index.runs) == {
            "r1": {"run_id": "r1", "said": "one"},
            "r2": {"run_id": "r2", "said": "two"},
        }
    finally:
        child.close()
    assert conversation.index_of(ws).legacy
    ws.close()


def test_checking_out_where_a_stuck_fork_stands_rebinds_it(store):
    """A branch a checkout already handed to its parent (before this was
    fixed) comes back by checking out any of its commits, the head
    included."""
    ws = store.open("chat")
    _turn(ws, "a")
    child = ws.fork("chat.child")
    try:
        conversation.write(
            child.provider.kv, Index(harness="agex", session="chat", runs=("a",))
        )
        stuck = child.commit(info={"tool": "test"})
        landed = child.checkout(stuck)
        assert landed != stuck
        index = conversation.index_of(child)
        assert index.session == "chat.child" and index.forked_from == "chat"
        # and once it is the fork's own, the same checkout writes nothing
        assert child.checkout(landed) == landed
    finally:
        child.close()
    ws.close()


def test_a_checkout_within_one_session_leaves_the_index_as_it_was(store):
    ws = store.open("chat")
    before = _turn(ws, "a")
    _turn(ws, "b")
    ws.checkout(before)
    assert conversation.index_of(ws) == Index(
        harness="agex", session="chat", runs=("a",)
    )
    ws.close()


# -- merges --------------------------------------------------------------------


def test_a_merge_from_a_legacy_session_brings_neither_plane(store):
    """A migrated parent merging a delegate still on the legacy plane
    keeps its own conversation and takes none of the delegate's, on
    either plane: both prefixes are the parent's alone."""
    parent = store.open("parent")
    parent.files.write("/workspace/a.txt", "base\n")
    parent.commit(info={"tool": "seed"})
    child = parent.fork("parent.child", inherit="fresh")
    _seed_legacy(child)
    child.files.write("/workspace/b.txt", "from the child\n")
    child.commit(info={"tool": "work"})
    child.close()

    conversation.write(
        parent.provider.kv, Index(harness="agno", session="parent", runs=("p1",))
    )
    parent.commit(info={"tool": "turn"})
    parent.merge("parent.child")

    assert parent.files.read("/workspace/b.txt") == b"from the child\n"
    assert _plane(parent, LEGACY_CONVERSATION_PREFIX) == []
    assert conversation.index_of(parent).runs == ("p1",)
    parent.close()


# -- clearing ------------------------------------------------------------------


def test_clear_removes_both_planes_and_keeps_compaction(store):
    ws = store.open("chat")
    _seed_legacy(ws)
    kv = ws.provider.kv
    kv[CONVERSATION_INDEX_KEY] = Index(harness="agno", session="chat").to_dict()
    kv[COMPACTION_PREFIX + "fold/000001"] = {"summary": "s"}
    assert conversation.clear(kv)
    assert _plane(ws, CONVERSATION_PREFIX) == []
    assert _plane(ws, LEGACY_CONVERSATION_PREFIX) == []
    assert _plane(ws, COMPACTION_PREFIX) == [COMPACTION_PREFIX + "fold/000001"]
    assert not conversation.clear(kv)
    ws.close()


# -- the agno db on a legacy branch --------------------------------------------


def test_the_agno_db_reads_a_legacy_branch_and_migrates_on_its_next_turn(
    store, tmp_path
):
    pytest.importorskip("agno")
    from agno.run.agent import RunOutput
    from agno.session import AgentSession

    from nontainer.adapters.agno_db import KvgitSessionDb

    ws = store.open("chat")
    kv = ws.provider.kv
    kv[LEGACY_SESSION_KEY] = {
        "session_id": "chat",
        "session_type": "agent",
        "agent_id": "a",
        "run_ids": ["r1"],
        "session_data": {"session_name": "rates"},
        "created_at": 1,
        "updated_at": 1,
    }
    kv[LEGACY_RUN_PREFIX + "r1"] = RunOutput(
        run_id="r1", session_id="chat", agent_id="a", content="one"
    ).to_dict()
    ws.commit(info={"tool": "turn"})
    db = KvgitSessionDb(ws, db_path=str(tmp_path / "agno"))

    session = db.get_session("chat")
    assert [r.run_id for r in session.runs] == ["r1"]
    assert session.session_data["session_name"] == "rates"

    data = session.to_dict()
    data["runs"] = [
        *data["runs"],
        RunOutput(
            run_id="r2", session_id="chat", agent_id="a", content="two"
        ).to_dict(),
    ]
    db.upsert_session(AgentSession.from_dict(data))

    assert _plane(ws, LEGACY_CONVERSATION_PREFIX) == []
    assert not ws.uncommitted
    assert conversation.index_of(ws).runs == ("r1", "r2")
    reread = db.get_session("chat")
    assert [r.run_id for r in reread.runs] == ["r1", "r2"]
    assert reread.session_data["session_name"] == "rates"
    ws.close()


def test_the_agno_db_leaves_another_harnesss_conversation_alone(store, tmp_path):
    pytest.importorskip("agno")
    from agno.session import AgentSession

    from nontainer.adapters.agno_db import KvgitSessionDb
    from nontainer.errors import NotSupportedError

    ws = store.open("chat")
    conversation.write(
        ws.provider.kv, Index(harness="agex", session="chat", runs=("x",))
    )
    ws.commit(info={"tool": "turn"})
    db = KvgitSessionDb(ws, db_path=str(tmp_path / "agno"))
    assert db.get_session("chat") is None
    with pytest.raises(NotSupportedError, match="agex"):
        db.upsert_session(AgentSession(session_id="chat", agent_id="a"))
    assert conversation.index_of(ws).harness == "agex"
    ws.close()
