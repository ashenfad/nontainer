"""Paths that are never work.

Three kinds: anything outside the workspace root (scratch under /tmp, a
library's cache), the authoring output the app runtime writes beside an
app (handler logs, test_app captures), and whatever the embedder's
``ignore=`` patterns name. They stay on their own branch, and nothing
that decides what a session's work IS sees them: ws-git status and
commits, the uncommitted check, diffs, merges, cherry-picks and
directory takes. The case that made the first two matter: a delegate
commits its scene, checks it with test_app, and answers with captures
newer than its commit; and a delegate's commit that swept up a font
cache matplotlib wrote at the filesystem root.
"""

import pytest

from nontainer import Store
from nontainer.ignore import IGNORED_DIRS, Patterns, is_ignored, why_ignored
from nontainer.sessions import Sessions
from nontainer.wsgit import register_wsgit

SHOT = "/workspace/app/screenshots/title-1.png"
LOG = "/workspace/app/logs/api.log"


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "store")
    yield s
    s.close()


@pytest.fixture
def ws(store):
    w = store.open("main")
    register_wsgit(w)
    w.files.write("/workspace/app/index.html", "<h1>v1</h1>\n")
    w.index.commit("start")
    yield w
    w.close()


def test_which_paths_are_authoring_output():
    assert is_ignored(SHOT, "/workspace")
    assert is_ignored(LOG, "/workspace")
    assert is_ignored("/workspace/app/screenshots", "/workspace")
    assert not is_ignored("/workspace/app/screenshots.html", "/workspace")
    assert not is_ignored("/workspace/app/index.html", "/workspace")
    assert is_ignored("/elsewhere/app/logs/x", "/workspace")  # outside the root
    assert is_ignored("/app/logs/x", "/")  # the flat legacy layout


def test_the_runtime_writes_where_the_rule_looks():
    """The rule names the app runtime's layout; if that layout moves,
    the rule has to move with it."""
    from nontainer.apps import dispatch

    written = {f"{dispatch.APP_DIR}/{d}" for d in dispatch.AUTHORING_DIRS}
    assert written == set(IGNORED_DIRS)


def test_ws_git_neither_lists_nor_takes_them(ws):
    ws.files.write(SHOT, b"\x89PNG")
    ws.files.write(LOG, "GET / 200\n")
    status = ws.index.status()
    assert status.staged == () and status.unstaged == ()

    ws.files.write("/workspace/app/index.html", "<h1>v2</h1>\n")
    assert ws.index.status().unstaged == ("/workspace/app/index.html",)
    with pytest.raises(ValueError, match="authoring output"):
        ws.index.stage([SHOT])
    ws.index.commit("v2")
    assert ws.index.status().unstaged == ()
    # still there for the agent that took it
    assert ws.files.read(SHOT) == b"\x89PNG"
    assert "screenshots" not in ws.terminal("ws-git status").stdout
    # nothing but captures is nothing to commit
    ws.files.write("/workspace/app/screenshots/again-2.png", b"\x89PNG")
    with pytest.raises(Exception, match="nothing to commit"):
        ws.index.commit("only captures")


def test_diffs_leave_them_out(ws):
    before = ws.head
    ws.files.write(SHOT, b"\x89PNG")
    ws.files.write("/workspace/app/index.html", "<h1>v2</h1>\n")
    ws.commit()
    diff = ws.diff(before, ws.head)
    assert diff.paths == {"/workspace/app/index.html"}
    assert ws.changed_since(before).paths == {"/workspace/app/index.html"}


