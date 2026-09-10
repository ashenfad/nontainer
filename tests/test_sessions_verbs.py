"""The sessions verbs: fork with a view, take, attach, and the
terminal spellings of all of them.

``test_sessions_corpus.py`` asks whether the verbs compose into the
scenarios the design is for. This file asks what each verb does on its
own — the write rule, the view record, the refusals, the output shape
of ``ws-git branch`` / ``merge`` / ``checkout -- <paths>`` /
``log <session>`` / ``diff <session>``.
"""

import pytest

from nontainer import CommitNotFoundError, NotSupportedError, Store, WorkspaceError
from nontainer.views import VIEW_KEY, parse_view
from nontainer.wsgit import register_wsgit


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "store")
    yield s
    s.close()


@pytest.fixture
def ws(store):
    w = store.open("main")
    register_wsgit(w)
    yield w
    w.close()


def _seed(w, **files: str) -> None:
    for name, text in files.items():
        w.files.write(f"/workspace/{name}", text)
    w.index.commit("seed")


# -- a fork point is a commit --------------------------------------------------


def test_a_fork_point_is_always_a_commit(ws):
    """A fork of the staging buffer would give the child a state that
    never existed and a merge base predating the parent's own edits."""
    ws.autocommit = False
    ws.files.write("/workspace/a.txt", "in flight\n")
    assert ws.uncommitted

    child = ws.fork("child")
    try:
        assert not ws.uncommitted  # landed first
        point = next(iter(ws.log(limit=1)))
        assert point.info == {"tool": "fork", "child": "child", "inherit": "full"}
        assert child.files.read("/workspace/a.txt") == b"in flight\n"
    finally:
        child.close()


def test_a_fork_at_an_earlier_commit_leaves_the_buffer_alone(ws):
    """``at`` branches from the past; the buffer belongs to this
    session's present."""
    _seed(ws, **{"a.txt": "one\n"})
    first = ws.head
    ws.autocommit = False
    ws.files.write("/workspace/a.txt", "two\n")

    child = ws.fork("child", at=first)
    try:
        assert ws.uncommitted  # untouched
        assert child.files.read("/workspace/a.txt") == b"one\n"
    finally:
        child.close()


# -- inherit -------------------------------------------------------------------


def test_inherit_fresh_drops_the_conversation_and_nothing_else(ws):
    _seed(ws, **{"a.txt": "one\n"})
    kv = ws._provider.kv
    kv["__agno__/session"] = {"session_id": "main"}
    kv["__agno__/runs/r1"] = {"run_id": "r1"}
    ws.commit(info={"tool": "test"})

    for inherit, expected in (("full", 2), ("fresh", 0)):
        child = ws.fork(f"child-{inherit}", inherit=inherit)
        try:
            kept = [k for k in child._provider.kv.keys() if k.startswith("__agno__/")]
            assert len(kept) == expected
            assert child.files.read("/workspace/a.txt") == b"one\n"
            assert not child.uncommitted
        finally:
            child.close()


def test_inherit_takes_only_its_two_words(ws):
    with pytest.raises(ValueError, match="must be 'full' or 'fresh'"):
        ws.fork("child", inherit="chapter")


# -- a fresh ws-git state ------------------------------------------------------


def _blob(w):
    from nontainer.agentgit import BLOB_KEY, parse_blob

    return parse_blob(w._provider.kv.get(BLOB_KEY))


def test_a_fork_starts_with_a_fresh_ws_git_state(ws):
    """A branch carries the workspace; it does not carry the agent's
    composition in progress. A delegate starts at no commit of its
    own, with nothing staged — which is also what lets the fiction's
    escape apply to it: a session that never made an agent commit
    merges at its store head."""
    _seed(ws, **{"a.txt": "one\n", "b.txt": "two\n"})
    ws.files.write("/workspace/a.txt", "edited\n")
    ws.index.stage(["/workspace/a.txt"])
    assert ws.index.head is not None and ws.index.status().staged

    for inherit in ("full", "fresh"):
        child = ws.fork(f"child-{inherit}", inherit=inherit)
        try:
            assert child.index.head is None
            assert child.index.status().staged == ()
            assert child.index.log() == []
            assert _blob(child) == {
                "head": None,
                "staged": [],
                "merge_source": None,
                "unresolved": [],
            }
        finally:
            child.close()


def test_the_reset_is_at_the_childs_store_head(ws, store):
    """Visible to any reader of the branch, not only through the handle
    the fork came back on."""
    _seed(ws, **{"a.txt": "one\n"})
    ws.index.stage(["/workspace/a.txt"])
    ws.fork("child").close()

    reopened = store.open("child")
    try:
        assert reopened.index.head is None
        assert reopened.index.status().staged == ()
    finally:
        reopened.close()


def test_the_reset_commit_is_bookkeeping(ws):
    """Nobody performed it, so ``log()`` hides it and ``kind='all'``
    shows it — the same rule the fiction's other records follow."""
    from nontainer.agentgit import FORK_TOOL

    _seed(ws, **{"a.txt": "one\n"})
    child = ws.fork("child")
    try:
        tools = [c.info.get("tool") for c in child.log(kind="all")]
        assert FORK_TOOL in tools
        assert FORK_TOOL not in [c.info.get("tool") for c in child.log()]
        assert FORK_TOOL.startswith("ws-git.")  # never an agent commit
    finally:
        child.close()


def test_a_fork_of_a_session_that_never_used_ws_git_makes_no_record(ws):
    """Nothing to reset is nothing to record: the fork of a session
    with no ws-git state costs no commit."""
    ws.files.write("/workspace/a.txt", "one\n")
    child = ws.fork("child")
    try:
        tools = [c.info.get("tool") for c in child.log(kind="all")]
        assert "ws-git.fork" not in tools
        assert child.index.head is None
    finally:
        child.close()


