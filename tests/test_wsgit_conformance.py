"""ws-git cross-rung conformance: local vs dud-subprocess.

The same scripts through ``ws.terminal`` must read identically on both
rungs — the verbs' contract, not the rung's dialect. This is the
discriminating set: same-script write+verb flows pass trivially where
the command and the files share one substrate (local) and pass on the
dud rung only via sync-on-verb, plus the stage-first composition
golden both rungs must agree on byte-for-byte.

Each test gets a fresh session per rung, so assertions stay exact
regardless of order. Shell features are restricted to what both shells
offer (heredocs, ``;`` — termish has no ``printf``).
"""

import re

import pytest

from nontainer import Store
from nontainer.wsgit import register_wsgit

SHORT = r"[0-9a-f]{7}"


@pytest.fixture(params=["local", "dud"])
def store(request, tmp_path):
    """One store per rung per test, with the rung bound as a FACTORY so
    a session forked by a verb runs where its parent does."""
    param = request.param
    factory = None
    if param == "dud":
        pytest.importorskip("dud")
        from nontainer.executor_dud import DudExecutor

        def factory():  # noqa: ANN202 - a fixture-local builder
            return DudExecutor(backend="subprocess")

    s = Store(tmp_path / "store")
    s.executor_factory = factory  # read by the ws fixture below
    s.rung = param
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def ws(request, store):
    """One fresh ws-git workspace per rung per test."""
    name = re.sub(r"[^A-Za-z0-9_.-]", "-", request.node.name)
    kw = (
        {"executor_factory": store.executor_factory}
        if store.executor_factory is not None
        else {}
    )
    w = store.open(f"wsgit-conf-{store.rung}-{name}", **kw)
    register_wsgit(w)
    try:
        yield w
    finally:
        w.close()


def test_same_script_write_then_stage(ws):
    """Writes earlier in the SAME script are visible to the verb — the
    heredoc pattern agents favor."""
    r = ws.terminal("cat > same.txt <<'EOF'\nhello\nEOF\nws-git stage same.txt")
    assert r.exit_code == 0
    assert r.stdout == ""
    assert ws.terminal("ws-git diff --cached same.txt").stdout == (
        "diff --git a/same.txt b/same.txt\n"
        "--- a/same.txt\n"
        "+++ b/same.txt\n"
        "@@ -0,0 +1 @@\n"
        "+hello\n"
    )


def test_same_script_write_stage_commit(ws):
    """A full write → stage → commit flow inside one script lands one
    agent commit, with nothing left staged or unstaged."""
    r = ws.terminal(
        "cat > flow.txt <<'EOF'\nflow\nEOF\nws-git stage flow.txt\nws-git commit -m flow"
    )
    assert r.exit_code == 0
    # One transcript: stage is silent, then the commit line.
    assert re.fullmatch(
        rf"\[wsgit-conf-[a-z]+-[a-z0-9_.-]+ {SHORT}\] flow \(1 file\)\n",
        r.stdout,
    )
    assert [e.info["message"] for e in ws.index.log()] == ["flow"]
    assert ws.terminal("ws-git status").stdout == ""


def test_same_script_status_sees_fresh_write(ws):
    """Reads need the sync too: status in the same script names the
    just-written file instead of reporting a clean tree."""
    r = ws.terminal("cat > fresh.txt <<'EOF'\nfresh\nEOF\nws-git status")
    assert r.exit_code == 0
    assert r.stdout == " M fresh.txt\n"


def test_stage_first_composition(ws):
    """Stage-first ordering composes across the boundary identically:
    staged content (including a later edit) diffs cached, and the
    commit snapshots exactly the staged set."""
    ws.files.fs.write("/workspace/a.txt", b"one\n")
    ws.files.fs.write("/workspace/b.txt", b"two\n")

    r = ws.terminal("ws-git stage a.txt b.txt")
    assert r.exit_code == 0
    assert r.stdout == ""

    # The framework commits the edit for durability; the composition
    # is measured against the agent's own head, so it does not notice.
    ws.terminal("echo second >> a.txt")
    assert not ws.uncommitted

    assert ws.terminal("ws-git status").stdout == "M  a.txt\nM  b.txt\n"
    assert ws.terminal("ws-git diff").stdout == ""
    assert ws.terminal("ws-git diff --cached").stdout == (
        "diff --git a/a.txt b/a.txt\n"
        "--- a/a.txt\n"
        "+++ b/a.txt\n"
        "@@ -0,0 +1,2 @@\n"
        "+one\n"
        "+second\n"
        "diff --git a/b.txt b/b.txt\n"
        "--- a/b.txt\n"
        "+++ b/b.txt\n"
        "@@ -0,0 +1 @@\n"
        "+two\n"
    )

    r = ws.terminal('ws-git commit -m "compose a"')
    assert r.exit_code == 0
    assert re.fullmatch(rf"\[.+ {SHORT}\] compose a \(2 files\)\n", r.stdout)

    assert [e.info["message"] for e in ws.index.log()] == ["compose a"]
    assert ws.terminal("ws-git status").stdout == ""


