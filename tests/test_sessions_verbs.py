"""The sessions verbs: fork with a view, take, attach, and the
terminal spellings of all of them.

``test_sessions_corpus.py`` asks whether the verbs compose into the
scenarios the design is for. This file asks what each verb does on its
own — the write rule, the view record, the refusals, the output shape
of ``ws-git branch`` / ``merge`` / ``checkout -- <paths>`` /
``log <session>`` / ``diff <session>``.
"""

import pytest

from nontainer import NotSupportedError, Store, WorkspaceError
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


def test_take_of_a_ref_and_of_nothing(ws):
    _seed(ws, **{"a.txt": "one\n"})
    first = ws.ref
    ws.files.write("/workspace/a.txt", "two\n")

    ws.checkout(first, paths=["a.txt"])
    assert ws.files.read("/workspace/a.txt") == b"one\n"

    with pytest.raises(Exception, match="nothing at"):
        ws.checkout(first, paths=["ghost.txt"])


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

    log = ws.terminal("ws-git log worker").stdout.splitlines()
    assert [line.split(" ", 1)[1] for line in log] == ["their work", "seed"]

    out = ws.terminal("ws-git diff worker").stdout
    assert "# 1 path(s) in worker's seed" in out
    assert "# 1 path(s) elsewhere" in out
    assert out.index("auth.py") < out.index("collateral")

    assert ws.terminal("ws-git diff worker --check").exit_code == 2
    assert ws.terminal("ws-git log nosuch").exit_code == 1


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


def test_a_narrowed_session_sees_its_view_through_its_own_verbs(ws, store):
    _seed(ws, **{"a.py": "a\n", "b.py": "b\n"})
    ws.terminal("ws-git branch worker --paths a.py")
    worker = store.open("worker")
    try:
        register_wsgit(worker)
        worker.terminal("echo done > /workspace/a.py")
        assert worker.terminal("ws-git status").stdout == " M a.py\n"
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