def test_ws_git_log_in_a_fresh_child_is_empty_but_reaches_the_parents(ws, store):
    _seed(ws, **{"a.txt": "one\n"})
    child = ws.fork("child")
    try:
        assert child.terminal("ws-git log").stdout.strip() == ""
        assert "seed" in child.terminal("ws-git log main").stdout
        # nothing STAGED, whatever the parent had staged. Everything
        # reads as modified instead, the way it does in a repo before
        # its first commit.
        porcelain = child.terminal("ws-git status --porcelain").stdout.splitlines()
        assert porcelain and all(line.startswith(" ") for line in porcelain)
    finally:
        child.close()


# -- the view ------------------------------------------------------------------


def test_the_view_narrows_reads_and_survives_reopen(ws, store):
    _seed(ws, **{"auth.py": "auth\n", "billing.py": "billing\n"})
    child = ws.fork("child", paths=["auth.py"])
    try:
        assert child.files.list("/workspace") == ["/workspace/auth.py"]
        assert not child.files.exists("/workspace/billing.py")
        with pytest.raises(FileNotFoundError):
            child.files.read("/workspace/billing.py")
        assert child.terminal("ls /workspace").stdout == "auth.py\n"
        assert child.terminal("cat /workspace/billing.py").exit_code != 0
        # the branch still holds everything: that is what keeps the
        # merge back an ordinary three-way
        assert (
            ws._provider.files_at(child.head)["/workspace/billing.py"] == b"billing\n"
        )
        assert parse_view(child._provider.kv.get(VIEW_KEY)) == ("/workspace/auth.py",)
    finally:
        child.close()

    reopened = store.open("child")
    try:
        assert reopened.files.list("/workspace") == ["/workspace/auth.py"]
    finally:
        reopened.close()


def test_the_write_rule_new_anywhere_never_over_what_you_cannot_see(ws):
    _seed(ws, **{"auth.py": "auth\n", "billing.py": "billing\n"})
    child = ws.fork("child", paths=["auth.py"])
    try:
        # in the view: ordinary
        child.files.write("/workspace/auth.py", "refactored\n")
        # a new path anywhere: allowed, and it joins the view
        child.files.write("/workspace/notes/why.md", "because\n")
        assert child.files.read("/workspace/notes/why.md") == b"because\n"
        assert "/workspace/notes/why.md" in parse_view(child._provider.kv.get(VIEW_KEY))

        # an existing path it cannot see: refused, by name
        with pytest.raises(PermissionError, match="outside this session's view"):
            child.files.write("/workspace/billing.py", "sneaky\n")
        with pytest.raises(PermissionError, match="outside this session's view"):
            child.files.fs.remove("/workspace/billing.py")
        assert (
            ws._provider.files_at(child.head)["/workspace/billing.py"] == b"billing\n"
        )

        # and the same refusal reaches the agent through the terminal
        r = child.terminal("echo sneaky > /workspace/billing.py")
        assert r.exit_code != 0
    finally:
        child.close()


def test_a_narrowed_session_cannot_stage_what_it_cannot_see(ws):
    """The index reads the whole branch; the view has the last word, or
    an agent could put a file it can neither read nor change into a
    composition where nothing would report it."""
    _seed(ws, **{"a.py": "a\n", "b.py": "b\n"})
    child = ws.fork("child", paths=["a.py"])
    try:
        with pytest.raises(ValueError, match="unknown path"):
            child.index.stage(["/workspace/b.py"])
        assert child.index.stage(["/workspace/a.py"]) == ("/workspace/a.py",)
    finally:
        child.close()


def test_a_fork_of_a_narrowed_session_is_not_narrowed_by_inheritance(ws):
    """A view describes the session it was given to. A child handed the
    whole tree must not inherit its parent's blinkers."""
    _seed(ws, **{"a.py": "a\n", "b.py": "b\n"})
    child = ws.fork("child", paths=["a.py"])
    try:
        grand = child.fork("grand")
        try:
            assert grand._view is None
            assert grand.files.exists("/workspace/b.py")
        finally:
            grand.close()
    finally:
        child.close()


def test_the_view_record_merges_as_ours(ws):
    """Merging a narrowed delegate does not narrow the caller."""
    _seed(ws, **{"a.py": "a\n", "b.py": "b\n"})
    child = ws.fork("child", paths=["a.py"])
    try:
        child.files.write("/workspace/a.py", "done\n")
        child.index.commit("done")
        assert ws.merge("child").merged
        assert ws._view is None
        assert ws.files.exists("/workspace/b.py")
    finally:
        child.close()


def test_the_filesystem_root_is_not_a_view(ws):
    """A view of everything is not a view, and it is spelled
    paths=None. Taken as a seed, ``/`` would hide every path below it
    instead of showing the tree, which is the opposite of what a
    caller asking for the root means."""
    _seed(ws, **{"a.py": "a\n"})
    for spelling in ("/", "//", "/."):
        with pytest.raises(ValueError, match="not a view"):
            ws.fork("kid", paths=[spelling])
    assert "kid" not in set(ws._store.sessions())

    r = ws.terminal("ws-git branch kid --paths /")
    assert r.exit_code == 1
    assert "not a view" in r.stderr
    assert "kid" not in set(ws._store.sessions())


def test_the_workspace_root_as_a_seed_shows_the_whole_tree(ws):
    """The seed a caller reaches for instead: an ordinary path that
    happens to hold everything this session has."""
    _seed(ws, **{"a.py": "a\n", "deep/b.py": "b\n"})
    child = ws.fork("child", paths=["/workspace"])
    try:
        assert child._view == ("/workspace",)
        assert child.files.read("/workspace/a.py") == b"a\n"
        assert child.files.read("/workspace/deep/b.py") == b"b\n"
        assert sorted(child.files.list("/workspace", recursive=True)) == [
            "/workspace/a.py",
            "/workspace/deep",
            "/workspace/deep/b.py",
        ]
        child.files.write("/workspace/a.py", "changed\n")
        assert child.files.read("/workspace/a.py") == b"changed\n"
    finally:
        child.close()


