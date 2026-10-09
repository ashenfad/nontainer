"""What a task's helper needs from delegation: an empty view forked from
the last commit, and a runner of its own for one ask."""

import asyncio

import pytest

from nontainer import Answer, SessionsError, Store
from nontainer.sessions import Sessions
from nontainer.views import ViewFS, encode_view, normalize_view, parse_view


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "store")
    yield s
    s.close()


@pytest.fixture
def parent(store):
    ws = store.open("lead")
    ws.files.write("/workspace/report.md", "# Rates\n")
    ws.index.commit("seed")
    yield ws
    ws.close()


# -- an empty view ---------------------------------------------------------------


def test_an_empty_view_is_a_view_and_not_the_whole_tree():
    assert normalize_view([], "/workspace") == ()
    assert parse_view(encode_view(())) == ()
    assert parse_view(None) is None


def test_an_empty_view_lists_its_root_and_sees_what_it_makes(parent):
    child = parent.fork("lead.helper", paths=[])
    try:
        assert child.files.list("/workspace") == []
        assert not child.files.exists("/workspace/report.md")
        child.files.write("/workspace/note.md", "mine")
        assert child.files.list("/workspace") == ["/workspace/note.md"]
        assert child.files.read("/workspace/note.md") == b"mine"
        with pytest.raises(PermissionError):
            child.files.write("/workspace/report.md", "not mine to change")
    finally:
        child.close()


def test_an_empty_view_is_kept_on_reopen(store, parent):
    parent.fork("lead.helper", paths=[]).close()
    again = store.open("lead.helper")
    try:
        assert again.files.list("/workspace") == []
    finally:
        again.close()


def test_an_empty_view_forks_from_the_last_commit_landing_nothing(parent):
    parent.autocommit = False
    parent.files.write("/workspace/draft.md", "mid-call")
    head = parent.head
    child = parent.fork("lead.helper", paths=[])
    try:
        assert parent.head == head  # nothing landed
        assert parent.files.read("/workspace/draft.md") == b"mid-call"  # still staged
        assert "/workspace/draft.md" not in child.diff(head, child.head).added
    finally:
        child.close()


def test_a_fork_with_paths_still_lands_first(parent):
    parent.autocommit = False
    parent.files.write("/workspace/draft.md", "mid-call")
    head = parent.head
    parent.fork("lead.helper", paths=["report.md"]).close()
    assert parent.head != head


def test_a_view_still_shows_its_seed_with_a_root_anchor():
    class Tree:
        def __init__(self):
            self.files = {"/workspace/a/x": "x", "/workspace/b/y": "y"}

        def getcwd(self):
            return "/workspace"

        def list(self, path, recursive=False):
            return ["a", "b"]

    view = ViewFS(Tree(), ("/workspace/a",), root="/workspace")
    assert view.sees("/workspace/a/x")
    assert not view.sees("/workspace/b/y")
    assert view.sees("/workspace") and view.sees("/")


# -- a runner for one ask ----------------------------------------------------------


class Default:
    def __init__(self):
        self.asked: list[str] = []

    def run(self, session, task, *, budget=None):
        self.asked.append(task)
        return "the helper's own runner"


class Typed:
    """A sync runner for one job, which takes the fork point."""

    def __init__(self):
        self.seen: list[tuple[str, str | None]] = []

    def run(self, session, task, *, budget=None, forked_at=None):
        self.seen.append((session, forked_at))
        return Answer(text="a typed value, summarized")


class AsyncTyped:
    def __init__(self):
        self.loops: set[int] = set()

    async def run(self, session, task, *, budget=None):
        self.loops.add(id(asyncio.get_running_loop()))
        await asyncio.sleep(0)
        return "async answer"


def test_an_ask_can_bring_its_own_runner(parent):
    default, typed = Default(), Typed()
    with Sessions(parent, default) as helper:
        answer = helper.ask(
            "corners(shape='square')", paths=[], runner=typed, wait=True
        )
        assert answer.text == "a typed value, summarized"
        assert typed.seen == [(typed.seen[0][0], parent.head)]
        assert typed.seen[0][0].startswith("lead.")
        assert helper.ask("other work", wait=True).text == "the helper's own runner"
        assert default.asked == ["other work"]


def test_a_helper_runs_an_ask_on_an_async_runner_with_a_loop(parent):
    async def scenario():
        runner = AsyncTyped()
        with Sessions(parent, Default()) as helper:
            answer = await helper.aask("t", paths=[], runner=runner, wait=True)
            assert answer.text == "async answer"
            assert runner.loops == {id(asyncio.get_running_loop())}

    asyncio.run(scenario())


def test_an_async_runner_with_no_loop_is_refused(parent):
    with Sessions(parent, Default()) as helper:
        with pytest.raises(SessionsError, match="needs an event loop"):
            helper.ask("t", runner=AsyncTyped())
