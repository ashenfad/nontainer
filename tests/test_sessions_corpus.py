"""The merge corpus: delegation the way an embedder performs it.

Every scenario here goes through the surface an embedder actually has —
``Store``, ``Workspace``, ``Runtime``, and the terminal's ``ws-git`` —
and never through a provider. That is the point of the file: the
provider-level merge is covered in ``test_provider_merge.py``, and what
is untested there is whether the *verbs* compose into the three
scenarios the design is for (fork yourself, fork a delegate, spawn
fresh) and whether the plane policy holds when the caller is a session
rather than a branch.

The scenarios, in the order they appear:

a. disjoint files on both sides merge clean
b. overlapping edits come back as markers, and the merge context lives
   in ``ws-git status`` / ``diff --check`` until the markers are gone
c. a deletion on one side against an edit on the other conflicts
d. a cache key written on both sides does not conflict; ours wins
e. the delegate's conversation never comes back
f. a fork with a narrowed view merges back with only its own changes
g. two delegates writing identical bytes to one file merge clean
h. the merge joins the ancestry: merging the same source again brings
   only what is new, and the merge is in the agent's log
i. a source with work its agent has not committed is refused
j. a provider without a merge engine refuses fork and merge by name
"""

import pytest

from nontainer import NotSupportedError, Store, WorkspaceError
from nontainer.wsgit import register_wsgit


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "store")
    yield s
    s.close()


@pytest.fixture
def parent(store):
    """A session with ws-git registered — an agent's session, not a
    bare workspace: half the corpus asserts what the agent sees."""
    ws = store.open("parent")
    register_wsgit(ws)
    yield ws
    ws.close()


def _seed(ws, **files: str) -> None:
    """Write files and land them as one agent commit, so the session
    has the baseline a fork branches from."""
    for name, text in files.items():
        ws.files.write(f"/workspace/{name}", text)
    ws.index.commit("seed")


def _fork(ws, name: str, **kwargs):
    """A delegate session. ``ws-git`` needs no re-registration: a fork
    rebuilds the framework's own commands bound to itself."""
    child = ws.fork(name, **kwargs)
    assert "ws-git" in child.runtime.commands
    return child


# -- a. disjoint ---------------------------------------------------------------


def test_disjoint_files_on_both_sides_merge_clean(parent):
    _seed(parent, base="base\n")
    child = _fork(parent, "child")
    try:
        child.files.write("/workspace/from_child.txt", "child\n")
        child.index.commit("child work")

        parent.files.write("/workspace/from_parent.txt", "parent\n")
        parent.index.commit("parent work")

        out = parent.merge("child")
        assert out.merged and not out.conflicts
        assert parent.files.read("/workspace/from_child.txt") == b"child\n"
        assert parent.files.read("/workspace/from_parent.txt") == b"parent\n"
        assert parent.files.read("/workspace/base") == b"base\n"
        # nothing left over: the merge and its record both landed
        assert not parent.uncommitted
        assert parent.index.status().staged == ()
        assert parent.index.status().unstaged == ()
    finally:
        child.close()


# -- b. overlapping ------------------------------------------------------------


def test_overlapping_edits_come_back_as_a_merge_the_agent_can_finish(parent):
    """Judgment to the LLM, bytes to the machine: the merge lands with
    markers and the agent resolves them with ordinary edits."""
    _seed(parent, shared="one\ntwo\nthree\n")
    child = _fork(parent, "child")
    try:
        child.files.write("/workspace/shared", "one\nCHILD\nthree\n")
        child.index.commit("child edit")

        parent.files.write("/workspace/shared", "one\nPARENT\nthree\n")
        parent.index.commit("parent edit")

        out = parent.merge("child")
        assert out.merged
        assert out.conflicts == ("/workspace/shared",)
        assert b"<<<<<<<" in parent.files.read("/workspace/shared")

        status = parent.terminal("ws-git status").stdout
        assert status.startswith("## merging child@")
        assert "(1 unresolved)" in status
        assert "UU shared" in status

        check = parent.terminal("ws-git diff --check")
        assert check.exit_code == 2
        assert "shared:2: leftover conflict marker" in check.stdout

        parent.files.write("/workspace/shared", "one\nBOTH\nthree\n")
        parent.index.commit("resolved")

        assert parent.terminal("ws-git status").stdout == ""
        assert parent.terminal("ws-git diff --check").exit_code == 0
    finally:
        child.close()