# -- diff grouping -------------------------------------------------------------


def test_diff_groups_the_delegates_view_from_its_collateral(ws):
    _seed(ws, **{"auth.py": "auth\n", "main.py": "main\n"})
    point = ws.head
    child = ws.fork("child", paths=["auth.py"])
    try:
        child.files.write("/workspace/auth.py", "refactored\n")
        child.files.write("/workspace/notes/why.md", "and here is why\n")
        child.index.commit("work")

        d = ws.diff(point, child.index.head)
        # the SEED, not the delegate's grown view: the note it made is
        # exactly the change the caller has not seen before
        assert d.seed == ("/workspace/auth.py",)
        assert d.in_seed == frozenset({"/workspace/auth.py"})
        assert d.elsewhere == frozenset({"/workspace/notes/why.md"})
    finally:
        child.close()


def test_a_full_view_diff_groups_nothing(ws):
    _seed(ws, **{"a.txt": "one\n"})
    point = ws.head
    ws.files.write("/workspace/a.txt", "two\n")
    d = ws.diff(point, ws.head)
    assert d.seed == ()
    assert d.in_seed == d.paths and d.elsewhere == frozenset()


# -- take ----------------------------------------------------------------------


def test_take_copies_paths_from_another_session(ws):
    _seed(ws, **{"a.txt": "mine\n"})
    child = ws.fork("child")
    try:
        child.files.write("/workspace/a.txt", "theirs\n")
        child.files.write("/workspace/only_theirs.txt", "theirs\n")
        child.files.write("/workspace/not_asked_for.txt", "theirs\n")
        child.index.commit("work")

        landed = ws.checkout("child", paths=["a.txt", "only_theirs.txt"])
        assert ws.files.read("/workspace/a.txt") == b"theirs\n"
        assert ws.files.read("/workspace/only_theirs.txt") == b"theirs\n"
        assert not ws.files.exists("/workspace/not_asked_for.txt")

        info = next(iter(ws.log(limit=1))).info
        assert landed == ws.head
        assert info["tool"] == "checkout"
        assert info["taken_from"] == f"child@{child.index.head}"
        assert info["paths"] == ["/workspace/a.txt", "/workspace/only_theirs.txt"]
        # a soft reference, not ancestry: the delegate's history is not ours
        assert "child" not in str(ws.index.log())
    finally:
        child.close()


def test_take_reads_a_session_at_its_agents_head(ws):
    """The same reading merge takes, so the two cannot disagree about
    what the delegate said."""
    _seed(ws, **{"a.txt": "mine\n"})
    child = ws.fork("child")
    try:
        child.files.write("/workspace/a.txt", "committed\n")
        child.index.commit("committed")
        child.terminal("echo in-flight > /workspace/a.txt")

        ws.checkout("child", paths=["a.txt"])
        assert ws.files.read("/workspace/a.txt") == b"committed\n"
    finally:
        child.close()


def test_a_take_the_view_refuses_writes_nothing_at_all(ws):
    """A take is one operation: refusing halfway would leave the tree
    holding part of a take the caller is told did not happen."""
    _seed(ws, **{"seen.txt": "S1\n", "hidden.txt": "H1\n"})
    src = ws.fork("src")
    child = ws.fork("child", paths=["seen.txt"])
    try:
        src.files.write("/workspace/seen.txt", "S2\n")
        src.files.write("/workspace/hidden.txt", "H2\n")
        src.index.commit("theirs")

        with pytest.raises(PermissionError, match="outside this session's view"):
            child.checkout("src", paths=["seen.txt", "hidden.txt"])
        assert child.files.read("/workspace/seen.txt") == b"S1\n"
        assert not child.uncommitted
    finally:
        src.close()
        child.close()


def test_take_of_a_ref_and_of_nothing(ws):
    _seed(ws, **{"a.txt": "one\n"})
    first = ws.ref
    ws.files.write("/workspace/a.txt", "two\n")

    ws.checkout(first, paths=["a.txt"])
    assert ws.files.read("/workspace/a.txt") == b"one\n"

    with pytest.raises(Exception, match="nothing at"):
        ws.checkout(first, paths=["ghost.txt"])


def test_taking_a_directory_mirrors_that_subtree(ws):
    """A take of a directory is about the subtree, not about the files
    that happen to be in the ref: a file the ref does not hold is
    removed here, so what the delegate deleted is deleted."""
    ws.files.write("/workspace/pkg/keep.py", "keep\n")
    ws.files.write("/workspace/pkg/deleted.py", "doomed\n")
    ws.files.write("/workspace/outside.py", "mine\n")
    ws.index.commit("seed")
    child = ws.fork("child")
    try:
        child.files.fs.remove("/workspace/pkg/deleted.py")
        child.files.write("/workspace/pkg/new.py", "new\n")
        child.index.commit("work")

        landed = ws.checkout("child", paths=["pkg"])
        assert landed == ws.head
        assert not ws.files.exists("/workspace/pkg/deleted.py")
        assert ws.files.read("/workspace/pkg/new.py") == b"new\n"
        # only the named subtree: everything else is left alone
        assert ws.files.read("/workspace/outside.py") == b"mine\n"

        info = next(iter(ws.log(limit=1))).info
        assert info["paths"] == [
            "/workspace/pkg/keep.py",
            "/workspace/pkg/new.py",
        ]
        assert info["removed"] == ["/workspace/pkg/deleted.py"]
    finally:
        child.close()


