"""nontainer.compaction: the harness-neutral half of compaction.

The record and where it lives (the ``__compaction__/`` plane, which
merges, forks and rewinds like the conversation it describes), the
fold in force, the policy, the texts, and the shrinking and chunking a
fold too large for one request needs.
"""

import pytest

from nontainer import Workspace, WorkspaceError
from nontainer.compaction import (
    FOLD_PREFIX,
    MARK,
    Fold,
    Item,
    Policy,
    chunks,
    estimate_tokens,
    folds,
    in_force,
    is_ours,
    record,
    reduce,
    summary_message,
    summary_request,
    transcript,
)
from nontainer.providers import KvgitProvider


@pytest.fixture
def ws():
    w = Workspace(KvgitProvider.open(None, session="chat"))
    yield w
    w.close()


def fold(through: str, summary: str = "s", **kw) -> Fold:
    return Fold(through=through, summary=summary, **kw)


# -- the record ------------------------------------------------------------------


def test_a_fold_round_trips_through_a_dict():
    f = fold(
        "m9", "what happened", runs=4, tokens_before=900, tokens_after=80, model="m"
    )
    assert Fold.from_dict(f.to_dict()) == f


def test_a_record_with_fields_this_version_does_not_know_still_reads():
    data = fold("m1").to_dict() | {"from_the_future": True}
    assert Fold.from_dict(data).through == "m1"


def test_folds_are_recorded_in_order_and_never_rewritten(ws):
    assert folds(ws) == []
    k1 = record(ws, fold("a", "first"))
    k2 = record(ws, fold("b", "second"))
    assert k1 < k2 and k1.startswith(FOLD_PREFIX)
    assert [f.summary for f in folds(ws)] == ["first", "second"]


def test_a_frozen_workspace_records_nothing(ws):
    ws.terminal("echo x > a.txt")
    ws.tags.add("v1")
    snap = ws.tags.at("v1")
    try:
        with pytest.raises(WorkspaceError):
            record(snap, fold("a"))
    finally:
        snap.close()


# -- the fold in force -----------------------------------------------------------


def test_the_latest_fold_whose_anchor_is_present_is_in_force():
    recorded = [fold("a", "one"), fold("b", "two"), fold("c", "three")]
    assert in_force(recorded, ["x", "a", "b", "c"]).summary == "three"
    # an edit unsaid the turn the latest fold reached: the one before it
    # still holds
    assert in_force(recorded, ["x", "a", "b"]).summary == "two"
    assert in_force(recorded, ["x"]) is None
    assert in_force([], ["a"]) is None


def test_in_force_reads_the_workspace(ws):
    record(ws, fold("a", "one"))
    assert in_force(ws, ["a"]).summary == "one"


# -- the plane -------------------------------------------------------------------


def test_a_fold_rides_the_next_commit_and_a_rewind_takes_it_back(ws):
    ws.terminal("echo one > a.txt")
    before = ws.provider.head
    record(ws, fold("a"))
    ws.terminal("echo two > b.txt")  # the turn's next commit carries it
    assert len(folds(ws)) == 1
    ws.checkout(before)
    assert folds(ws) == []


def test_a_fresh_fork_drops_the_folds_and_a_full_one_keeps_them(ws):
    ws.terminal("echo one > a.txt")
    record(ws, fold("a", "parent's"))
    ws.commit(info={"tool": "test"})
    fresh = ws.fork("kid-fresh", inherit="fresh")
    full = ws.fork("kid-full", inherit="full")
    try:
        assert folds(fresh) == []
        assert [f.summary for f in folds(full)] == ["parent's"]
    finally:
        fresh.close()
        full.close()


def test_a_merge_keeps_this_sessions_folds_and_takes_none_of_the_others(ws):
    ws.terminal("echo one > a.txt")
    record(ws, fold("a", "base"))
    ws.commit(info={"tool": "test"})
    kid = ws.fork("worker")
    try:
        record(ws, fold("b", "ours"))
        ws.commit(info={"tool": "test"})
        record(kid, fold("c", "theirs"))
        kid.terminal("echo two > b.txt")

        out = ws.provider.merge("worker")
        assert out.merged and out.conflicts == ()
        assert [f.summary for f in folds(ws)] == ["base", "ours"]
        assert ws.files.read("/workspace/b.txt") == b"two\n"
    finally:
        kid.close()


# -- policy and measuring --------------------------------------------------------


def test_the_policy_says_when_to_fold_and_what_fits():
    p = Policy(budget=100, window=150)
    assert not p.due(99) and p.due(100)
    assert p.fits(149) and not p.fits(150)
    assert Policy(budget=100).fits(10**9)  # no window known: no limit known


def test_a_policy_that_could_never_fold_in_time_is_refused():
    with pytest.raises(ValueError):
        Policy(budget=0)
    with pytest.raises(ValueError):
        Policy(budget=200, window=100)


def test_tokens_are_estimated_at_four_characters_each():
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2


# -- the texts -------------------------------------------------------------------


def test_the_cache_friendly_request_asks_for_the_summary_alone():
    text = summary_request()
    assert "Do not call any tools" in text
    assert "only ever one summary" in text


def test_the_reduced_request_carries_the_transcript_and_the_summary_so_far():
    text = summary_request("[user]\nhi", prior="earlier", part="part 2 of 3")
    assert "part 2 of 3 of a transcript" in text
    assert "<summary>\nearlier\n</summary>" in text
    assert text.endswith("<transcript>\n[user]\nhi\n</transcript>")


def test_the_summary_message_says_the_person_still_sees_everything():
    text = summary_message("we built a game")
    assert text.endswith("we built a game")
    assert "The person still sees the whole conversation" in text


def test_marked_ids_are_recognised():
    assert is_ours(MARK + "summary:m1")
    assert not is_ours("m1") and not is_ours(None)


# -- a fold too large for one request -------------------------------------------


def test_reducing_cuts_tool_output_and_keeps_what_was_said():
    said = Item("user", "please " * 500)
    result = Item("tool", "x" * 50_000, "tool_result")
    call = Item("assistant", '{"content": "' + "y" * 9_000 + '"}', "tool_call")
    small = Item("tool", "ok", "tool_result")
    out = reduce([said, result, call, small])
    assert out[0] == said
    assert out[1].text.startswith("[50,000 characters; it began:]")
    assert len(out[1].text) < 400
    assert out[2].text.endswith("more characters]") and len(out[2].text) < 2100
    assert out[3] == small


def test_chunks_keep_order_and_split_only_between_items():
    items = [Item("user", "a" * 400) for _ in range(10)]  # ~104 tokens each
    parts = chunks(items, 250)
    assert [len(p) for p in parts] == [2, 2, 2, 2, 2]
    assert [i for p in parts for i in p] == items


def test_an_item_too_large_for_any_chunk_is_cut_to_fit():
    huge = Item("tool", "z" * 10_000, "tool_result")
    parts = chunks([Item("user", "hi"), huge, Item("user", "bye")], 100)
    flat = [i for p in parts for i in p]
    assert [i.text[:2] for i in flat] == ["hi", "zz", "by"]
    assert flat[1].text.endswith("[… cut to fit]")
    assert all(sum(estimate_tokens(i.text) + 4 for i in p) <= 100 for p in parts)


def test_a_transcript_labels_each_kind():
    text = transcript(
        [
            Item("user", "hi"),
            Item("assistant", "{}", "tool_call"),
            Item("tool", "ok", "tool_result"),
        ]
    )
    assert text == "[user]\nhi\n\n[tool call]\n{}\n\n[tool result]\nok"