def test_partial_commit_across_the_rungs(ws):
    """The fiction's own property, on every rung: what the agent left
    out of its commit is out of the commit and still in the tree."""
    ws.terminal("cat > a.txt <<'EOF'\nbase\nEOF\ncat > b.txt <<'EOF'\nbase\nEOF")
    ws.terminal("ws-git commit -m base")
    ws.terminal("ws-git stage a.txt")
    ws.terminal("cat > a.txt <<'EOF'\nedited\nEOF\ncat > b.txt <<'EOF'\nedited\nEOF")

    r = ws.terminal("ws-git commit -m 'just a'")
    assert r.exit_code == 0, r.stdout

    assert ws.terminal("ws-git status").stdout == " M b.txt\n"
    assert ws.terminal("cat b.txt").stdout == "edited\n"
    tree = ws._provider.files_at(ws.index.head)
    assert tree["/workspace/a.txt"] == b"edited\n"
    assert tree["/workspace/b.txt"] == b"base\n"


def test_checkout_restores_the_tree_across_the_rungs(ws):
    """A verb that REWRITES worktree files has to reach the guest too:
    the restore is pushed back on the next call, not harvested away."""
    ws.terminal("cat > a.txt <<'EOF'\none\nEOF")
    ws.terminal("ws-git commit -m first")
    first = ws.terminal("ws-git log").stdout.split()[0]
    ws.terminal("cat > a.txt <<'EOF'\ntwo\nEOF\ncat > b.txt <<'EOF'\nnew\nEOF")
    ws.terminal("ws-git commit -m second")

    r = ws.terminal(f"ws-git checkout {first}")
    assert r.exit_code == 0, r.stdout

    assert ws.terminal("cat a.txt").stdout == "one\n"
    assert ws.terminal("ls b.txt").exit_code != 0
    assert ws.terminal("ws-git status").stdout == ""
    assert [e.info["message"] for e in ws.index.log()] == ["first"]


def test_log_S_across_the_rungs(ws):
    """The pickaxe reads the same wherever the writing was done: the
    commits where the string appeared and vanished, signed, and the one
    that only moved it left out."""
    ws.terminal("cat > a.txt <<'EOF'\nplain\nEOF\nws-git commit -m base")
    ws.terminal("cat > a.txt <<'EOF'\nneedle\ntail\nEOF\nws-git commit -m adds")
    ws.terminal("cat > a.txt <<'EOF'\ntail\nneedle\nEOF\nws-git commit -m moved")
    ws.terminal("cat > a.txt <<'EOF'\nplain\nEOF\nws-git commit -m drops")

    out = ws.terminal("ws-git log -S needle").stdout.splitlines()
    assert [line.split(" ", 1)[1] for line in out] == ["- drops", "+ adds"]
    for line in out:
        assert re.fullmatch(SHORT, line.split(" ", 1)[0]), line

    # the default walk is the agent's; --all is every commit the
    # session holds, so it reaches one the agent never made
    ws.files.fs.write("/workspace/b.txt", b"needle\n")
    ws.commit(info={"tool": "framework"})
    assert ws.terminal("ws-git log -S needle").stdout.splitlines() == out
    every = ws.terminal("ws-git log -S needle --all").stdout.splitlines()
    assert every[0].split(" ", 1)[1] == "+ framework"


# -- the sessions verbs, on both rungs -----------------------------------------


def test_branch_merge_and_log_across_the_rungs(ws, store):
    """Fork, work in the child on the same rung, merge back: the whole
    delegation round trip reads identically wherever code runs."""
    ws.terminal("cat > a.txt <<'EOF'\nbase\nEOF\nws-git commit -m base")

    assert ws.terminal(f"ws-git branch {ws.session}-w").stdout == ""
    listed = ws.terminal("ws-git branch").stdout.splitlines()
    assert f"* {ws.session}" in listed
    assert f"  {ws.session}-w" in listed

    worker = store.open(
        f"{ws.session}-w",
        **(
            {"executor_factory": store.executor_factory}
            if store.executor_factory is not None
            else {}
        ),
    )
    register_wsgit(worker)
    try:
        r = worker.terminal("cat > b.txt <<'EOF'\nworker\nEOF\nws-git commit -m work")
        assert r.exit_code == 0, r.stdout
    finally:
        worker.close()

    # the worker's log is the worker's: a branch carries the workspace,
    # not the parent's commit graph
    assert [
        line.split(" ", 1)[1]
        for line in ws.terminal(f"ws-git log {ws.session}-w").stdout.splitlines()
    ] == ["work"]

    r = ws.terminal(f"ws-git merge {ws.session}-w")
    assert r.exit_code == 0, (r.stdout, r.stderr)
    assert "Auto-merging b.txt" in r.stdout
    assert re.search(rf"\] merge {re.escape(ws.session)}-w \(1 file\)", r.stdout)
    assert ws.terminal("cat b.txt").stdout == "worker\n"
    assert ws.terminal("ws-git status").stdout == ""