def test_taking_one_file_removes_nothing(ws):
    """The mirror is the DIRECTORY's rule. A file names itself, and a
    take of it says nothing about what sits beside it."""
    ws.files.write("/workspace/pkg/a.py", "mine\n")
    ws.files.write("/workspace/pkg/beside.py", "mine\n")
    ws.index.commit("seed")
    child = ws.fork("child")
    try:
        child.files.fs.remove("/workspace/pkg/beside.py")
        child.files.write("/workspace/pkg/a.py", "theirs\n")
        child.index.commit("work")

        ws.checkout("child", paths=["pkg/a.py"])
        assert ws.files.read("/workspace/pkg/a.py") == b"theirs\n"
        assert ws.files.read("/workspace/pkg/beside.py") == b"mine\n"

        info = next(iter(ws.log(limit=1))).info
        assert info["paths"] == ["/workspace/pkg/a.py"]
        assert "removed" not in info
    finally:
        child.close()


def test_a_take_that_would_drop_a_hidden_file_writes_nothing_at_all(ws):
    """The view's write rule covers the mirror's removals: a file this
    session cannot see is one it may not delete, and the take is one
    operation, so the whole thing is refused before anything lands."""
    ws.files.write("/workspace/pkg/seen.py", "S1\n")
    ws.files.write("/workspace/pkg/hidden.py", "H1\n")
    ws.index.commit("seed")
    src = ws.fork("src")
    child = ws.fork("child", paths=["pkg/seen.py"])
    try:
        src.files.fs.remove("/workspace/pkg/hidden.py")
        src.files.write("/workspace/pkg/seen.py", "S2\n")
        src.index.commit("theirs")

        with pytest.raises(PermissionError, match="outside this session's view"):
            child.checkout("src", paths=["pkg"])
        assert child.files.read("/workspace/pkg/seen.py") == b"S1\n"
        assert not child.uncommitted
        # and the hidden file is still there for whoever can see it
        assert ws.files.read("/workspace/pkg/hidden.py") == b"H1\n"
    finally:
        src.close()
        child.close()


# -- attachments ---------------------------------------------------------------


def test_attach_mounts_another_sessions_tree_read_only(ws, store):
    _seed(ws, **{"a.txt": "mine\n"})
    other = store.open("reviewer")
    try:
        other.files.write("/workspace/review.md", "looks fine\n")
        other.commit(info={"tool": "test"})
        ref = other.ref
    finally:
        other.close()

    at = ws.files.attach(ref, "/reviews")
    assert at == str(ref)
    assert ws.files.attachments() == {"/reviews": str(ref)}
    assert ws.files.read("/reviews/review.md") == b"looks fine\n"
    assert ws.terminal("cat /reviews/review.md").stdout == "looks fine\n"
    assert "review.md" in ws.terminal("ls /reviews").stdout
    with pytest.raises(PermissionError):
        ws.files.fs.write("/reviews/review.md", b"nope")

    # not versioned, not in the session's own tree
    assert "/reviews/review.md" not in ws._provider.working_files()

    ws.files.detach("/reviews")
    assert ws.files.attachments() == {}
    assert not ws.files.exists("/reviews/review.md")


def test_attaching_does_not_move_the_session(ws, store):
    """Composing an attachment moves the working directory into the
    composition, which needs the filesystem beneath it at the root.
    The session must not notice: same cwd, same relative paths."""
    _seed(ws, **{"a.txt": "mine\n"})
    other = store.open("reviewer")
    other.files.write("/workspace/review.md", "ok\n")
    other.commit(info={"tool": "test"})
    other.close()

    ws.terminal("mkdir -p deep; cd deep")
    assert ws.terminal("pwd").stdout.strip() == "/workspace/deep"

    ws.files.attach("reviewer", "reviews")
    assert ws.terminal("pwd").stdout.strip() == "/workspace/deep"
    assert ws.terminal("cat ../a.txt").stdout == "mine\n"
    assert ws.terminal("cat ../reviews/review.md").stdout == "ok\n"

    ws.files.detach("reviews")
    assert ws.terminal("pwd").stdout.strip() == "/workspace/deep"
    assert ws.terminal("cat ../a.txt").stdout == "mine\n"


def test_a_session_that_had_a_tree_attached_reopens_at_its_root(ws, store):
    """Composing parks the filesystem underneath at the root, and on a
    backend where that filesystem OWNS the cwd key the park is a write
    that rides the next commit. The filesystem root is not somewhere a
    session was, so reopening starts at the workspace root instead."""
    from monkeyfs import VirtualFS

    _seed(ws, **{"a.txt": "mine\n"})
    other = store.open("reviewer")
    other.files.write("/workspace/review.md", "ok\n")
    other.commit(info={"tool": "test"})
    other.close()

    ws.terminal("mkdir -p deep; cd deep")
    ws.files.attach("reviewer", "reviews")
    assert ws._provider.kv.get(VirtualFS.CWD_KEY) == "/"  # the park
    ws.commit(info={"tool": "test"})

    # detaching from INSIDE the attachment has nowhere to go back to,
    # so it lands on the workspace root rather than the filesystem's
    ws.terminal("cd /workspace/reviews")
    ws.files.detach("reviews")
    assert ws.terminal("pwd").stdout.strip() == "/workspace"
    ws.close()

    reopened = store.open("main")
    try:
        assert reopened.terminal("pwd").stdout.strip() == "/workspace"
        assert reopened.terminal("cat a.txt").stdout == "mine\n"
    finally:
        reopened.close()


def test_attach_by_session_name_and_its_refusals(ws, store):
    _seed(ws, **{"a.txt": "mine\n"})
    other = store.open("reviewer")
    other.files.write("/workspace/review.md", "ok\n")
    other.commit(info={"tool": "test"})
    other.close()

    ws.files.attach("reviewer", "/reviews")
    with pytest.raises(ValueError, match="already taken"):
        ws.files.attach("reviewer", "/reviews")
    with pytest.raises(NotSupportedError, match="accepts no writes"):
        ws.files.attach("reviewer", "/other", readonly=False)
    with pytest.raises(ValueError, match="nothing attached"):
        ws.files.detach("/nowhere")