# -- c. deletion against an edit -----------------------------------------------


def test_a_deletion_against_an_edit_conflicts(parent):
    """One side removed the file and the other rewrote it: no rule
    resolves that, so it comes back marked like any other conflict."""
    _seed(parent, doomed="keep\n")
    child = _fork(parent, "child")
    try:
        child.terminal("rm /workspace/doomed")
        child.index.commit("dropped it")

        parent.files.write("/workspace/doomed", "edited\n")
        parent.index.commit("kept it")

        out = parent.merge("child")
        assert out.merged
        assert out.conflicts == ("/workspace/doomed",)
        body = parent.files.read("/workspace/doomed")
        assert b"<<<<<<<" in body and b"edited" in body
        assert "UU doomed" in parent.terminal("ws-git status").stdout
    finally:
        child.close()


# -- d. the cache plane --------------------------------------------------------


def test_a_cache_key_written_on_both_sides_keeps_ours(parent):
    """The cache is a plane, not a file: the delegate's cache never
    comes back, and a key both sides wrote is not a conflict."""
    _seed(parent, base="base\n")
    parent.cache["shared"] = "before the fork"
    parent.commit(info={"tool": "test"})

    child = _fork(parent, "child")
    try:
        child.cache["shared"] = "the delegate's"
        child.cache["only_theirs"] = "never travels"
        child.commit(info={"tool": "test"})
        child.files.write("/workspace/result.txt", "done\n")
        child.index.commit("delegate work")

        parent.cache["shared"] = "the caller's"
        parent.commit(info={"tool": "test"})

        out = parent.merge("child")
        assert out.merged and not out.conflicts
        assert parent.files.read("/workspace/result.txt") == b"done\n"
        assert parent.cache["shared"] == "the caller's"
        assert "only_theirs" not in parent.cache
    finally:
        child.close()


# -- e. the conversation plane -------------------------------------------------


def test_the_delegates_conversation_never_comes_back(parent, tmp_path):
    """A delegate returns files. Its conversation folds into whatever
    the runner reports; merging it into the caller's would interleave
    two chats that never happened together."""
    pytest.importorskip("agno")
    from agno.session import AgentSession

    from nontainer.adapters.agno_db import KvgitSessionDb, fork_session

    _seed(parent, base="base\n")
    here = KvgitSessionDb(parent, db_path=str(tmp_path / "agno-parent"))
    here.upsert_session(AgentSession(session_id="parent", runs=[]))
    parent.commit(info={"tool": "test"})

    # fork_session is the conversation-aware fork: the child's record
    # carries its own session_id, so the two chats are distinguishable
    child = fork_session(parent, "child")
    theirs = KvgitSessionDb(child, db_path=str(tmp_path / "agno-child"))
    try:
        theirs.rename_session("child", None, "the delegate's chat")
        child.files.write("/workspace/result.txt", "done\n")
        child.index.commit("delegate work")

        assert parent.merge("child").merged
        assert parent.files.read("/workspace/result.txt") == b"done\n"

        record = here.get_session(session_id="parent", session_type=None)
        assert record is not None
        assert record.session_id == "parent"
        assert (record.session_data or {}).get("session_name") is None
    finally:
        child.close()


# -- f. a narrowed view --------------------------------------------------------


def test_a_narrowed_view_merges_back_only_its_own_changes(parent):
    """A fresh spawn is a fork with a narrowed view: its branch holds
    everything, its filesystem shows the seed, and the merge is
    ordinary three-way over what it actually changed."""
    _seed(parent, **{"auth.py": "auth\n", "billing.py": "billing\n"})
    child = _fork(parent, "child", inherit="fresh", paths=["/workspace/auth.py"])
    try:
        assert child.files.list("/workspace") == ["/workspace/auth.py"]
        assert not child.files.exists("/workspace/billing.py")

        child.files.write("/workspace/auth.py", "auth, refactored\n")
        child.files.write("/workspace/notes.md", "what I did\n")
        child.index.commit("delegate work")

        # the caller moved on outside the delegate's view
        parent.files.write("/workspace/billing.py", "billing, edited\n")
        parent.index.commit("caller work")

        out = parent.merge("child")
        assert out.merged and not out.conflicts
        assert parent.files.read("/workspace/auth.py") == b"auth, refactored\n"
        assert parent.files.read("/workspace/notes.md") == b"what I did\n"
        # untouched by a delegate that could not see it
        assert parent.files.read("/workspace/billing.py") == b"billing, edited\n"
    finally:
        child.close()