def test_a_merge_keeps_this_sides_copies(store, ws):
    ws.files.write(SHOT, b"ours")
    ws.commit()  # the framework's commit: an agent's would take nothing
    other = ws.fork("other")
    try:
        other.files.write("/workspace/app/index.html", "<h1>theirs</h1>\n")
        other.files.write(SHOT, b"theirs")  # changed on their side
        other.files.write("/workspace/app/screenshots/new-2.png", b"new")  # added
        other.files.write(LOG, "their log\n")
        other.index.commit("their work")
        # a capture after the commit is not work left uncommitted
        other.files.write("/workspace/app/screenshots/late-3.png", b"late")
    finally:
        other.close()

    outcome = ws.merge("other")
    assert outcome.merged and outcome.conflicts == ()
    assert ws.files.read("/workspace/app/index.html") == b"<h1>theirs</h1>\n"
    assert ws.files.read(SHOT) == b"ours"
    assert not ws.files.exists("/workspace/app/screenshots/new-2.png")
    assert not ws.files.exists("/workspace/app/screenshots/late-3.png")
    assert not ws.files.exists(LOG)
    assert "/workspace/app/screenshots/new-2.png" not in outcome.auto_merged


def test_a_cherry_pick_leaves_them_behind(ws):
    other = ws.fork("other")
    try:
        other.files.write("/workspace/app/index.html", "<h1>picked</h1>\n")
        other.files.write(SHOT, b"theirs")
        commit = other.index.commit("work and a capture")
    finally:
        other.close()
    assert ws.cherry_pick(f"other@{commit}").merged
    assert ws.files.read("/workspace/app/index.html") == b"<h1>picked</h1>\n"
    assert not ws.files.exists(SHOT)


def test_taking_a_directory_skips_them_both_ways(ws):
    ws.files.write("/workspace/app/screenshots/mine.png", b"mine")
    ws.commit()
    other = ws.fork("other")
    try:
        other.files.remove("/workspace/app/screenshots/mine.png")
        other.files.write("/workspace/app/index.html", "<h1>taken</h1>\n")
        other.files.write(SHOT, b"theirs")
        other.index.commit("theirs")
    finally:
        other.close()

    ws.checkout("other", paths=["app"])
    assert ws.files.read("/workspace/app/index.html") == b"<h1>taken</h1>\n"
    assert not ws.files.exists(SHOT)  # not brought
    assert (
        ws.files.read("/workspace/app/screenshots/mine.png") == b"mine"
    )  # not dropped

    # named outright, it is taken, as git takes an ignored file it is told to
    ws.checkout("other", paths=[SHOT])
    assert ws.files.read(SHOT) == b"theirs"


def test_a_delegate_that_checked_its_work_after_committing_merges_cleanly(store, ws):
    """The case behind all of this: a delegate commits its scene, takes
    captures checking it, and answers. Its answer is its work, its
    changed paths name the scene alone, and the merge takes the scene
    without the captures."""

    class Checked:
        def run(self, session, task, *, budget=None):
            child = store.open(session)
            try:
                child.files.write("/workspace/app/intro.html", "<p>intro</p>\n")
                child.index.commit("the intro scene")
                child.files.write(SHOT, b"\x89PNG")
                child.files.write(LOG, "GET /intro.html 200\n")
            finally:
                child.close()
            return "built and checked the intro"

    with Sessions(ws, Checked()) as sessions:
        answer = sessions.ask("build the intro", wait=True)

    assert answer.uncommitted is False
    assert answer.changed["seed"] == ("/workspace/app/intro.html",)
    assert ws.merge(answer.branch).merged
    assert ws.files.read("/workspace/app/intro.html") == b"<p>intro</p>\n"
    assert not ws.files.exists(SHOT)
    assert not ws.files.exists(LOG)


# -- outside the root ------------------------------------------------------------


def test_anything_outside_the_root_is_never_work(ws):
    ws.files.write("/workspace/notes.md", "work\n")
    ws.terminal(
        "echo scratch > /tmp/t.mjs; mkdir -p /.matplotlib; echo cache > /.matplotlib/fontlist.json"
    )
    status = ws.terminal("ws-git status --porcelain").stdout
    assert status == " A notes.md\n"
    ws.terminal("ws-git add -A && ws-git commit -m work")
    assert ws.terminal("ws-git status --porcelain").stdout == ""
    # still there to read back
    assert ws.terminal("cat /tmp/t.mjs").stdout == "scratch\n"