def test_attachments_do_not_travel_and_close_with_the_session(ws, store):
    _seed(ws, **{"a.txt": "mine\n"})
    other = store.open("reviewer")
    other.files.write("/workspace/review.md", "ok\n")
    other.commit(info={"tool": "test"})
    other.close()

    ws.files.attach("reviewer", "/reviews")
    child = ws.fork("child")
    try:
        assert child.files.attachments() == {}
        assert not child.files.exists("/reviews/review.md")
    finally:
        child.close()


# -- Store.fork ----------------------------------------------------------------


def test_store_fork_mirrors_the_workspace_verb(ws, store):
    _seed(ws, **{"a.py": "a\n", "b.py": "b\n"})
    ws.close()
    child = store.fork("main", "child", inherit="fresh", paths=["a.py"])
    try:
        assert child.session == "child"
        assert child.files.list("/workspace") == ["/workspace/a.py"]
        assert "main" in store.sessions() and "child" in store.sessions()
    finally:
        child.close()


# -- the terminal spellings ----------------------------------------------------


def test_ws_git_branch_lists_and_forks(ws, store):
    _seed(ws, **{"a.py": "a\n", "b.py": "b\n"})

    r = ws.terminal("ws-git branch")
    assert r.stdout == "* main\n"

    assert ws.terminal("ws-git branch worker").stdout == ""  # silent, like git
    assert ws.terminal("ws-git branch").stdout == "* main\n  worker\n"

    # --paths takes the run of paths that follows it; a flag after them
    # is still a flag
    r = ws.terminal("ws-git branch narrow --paths a.py --fresh")
    assert r.exit_code == 0
    narrow = store.open("narrow")
    try:
        assert narrow.files.list("/workspace") == ["/workspace/a.py"]
        assert narrow._view == ("/workspace/a.py",)
    finally:
        narrow.close()

    assert ws.terminal("ws-git branch nope --paths").exit_code == 2

    assert ws.terminal("ws-git branch worker").exit_code == 1  # already exists
    assert ws.terminal("ws-git branch x --nope").exit_code == 2


def test_ws_git_merge_reports_what_it_took(ws, store):
    _seed(ws, **{"shared": "one\ntwo\n"})
    ws.terminal("ws-git branch worker")
    worker = store.open("worker")
    try:
        worker.files.write("/workspace/shared", "one\nWORKER\n")
        worker.files.write("/workspace/new.txt", "new\n")
        worker.index.commit("work")
    finally:
        worker.close()

    r = ws.terminal("ws-git merge worker")
    assert r.exit_code == 0
    assert "Auto-merging new.txt" in r.stdout
    assert "] merge worker (" in r.stdout
    assert ws.files.read("/workspace/new.txt") == b"new\n"

    assert ws.terminal("ws-git merge").exit_code == 2
    assert ws.terminal("ws-git merge nosuch").exit_code == 1


def test_ws_git_merge_says_what_to_do_about_conflicts(ws, store):
    _seed(ws, shared="one\ntwo\nthree\n")
    ws.terminal("ws-git branch worker")
    worker = store.open("worker")
    try:
        worker.files.write("/workspace/shared", "one\nWORKER\nthree\n")
        worker.index.commit("work")
    finally:
        worker.close()
    ws.files.write("/workspace/shared", "one\nMINE\nthree\n")
    ws.index.commit("mine")

    r = ws.terminal("ws-git merge worker")
    assert r.exit_code == 1
    assert "CONFLICT (content): Merge conflict in shared" in r.stdout
    assert "fix them and commit" in r.stderr
    assert "UU shared" in ws.terminal("ws-git status").stdout


def test_ws_git_log_and_diff_read_another_session(ws, store):
    _seed(ws, **{"auth.py": "auth\n", "main.py": "main\n"})
    ws.terminal("ws-git branch worker --paths auth.py")
    worker = store.open("worker")
    try:
        worker.files.write("/workspace/auth.py", "refactored\n")
        worker.files.write("/workspace/notes.md", "collateral\n")
        worker.index.commit("their work")
    finally:
        worker.close()

    # the delegate's log is its OWN: a branch carries the workspace,
    # not the parent's commit graph, and the parent's is still where it
    # was
    log = ws.terminal("ws-git log worker").stdout.splitlines()
    assert [line.split(" ", 1)[1] for line in log] == ["their work"]
    assert [
        line.split(" ", 1)[1] for line in ws.terminal("ws-git log").stdout.splitlines()
    ] == ["seed"]

    out = ws.terminal("ws-git diff worker").stdout
    assert "# 1 path(s) in worker's seed" in out
    assert "# 1 path(s) elsewhere" in out
    assert out.index("auth.py") < out.index("collateral")

    assert ws.terminal("ws-git diff worker --check").exit_code == 2
    assert ws.terminal("ws-git log nosuch").exit_code == 1

    # a word that is neither a session nor anything in the tree is
    # refused: silence here means "no differences", and a mistyped
    # delegate name must not earn it
    r = ws.terminal("ws-git diff wrker")
    assert r.exit_code == 1
    assert "ambiguous argument 'wrker'" in r.stderr


def test_diff_takes_a_directory_pathspec(ws):
    _seed(ws, **{"sub/a.py": "a\n", "b.py": "b\n"})
    ws.files.write("/workspace/sub/a.py", "edited\n")
    ws.files.write("/workspace/b.py", "edited\n")

    out = ws.terminal("ws-git diff sub").stdout
    assert "a/sub/a.py" in out and "b.py b/b.py" not in out
    assert ws.terminal("ws-git diff sub/nope.py").exit_code == 1


def test_ws_git_checkout_takes_paths_from_another_session(ws, store):
    _seed(ws, **{"a.py": "mine\n"})
    ws.terminal("ws-git branch worker")
    worker = store.open("worker")
    try:
        worker.files.write("/workspace/a.py", "theirs\n")
        worker.index.commit("work")
    finally:
        worker.close()

    r = ws.terminal("ws-git checkout worker -- a.py")
    assert r.exit_code == 0
    assert r.stdout == "Updated 1 path from worker\n"
    assert ws.files.read("/workspace/a.py") == b"theirs\n"
    assert ws.terminal("ws-git status").stdout == " M a.py\n"