# -- g. identical bytes --------------------------------------------------------


def test_two_delegates_generating_the_same_file_merge_clean(parent):
    """Fan-out's common case: two delegates regenerate one file from
    the same inputs. Equal bytes are not a conflict."""
    _seed(parent, base="base\n")
    one = _fork(parent, "one")
    two = _fork(parent, "two")
    try:
        for child, extra in ((one, "one.txt"), (two, "two.txt")):
            child.files.write("/workspace/generated.txt", "deterministic\n")
            child.files.write(f"/workspace/{extra}", "note\n")
            child.index.commit("generated")

        assert parent.merge("one").merged
        out = parent.merge("two")
        assert out.merged and not out.conflicts
        assert parent.files.read("/workspace/generated.txt") == b"deterministic\n"
        assert parent.files.exists("/workspace/one.txt")
        assert parent.files.exists("/workspace/two.txt")
    finally:
        one.close()
        two.close()


# -- h. the merge joins the ancestry -------------------------------------------


def test_the_merge_joins_the_ancestry_and_shows_in_the_agents_log(parent):
    """The merge commit keeps both parents, so merging the same source
    again is a merge of what is NEW — not a second pass over work
    already taken, which is what a squash would give."""
    _seed(parent, shared="one\ntwo\nthree\n")
    child = _fork(parent, "child")
    try:
        child.files.write("/workspace/shared", "one\nCHILD\nthree\n")
        child.index.commit("child edit")
        parent.files.write("/workspace/shared", "one\nPARENT\nthree\n")
        parent.index.commit("parent edit")

        first = parent.merge("child")
        assert first.conflicts == ("/workspace/shared",)
        parent.files.write("/workspace/shared", "one\nBOTH\nthree\n")
        parent.index.commit("resolved")

        log = parent.terminal("ws-git log").stdout.splitlines()
        assert log[1].startswith(f"{first.commit[:7]} ws-git.merge from child")
        assert [line.split(" ", 1)[1].removesuffix(" (sizes)") for line in log] == [
            "resolved",
            "ws-git.merge from child",
            "parent edit",
            "seed",
        ]

        # the second round: only the new file, and the resolution the
        # caller made is not re-contested
        child.files.write("/workspace/later.txt", "later\n")
        child.index.commit("more work")
        second = parent.merge("child")
        assert second.merged and not second.conflicts
        assert parent.files.read("/workspace/shared") == b"one\nBOTH\nthree\n"
        assert parent.files.read("/workspace/later.txt") == b"later\n"
    finally:
        child.close()


# -- i. an uncommitted source --------------------------------------------------


def test_a_source_with_work_in_flight_is_refused(parent):
    """A merge takes only what has been committed, on both sides. A
    delegate still composing is refused, not merged at the state it
    has already moved past."""
    _seed(parent, base="base\n")
    child = _fork(parent, "child")
    try:
        child.files.write("/workspace/done.txt", "done\n")
        child.index.commit("landed")
        child.terminal("echo wip > /workspace/wip.txt")
        assert not child.uncommitted  # the STORE has it; its agent has not
        assert child.index.status().unstaged == ("/workspace/wip.txt",)

        with pytest.raises(WorkspaceError, match="uncommitted ws-git work on 'child'"):
            parent.merge("child")
        assert not parent.files.exists("/workspace/done.txt")

        child.terminal("ws-git commit -m 'wip too'")
        assert parent.merge("child").merged
        assert parent.files.read("/workspace/wip.txt") == b"wip\n"
    finally:
        child.close()


# -- j. a provider without a merge engine --------------------------------------


def test_agentfs_refuses_fork_and_merge_by_name(tmp_path):
    """Degrade honestly: a fork you cannot merge back is a trap, so
    the rung that has no merge engine refuses both verbs."""
    pytest.importorskip("agentfs_sdk")
    from nontainer import Workspace
    from nontainer.providers import AgentFSProvider

    ws = Workspace(AgentFSProvider(tmp_path / "s1.db", session="s1"))
    try:
        assert not ws.caps.merge
        with pytest.raises(NotSupportedError):
            ws.fork("child")
        with pytest.raises(NotSupportedError, match="cannot merge"):
            ws.merge("child")
    finally:
        ws.close()