def test_take_paths_from_a_ref_across_the_rungs(ws):
    """The take rewrites worktree files, so it has to reach the guest
    the way the whole-tree restore does."""
    ws.terminal("cat > a.txt <<'EOF'\none\nEOF\nws-git commit -m first")
    first = ws.terminal("ws-git log").stdout.split()[0]
    ws.terminal("cat > a.txt <<'EOF'\ntwo\nEOF\nws-git commit -m second")

    r = ws.terminal(f"ws-git checkout {first} -- a.txt")
    assert r.exit_code == 0, r.stdout
    assert r.stdout.startswith("Updated 1 path from ")
    assert ws.terminal("cat a.txt").stdout == "one\n"
    assert ws.terminal("ws-git status").stdout == " M a.txt\n"


def test_a_narrowed_view_is_the_same_tree_on_both_rungs(ws, store):
    """The guest is given only the seeded subtree, and the write rule
    holds there too — a path the view hides cannot be recreated by
    name, however the writing was done."""
    ws.terminal(
        "cat > seen.txt <<'EOF'\nseen\nEOF\ncat > hidden.txt <<'EOF'\nhidden\nEOF\n"
        "ws-git commit -m base"
    )
    ws.terminal(f"ws-git branch {ws.session}-n --paths seen.txt")

    child = store.open(
        f"{ws.session}-n",
        **(
            {"executor_factory": store.executor_factory}
            if store.executor_factory is not None
            else {}
        ),
    )
    register_wsgit(child)
    try:
        assert child.terminal("ls").stdout == "seen.txt\n"
        assert child.terminal("cat hidden.txt").exit_code != 0

        # the child can ask what it was given rather than finding out
        # by being refused, and status leads with the same answer
        assert child.terminal("ws-git sparse-checkout list").stdout == "seen.txt\n"
        assert child.terminal("ws-git sparse-checkout").stdout == "seen.txt\n"

        # a new path anywhere is ordinary work. The child starts at no
        # commit of its own, so what it can SEE reads as modified —
        # and what its view hides is absent here as everywhere else
        r = child.terminal("cat > note.md <<'EOF'\nnote\nEOF\nws-git status")
        assert r.exit_code == 0, r.stdout
        assert r.stdout == "view: seen.txt\n M note.md\n M seen.txt\n"
        # the seed, not the view as it stands: a file the child made
        # joined its view because it made it
        assert child.terminal("ws-git sparse-checkout list").stdout == "seen.txt\n"

        # the hidden one is refused, and the call does not land
        before = child.terminal("ws-git status").stdout
        r = child.terminal("cat > hidden.txt <<'EOF'\nsneaky\nEOF")
        assert r.exit_code != 0
        assert child.terminal("ws-git status").stdout == before
        assert ws._provider.files_at(child.head)["/workspace/hidden.txt"] == b"hidden\n"
    finally:
        child.close()


def test_a_full_session_has_no_view_across_the_rungs(ws):
    """A session that sees the whole tree says so, and its status
    carries no header to explain."""
    ws.terminal("cat > a.txt <<'EOF'\nbase\nEOF\nws-git commit -m base")

    assert ws.terminal("ws-git sparse-checkout list").stdout == "(full)\n"
    ws.terminal("cat > a.txt <<'EOF'\nedited\nEOF")
    assert ws.terminal("ws-git status").stdout == " M a.txt\n"


def test_worktree_reads_a_neighbour_across_the_rungs(ws, store):
    """Reading another session's tree is the same three verbs wherever
    code runs: the worktree is host-side state on the workspace, and
    the guest is handed the files like any other part of the tree."""
    ws.terminal("cat > a.txt <<'EOF'\nbase\nEOF\nws-git commit -m base")
    peer = f"{ws.session}-p"
    assert ws.terminal(f"ws-git branch {peer}").exit_code == 0

    worker = store.open(
        peer,
        **(
            {"executor_factory": store.executor_factory}
            if store.executor_factory is not None
            else {}
        ),
    )
    register_wsgit(worker)
    try:
        r = worker.terminal("cat > note.md <<'EOF'\npeer\nEOF\nws-git commit -m note")
        assert r.exit_code == 0, r.stdout
    finally:
        worker.close()

    assert ws.terminal("ws-git worktree list").stdout == "(none)\n"
    r = ws.terminal(f"ws-git worktree add peek {peer}")
    assert r.exit_code == 0, r.stderr
    line = rf"worktree peek: {re.escape(peer)}@{SHORT} \(read-only\)\n"
    assert re.fullmatch(line, r.stdout), r.stdout
    assert ws.terminal("ws-git worktree list").stdout == r.stdout

    # ordinary tools read it, and nothing in it is work of this session
    assert ws.terminal("cat peek/note.md").stdout == "peer\n"
    assert ws.terminal("ws-git status").stdout == "worktrees:\n" + r.stdout
    assert ws.terminal("ws-git diff").stdout == ""

    assert ws.terminal("ws-git worktree remove peek").stdout == ""
    assert ws.terminal("ws-git worktree list").stdout == "(none)\n"
    assert ws.terminal("ws-git status").stdout == ""