def test_ws_git_checkout_takes_paths_at_a_short_id(ws, store):
    """A log line prints seven characters of a commit id, and the take
    accepts exactly what it printed."""
    _seed(ws, **{"a.py": "mine\n"})
    ws.terminal("ws-git branch worker")
    worker = store.open("worker")
    try:
        register_wsgit(worker)
        worker.files.write("/workspace/a.py", "theirs\n")
        worker.index.commit("work")
        short = worker.terminal("ws-git log").stdout.split()[0]
    finally:
        worker.close()

    r = ws.terminal(f"ws-git checkout worker@{short} -- a.py")
    assert r.exit_code == 0, r.stderr
    assert r.stdout == f"Updated 1 path from worker@{short}\n"
    assert ws.files.read("/workspace/a.py") == b"theirs\n"


def test_ws_git_take_of_an_unknown_short_id_says_so(ws, store):
    _seed(ws, **{"a.py": "mine\n"})
    ws.terminal("ws-git branch worker")

    r = ws.terminal("ws-git checkout worker@0123456 -- a.py")
    assert r.exit_code == 1
    assert "0123456" in r.stderr


def test_a_narrowed_session_sees_its_view_through_its_own_verbs(ws, store):
    _seed(ws, **{"a.py": "a\n", "b.py": "b\n"})
    ws.terminal("ws-git branch worker --paths a.py")
    worker = store.open("worker")
    try:
        register_wsgit(worker)
        worker.terminal("echo done > /workspace/a.py")
        assert worker.terminal("ws-git status").stdout == "view: a.py\n M a.py\n"
        r = worker.terminal("ws-git commit -m done")
        assert r.exit_code == 0 and "(1 file)" in r.stdout
    finally:
        worker.close()


def test_a_workspace_with_no_store_cannot_read_its_neighbours(tmp_path):
    from nontainer import Workspace
    from nontainer.providers import KvgitProvider

    w = Workspace(KvgitProvider.open(None, session="lonely"))
    register_wsgit(w)
    try:
        w.files.write("/workspace/a.txt", "a\n")
        w.index.commit("seed")
        assert w.terminal("ws-git branch").stdout == "* lonely\n"
        w.terminal("ws-git branch worker")
        r = w.terminal("ws-git log worker")
        assert r.exit_code == 1
        assert "not opened from a store" in r.stderr
        # diff still works: the substrate knows its own branches
        assert w.terminal("ws-git diff worker").exit_code == 0
    finally:
        w.close()


def test_merge_refuses_where_the_substrate_cannot(store, tmp_path):
    """The facade refusal, in the terminal's words."""
    from nontainer import Workspace
    from nontainer.providers.dir import DirProvider

    w = Workspace(DirProvider(tmp_path / "plain", session="plain"))
    try:
        with pytest.raises(NotSupportedError):
            w.merge("other")
        with pytest.raises((NotSupportedError, WorkspaceError)):
            w.fork("child")
    finally:
        w.close()


# -- links cannot be drawn around the view -------------------------------------


@pytest.fixture
def linked(tmp_path):
    """A ViewFS over a filesystem that has real symlinks.

    monkeyfs's ``VirtualFS`` (the kvgit backend) refuses symlinks
    outright, so the rule would go untested there — but ``ViewFS`` is
    generic and the ``dir`` backend's ``IsolatedFS`` has them.
    """
    from monkeyfs import IsolatedFS

    from nontainer.views import ViewFS

    base = IsolatedFS(str(tmp_path / "tree"))
    base.makedirs("seen", exist_ok=True)
    base.makedirs("hidden", exist_ok=True)
    base.write("seen/ok.txt", b"ok")
    base.write("hidden/secret.txt", b"S3CR3T")
    return base, ViewFS(base, ("/seen",))


def test_an_existing_link_out_of_the_view_reads_as_absent(linked):
    """The link was there before the view was drawn — following it is
    how you read what the view was drawn to keep out."""
    base, view = linked
    base.symlink("/hidden/secret.txt", "/seen/alias.txt")

    assert view.read("/seen/ok.txt") == b"ok"
    assert not view.exists("/seen/alias.txt")
    assert not view.isfile("/seen/alias.txt")
    assert view.list("/seen") == ["ok.txt"]
    assert view.glob("/seen/*.txt") == ["/seen/ok.txt"]
    for call in (
        lambda: view.read("/seen/alias.txt"),
        lambda: view.open("/seen/alias.txt", "rb"),
        lambda: view.stat("/seen/alias.txt"),
        lambda: view.getsize("/seen/alias.txt"),
        lambda: view.readlink("/seen/alias.txt"),
        lambda: view.realpath("/seen/alias.txt"),
    ):
        with pytest.raises(FileNotFoundError):
            call()
    with pytest.raises(PermissionError, match="outside this session's view"):
        view.write("/seen/alias.txt", b"through the alias")
    assert base.read("/hidden/secret.txt") == b"S3CR3T"


def test_a_link_to_a_hidden_file_cannot_be_made(linked):
    base, view = linked
    for call in (
        lambda: view.symlink("/hidden/secret.txt", "/seen/abs.txt"),
        lambda: view.symlink("../hidden/secret.txt", "/seen/rel.txt"),
        lambda: view.link("/hidden/secret.txt", "/seen/hard.txt"),
    ):
        with pytest.raises(PermissionError, match="outside this session's view"):
            call()
    # refused on the way out, so the destination never joined the view
    assert view.view == ("/seen",)
    assert not base.exists("/seen/abs.txt")

    # a link INSIDE the view is ordinary
    view.symlink("/seen/ok.txt", "/seen/fine.txt")
    assert view.read("/seen/fine.txt") == b"ok"


