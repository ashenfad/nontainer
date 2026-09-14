"""The merge gate: run something against the source before taking it.

``ws.merge(source, check=...)`` hands the check a frozen workspace over
the source at exactly the commit the merge would take, and refuses the
merge when the check says no. The first consumer is "the delegate's
tests pass" — ``check=lambda src: run_pytest(src)``, a ``TestReport``
being truthy when it is green — and the same hook is a session-level
policy (``merge_check=``) that gates the agent's own ``ws-git merge``
in the terminal.
"""

import pytest

from nontainer import (
    MergeRefused,
    NotSupportedError,
    Store,
    Workspace,
    WorkspaceError,
)
from nontainer.providers import KvgitProvider
from nontainer.wsgit import register_wsgit
from nontainer.wspytest import TestReport as Report
from nontainer.wspytest import run_pytest


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "store")
    yield s
    s.close()


@pytest.fixture
def parent(store):
    ws = store.open("analyst")
    register_wsgit(ws)
    ws.files.write("/workspace/report.md", "# Rates\n")
    ws.index.commit("seed")
    yield ws
    ws.close()


@pytest.fixture
def worker(parent):
    """A delegate's branch with one committed change on it."""
    child = parent.fork("worker")
    child.files.write("/workspace/notes.md", "committed\n")
    child.index.commit("the delegate's work")
    yield child
    child.close()


def test_the_check_sees_the_source_at_the_commit_the_merge_would_take(parent, worker):
    """Frozen, at the merge commit: what the check judges is what the
    merge would take, and it cannot change it on the way past."""
    seen = {}

    def check(src):
        seen["session"] = src.session
        seen["head"] = src.head
        seen["notes"] = src.files.read("/workspace/notes.md").decode()
        with pytest.raises(NotSupportedError, match="frozen"):
            src.files.write("/workspace/notes.md", "the check's own edit\n")
        return True

    parent.merge("worker", check=check)

    assert seen["session"] == "worker"
    assert seen["head"] == worker.index.head
    assert seen["notes"] == "committed\n"
    # and the check's refused write is nowhere: not on the branch it
    # judged, and not in what the merge brought home
    assert parent.files.read("/workspace/notes.md").decode() == "committed\n"


def test_a_truthy_verdict_merges_as_an_unchecked_merge_does(parent, worker):
    out = parent.merge("worker", check=lambda src: True)

    assert out.merged
    assert out.auto_merged == ("/workspace/notes.md",)
    assert out.commit == parent.index.head
    assert parent.files.read("/workspace/notes.md").decode() == "committed\n"


def test_a_falsy_verdict_refuses_and_changes_nothing(parent, worker):
    before_head = parent.index.head
    before_log = [e.id for e in parent.index.log()]

    with pytest.raises(MergeRefused) as excinfo:
        parent.merge("worker", check=lambda src: False)

    assert excinfo.value.verdict is False
    assert "refused by its check" in str(excinfo.value)
    assert "worker" in str(excinfo.value)
    assert not parent.files.exists("/workspace/notes.md")
    assert parent.index.head == before_head
    assert [e.id for e in parent.index.log()] == before_log
    status = parent.index.status()
    assert status.merge_source is None
    assert not status.staged and not status.unstaged


def test_a_report_verdict_says_what_the_tests_did(parent, worker):
    """The refusal quotes the verdict, so a caller reads why without
    going back to the object."""
    report = Report(
        tool="pytest",
        ok=False,
        collected=10,
        passed=8,
        failed=2,
        errors=0,
        skipped=0,
        duration=0.31,
        exit_code=1,
    )

    with pytest.raises(MergeRefused) as excinfo:
        parent.merge("worker", check=lambda src: report)

    message = str(excinfo.value)
    assert message.endswith("refused by its check: 2 failed, 8 passed in 0.31s")
    assert excinfo.value.verdict is report


def test_a_verdict_with_nothing_to_say_leaves_no_dangling_reason(parent, worker):
    """An empty verdict is still a refusal, and the message says only
    that. A verdict with text in it is TRUTHY, so a check that wants
    to refuse with a reason returns something falsy that renders —
    a TestReport, say."""
    with pytest.raises(MergeRefused) as excinfo:
        parent.merge("worker", check=lambda src: "")

    assert str(excinfo.value).endswith("refused by its check")
    assert parent.merge("worker", check=lambda src: "looks fine to me").merged


def test_a_bare_false_ends_the_message_at_the_check(parent, worker):
    """Nothing to append: ``False`` and ``None`` say only that the
    check said no, and a message reading ``check: False`` would pretend
    otherwise."""
    with pytest.raises(MergeRefused) as excinfo:
        parent.merge("worker", check=lambda src: None)

    assert str(excinfo.value).endswith("refused by its check")