def test_the_reason_is_named():
    assert (
        why_ignored("/tmp/x", "/workspace")
        == "it is outside the workspace root (/workspace)"
    )
    assert "authoring output" in why_ignored(LOG, "/workspace")
    assert why_ignored("/workspace/a.py", "/workspace") is None
    assert why_ignored("/workspace", "/workspace") is None
    assert why_ignored("/tmp/x", "/") is None  # a root at / leaves nothing outside


# -- the embedder's patterns -----------------------------------------------------


@pytest.mark.parametrize(
    "pattern, ignored, kept",
    [
        (
            "__pycache__/",
            ["__pycache__/m.pyc", "pkg/__pycache__/m.pyc"],
            ["__pycache__.txt"],
        ),
        ("*.log", ["a.log", "deep/b.log"], ["a.log.txt", "logs/x"]),
        ("/build/", ["build/out.js"], ["src/build/out.js"]),
        ("data/raw", ["data/raw/x.csv", "data/raw"], ["other/data/raw/x.csv"]),
        ("docs/**/*.tmp", ["docs/a.tmp", "docs/x/y/b.tmp"], ["a.tmp"]),
        ("cache?", ["cache1/x", "cacheA"], ["cache12/x"]),
        ("[ab].txt", ["a.txt", "x/b.txt"], ["c.txt"]),
    ],
)
def test_patterns_follow_gitignore(pattern, ignored, kept):
    p = Patterns([pattern])
    for rel in ignored:
        assert p.match(rel), (pattern, rel)
    for rel in kept:
        assert not p.match(rel), (pattern, rel)


def test_a_directory_pattern_does_not_match_a_file_of_that_name():
    assert not Patterns(["build/"]).match("build")
    assert Patterns(["build"]).match("build")


def test_blank_lines_and_comments_are_skipped_and_negation_is_refused():
    assert Patterns(["", "# a comment", "*.log"]).source == ("*.log",)
    assert Patterns("*.log\n\n# c\n*.tmp").source == ("*.log", "*.tmp")
    with pytest.raises(ValueError, match="negation"):
        Patterns(["*.log", "!keep.log"])


def test_a_bad_pattern_fails_when_the_workspace_is_built(store):
    with pytest.raises(ValueError, match="negation"):
        store.open("bad", ignore=["!x"])


@pytest.fixture
def ignoring(store):
    w = store.open("ignoring", ignore=["__pycache__/", "*.log"])
    register_wsgit(w)
    w.files.write("/workspace/app.py", "x = 1\n")
    w.index.commit("start")
    yield w
    w.close()


def test_status_and_commits_leave_matched_paths_out(ignoring):
    assert ignoring.ignore == ("__pycache__/", "*.log")
    ignoring.files.write("/workspace/__pycache__/app.cpython-313.pyc", b"\0")
    ignoring.files.write("/workspace/run.log", "ran\n")
    ignoring.files.write("/workspace/app.py", "x = 2\n")
    assert ignoring.terminal("ws-git status --porcelain").stdout == " M app.py\n"
    assert ignoring.terminal("ws-git add -A && ws-git commit -m two").exit_code == 0
    assert ignoring.terminal("ws-git status --porcelain").stdout == ""
    r = ignoring.terminal("ws-git add run.log")
    assert r.exit_code != 0
    assert "matches the session's ignore patterns" in (r.stdout + r.stderr)


def test_a_fork_inherits_the_patterns_and_a_merge_keeps_them_out(ignoring):
    kid = ignoring.fork("ignoring.kid")
    try:
        assert kid.ignore == ("__pycache__/", "*.log")
        register_wsgit(kid)
        kid.files.write("/workspace/__pycache__/x.pyc", b"\0")
        kid.files.write("/workspace/kid.log", "kid\n")
        kid.files.write("/workspace/feature.py", "y = 1\n")
        kid.index.commit("feature")
    finally:
        kid.close()
    outcome = ignoring.merge("ignoring.kid")
    assert outcome.merged
    assert ignoring.files.exists("/workspace/feature.py")
    assert not ignoring.files.exists("/workspace/kid.log")
    assert not ignoring.files.exists("/workspace/__pycache__/x.pyc")