def test_a_linked_directory_is_not_a_way_around_the_view(linked):
    """A link in the MIDDLE of a path leads out of the view exactly as
    a link at the end does, so the resolved path is what every access
    is checked against — reads, writes and listings alike."""
    base, view = linked
    base.symlink("/hidden", "/seen/out")

    assert not view.exists("/seen/out/secret.txt")
    assert not view.isdir("/seen/out")
    for call in (
        lambda: view.read("/seen/out/secret.txt"),
        lambda: view.open("/seen/out/secret.txt", "rb"),
        lambda: view.stat("/seen/out/secret.txt"),
        lambda: view.list("/seen/out"),
        lambda: view.list("/seen/out", recursive=True),
    ):
        with pytest.raises(FileNotFoundError):
            call()
    assert view.list("/seen") == ["ok.txt"]
    assert view.list("/seen", recursive=True) == ["ok.txt"]

    for call in (
        lambda: view.write("/seen/out/secret.txt", b"through the directory"),
        lambda: view.write("/seen/out/planted.txt", b"planted"),
    ):
        with pytest.raises(PermissionError, match="outside this session's view"):
            call()
    assert base.read("/hidden/secret.txt") == b"S3CR3T"
    assert not base.exists("/hidden/planted.txt")


def test_a_link_that_stays_inside_the_view_is_ordinary(linked):
    """Resolving every path must not cost the view its own links: one
    that lands inside is a name like any other, readable and
    writable."""
    base, view = linked
    base.makedirs("seen/inner", exist_ok=True)
    base.write("seen/inner/note.md", b"note")
    base.symlink("/seen/inner", "/seen/near")

    assert view.read("/seen/near/note.md") == b"note"
    assert view.list("/seen/near") == ["note.md"]
    assert "near" in view.list("/seen")
    view.write("/seen/near/more.md", b"more")
    assert base.read("/seen/inner/more.md") == b"more"


# -- one root for a lineage ----------------------------------------------------


def test_store_fork_opens_the_source_at_the_childs_root(store):
    """A lineage shares one root: a view normalized against some other
    one would name paths the child cannot see."""
    ws = store.open("origin", root="/data")
    ws.files.write("/data/a.py", "a\n")
    ws.files.write("/data/b.py", "b\n")
    ws.index.commit("seed")
    ws.close()

    child = store.fork("origin", "child", root="/data", paths=["a.py"])
    try:
        assert child._view == ("/data/a.py",)
        assert child.files.list("/data") == ["/data/a.py"]
        assert not child.files.exists("/data/b.py")
    finally:
        child.close()


def test_log_of_another_session_opens_it_at_the_lineages_root(store):
    """Reading another session's log must not write to it. Opening a
    session at a root it does not use makes that directory and can
    commit the making of it, so the root a verb opens with is this
    session's."""
    ws = store.open("origin", root="/data")
    register_wsgit(ws)
    try:
        ws.files.write("/data/a.py", "a\n")
        ws.index.commit("seed")
        child = ws.fork("child")
        child.files.write("/data/b.py", "b\n")
        child.index.commit("work")
        before = len(list(child.log()))
        child.close()

        r = ws.terminal("ws-git log child")
        assert r.exit_code == 0
        assert "work" in r.stdout

        again = store.open("child", root="/data")
        try:
            assert len(list(again.log())) == before
            assert not again.files.exists("/workspace")
        finally:
            again.close()
    finally:
        ws.close()


def test_attach_reads_the_lineages_root_and_the_whole_branch(store):
    """Two things at once: a commit holds its files at the root the
    session used, and what is attached is the source's whole branch —
    a delegate with a narrow view is exactly the one worth attaching."""
    ws = store.open("origin", root="/data")
    ws.files.write("/data/a.py", "a\n")
    ws.files.write("/data/b.py", "b\n")
    ws.index.commit("seed")
    child = ws.fork("child", paths=["a.py"])
    child.files.write("/data/a.py", "refactored\n")
    child.index.commit("work")
    child.close()

    try:
        ws.files.attach("child", "reviews")
        listed = ws.files.list("/data/reviews")
        assert listed == ["/data/reviews/a.py", "/data/reviews/b.py"]
        assert ws.files.read("/data/reviews/a.py") == b"refactored\n"
        # outside the delegate's own view, and still readable here
        assert ws.files.read("/data/reviews/b.py") == b"b\n"
        ws.files.detach("reviews")
    finally:
        ws.close()


def test_attach_takes_an_explicit_root_for_a_session_from_elsewhere(store):
    """The default is this session's root, since a lineage shares one;
    a session from somewhere else needs its own named."""
    other = store.open("elsewhere", root="/srv")
    other.files.write("/srv/note.md", "hello\n")
    other.commit(info={"tool": "test"})
    other.close()

    ws = store.open("main")  # the default /workspace root
    try:
        ws.files.attach("elsewhere", "wrong")
        assert ws.files.list("/workspace/wrong") == []  # nothing at /workspace there
        ws.files.detach("wrong")

        ws.files.attach("elsewhere", "right", root="/srv")
        assert ws.files.read("/workspace/right/note.md") == b"hello\n"
    finally:
        ws.close()


# -- the path/session collision ------------------------------------------------


def test_diff_reads_a_word_that_is_both_a_path_and_a_session_as_the_path(ws, store):
    """The pathspec reading is the one that still works when the two
    collide: name the file, not the branch."""
    _seed(ws, **{"worker": "one\n"})
    ws.terminal("ws-git branch worker")
    assert "worker" in store.sessions()

    ws.files.write("/workspace/worker", "edited\n")
    out = ws.terminal("ws-git diff worker").stdout
    assert out.startswith("diff --git a/worker b/worker")
    assert "# " not in out  # not the grouped session diff

    # with no such file here, the same word is the session
    ws.index.commit("edited")
    ws.terminal("rm worker")
    ws.index.commit("gone")
    assert ws.terminal("ws-git diff worker").stdout.startswith("diff --git a/worker")


