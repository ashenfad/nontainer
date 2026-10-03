"""Authoring output is never work.

The app runtime writes handler logs and test_app captures into the app
tree for the agent to read back. They stay on their own branch, and
nothing that decides what a session's work IS sees them: ws-git status
and commits, the uncommitted check, diffs, merges, cherry-picks and
directory takes. The case that made this matter: a delegate commits its
scene, checks it with test_app, and answers with captures newer than
its commit.
"""

import pytest

from nontainer import Store
from nontainer.ignore import IGNORED_DIRS, is_ignored
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
    assert not is_ignored("/elsewhere/app/logs/x", "/workspace")
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