def test_a_delegate_answers_with_its_own_work_and_not_what_it_inherited(store, ws):
    """A delegate starts from its parent's tree. What it inherited is
    not work it did: its status, its commit and the paths its answer
    names are what it changed, and nothing it wrote outside the root."""
    ws.files.write("/workspace/SCHEMA.md", "contract\n")
    ws.index.commit("phase 0")

    class Builder:
        def run(self, session, task, *, budget=None):
            child = store.open(session)
            register_wsgit(child)
            try:
                assert child.terminal("ws-git status --porcelain").stdout == ""
                child.terminal("echo cache > /tmp/cache.json")
                child.files.write("/workspace/app/engine.js", "export {}\n")
                child.terminal("ws-git add -A && ws-git commit -m engine")
            finally:
                child.close()
            return "built the engine"

    with Sessions(ws, Builder()) as sessions:
        answer = sessions.ask("build the engine", wait=True)

    assert answer.uncommitted is False
    assert answer.changed["seed"] == ("/workspace/app/engine.js",)


# -- a fork's base, before its first commit ----------------------------------------


@pytest.fixture
def fork_of(store):
    parent = store.open("base-parent")
    register_wsgit(parent)
    parent.files.write("/workspace/a.txt", "a\n")
    parent.files.write("/workspace/b.txt", "b\n")
    parent.files.write("/workspace/c.txt", "c\n")
    parent.index.commit("seed")
    kid = parent.fork("base-parent.kid")
    register_wsgit(kid)
    yield parent, kid
    kid.close()
    parent.close()


def test_a_forks_partial_first_commit_puts_the_rest_back_to_its_base(fork_of):
    """Edits to two inherited files, one staged: the commit holds that
    one, and the other is the base's in it, neither deleted nor taken."""
    parent, kid = fork_of
    kid.files.write("/workspace/a.txt", "a edited\n")
    kid.files.write("/workspace/b.txt", "b edited\n")
    assert kid.terminal("ws-git add a.txt && ws-git commit -m a").exit_code == 0
    assert kid.terminal("ws-git status --porcelain").stdout == " M b.txt\n"
    commit = kid.index.head
    assert kid._provider.files_at(commit).keys() >= {
        "/workspace/a.txt",
        "/workspace/b.txt",
    }
    assert kid.terminal(f"ws-git show {commit[:7]}:b.txt").stdout == "b\n"
    # merged into the parent, the commit changes a.txt and nothing else
    kid.terminal("ws-git checkout -- b.txt")
    kid.close()
    outcome = parent.merge("base-parent.kid")
    assert outcome.merged
    assert parent.files.read("/workspace/a.txt") == b"a edited\n"
    assert parent.files.read("/workspace/b.txt") == b"b\n"
    assert parent.files.exists("/workspace/c.txt")


def test_a_fork_can_stage_the_deletion_of_an_inherited_file(fork_of):
    _, kid = fork_of
    kid.files.remove("/workspace/c.txt")
    assert kid.terminal("ws-git status --porcelain").stdout == " D c.txt\n"
    assert kid.terminal("ws-git add c.txt && ws-git commit -m drop").exit_code == 0
    assert kid.terminal("ws-git status --porcelain").stdout == ""
    assert not kid.files.exists("/workspace/c.txt")


def test_a_fork_puts_an_inherited_file_back_before_its_first_commit(fork_of):
    _, kid = fork_of
    kid.files.write("/workspace/a.txt", "oops\n")
    r = kid.terminal("ws-git checkout -- a.txt")
    assert r.exit_code == 0, r.stdout + r.stderr
    assert kid.files.read("/workspace/a.txt") == b"a\n"
    assert kid.terminal("ws-git status --porcelain").stdout == ""