# -- the sparse checkout -------------------------------------------------------


def _narrowed(store, ws, *paths: str):
    """A worker forked with a view, opened with ws-git registered."""
    ws.terminal("ws-git branch worker --paths " + " ".join(paths))
    worker = store.open("worker")
    register_wsgit(worker)
    return worker


def test_sparse_checkout_lists_the_seed(ws, store):
    """A narrowed session can ask what its view is, instead of finding
    out by hitting the write rule."""
    _seed(ws, **{"a.py": "a\n", "b.py": "b\n"})
    ws.files.write("/workspace/pkg/mod.py", "m\n")
    ws.index.commit("pkg")

    worker = _narrowed(store, ws, "a.py", "pkg")
    try:
        assert worker.terminal("ws-git sparse-checkout list").stdout == "a.py\npkg/\n"
        # git's default subcommand: the bare verb lists
        assert worker.terminal("ws-git sparse-checkout").stdout == "a.py\npkg/\n"
    finally:
        worker.close()


def test_status_leads_with_the_view(ws, store):
    """One line, before the rows, so a reader sees what it can see
    before it reads what changed."""
    _seed(ws, **{"a.py": "a\n", "b.py": "b\n"})

    worker = _narrowed(store, ws, "a.py")
    try:
        worker.terminal("echo done > /workspace/a.py")
        assert worker.terminal("ws-git status").stdout.startswith("view: a.py\n")
        assert (
            worker.terminal("ws-git status --porcelain").stdout
            == worker.terminal("ws-git status").stdout
        )
    finally:
        worker.close()


def test_a_full_session_has_no_view_to_print(ws):
    _seed(ws, **{"a.py": "a\n"})
    assert ws.terminal("ws-git sparse-checkout list").stdout == "(full)\n"

    ws.files.write("/workspace/a.py", "edited\n")
    assert ws.terminal("ws-git status").stdout == " M a.py\n"


def test_sparse_checkout_takes_no_other_subcommand(ws):
    """The view is a fork-time decision, so the verb that reads it does
    not pretend it can be set here."""
    _seed(ws, **{"a.py": "a\n"})
    r = ws.terminal("ws-git sparse-checkout set a.py")
    assert r.exit_code == 2
    assert r.stderr.startswith(
        "ws-git: sparse-checkout takes no 'set' (list is all there is): a "
        "view is given when the session is forked (ws-git branch <name> "
        "--paths <paths>) and cannot be changed here."
    )


# -- short commit ids ----------------------------------------------------------


def test_store_resolve_takes_a_short_commit_id(ws, store):
    """Every ref spelling nontainer prints is one it accepts back: the
    seven characters a log line shows name the commit they came from."""
    _seed(ws, **{"a.txt": "one\n"})
    full = ws.head
    ws.files.write("/workspace/a.txt", "two\n")
    ws.commit()

    frozen = store.resolve(f"main@{full[:7]}")
    try:
        assert frozen.files.read("/workspace/a.txt") == b"one\n"
        assert str(frozen.ref) == f"main@{full}"
    finally:
        frozen.close()


def test_a_take_reads_a_short_commit_id(ws):
    """``ws.checkout(ref, paths=)`` takes the short spelling too, of
    this session and of another."""
    _seed(ws, **{"a.txt": "one\n"})
    mine = ws.head
    child = ws.fork("child")
    try:
        child.files.write("/workspace/b.txt", "theirs\n")
        child.index.commit("work")
        theirs = child.index.head

        ws.files.write("/workspace/a.txt", "two\n")
        ws.checkout(f"main@{mine[:7]}", paths=["a.txt"])
        assert ws.files.read("/workspace/a.txt") == b"one\n"

        ws.checkout(f"child@{theirs[:7]}", paths=["b.txt"])
        assert ws.files.read("/workspace/b.txt") == b"theirs\n"
        assert next(iter(ws.log(limit=1))).info["taken_from"] == f"child@{theirs}"
    finally:
        child.close()


def test_a_whole_tree_checkout_reads_a_short_commit_id(ws):
    _seed(ws, **{"a.txt": "one\n"})
    first = ws.head
    ws.files.write("/workspace/a.txt", "two\n")
    ws.commit()

    ws.checkout(first[:7])
    assert ws.files.read("/workspace/a.txt") == b"one\n"


def test_attach_takes_a_short_commit_id(ws, store):
    _seed(ws, **{"a.txt": "one\n"})
    child = ws.fork("child")
    try:
        child.files.write("/workspace/note.md", "theirs\n")
        child.index.commit("note")
        short = child.index.head[:7]
    finally:
        child.close()

    ws.files.attach(f"child@{short}", "/workspace/peek")
    assert ws.files.read("/workspace/peek/note.md") == b"theirs\n"
    assert ws.files.attachments() == {
        "/workspace/peek": f"child@{store.open('child').index.head}"
    }


def test_a_short_id_of_a_framework_commit_is_a_ref(ws, store):
    """A worktree reads a state and takes no place in anyone's graph,
    so every commit of the session counts — the framework's included."""
    _seed(ws, **{"a.txt": "one\n"})
    ws.files.write("/workspace/a.txt", "two\n")
    framework = ws.commit()
    assert framework not in [e.id for e in ws.index.log()]

    frozen = store.resolve(f"main@{framework[:7]}")
    try:
        assert frozen.files.read("/workspace/a.txt") == b"two\n"
    finally:
        frozen.close()


def test_a_short_id_nothing_matches_reads_like_a_whole_one(ws, store):
    _seed(ws, **{"a.txt": "one\n"})
    with pytest.raises(CommitNotFoundError):
        store.resolve("main@0123456")
    with pytest.raises(CommitNotFoundError):
        store.resolve(f"main@{'0' * 40}")
