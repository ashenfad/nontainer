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

    log = ws.terminal("ws-git log worker").stdout.splitlines()
    assert [line.split(" ", 1)[1] for line in log] == ["their work", "seed"]

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