def test_the_delegates_tests_are_run_on_its_own_branch(parent, store):
    """End to end, the one-liner the hook exists for: red refuses,
    green merges, and the tests run against the delegate's tree rather
    than the caller's."""
    child = parent.fork("tester")
    try:
        child.files.write(
            "/workspace/tests/test_x.py", "def test_math():\n    assert 1 + 1 == 3\n"
        )
        child.index.commit("a red suite")

        with pytest.raises(MergeRefused, match="1 failed"):
            parent.merge("tester", check=run_pytest)
        assert not parent.files.exists("/workspace/tests/test_x.py")

        child.files.write(
            "/workspace/tests/test_x.py", "def test_math():\n    assert 1 + 1 == 2\n"
        )
        child.index.commit("a green suite")

        assert parent.merge("tester", check=run_pytest).merged
        assert parent.files.exists("/workspace/tests/test_x.py")
    finally:
        child.close()


def test_a_session_policy_gates_the_agents_own_merge(store):
    """``merge_check`` on the open is what stands between the agent's
    `ws-git merge` and a red branch: the verb funnels into
    ``Workspace.merge``, so the refusal is the terminal's error text."""
    ws = store.open("analyst", merge_check=run_pytest)
    register_wsgit(ws)
    ws.files.write("/workspace/report.md", "# Rates\n")
    ws.index.commit("seed")
    child = ws.fork("worker")
    try:
        child.files.write(
            "/workspace/tests/test_x.py", "def test_math():\n    assert False\n"
        )
        child.index.commit("a red suite")

        result = ws.terminal("ws-git merge worker")

        assert result.exit_code == 1
        assert "refused by its check" in result.stderr
        assert "1 failed" in result.stderr
        assert "Traceback" not in result.stderr
        # nothing moved: no merge context, no files, and the next verb
        # is not refused for a merge that never happened
        assert ws.index.status().merge_source is None
        assert not ws.files.exists("/workspace/tests/test_x.py")
        assert ws.terminal("ws-git status").exit_code == 0
    finally:
        child.close()
        ws.close()


def test_a_fork_inherits_the_session_policy(store):
    """A construction setting, so the delegate that delegates merges
    under the same gate its parent does."""
    ws = store.open("analyst", merge_check=lambda src: False)
    ws.files.write("/workspace/report.md", "# Rates\n")
    ws.index.commit("seed")
    fork = ws.fork("mid")
    try:
        grandchild = fork.fork("leaf")
        grandchild.files.write("/workspace/leaf.md", "work\n")
        grandchild.index.commit("its work")
        grandchild.close()

        with pytest.raises(MergeRefused, match="merge of 'leaf'"):
            fork.merge("leaf")
    finally:
        fork.close()
        ws.close()


def test_a_call_level_check_overrides_the_session_policy(store):
    ws = store.open("analyst", merge_check=lambda src: False)
    ws.files.write("/workspace/report.md", "# Rates\n")
    ws.index.commit("seed")
    child = ws.fork("worker")
    try:
        child.files.write("/workspace/notes.md", "committed\n")
        child.index.commit("the delegate's work")

        assert ws.merge("worker", check=lambda src: True).merged
    finally:
        child.close()
        ws.close()


def test_a_check_that_raises_is_the_embedders_bug_and_propagates(parent, worker):
    """A broken check is not a refusal: it says nothing about the
    branch, so it must not read as "your tests failed"."""

    def check(src):
        raise ZeroDivisionError("the check itself is broken")

    with pytest.raises(ZeroDivisionError, match="the check itself is broken"):
        parent.merge("worker", check=check)
    assert not parent.files.exists("/workspace/notes.md")


def test_the_frozen_workspace_is_closed_however_the_check_goes(parent, worker, store):
    """It holds an executor and a store handle of its own, and the
    merge is what opened it."""
    opened = []
    real_resolve = store.resolve

    def spy(*args, **kwargs):
        ws = real_resolve(*args, **kwargs)
        opened.append(ws)
        return ws

    store.resolve = spy
    try:
        parent.merge("worker", check=lambda src: True)
        with pytest.raises(MergeRefused):
            parent.merge("worker", check=lambda src: False)
        with pytest.raises(ZeroDivisionError):
            parent.merge("worker", check=lambda src: 1 / 0)
    finally:
        store.resolve = real_resolve

    assert len(opened) == 3
    assert all(ws._closed for ws in opened)


def test_a_check_needs_the_store_the_source_lives_in():
    """A workspace built straight from a provider belongs to no store,
    and a frozen view at a commit is a store's verb."""
    ws = Workspace(KvgitProvider.open(None, session="solo"))
    try:
        ws.files.write("/workspace/a.txt", "base\n")
        ws.index.commit("seed")
        fork = ws.fork("worker")
        fork.files.write("/workspace/b.txt", "work\n")
        fork.index.commit("its work")
        fork.close()

        with pytest.raises(WorkspaceError, match="Store.open"):
            ws.merge("worker", check=lambda src: True)
        # and an unchecked merge on the same workspace still works
        assert ws.merge("worker").merged
    finally:
        ws.close()
