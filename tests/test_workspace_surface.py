"""The public seams the layers above core sit on.

Sessions and the adapters used to reach past `Workspace` for three
things: the store it was opened from, the provider under it, and the
funnel that turns the commit half of a ref into a whole commit id.
Each has a public spelling now, and these pin what it answers — a
private attribute is the package's to move, a public one is a promise.
"""

import pytest

from nontainer import CommitNotFoundError, Ref, Store, Workspace, store, workspace
from nontainer.providers import KvgitProvider

# -- what a workspace was opened from ---------------------------------------


def test_store_is_the_store_a_session_was_opened_from(tmp_path):
    s = store(tmp_path / "st")
    ws = s.open("alice")
    assert ws.store is s
    # carried across a fork, which is what lets a delegate's helper
    # find the same store its parent's did
    child = ws.fork("alice.helper")
    assert child.store is s
    child.close()
    ws.close()
    s.close()


def test_store_is_none_for_a_workspace_built_from_a_provider():
    ws = Workspace(KvgitProvider.open(None, session="loose"))
    assert ws.store is None
    ws.close()


def test_provider_is_the_substrate_under_the_session():
    provider = KvgitProvider.open(None, session="loose")
    ws = Workspace(provider)
    assert ws.provider is provider
    assert ws.provider.session == "loose"
    ws.close()


def test_provider_of_a_store_session_is_the_one_the_store_built(tmp_path):
    s = store(tmp_path / "st")
    ws = s.open("alice")
    assert ws.provider.session == "alice"
    assert ws.provider.caps is ws.caps
    ws.close()
    s.close()


# -- expand_ref: the commit half of a ref, spelled whole ---------------------


@pytest.fixture
def two_sessions(tmp_path):
    s = store(tmp_path / "st")
    ws = s.open("alice")
    ws.files.write("/workspace/a.txt", "one")
    other = s.open("bob")
    other.files.write("/workspace/b.txt", "two")
    other.close()
    yield s, ws
    ws.close()
    s.close()


def test_expand_ref_spells_a_short_commit_whole(two_sessions):
    _, ws = two_sessions
    full = ws.head
    short = full[:7]
    assert ws.expand_ref(f"alice@{short}").commit == full
    assert ws.expand_ref(f"alice@{full}").commit == full


def test_expand_ref_resolves_a_ws_git_tag(two_sessions):
    _, ws = two_sessions
    ws.index.stage(["/workspace/a.txt"])
    ws.index.commit("first")
    commit = ws.index.tag("milestone")
    assert ws.expand_ref("alice@milestone") == Ref("alice", commit)


def test_expand_ref_reads_another_sessions_head_and_tags(two_sessions):
    s, ws = two_sessions
    bob = s.open("bob")
    bob.index.stage(["/workspace/b.txt"])
    bob.index.commit("first")
    commit = bob.index.tag("shipped")
    bob.commit()  # the tag lives in bob's blob; land it on bob's branch
    bob.close()
    # alice's handle was opened before bob wrote: the substrate is what
    # knows how to catch up, which is what `ws.provider` is for
    ws.provider.refresh()
    assert ws.expand_ref("bob@shipped").commit == commit
    assert ws.expand_ref(f"bob@{commit[:7]}").commit == commit


def test_expand_ref_takes_a_ref_object_and_keeps_its_path(two_sessions):
    _, ws = two_sessions
    full = ws.head
    expanded = ws.expand_ref(Ref("alice", full[:7], "/a.txt"))
    assert expanded == Ref("alice", full, "/a.txt")


def test_expand_ref_refuses_an_unknown_session_before_the_commit(two_sessions):
    _, ws = two_sessions
    # the session half first: a report about the commit half of a
    # session that is not there sends a reader to list what nothing holds
    with pytest.raises(ValueError, match="unknown session 'ghost'"):
        ws.expand_ref(f"ghost@{ws.head}")


def test_expand_ref_refuses_a_word_that_is_neither_tag_nor_commit(two_sessions):
    _, ws = two_sessions
    with pytest.raises(CommitNotFoundError):
        ws.expand_ref("alice@nonsense")


def test_expand_ref_needs_both_halves(two_sessions):
    _, ws = two_sessions
    with pytest.raises(ValueError, match="Not a ref"):
        ws.expand_ref("alice")


def test_expand_ref_feeds_store_resolve(two_sessions):
    """The round trip the public pair exists for: expand a ref a caller
    typed short, then read that exact state through the store."""
    s, ws = two_sessions
    exact = ws.expand_ref(f"alice@{ws.head[:7]}")
    frozen = s.resolve(exact)
    assert frozen.files.read("/workspace/a.txt") == b"one"
    frozen.close()


# -- the workspace factory keeps its own seams ------------------------------


def test_factory_workspace_has_a_store(tmp_path):
    ws = workspace("solo", store=tmp_path / "st")
    assert isinstance(ws.store, Store)
    assert ws.store.sessions() == ["solo"]
    ws.close()


def test_ws_log_is_a_list():
    """``ws.log()`` and ``ws.index.log()`` are one shape: a caller
    counts entries, indexes them and reads them twice without
    remembering which one hands back a generator."""
    ws = Workspace(KvgitProvider.open(None, session="logs"))
    try:
        ws.files.write("a.txt", "one")
        ws.index.commit("first")
        ws.files.write("b.txt", "two")
        for kind in ("work", "agent", "all"):
            entries = ws.log(kind=kind)
            assert isinstance(entries, list), kind
            assert len(entries) == len(list(entries)), kind  # reads twice
        assert len(ws.log(limit=1)) == 1
        assert ws.log(limit=0) == []
        assert ws.log(kind="all", limit=0) == []
        # what one consumer already writes, which a list answers the same
        assert next(iter(ws.log(limit=1)), None) == ws.log(limit=1)[0]
        assert ws.log()[0].id == ws.head
    finally:
        ws.close()
