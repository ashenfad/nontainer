"""The host-side delegation helper: ask, list, result, cancel, keep.

The runner here is scripted rather than a model: it opens the child by
name the way an embedder's loop would, writes files, sometimes commits,
and returns text or an ``Answer``. What the file asserts is the
helper's half of the contract — the name it mints, the fork point it
takes, the commit it makes on the child's behalf so ``merge`` and
``checkout(ref, paths=)`` accept the delegate, the paths it reports
grouped seed vs elsewhere, and that no job can die with its thread.
"""

import threading
import time
from dataclasses import replace

import pytest

from nontainer import (
    Answer,
    Job,
    JobRunning,
    SessionsError,
    Store,
    WorkspaceError,
    conversation,
)

# These tests seed the conversation plane as sessions stored before it
# was harness-neutral wrote it (``__agno__/``): what a fork carries, and
# how it is rebound, is then the legacy plane's read and migration.
from nontainer.planes import LEGACY_CONVERSATION_PREFIX as CONVERSATION_PREFIX
from nontainer.sessions import (
    SEPARATOR,
    Sessions,
    pet_name,
    render_answer,
    run_action,
)
from nontainer.wsgit import register_wsgit


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "store")
    yield s
    s.close()


@pytest.fixture
def parent(store):
    ws = store.open("analyst")
    register_wsgit(ws)
    ws.files.write("/workspace/report.md", "# Rates\n\nnorth 4, south 7\n")
    ws.files.write("/workspace/main.py", "print('rates')\n")
    ws.index.commit("seed")
    yield ws
    ws.close()


class Scripted:
    """A runner that opens the child, edits it, and answers.

    ``commits`` says whether the delegate itself ran ws-git commit —
    the case that matters is ``False``, where the helper has to commit
    for it or nothing merges.
    """

    def __init__(self, store, writes, text="done", *, commits=False, status=None):
        self.store = store
        self.writes = writes
        self.text = text
        self.commits = commits
        self.status = status
        self.seen: list[tuple[str, str, object]] = []

    def run(self, session, task, *, budget=None):
        self.seen.append((session, task, budget))
        child = self.store.open(session)
        try:
            for path, content in self.writes.items():
                child.files.write(path, content)
            if self.commits:
                child.index.commit("the delegate's own commit")
        finally:
            child.close()
        if self.status is None:
            return self.text
        return Answer(text=self.text, status=self.status)


# -- names ---------------------------------------------------------------------


def test_a_pet_name_is_two_words():
    for _ in range(20):
        first, sep, second = pet_name().partition("-")
        assert sep and first.isalpha() and second.isalpha()


def test_a_child_is_scoped_under_its_parent(parent, store):
    """Session ids become branch names, so the scope separator has to
    be one a session id may hold."""
    runner = Scripted(store, {"/workspace/report.md": "polished\n"})
    with Sessions(parent, runner) as sessions:
        name = sessions.ask("polish it", wait=True).branch

    assert name.startswith(f"analyst{SEPARATOR}")
    assert name in store.sessions()


def test_a_minted_name_is_suffixed_past_a_collision(parent, store, monkeypatch):
    """A pet name is the helper's own invention, so a second one that
    lands on the same words is settled rather than reported."""
    monkeypatch.setattr("nontainer.sessions.pet_name", lambda: "twin")
    runner = Scripted(store, {"/workspace/notes.md": "note\n"})
    with Sessions(parent, runner, max_workers=2) as sessions:
        jobs = [sessions.ask("one"), sessions.ask("two")]
        names = {j.name for j in jobs}
        sessions.close()

    assert len(names) == 2
    assert f"analyst{SEPARATOR}twin" in names
    assert f"analyst{SEPARATOR}twin{SEPARATOR}2" in names


def test_an_asked_for_name_that_is_taken_is_refused(parent, store):
    """``name=`` names the child's branch, so a collision is the
    caller's to settle: a silent suffix hands back a branch that is not
    the one asked for, and the next verb goes to the wrong child."""
    runner = Scripted(store, {"/workspace/notes.md": "note\n"})
    with Sessions(parent, runner, max_workers=2) as sessions:
        first = sessions.ask("one", name="twin", wait=True)
        with pytest.raises(SessionsError, match="already") as exc:
            sessions.ask("two", name="twin")
        message = str(exc.value)
        assert f"analyst{SEPARATOR}twin" in message
        assert "resume=" in message
        sessions.close()

    assert first.branch == f"analyst{SEPARATOR}twin"
    assert f"analyst{SEPARATOR}twin{SEPARATOR}2" not in store.sessions()


def test_a_name_that_cannot_be_a_session_id_is_refused(parent, store):
    with Sessions(parent, Scripted(store, {})) as sessions:
        with pytest.raises(SessionsError, match="not a session id"):
            sessions.ask("go", name="../escape")


# -- ask / list / result -------------------------------------------------------


def test_ask_then_list_then_result(parent, store):
    """Delivery is pull: the job comes back at once and the answer is
    collected on a later turn."""
    release = threading.Event()

    class Gated(Scripted):
        def run(self, session, task, *, budget=None):
            release.wait(5)
            return super().run(session, task, budget=budget)

    runner = Gated(store, {"/workspace/report.md": "polished\n"}, text="polished it")
    with Sessions(parent, runner) as sessions:
        job = sessions.ask("polish the report")
        assert isinstance(job, Job)
        assert job.status == "running"
        assert sessions.list() == [job]

        with pytest.raises(JobRunning, match="still running"):
            sessions.result(job.name)

        release.set()
        sessions.close()  # joins the worker
        answer = sessions.result(job.name)

    assert str(answer) == "polished it"
    assert answer.status == "answered"
    assert answer.branch == job.name
    assert answer.ref == f"{job.name}@{answer.provenance['commit']}"
    listed = sessions.list()[0]
    assert listed.status == "answered"
    assert listed.finished >= listed.started
    assert listed.ref == answer.ref


def test_wait_returns_the_answer(parent, store):
    runner = Scripted(store, {"/workspace/report.md": "polished\n"}, text="here")
    with Sessions(parent, runner) as sessions:
        answer = sessions.ask("polish", wait=True)
    assert isinstance(answer, Answer)
    assert answer.text == "here"


def test_the_runner_is_given_the_child_and_the_budget(parent, store):
    runner = Scripted(store, {})
    with Sessions(parent, runner, budget=7) as sessions:
        answer = sessions.ask("go", wait=True)
        sessions.ask("go again", budget=9, wait=True)
    assert {(task, budget) for _, task, budget in runner.seen} == {
        ("go", 7),
        ("go again", 9),
    }
    assert runner.seen[0][0] == answer.branch


def test_an_unknown_name_is_refused(parent, store):
    with Sessions(parent, Scripted(store, {})) as sessions:
        with pytest.raises(SessionsError, match="no job named"):
            sessions.result("nobody")
        with pytest.raises(SessionsError, match="no job named"):
            sessions.cancel("nobody")


# -- the commit the helper makes for the child ---------------------------------


def test_a_child_that_never_used_ws_git_merges_at_its_head(parent, store):
    """A delegate that never touched ws-git said everything with its
    branch: autocommit landed every write, so its head IS its answer,
    and nothing has to commit on its behalf."""
    runner = Scripted(
        store,
        {"/workspace/report.md": "# Rates\n\nNorth 4, South 7.\n"},
        text="capitalized the regions",
    )
    with Sessions(parent, runner) as sessions:
        answer = sessions.ask("polish the report", wait=True)

    child = store.open(answer.branch)
    try:
        assert child.index.head is None  # it made no commit of its own
        assert answer.ref == f"{answer.branch}@{child.head}"
    finally:
        child.close()
    assert answer.uncommitted is False

    out = parent.merge(answer.branch)
    assert out.merged and not out.conflicts
    assert parent.files.read("/workspace/report.md").decode().endswith("South 7.\n")


def test_take_from_the_child_works_too(parent, store):
    runner = Scripted(store, {"/workspace/notes.md": "how I did it\n"})
    with Sessions(parent, runner) as sessions:
        answer = sessions.ask("write notes", wait=True)

    parent.checkout(answer.branch, paths=["notes.md"])
    assert parent.files.read("/workspace/notes.md") == b"how I did it\n"
    assert next(iter(parent.log(limit=1))).info["taken_from"] == answer.ref


def test_a_delegate_that_committed_is_named_at_its_own_commit(parent, store):
    """A delegate is the author of its own commits, and the answer
    names the last one it made."""
    runner = Scripted(store, {"/workspace/report.md": "own\n"}, commits=True)
    with Sessions(parent, runner) as sessions:
        answer = sessions.ask("polish", wait=True)

    child = store.open(answer.branch)
    try:
        messages = [c.info["message"] for c in child.index.log()]
        assert messages == ["the delegate's own commit"]
        assert answer.ref == f"{answer.branch}@{child.index.head}"
    finally:
        child.close()
    assert answer.uncommitted is False
    assert parent.merge(answer.branch).merged


# -- what changed --------------------------------------------------------------


def test_changed_groups_seed_and_elsewhere(parent, store):
    """A delegate sent to fix the report that also left a note is the
    common case; the grouping is what keeps the second visible. The
    seed is what the child was GIVEN and never grows, so a path the
    delegate created is elsewhere even though it can read it back."""
    runner = Scripted(
        store,
        {"/workspace/report.md": "polished\n", "/workspace/notes.md": "why\n"},
    )
    with Sessions(parent, runner) as sessions:
        answer = sessions.ask("polish the report", paths=["report.md"], wait=True)

    assert answer.status == "answered"
    assert answer.changed == {
        "seed": ("/workspace/report.md",),
        "elsewhere": ("/workspace/notes.md",),
    }
    assert sessions.list()[0].changed == answer.changed


def test_a_resumed_answer_lists_only_what_is_new_since_the_merge(parent, store):
    """Sent back after its first answer was merged, a delegate's next
    answer names what THIS task changed: measured from where the two
    last met, as `ws-git diff <name>` and a merge are. From the fork
    point, the merged work came back listed as changed again, and the
    parent had to diff by hand to find the real delta."""

    class ByTask(Scripted):
        def run(self, session, task, *, budget=None):
            self.writes = {"/workspace/" + task: task + "\n"}
            return super().run(session, task, budget=budget)

    with Sessions(parent, ByTask(store, {})) as sessions:
        first = sessions.ask("api.py", wait=True)
        assert first.changed["seed"] == ("/workspace/api.py",)
        assert parent.merge(first.branch).merged
        second = sessions.ask("export.py", resume=first.branch, wait=True)
    assert second.changed == {"seed": ("/workspace/export.py",), "elsewhere": ()}


def test_a_resumed_delegate_is_judged_by_the_parents_ignore_rules(store):
    """A resumed child is read through a handle opened again, which must
    carry the parent's ignore rules as a fork does: an ignored path is
    neither in the answer's changes nor in the commit made for the
    delegate's remainder."""
    parent = store.open("analyst", ignore=("scratch/**",))
    register_wsgit(parent)
    parent.files.write("/workspace/report.md", "# Rates\n")
    parent.index.commit("seed")

    class Resumed(Scripted):
        def run(self, session, task, *, budget=None):
            child = self.store.open(session)
            try:
                child.files.write(f"/workspace/{task}", task)
                if task == "export.py":
                    child.index.commit("the delegate's own commit")
                    child.files.write("/workspace/notes.md", "notes")
                    child.files.write("/workspace/scratch/log", "noise")
            finally:
                child.close()
            return "done"

    try:
        with Sessions(parent, Resumed(store, {})) as sessions:
            first = sessions.ask("api.py", wait=True)
            second = sessions.ask("export.py", resume=first.branch, wait=True)
        changed = set(second.changed["seed"]) | set(second.changed["elsewhere"])
        assert "/workspace/scratch/log" not in changed
        assert {"/workspace/export.py", "/workspace/notes.md"} <= changed
        assert not second.uncommitted
        child = store.open(first.branch)
        try:
            remainder = next(iter(child.log()))
            assert remainder.info.get("committed_for") == first.branch
            assert remainder.info["files"] == ["/workspace/notes.md"]
        finally:
            child.close()
    finally:
        parent.close()


def test_an_unmerged_resume_still_lists_everything_since_the_fork(parent, store):
    """Nothing merged, nothing met since the fork: all of it is new to
    the parent, and all of it is listed."""

    class ByTask(Scripted):
        def run(self, session, task, *, budget=None):
            self.writes = {"/workspace/" + task: task + "\n"}
            return super().run(session, task, budget=budget)

    with Sessions(parent, ByTask(store, {})) as sessions:
        first = sessions.ask("api.py", wait=True)
        second = sessions.ask("export.py", resume=first.branch, wait=True)
    assert second.changed["seed"] == ("/workspace/api.py", "/workspace/export.py")


def test_a_child_with_the_whole_tree_reports_everything_as_seed(parent, store):
    runner = Scripted(store, {"/workspace/main.py": "print('x')\n"})
    with Sessions(parent, runner) as sessions:
        answer = sessions.ask("edit main", wait=True)
    assert answer.changed == {"seed": ("/workspace/main.py",), "elsewhere": ()}


def test_provenance_names_the_chain_not_the_last_hop(parent, store):
    runner = Scripted(store, {"/workspace/notes.md": "n\n"})
    with Sessions(parent, runner, chain=("boss@aaaaaa",)) as sessions:
        answer = sessions.ask("go", wait=True)

    chain = answer.provenance["chain"]
    assert chain[0] == "boss@aaaaaa"
    assert chain[1].startswith("analyst@")
    assert chain[2] == answer.ref
    assert answer.provenance["session"] == answer.branch
    assert answer.provenance["finished"] >= answer.provenance["started"]


def test_the_runner_is_told_the_fork_point(parent, store):
    """A provenance header ("asked by session X at commit Y") is written
    before the child's first turn, so the fork point is a parameter of
    the run, not something read off the answer afterwards."""
    seen = {}

    class Aware(Scripted):
        def run(self, session, task, *, budget=None, forked_at=None):
            seen["forked_at"] = forked_at
            return super().run(session, task, budget=budget)

    runner = Aware(store, {"/workspace/notes.md": "n\n"})
    with Sessions(parent, runner) as sessions:
        answer = sessions.ask("go", wait=True)

    # ``fork`` lands the parent's uncommitted writes first, so the fork
    # point is the parent's head once the child exists.
    assert seen["forked_at"] == parent.head
    assert answer.provenance["chain"][0] == f"analyst@{seen['forked_at']}"


def test_a_runner_with_the_old_signature_still_runs(parent, store):
    """Only a runner whose signature accepts the fork point is handed
    it; one written before the parameter existed keeps working."""
    runner = Scripted(store, {"/workspace/notes.md": "n\n"}, text="ran")
    with Sessions(parent, runner) as sessions:
        answer = sessions.ask("go", wait=True)
    assert (answer.text, answer.status) == ("ran", "answered")


def test_base_names_the_fork_point_by_job(parent, store):
    runner = Scripted(store, {"/workspace/notes.md": "n\n"})
    with Sessions(parent, runner) as sessions:
        answer = sessions.ask("go", wait=True)
        assert sessions.base(answer.branch) == parent.head
        with pytest.raises(SessionsError, match="no job named"):
            sessions.base("nobody")


# -- statuses ------------------------------------------------------------------


@pytest.mark.parametrize("status", ["declined", "capped"])
def test_declined_and_capped_pass_through(parent, store, status):
    runner = Scripted(store, {}, text="not doing that", status=status)
    with Sessions(parent, runner) as sessions:
        answer = sessions.ask("do the thing", wait=True)
    assert answer.status == status
    assert answer.text == "not doing that"
    assert sessions.list()[0].status == status


def test_a_raising_runner_yields_failed(parent, store):
    class Broken:
        def run(self, session, task, *, budget=None):
            raise TimeoutError("the model never answered")

    with Sessions(parent, Broken()) as sessions:
        answer = sessions.ask("go", wait=True)

    assert answer.status == "failed"
    assert answer.text == "TimeoutError: the model never answered"
    assert sessions.list()[0].status == "failed"


def test_the_fork_point_is_a_commit_even_when_the_parent_is_dirty(parent, store):
    parent.autocommit = False
    parent.files.write("/workspace/in-flight.txt", "mine\n")
    assert parent.uncommitted

    runner = Scripted(store, {"/workspace/notes.md": "n\n"})
    with Sessions(parent, runner) as sessions:
        answer = sessions.ask("go", wait=True)

    assert not parent.uncommitted  # landed as the fork point
    child = store.open(answer.branch)
    try:
        assert child.files.read("/workspace/in-flight.txt") == b"mine\n"
    finally:
        child.close()


# -- cancel and keep -----------------------------------------------------------


def test_cancel_discards_the_answer_and_leaves_the_branch(parent, store):
    started = threading.Event()
    release = threading.Event()

    class Slow:
        def run(self, session, task, *, budget=None):
            child = store.open(session)
            try:
                child.files.write("/workspace/half.md", "half done\n")
            finally:
                child.close()
            started.set()
            release.wait(5)
            return "too late"

    with Sessions(parent, Slow()) as sessions:
        job = sessions.ask("go")
        started.wait(5)
        cancelled = sessions.cancel(job.name)
        assert cancelled.status == "cancelled"
        release.set()
        sessions.close()

        with pytest.raises(SessionsError, match="cancelled"):
            sessions.result(job.name)
        assert sessions.list()[0].status == "cancelled"

    # the branch is left exactly as the delegate left it
    child = store.open(job.name)
    try:
        assert child.files.read("/workspace/half.md") == b"half done\n"
        assert child.index.head is None  # nothing committed for it
    finally:
        child.close()


def test_cancelling_a_finished_job_changes_nothing(parent, store):
    with Sessions(parent, Scripted(store, {})) as sessions:
        job = sessions.ask("go", wait=True)
        again = sessions.cancel(job.branch)
    assert again.status == "answered"


def test_keep_records_a_flag(parent, store):
    with Sessions(parent, Scripted(store, {})) as sessions:
        answer = sessions.ask("go", wait=True)
        assert not sessions.list()[0].kept
        kept = sessions.keep(answer.branch)
        assert kept.kept
        assert sessions.list()[0].kept
        with pytest.raises(SessionsError, match="no job named"):
            sessions.keep("nobody")


# -- lifecycle -----------------------------------------------------------------


def test_close_joins_the_workers_and_keeps_the_branches(parent, store):
    runner = Scripted(store, {"/workspace/notes.md": "n\n"})
    sessions = Sessions(parent, runner)
    job = sessions.ask("go")
    sessions.close()

    assert sessions.result(job.name).status == "answered"
    assert job.name in store.sessions()
    with pytest.raises(SessionsError, match="closed"):
        sessions.ask("again")


def test_several_delegates_run_at_once(parent, store):
    """The helper hands each ask to a worker, so a second one does not
    wait on the first."""
    inside = threading.Barrier(3, timeout=10)

    class Concurrent:
        def run(self, session, task, *, budget=None):
            inside.wait()
            return f"done: {task}"

    with Sessions(parent, Concurrent(), max_workers=3) as sessions:
        jobs = [sessions.ask(f"task {i}") for i in range(3)]
        sessions.close()
    texts = {sessions.result(j.name).text for j in jobs}
    assert texts == {"done: task 0", "done: task 1", "done: task 2"}
    assert len({j.name for j in jobs}) == 3


def test_a_job_never_dies_with_its_thread(parent, store):
    """Whatever the runner does — even leaving the child unopenable —
    the job resolves, because a job that never resolves is one the
    caller waits on forever."""

    class NotAnException(BaseException):
        pass

    class Vandal:
        def run(self, session, task, *, budget=None):
            raise NotAnException("not even an Exception")

    with Sessions(parent, Vandal()) as sessions:
        answer = sessions.ask("go", wait=True)
    assert answer.status == "failed"
    assert "not even an Exception" in answer.text
    assert time.time() >= sessions.list()[0].started


def test_cancel_before_landing_leaves_the_branch_untouched(parent, store):
    """Cancel and the landing are one state transition: after a cancel
    succeeds, no commit of ours can start."""
    started = threading.Event()
    release = threading.Event()

    class Slow:
        def run(self, session, task, *, budget=None):
            child = store.open(session)
            try:
                child.files.write("/workspace/half.md", "half done\n")
            finally:
                child.close()
            started.set()
            release.wait(5)
            return "too late"

    with Sessions(parent, Slow()) as sessions:
        job = sessions.ask("go")
        started.wait(5)
        assert sessions.cancel(job.name).status == "cancelled"
        release.set()
        sessions.close()

    child = store.open(job.name)
    try:
        # nothing of ours went onto the branch: it made no commit of
        # its own, and its work is still there in the tree
        assert child.index.head is None
        assert "/workspace/half.md" in child.index.status().unstaged
    finally:
        child.close()


def test_what_a_delegate_left_uncommitted_is_committed_for_it(parent, store):
    """Everything a delegate wrote is its answer. One that committed
    part of its work and wrote more would otherwise answer with less
    than it did and have its merge refused, so the rest is committed for
    it when it answers, under a message that says who made the commit.
    Its own commit stays in its log as a checkpoint."""

    class HalfDone:
        def run(self, session, task, *, budget=None):
            child = store.open(session)
            try:
                child.files.write("/workspace/report.md", "polished\n")
                child.index.commit("the part I finished")
                child.terminal("echo wip > /workspace/notes.md")
            finally:
                child.close()
            return "polished the report; the notes are still rough"

    with Sessions(parent, HalfDone()) as sessions:
        answer = sessions.ask("polish the report", wait=True)

    assert answer.uncommitted is False
    assert sessions.list()[0].uncommitted is False
    child = store.open(answer.branch)
    try:
        # the answer names the commit that holds everything
        assert answer.ref == f"{answer.branch}@{child.index.head}"
        log = [c.info.get("message", "") for c in child.index.log()]
        assert log[0].startswith("Work the delegate had not committed")
        assert "the part I finished" in log
        assert child.index.status().unstaged == ()
    finally:
        child.close()
    assert answer.changed["seed"] == ("/workspace/notes.md", "/workspace/report.md")
    assert "left work uncommitted" not in render_answer(answer)

    # and the merge takes all of it
    assert parent.merge(answer.branch).merged
    assert parent.files.read("/workspace/report.md") == b"polished\n"
    assert parent.files.read("/workspace/notes.md") == b"wip\n"


def test_a_delegate_answering_mid_merge_is_not_committed_for(parent, store):
    """Conflict markers of a merge the delegate has not resolved would
    go into a commit made for it and on to the parent with nothing
    saying they were there. So that remainder is left uncommitted, and
    the merge refuses the branch, as it always has."""

    class MidMerge:
        def run(self, session, task, *, budget=None):
            child = store.open(session)
            try:
                child.files.write("/workspace/report.md", "# Rates\n\nnorth 5\n")
                child.index.commit("north is 5")
                side = child.fork(f"{session}-side")
                try:
                    side.files.write("/workspace/report.md", "# Rates\n\nnorth 6\n")
                    side.index.commit("north is 6")
                finally:
                    side.close()
                child.files.write("/workspace/report.md", "# Rates\n\nnorth 7\n")
                child.index.commit("north is 7")
                assert child.merge(f"{session}-side").conflicts
                child.files.write("/workspace/notes.md", "still deciding\n")
            finally:
                child.close()
            return "two figures for north; not settled"

    with Sessions(parent, MidMerge()) as sessions:
        answer = sessions.ask("settle the north figure", wait=True)

    assert answer.uncommitted is True
    child = store.open(answer.branch)
    try:
        log = [c.info.get("message", "") for c in child.index.log()]
        assert not any(m.startswith("Work the delegate") for m in log)
        assert child.index.status().merge_unresolved
    finally:
        child.close()
    assert "left work uncommitted" in render_answer(answer)
    with pytest.raises(WorkspaceError, match="uncommitted ws-git work on"):
        parent.merge(answer.branch)


def test_a_delegate_left_mid_merge_is_refused_rather_than_merged(parent, store):
    """A delegate whose last commit IS an unresolved merge has nothing
    past that commit, so nothing reads as uncommitted. Merged as it
    stands, it would hand its conflict markers over as file content and
    the outcome would report no conflict. The merge refuses instead,
    naming the session and the two ways out (issue #172)."""

    class LeftMidMerge:
        def run(self, session, task, *, budget=None):
            child = store.open(session)
            try:
                child.files.write("/workspace/report.md", "# Rates\n\nnorth 5\n")
                child.index.commit("north is 5")
                side = child.fork(f"{session}-side")
                try:
                    side.files.write("/workspace/report.md", "# Rates\n\nnorth 6\n")
                    side.index.commit("north is 6")
                finally:
                    side.close()
                child.files.write("/workspace/report.md", "# Rates\n\nnorth 7\n")
                child.index.commit("north is 7")
                assert child.merge(f"{session}-side").conflicts
            finally:
                child.close()
            return "two figures for north; not settled"

    with Sessions(parent, LeftMidMerge()) as sessions:
        answer = sessions.ask("settle the north figure", wait=True)

    assert answer.uncommitted is False
    before = parent.files.read("/workspace/report.md")
    with pytest.raises(WorkspaceError, match="has an unresolved merge from") as exc:
        parent.merge(answer.branch)
    assert "report.md" in str(exc.value)
    assert "ws-git merge --abort in that session" in str(exc.value)
    assert parent.files.read("/workspace/report.md") == before  # nothing landed

    # resolved there, it merges as any other branch does
    child = store.open(answer.branch)
    try:
        child.files.write("/workspace/report.md", "# Rates\n\nnorth 7\n")
        child.index.commit("north is 7, settled")
    finally:
        child.close()
    outcome = parent.merge(answer.branch)
    assert outcome.merged and not outcome.conflicts
    assert parent.files.read("/workspace/report.md") == b"# Rates\n\nnorth 7\n"


def test_a_merge_checks_and_takes_one_state_of_a_moving_source(
    parent, store, monkeypatch
):
    """A source still running can move between the merge's check and
    its choice of commit. Read once, the head the checks passed is the
    head that is merged: a conflicted merge the source lands in between
    is not taken, so its markers cannot arrive unreported."""
    kid = parent.fork("analyst.kid", inherit="fresh")
    try:
        kid.files.write("/workspace/report.md", "# Rates\n\nnorth 5\n")
        kid.index.commit("north is 5")
        side = kid.fork("analyst.kid-side")
        try:
            side.files.write("/workspace/report.md", "# Rates\n\nnorth 6\n")
            side.index.commit("north is 6")
        finally:
            side.close()
        kid.files.write("/workspace/report.md", "# Rates\n\nnorth 7\n")
        kid.index.commit("north is 7")
        clean = parent.provider.branch_head("analyst.kid")
        assert kid.merge("analyst.kid-side").conflicts  # now mid-merge
    finally:
        kid.close()

    # The first two reads see the clean state, every later one the source
    # as it is now, conflicted: read separately, both checks would pass
    # and the commit picked after them would be the conflicted one.
    real = parent.provider.branch_head
    reads = {"n": 0}

    def moving(session):
        if session != "analyst.kid":
            return real(session)
        reads["n"] += 1
        return clean if reads["n"] <= 2 else real(session)

    monkeypatch.setattr(parent.provider, "branch_head", moving)
    outcome = parent.merge("analyst.kid")
    assert outcome.merged
    report = parent.files.read("/workspace/report.md").decode()
    assert "<<<<<<<" not in report
    assert report == "# Rates\n\nnorth 7\n"


def test_a_remainder_that_cannot_be_committed_is_still_reported(
    parent, store, monkeypatch
):
    """Committing the rest is best-effort. Where it fails, the answer
    says what it always said: the work is left uncommitted, merge will
    refuse it, and taking paths is the way through."""
    from nontainer.workspace import WorkspaceIndex

    plain = WorkspaceIndex.commit

    def refuse_ours(self, message=None, **kwargs):
        if (message or "").startswith("Work the delegate"):
            raise RuntimeError("cannot commit here")
        return plain(self, message, **kwargs)

    monkeypatch.setattr(WorkspaceIndex, "commit", refuse_ours)

    class HalfDone:
        def run(self, session, task, *, budget=None):
            child = store.open(session)
            try:
                child.files.write("/workspace/report.md", "polished\n")
                child.index.commit("the part I finished")
                child.terminal("echo wip > /workspace/notes.md")
            finally:
                child.close()
            return "polished the report; the notes are still rough"

    with Sessions(parent, HalfDone()) as sessions:
        answer = sessions.ask("polish the report", wait=True)

    assert answer.uncommitted is True
    text = render_answer(answer)
    assert "left work uncommitted" in text
    assert f"ws-git checkout {answer.branch} -- <paths>" in text
    with pytest.raises(WorkspaceError, match="uncommitted ws-git work on"):
        parent.merge(answer.branch)
    parent.checkout(answer.branch, paths=["report.md"])
    assert parent.files.read("/workspace/report.md") == b"polished\n"
    assert "left work uncommitted" in run_action(sessions, "list")


# -- a fork point somewhere else -----------------------------------------------


@pytest.fixture
def fork_point(store):
    """State that is not this session's: one session, one commit, a
    store tag naming it. Returns the commit.

    The tree is deliberately not the parent's, so a fork of it can be
    told apart from a fork of the asking session, and the session
    carries a conversation of its own, so a fork can be asked to
    continue one that was never the asker's.
    """
    ws = store.open("sage")
    ws.files.write("/workspace/rates.md", "# Rates\n\nnorth 4, south 7\n")
    ws._provider.kv[f"{CONVERSATION_PREFIX}runs/1"] = b"what are the rates?"
    ws.index.commit("curated")
    commit = store.tags.add(ws, "rates-2026")
    ws.close()
    return commit


def _index(store, session):
    """The conversation's index on a branch, as core reads it."""
    ws = store.open(session)
    try:
        return conversation.index_of(ws)
    finally:
        ws.close()


def _conversation(store, session):
    """The legacy-plane keys stored on a branch, by key."""
    ws = store.open(session)
    try:
        kv = ws._provider.kv
        return {k: kv[k] for k in kv if k.startswith(CONVERSATION_PREFIX)}
    finally:
        ws.close()


def test_a_job_forked_from_this_session_names_no_origin():
    """The field is at the end with a default, so a job built the way
    it always was is a fork of the asking session."""
    job = Job("analyst.sleepy-otter", "polish it", "running", None, 1.0)
    assert job.origin is None


def test_ask_from_a_store_tag_forks_that_state(parent, store, fork_point):
    """fork_from moves the fork point: the child holds the named state and
    nothing of the asker's."""
    runner = Scripted(store, {}, text="north is 4")
    with Sessions(parent, runner) as sessions:
        answer = sessions.ask("what is north?", fork_from="rates-2026", wait=True)

        job = sessions.list()[0]
        assert job.origin == ("rates-2026", fork_point)
        assert answer.branch.startswith(f"analyst{SEPARATOR}")

    child = store.open(answer.branch)
    try:
        assert sorted(child.files.list("/workspace")) == ["/workspace/rates.md"]
    finally:
        child.close()
    # nothing landed here: an ask always leaves a branch and merging is
    # the caller's own step
    assert parent.files.read("/workspace/report.md").decode().endswith("south 7\n")


def test_ask_from_a_session_ref_a_short_id_and_a_ref(parent, store, fork_point):
    """Every spelling the store hands out is one fork_from takes back: the
    funnel that resolves a ref for every other verb resolves this one."""
    from nontainer import Ref

    runner = Scripted(store, {}, text="answered")
    with Sessions(parent, runner) as sessions:
        sessions.ask("whole id", fork_from=f"sage@{fork_point}", wait=True)
        sessions.ask("short id", fork_from=f"sage@{fork_point[:7]}", wait=True)
        sessions.ask("a Ref", fork_from=Ref("sage", fork_point), wait=True)
        asked = [j.origin for j in sessions.list()]

    assert asked == [
        (f"sage@{fork_point}", fork_point),
        (f"sage@{fork_point[:7]}", fork_point),
        (f"sage@{fork_point}", fork_point),
    ]
    # a ref-spelled fork point is a ref in the chain, short id expanded
    with Sessions(parent, runner) as sessions:
        answer = sessions.ask("go", fork_from=f"sage@{fork_point[:7]}", wait=True)
    assert answer.provenance["chain"][0] == f"sage@{fork_point}"
    assert answer.provenance["from"] == f"sage@{fork_point[:7]}"


def test_ask_from_refuses_a_fork_point_nothing_names(parent, store, fork_point):
    with Sessions(parent, Scripted(store, {})) as sessions:
        with pytest.raises(SessionsError, match="rates-2026"):
            sessions.ask("q", fork_from="rates-2025")
        with pytest.raises(ValueError, match="unknown session"):
            sessions.ask("q", fork_from="nobody@abcdef1")
        with pytest.raises(SessionsError, match="nothing to fork"):
            sessions.ask("q", fork_from="sage@abcdef1")


def test_ask_from_elsewhere_continues_that_conversation_by_default(
    parent, store, fork_point
):
    """Forked from elsewhere, the delegate is the agent that was there:
    another session's files are already in reach through ws-git, and
    its memory is what only a fork brings. So the conversation comes
    along unless the ask says otherwise."""
    with Sessions(parent, Scripted(store, {})) as sessions:
        answer = sessions.ask("what is north?", fork_from="rates-2026", wait=True)

    assert _conversation(store, answer.branch) == {
        f"{CONVERSATION_PREFIX}runs/1": b"what are the rates?"
    }


def test_ask_from_elsewhere_with_fresh_takes_only_the_files(parent, store, fork_point):
    with Sessions(parent, Scripted(store, {})) as sessions:
        answer = sessions.ask(
            "what is north?", fork_from="rates-2026", inherit="fresh", wait=True
        )

    assert _conversation(store, answer.branch) == {}


def test_an_ask_of_your_own_still_starts_fresh(parent, store):
    """Forked from the asker, the delegate starts a chat of its own: the
    brief carries what it needs, not the asker's whole conversation."""
    kv = parent._provider.kv
    kv[f"{CONVERSATION_PREFIX}session"] = {"session_id": "analyst", "run_ids": ["1"]}
    kv[f"{CONVERSATION_PREFIX}runs/1"] = b"something private"
    parent.commit(info={"tool": "test"})
    with Sessions(parent, Scripted(store, {})) as sessions:
        answer = sessions.ask("polish it", wait=True)
    assert f"{CONVERSATION_PREFIX}runs/1" not in _conversation(store, answer.branch)


def test_ask_from_a_bare_session_name_forks_it_as_it_is_now(parent, store, fork_point):
    """`fork_from="sage"` is that session at its latest commit, files and
    conversation, as every ws-git verb reads a bare session name. An
    agent asking another session about its work was refused for not
    knowing a commit id it had no need of."""
    later = store.open("sage")
    later._provider.kv[f"{CONVERSATION_PREFIX}runs/2"] = b"said after the tag"
    later.files.write("/workspace/rates.md", "# Rates\n\nnorth 9, south 7\n")
    later.index.commit("kept talking")
    head = later.head
    later.close()

    runner = Scripted(store, {}, text="north went to 9")
    with Sessions(parent, runner) as sessions:
        answer = sessions.ask("what changed?", fork_from="sage", wait=True)
        assert sessions.list()[0].origin == ("sage", head)

    assert _conversation(store, answer.branch) == {
        f"{CONVERSATION_PREFIX}runs/1": b"what are the rates?",
        f"{CONVERSATION_PREFIX}runs/2": b"said after the tag",
    }
    child = store.open(answer.branch)
    try:
        assert "north 9" in child.files.read("/workspace/rates.md").decode()
    finally:
        child.close()
    # the chain names the session at the commit it was at
    assert answer.provenance["chain"][0] == f"sage@{head}"
    assert answer.provenance["from"] == "sage"


def test_a_store_tag_wins_over_a_session_of_the_same_name(parent, store, fork_point):
    """What already resolved keeps resolving the same way."""
    sage = store.open("sage")
    store.tags.add(sage, "analyst")  # a tag named like the asking session
    tagged = sage.head
    sage.close()
    parent.files.write("/workspace/later.md", "moved on\n")
    parent.commit(info={"tool": "test"})
    with Sessions(parent, Scripted(store, {})) as sessions:
        sessions.ask("q", fork_from="analyst", wait=True)
        assert sessions.list()[0].origin == ("analyst", tagged)


def test_provenance_keeps_what_the_fork_point_resolved_to(parent, store, fork_point):
    """A bare name is classified once, when the ask resolves it. A tag
    of the same name made while the delegate runs does not turn the
    session it was forked from into a sessionless commit."""
    sage = store.open("sage")
    head = sage.head
    sage.close()

    class TagsMidRun(Scripted):
        def run(self, session, task, *, budget=None):
            other = self.store.open("analyst")
            try:
                self.store.tags.add(other, "sage")
            finally:
                other.close()
            return super().run(session, task, budget=budget)

    with Sessions(parent, TagsMidRun(store, {})) as sessions:
        answer = sessions.ask("what changed?", fork_from="sage", wait=True)
    assert answer.provenance["chain"][0] == f"sage@{head}"


def test_a_diff_of_a_delegate_forked_elsewhere_shows_its_own_work(
    parent, store, fork_point
):
    """Measured from where the asker and the delegate last met, a
    delegate forked from another session's state showed that state's
    files as its own changes, and an agent read them as the delegate's
    work. It is measured from where the delegate began, and says what a
    merge would bring besides."""
    runner = Scripted(store, {"/workspace/status.py": "def status():\n    return []\n"})
    with Sessions(parent, runner) as sessions:
        answer = sessions.ask("add status", fork_from="sage", wait=True)
    assert answer.changed["seed"] == ("/workspace/status.py",)

    out = parent.terminal(f"ws-git diff {answer.branch} --stat").stdout
    assert "status.py" in out
    assert "rates.md" not in out.split("\n", 1)[1]  # the header names none
    assert "a state this session's history does not hold" in out
    assert f"`ws-git merge {answer.branch}` would also bring 1 other path(s)" in out
    full = parent.terminal(f"ws-git diff {answer.branch}").stdout
    assert "+def status():" in full and "+# Rates" not in full
    # and the answer's next step does not offer the merge as "take all"
    assert answer.provenance["outside"] is True
    text = render_answer(answer)
    assert f"ws-git merge {answer.branch} would bring sage's files too" in text
    assert "(take all of it)" not in text


def test_a_path_the_delegate_changed_is_not_counted_as_brought_besides(
    parent, store, fork_point
):
    """sage's rates.md is the delegate's edit here, so it is in the diff
    shown, not among what a merge would bring besides."""
    runner = Scripted(store, {"/workspace/rates.md": "# Rates\n\nnorth 9\n"})
    with Sessions(parent, runner) as sessions:
        answer = sessions.ask("update", fork_from="sage", wait=True)
    out = parent.terminal(f"ws-git diff {answer.branch} --stat").stdout
    assert "brings these and nothing else" in out
    assert "rates.md" in out


def test_a_path_the_asker_also_holds_is_flagged_before_a_checkout(
    parent, store, fork_point
):
    """The asker has main.py and the delegate wrote one too: a checkout of
    it replaces the asker's, so the diff says so, as it says a merge
    combines the two."""
    runner = Scripted(store, {"/workspace/main.py": "print('theirs')\n"})
    with Sessions(parent, runner) as sessions:
        answer = sessions.ask("write main", fork_from="sage", wait=True)
    out = parent.terminal(f"ws-git diff {answer.branch} --stat").stdout
    flagged = [line for line in out.splitlines() if line.startswith("# also")]
    assert flagged and "main.py" in flagged[0]
    assert "a checkout of it replaces yours" in flagged[0]


def test_a_fork_point_in_the_askers_history_is_not_outside(parent, store):
    """A store tag of the asker's own commit is an ordinary ancestor: the
    child merges back as any fork does."""
    store.tags.add(parent, "mine")
    with Sessions(parent, Scripted(store, {"/workspace/x.md": "x\n"})) as sessions:
        answer = sessions.ask("x", fork_from="mine", wait=True)
    assert answer.provenance["from"] == "mine"
    assert "outside" not in answer.provenance


def test_a_diff_of_a_delegate_forked_here_reads_as_before(parent, store):
    runner = Scripted(store, {"/workspace/main.py": "print('rates!')\n"})
    with Sessions(parent, runner) as sessions:
        answer = sessions.ask("tweak", wait=True)
    out = parent.terminal(f"ws-git diff {answer.branch} --stat").stdout
    assert out.startswith(f"# what {answer.branch} changed since ")
    assert "does not hold" not in out
    assert "outside" not in answer.provenance
    assert "(take all of it)" in render_answer(answer)


def test_an_unknown_fork_point_names_the_sessions_and_tags_there_are(
    parent, store, fork_point
):
    with Sessions(parent, Scripted(store, {})) as sessions:
        with pytest.raises(SessionsError) as e:
            sessions.ask("q", fork_from="nobody")
    assert "sessions: analyst, sage" in str(e.value)
    assert "store tags: rates-2026" in str(e.value)


def test_ask_from_elsewhere_can_continue_that_conversation(parent, store, fork_point):
    """``inherit="full"`` with a fork point makes the delegate the agent
    that was there as of that commit: the conversation the fork point
    holds comes with the branch, and the task is its next turn."""
    runner = Scripted(store, {}, text="south is 7")
    with Sessions(parent, runner) as sessions:
        answer = sessions.ask(
            "and south?", fork_from="rates-2026", inherit="full", wait=True
        )

    assert _conversation(store, answer.branch) == {
        f"{CONVERSATION_PREFIX}runs/1": b"what are the rates?"
    }
    assert answer.text == "south is 7"


def test_a_full_inherit_makes_the_conversation_the_childs_own(parent, store):
    """The delegate IS the agent that was there: the record it inherits
    names the delegate, so its db reads that conversation for it and
    stores its turns beside it, with the asker kept as its lineage."""
    kv = parent._provider.kv
    kv[f"{CONVERSATION_PREFIX}session"] = {"session_id": "analyst", "run_ids": ["1"]}
    kv[f"{CONVERSATION_PREFIX}runs/1"] = b"what are the rates?"
    parent.commit(info={"tool": "test"})

    with Sessions(parent, Scripted(store, {})) as sessions:
        answer = sessions.ask("and south?", inherit="full", wait=True)

    index = _index(store, answer.branch)
    assert index.session == answer.branch
    assert index.forked_from == "analyst"
    assert conversation.read_index(kv).session == "analyst"


def test_a_full_inherit_from_elsewhere_names_the_session_it_came_from(parent, store):
    """The lineage is the session whose conversation the child carries,
    which for a fork point elsewhere is not the asker."""
    sage = store.open("sage")
    sage.files.write("/workspace/rates.md", "# Rates\n\nnorth 4, south 7\n")
    kv = sage._provider.kv
    kv[f"{CONVERSATION_PREFIX}session"] = {"session_id": "sage", "run_ids": ["1"]}
    kv[f"{CONVERSATION_PREFIX}runs/1"] = b"what are the rates?"
    sage.index.commit("curated")
    store.tags.add(sage, "rates-2027")
    sage.close()

    with Sessions(parent, Scripted(store, {})) as sessions:
        answer = sessions.ask(
            "and south?", fork_from="rates-2027", inherit="full", wait=True
        )

    index = _index(store, answer.branch)
    assert index.session == answer.branch
    assert index.forked_from == "sage"


def test_a_full_inherit_from_a_session_ref_carries_that_commit(
    parent, store, fork_point
):
    """The conversation is the one stored AT the fork point, whichever
    spelling named it."""
    later = store.open("sage")
    later._provider.kv[f"{CONVERSATION_PREFIX}runs/2"] = b"said after the tag"
    later.files.write("/workspace/rates.md", "# Rates\n\nnorth 9, south 7\n")
    later.index.commit("kept talking")
    later.close()

    with Sessions(parent, Scripted(store, {})) as sessions:
        answer = sessions.ask(
            "and south?", fork_from=f"sage@{fork_point}", inherit="full", wait=True
        )

    assert _conversation(store, answer.branch) == {
        f"{CONVERSATION_PREFIX}runs/1": b"what are the rates?"
    }


def test_a_full_inherit_reads_a_fork_point_whose_session_is_gone(
    parent, store, fork_point
):
    """A store tag holds its commit, so the agent that was there can be
    cloned long after its session is deleted — the reason a publication
    tags the state it came from."""
    store.delete("sage")
    assert "sage" not in store.sessions()

    with Sessions(parent, Scripted(store, {})) as sessions:
        answer = sessions.ask(
            "and south?", fork_from="rates-2026", inherit="full", wait=True
        )

    assert _conversation(store, answer.branch) == {
        f"{CONVERSATION_PREFIX}runs/1": b"what are the rates?"
    }
    child = store.open(answer.branch)
    try:
        assert child.files.read("/workspace/rates.md").decode().endswith("south 7\n")
    finally:
        child.close()


def test_resume_refuses_a_conversation_it_would_replace(parent, store):
    """A resumed child keeps the conversation it has: there is no
    second fork to seed, so the refusal names resume."""
    with Sessions(parent, Scripted(store, {})) as sessions:
        answer = sessions.ask("one", wait=True)
        with pytest.raises(SessionsError, match="when resuming"):
            sessions.ask("two", resume=answer.branch, inherit="full")


def test_the_tool_asks_from_elsewhere_with_the_conversation(parent, store, fork_point):
    """The tool path takes the pair the host verb takes."""
    with Sessions(parent, Scripted(store, {}, text="south is 7")) as sessions:
        out = run_action(
            sessions,
            "ask",
            task="and south?",
            fork_from="rates-2026",
            inherit="full",
            wait=True,
        )
        branch = sessions.list()[0].name

    assert "south is 7" in out
    assert _conversation(store, branch) == {
        f"{CONVERSATION_PREFIX}runs/1": b"what are the rates?"
    }


def test_the_source_branch_is_untouched_by_an_ask_from_it(parent, store, fork_point):
    """A fork point is read, never written: the ask forks it, and the
    child is what the runner drives."""
    before = store.open("sage")
    try:
        head, virtual = before.head, before.index.head
        commits = len(list(before.log()))
    finally:
        before.close()

    runner = Scripted(store, {"/workspace/rates.md": "north 40\n"}, text="rewritten")
    with Sessions(parent, runner) as sessions:
        answer = sessions.ask("rewrite it", fork_from="rates-2026", wait=True)

    after = store.open("sage")
    try:
        assert (after.head, after.index.head) == (head, virtual)
        assert len(list(after.log())) == commits
        assert after.files.read("/workspace/rates.md").decode().endswith("south 7\n")
    finally:
        after.close()
    assert store.tags.list()["rates-2026"] == fork_point
    assert answer.branch != "sage"


def test_the_runner_is_told_the_fork_point_it_was_given(parent, store, fork_point):
    """The child's base is the commit it was forked from, wherever that
    came from, so the header it arrives with names where it started."""
    seen = {}

    class Aware(Scripted):
        def run(self, session, task, *, budget=None, forked_at=None):
            seen["forked_at"] = forked_at
            return super().run(session, task, budget=budget)

    runner = Aware(store, {}, text="ok")
    with Sessions(parent, runner) as sessions:
        answer = sessions.ask("what is north?", fork_from="rates-2026", wait=True)
        assert sessions.base(answer.branch) == fork_point

    assert seen["forked_at"] == fork_point
    assert runner.seen[0][1] == "what is north?"
    assert answer.provenance["chain"][0] == fork_point  # the tag names a commit
    assert answer.provenance["chain"][-1] == answer.ref


def test_a_capped_run_from_elsewhere_resolves(parent, store, fork_point):
    """Running out of budget is a status the caller reads, never an
    exception, wherever the child was forked from."""
    runner = Scripted(store, {}, text="I ran out of room", status="capped")
    with Sessions(parent, runner) as sessions:
        answer = sessions.ask("explain everything", fork_from="rates-2026", wait=True)
        assert sessions.list()[0].status == "capped"
    assert answer.status == "capped"
    assert answer.text == "I ran out of room"


def test_a_branch_forked_from_elsewhere_is_read_and_taken_like_any_other(
    parent, store, fork_point
):
    """Nothing comes back on its own. The branch is a branch, so
    reading it and taking from it are the verbs that read and take from
    any branch."""
    runner = Scripted(
        store, {"/workspace/answer.md": "north is 4\n"}, text="see answer.md"
    )
    with Sessions(parent, runner) as sessions:
        answer = sessions.ask("write it down", fork_from="rates-2026", wait=True)

    parent.files.attach(answer.branch, "/workspace/theirs")
    assert parent.files.read("/workspace/theirs/answer.md") == b"north is 4\n"
    parent.files.detach("/workspace/theirs")

    parent.checkout(answer.branch, paths=["answer.md"])
    assert parent.files.read("/workspace/answer.md") == b"north is 4\n"


# -- resume: a child you already have ------------------------------------------


class Echo:
    """A runner that answers with the task, gated so a test can hold a
    run open while it tries to start another."""

    def __init__(self, release=None, hold=()):
        self.release = release
        self.hold = hold
        self.seen: list[tuple[str, str, object]] = []

    def run(self, session, task, *, budget=None):
        self.seen.append((session, task, budget))
        if self.release is not None and (not self.hold or task in self.hold):
            self.release.wait(5)
        return f"answering {task}"


def _settle(sessions, name, timeout=5):
    """Wait for a job to leave `running`, the way a caller polling
    `sessions list` across turns would."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = next(j for j in sessions.list() if j.name == name)
        if job.status != "running":
            return job
        time.sleep(0.01)
    raise AssertionError(f"{name} never finished")


def test_resume_continues_the_child(parent, store):
    """A second task for a delegate you already have goes to the branch
    it has: its conversation kept, no second fork."""

    class Chatty(Echo):
        def run(self, session, task, *, budget=None):
            child = store.open(session)
            try:
                turns = child._provider.kv.get("__agno__/turns") or b""
                child._provider.kv["__agno__/turns"] = turns + task.encode() + b"|"
                child._provider.commit({"tool": "test"})
            finally:
                child.close()
            return super().run(session, task, budget=budget)

    runner = Chatty()
    with Sessions(parent, runner) as sessions:
        first = sessions.ask("polish the report", wait=True)
        branches = set(store.sessions())
        second = sessions.ask("now the summary", resume=first.branch, wait=True)
        rows = sessions.list()

    assert second.branch == first.branch
    assert second.text == "answering now the summary"
    assert set(store.sessions()) == branches  # nothing new was forked
    assert [(s, t) for s, t, _ in runner.seen] == [
        (first.branch, "polish the report"),
        (first.branch, "now the summary"),
    ]
    # one child, one row: the new task replaces the one it was given
    assert [(j.name, j.task) for j in rows] == [(first.branch, "now the summary")]

    child = store.open(first.branch)
    try:
        assert (
            child._provider.kv.get("__agno__/turns")
            == b"polish the report|now the summary|"
        )
    finally:
        child.close()


def test_the_name_you_asked_with_addresses_the_child(parent, store):
    """``name="backend"`` makes ``<session>.backend``; the name the agent
    gave works wherever a job is named, so it is not refused for
    leaving off the prefix it never typed."""
    with Sessions(parent, Echo()) as sessions:
        first = sessions.ask("build it", name="backend", wait=True)
        assert first.branch == f"{parent.session}.backend"
        assert sessions.result("backend").branch == first.branch
        second = sessions.ask("fix the 404", resume="backend", wait=True)
        assert second.branch == first.branch
        assert second.text == "answering fix the 404"
        assert sessions.keep("backend").name == first.branch
        assert sessions.cancel("backend").name == first.branch
        # what the tool says names the whole branch, which ws-git takes
        said = run_action(sessions, "keep", name="backend")
        assert said.startswith(f"{first.branch} kept")
        said = run_action(sessions, "cancel", name="backend")
        assert said.startswith(f"{first.branch} had already finished")
        with pytest.raises(SessionsError, match="no job named 'frontend'"):
            sessions.result("frontend")


def test_action_resume_is_the_ask_that_continues_a_delegate(parent, store):
    """The description says `resume=<name>`, and a model with an
    `action` to fill in writes `action="resume"`. That was refused as an
    unknown action, and an agent sent work back by forking a second
    delegate from the first one's commit instead."""
    with Sessions(parent, Echo()) as sessions:
        first = sessions.ask("build it", name="data", wait=True)
        said = run_action(
            sessions, "resume", name=first.branch, task="add a year filter", wait=True
        )
        assert "answering add a year filter" in said
        assert [j.name for j in sessions.list()] == [first.branch]  # no second fork
        said = run_action(
            sessions, "resume", resume="data", task="and a chart", wait=True
        )
        assert "answering and a chart" in said
        assert "needs the delegate's name" in run_action(sessions, "resume", task="x")
        assert 'resume="<name>"' in run_action(sessions, "sideways")


def test_resume_refuses_a_branch_this_session_has_no_job_for(parent, store):
    with Sessions(parent, Echo()) as sessions:
        with pytest.raises(SessionsError, match="no job named"):
            sessions.ask("q", resume="analyst.nobody")


def test_resume_refuses_to_carry_your_conversation(parent, store):
    """A delegate resumed keeps the conversation it has; nothing here
    can hand it yours instead."""
    with Sessions(parent, Echo()) as sessions:
        first = sessions.ask("polish it", wait=True)
        with pytest.raises(SessionsError, match="fresh"):
            sessions.ask("more", resume=first.branch, inherit="full")


def test_resume_refuses_a_fork_point_the_child_did_not_come_from(
    parent, store, fork_point
):
    """A child carries the conversation of the state it was forked
    from, so naming a different fork point is refused rather than
    quietly answered by the child at hand."""
    with Sessions(parent, Echo()) as sessions:
        theirs = sessions.ask("read it", fork_from="rates-2026", wait=True)
        with pytest.raises(SessionsError, match="rates-2026"):
            sessions.ask("more", fork_from=f"sage@{fork_point}", resume=theirs.branch)
        # the fork point it did come from, spelled the same way, continues it
        again = sessions.ask("more", fork_from="rates-2026", resume=theirs.branch)
        assert again.name == theirs.branch
        _settle(sessions, theirs.branch)

        mine = sessions.ask("polish it", wait=True)
        with pytest.raises(SessionsError, match="this session"):
            sessions.ask("more", fork_from="rates-2026", resume=mine.branch)


def test_a_resume_is_refused_while_the_run_it_continues_is_in_flight(parent, store):
    """A child runs one task at a time. The runner cannot be
    interrupted, so a second run on the same branch would drive the
    child the first is still driving and land against the wrong job."""
    release = threading.Event()
    runner = Echo(release, hold=("polish the report",))
    with Sessions(parent, runner) as sessions:
        first = sessions.ask("polish the report")
        while not runner.seen:
            time.sleep(0.01)

        with pytest.raises(SessionsError, match="still finishing"):
            sessions.ask("now the summary", resume=first.name)

        release.set()
        _settle(sessions, first.name)

        # once it is done the resume proceeds, and the answer that
        # lands under the child's name is the one to the new task
        answer = sessions.ask("now the summary", resume=first.name, wait=True)

    assert answer.text == "answering now the summary"
    assert sessions.result(first.name).text == "answering now the summary"
    assert [j.task for j in sessions.list()] == ["now the summary"]


def test_a_cancelled_run_still_holds_its_child_until_the_runner_is_done(parent, store):
    """Cancel discards the answer; it cannot stop the runner. The child
    is still being driven, so it is not free for another task until the
    run it was cancelled from is over."""
    release = threading.Event()
    runner = Echo(release)
    with Sessions(parent, runner) as sessions:
        first = sessions.ask("polish the report")
        while not runner.seen:
            time.sleep(0.01)
        assert sessions.cancel(first.name).status == "cancelled"

        with pytest.raises(SessionsError, match="still finishing"):
            sessions.ask("now the summary", resume=first.name)

        release.set()
        sessions.close()


def test_two_resumes_of_one_child_yield_one_run(parent, store):
    """The check that the branch is free and the reservation of it are
    one critical section, so two callers cannot both pass it."""
    release = threading.Event()
    runner = Echo(release, hold=("a", "b"))
    out: list[tuple[str, object]] = []
    with Sessions(parent, runner, max_workers=3) as sessions:
        first = sessions.ask("polish the report", wait=True)
        gate = threading.Barrier(2, timeout=10)

        def go(task):
            gate.wait()
            try:
                out.append(("ok", sessions.ask(task, resume=first.branch)))
            except SessionsError as exc:
                out.append(("refused", str(exc)))

        threads = [threading.Thread(target=go, args=(t,)) for t in ("a", "b")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        release.set()
        sessions.close()

    assert sorted(kind for kind, _ in out) == ["ok", "refused"]
    assert "still finishing" in next(text for kind, text in out if kind == "refused")
    tasks = [task for _, task, _ in runner.seen]
    assert tasks.count("a") + tasks.count("b") == 1


def test_a_job_cancelled_before_it_ran_frees_its_child(parent, store):
    """A cancel that bites before the runner starts means no run will
    land for that job and nothing will release what it reserved, so the
    cancel releases it."""
    release = threading.Event()
    runner = Echo(release, hold=("hold the worker",))
    with Sessions(parent, runner, max_workers=1) as sessions:
        sessions.ask("hold the worker")
        while not runner.seen:
            time.sleep(0.01)
        queued = sessions.ask("polish the report")
        assert sessions.cancel(queued.name).status == "cancelled"
        release.set()

        answer = sessions.ask("now the summary", resume=queued.name, wait=True)

    assert answer.text == "answering now the summary"


def test_a_resumed_job_keeps_what_belongs_to_the_child(parent, store, fork_point):
    """Retention and where the child came from belong to the branch,
    not to one run of it: a child the caller kept is still kept after
    it answers again, and it still names the fork point it was made
    from."""
    with Sessions(parent, Echo()) as sessions:
        first = sessions.ask("read it", fork_from="rates-2026", wait=True)
        assert sessions.keep(first.branch).kept
        sessions.ask("read it again", resume=first.branch, wait=True)
        row = sessions.list()[0]

    assert row.kept is True
    assert row.task == "read it again"
    assert row.origin == ("rates-2026", fork_point)
    assert sessions.base(first.branch) == fork_point


# -- the tool's half of keep -------------------------------------------------------


def test_the_tool_keeps_a_job(parent, store):
    """``sessions keep <name>`` promotes the job: what the tool says
    and what the table records are the same fact."""
    runner = Echo()
    with Sessions(parent, runner) as sessions:
        sessions.ask("hold this", wait=True)
        name = sessions.list()[0].name
        said = run_action(sessions, "keep", name=name)
        assert name in said and "kept" in said
        assert sessions.list()[0].kept is True
        assert "no job named" in run_action(sessions, "keep", name="nobody")


# -- the tool's half of fork_from and resume ---------------------------------------


def test_the_tool_asks_from_a_fork_point_and_resumes(parent, store, fork_point):
    runner = Echo()
    with Sessions(parent, runner) as sessions:
        sent = run_action(
            sessions, "ask", task="what is north?", fork_from="rates-2026", wait=True
        )
        name = sessions.list()[0].name
        assert "answering what is north?" in sent and name in sent

        again = run_action(sessions, "ask", task="and south?", resume=name, wait=True)
        assert "answering and south?" in again and name in again
        assert [session for session, _, _ in runner.seen] == [name, name]

        # a listing marks where a child came from when it is not here
        listed = run_action(sessions, "list")
        assert name in listed and "rates-2026" in listed and "and south?" in listed
        mine = sessions.ask("polish it", wait=True)
        both = run_action(sessions, "list").splitlines()

    # one row per child, and only the one forked elsewhere is marked
    assert len(both) == 3
    assert "rates-2026" in next(line for line in both if name in line)
    assert "rates-2026" not in next(line for line in both if mine.branch in line)


def test_a_job_sent_off_from_a_fork_point_names_it(parent, store, fork_point):
    runner = Echo()
    with Sessions(parent, runner) as sessions:
        sent = run_action(sessions, "ask", task="read it", fork_from="rates-2026")
        sessions.close()  # joins the worker
    name = sessions.list()[0].name
    assert "rates-2026" in sent and name in sent
    assert "later turn" in sent


def test_the_tool_says_what_an_ask_cannot_do(parent, store, fork_point):
    with Sessions(parent, Echo()) as sessions:
        assert "needs a task" in run_action(sessions, "ask", fork_from="rates-2026")
        assert "nothing named" in run_action(
            sessions, "ask", task="q", fork_from="nobody"
        )
        assert "no job named" in run_action(
            sessions, "ask", task="q", resume="analyst.nobody"
        )
        assert "when resuming" in run_action(
            sessions, "ask", task="q", resume="analyst.nobody", inherit="full"
        )


def test_every_answer_ends_with_the_same_next_step(parent, store, fork_point):
    """An ask always leaves a branch, and what the parent does with it
    is the same choice wherever the child was forked from."""
    runner = Scripted(store, {"/workspace/answer.md": "north is 4\n"}, text="done")
    with Sessions(parent, runner) as sessions:
        theirs = sessions.ask("read it", fork_from="rates-2026", wait=True)
        mine = sessions.ask("polish it", wait=True)
        elsewhere = run_action(sessions, "result", name=theirs.branch)
        here = run_action(sessions, "result", name=mine.branch)

    for text, job in ((elsewhere, theirs), (here, mine)):
        assert f"ws-git diff {job.branch}" in text
        assert f"ws-git merge {job.branch}" in text
        assert f"ws-git checkout {job.branch} -- <paths>" in text


def test_a_child_forked_from_elsewhere_merges_that_whole_tree(
    parent, store, fork_point
):
    """It shares no history with the asker, so its branch holds the
    other state's whole tree and a merge brings all of it — which is
    why the docs say to take files from such a child unless bringing
    that state in is what you meant."""
    runner = Scripted(
        store, {"/workspace/answer.md": "north is 4\n"}, text="see answer.md"
    )
    with Sessions(parent, runner) as sessions:
        answer = sessions.ask("write it down", fork_from="rates-2026", wait=True)

    # what the child changed is measured from ITS fork point
    assert answer.changed == {"seed": ("/workspace/answer.md",), "elsewhere": ()}

    assert parent.merge(answer.branch).merged
    assert sorted(parent.files.list("/workspace")) == [
        "/workspace/answer.md",  # what the child wrote
        "/workspace/main.py",  # what was already here
        "/workspace/rates.md",  # and the whole tree it was forked from
        "/workspace/report.md",
    ]


def test_a_finished_run_releases_only_its_own_hold_on_the_child(parent, store):
    """A reservation belongs to ONE run. A run that has recorded its
    answer still has its exit to make, and a resume that starts in that
    window holds the child from then on: the earlier run's exit must
    not hand the child away underneath it.

    The window is `child.close()`, between recording the answer and the
    worker's exit, so the test holds it there — and the helper's own
    table is read to get at the handle and the first run's future,
    because the race is between two of its internals.
    """
    closing = threading.Event()
    first_ran = threading.Event()
    second_ran = threading.Event()
    runner = Echo()

    with Sessions(parent, runner, max_workers=3) as sessions:
        runner.release, runner.hold = first_ran, ("first",)
        first = sessions.ask("first")
        future = sessions._futures[first.name]
        child = sessions._children[first.name]
        real_close = child.close
        child.close = lambda: (closing.wait(5), real_close())[1]

        runner.release, runner.hold = second_ran, ("second",)
        first_ran.set()
        _settle(sessions, first.name)  # the answer is recorded

        # a caller that sees the finished status resumes at once
        second = sessions.ask("second", resume=first.name)
        closing.set()
        future.result(5)  # the first run is now fully out

        # the second run still holds the child, so a third is refused
        with pytest.raises(SessionsError, match="still finishing"):
            sessions.ask("third", resume=first.name)

        second_ran.set()
        _settle(sessions, second.name)
        assert sessions.result(second.name).text == "answering second"


def test_a_keep_while_a_resume_is_being_set_up_survives_it(parent, store):
    """What belongs to the child is read where the resumed job is
    installed, so a keep that lands while the resume is being prepared
    is not overwritten by a copy taken a moment earlier.

    The helper's own seam between the two — the look-up of the child's
    row, then the install — is where the keep is slipped in.
    """
    looked_up = threading.Event()
    kept = threading.Event()

    class Hooked(Sessions):
        def _resumed(self, resume, fork_from):
            out = super()._resumed(resume, fork_from)
            looked_up.set()  # the row has been read
            kept.wait(5)  # and a keep lands before it is installed
            return out

    with Hooked(parent, Echo()) as sessions:
        first = sessions.ask("first", wait=True)

        def resume():
            sessions.ask("second", resume=first.branch, wait=True)

        worker = threading.Thread(target=resume)
        worker.start()
        looked_up.wait(5)
        assert sessions.keep(first.branch).kept
        kept.set()
        worker.join(10)

        row = sessions.list()[0]

    assert row.kept is True
    assert row.task == "second"


# -- take: the push half of delivery -------------------------------------------


def landed(sessions, name, timeout=10):
    """Wait for a job's answer to land without collecting it.

    ``ask(wait=True)`` reads the answer, which marks it collected —
    exactly what these tests need NOT to happen — so they wait on the
    job's status instead.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = next(j for j in sessions.list() if j.name == name)
        if job.status != "running":
            return job
        time.sleep(0.01)
    raise AssertionError(f"job {name} never landed")


def test_take_hands_over_an_answer_once(parent, store):
    runner = Scripted(store, {"/workspace/report.md": "polished\n"}, text="polished")
    with Sessions(parent, runner) as sessions:
        job = sessions.ask("polish it")
        landed(sessions, job.name)

        taken = sessions.take()
        assert [(name, str(answer)) for name, answer in taken] == [
            (job.name, "polished")
        ]
        assert isinstance(taken[0][1], Answer)
        assert sessions.take() == []


def test_take_and_result_share_one_collected_mark(parent, store):
    """An embedder that reads between turns and takes mid-turn never
    gets the same answer twice."""
    runner = Scripted(store, {}, text="here")
    with Sessions(parent, runner) as sessions:
        first = sessions.ask("one")
        landed(sessions, first.name)
        assert sessions.result(first.name).text == "here"
        assert sessions.take() == []  # result collected it

        second = sessions.ask("two")
        landed(sessions, second.name)
        assert [name for name, _ in sessions.take()] == [second.name]
        # taking is not consuming: the answer is still readable
        assert sessions.result(second.name).text == "here"
        assert sessions.take() == []


def test_a_resume_discards_the_unread_answer_and_its_mark(parent, store):
    """A resume replaces the run, answer included, exactly as it does
    for ``result``: nothing to take until the new answer lands, and
    then only the new one."""
    runner = Scripted(store, {}, text="first")
    with Sessions(parent, runner) as sessions:
        job = sessions.ask("one")
        landed(sessions, job.name)
        runner.text = "second"
        sessions.ask("two", resume=job.name)
        assert sessions.take() == []
        landed(sessions, job.name)
        assert [(n, str(a)) for n, a in sessions.take()] == [(job.name, "second")]
        assert sessions.take() == []


def test_take_touches_the_job_the_way_result_does(parent, store):
    """Retention is an idle TTL, and taking an answer is the caller
    dealing with the job."""
    runner = Scripted(store, {}, text="here")
    with Sessions(parent, runner) as sessions:
        job = sessions.ask("go")
        landed(sessions, job.name)
        with sessions._lock:
            sessions._jobs[job.name] = replace(
                sessions._jobs[job.name], touched=time.time() - 120
            )

        sessions.take()
        assert sessions.list()[0].touched > time.time() - 5


def test_take_returns_answers_in_landing_order(parent, store):
    runner = Scripted(store, {}, text="here")
    with Sessions(parent, runner, max_workers=1) as sessions:
        first = sessions.ask("one")
        landed(sessions, first.name)
        second = sessions.ask("two")
        landed(sessions, second.name)
        assert [name for name, _ in sessions.take()] == [first.name, second.name]


def test_a_cancelled_job_is_never_taken(parent, store):
    """Its answer was discarded, so there is nothing to hand over."""
    release = threading.Event()

    class Slow(Scripted):
        def run(self, session, task, *, budget=None):
            release.wait(5)
            return super().run(session, task, budget=budget)

    runner = Slow(store, {}, text="too late")
    with Sessions(parent, runner) as sessions:
        job = sessions.ask("go")
        sessions.cancel(job.name)
        release.set()
        sessions.close()  # joins the worker
        assert sessions.take() == []


def test_an_expired_job_is_never_taken(parent, store):
    """The branch the answer describes is gone with the sweep."""
    runner = Scripted(store, {}, text="here")
    with Sessions(parent, runner) as sessions:
        job = sessions.ask("go")
        landed(sessions, job.name)
        with sessions._lock:
            sessions._jobs[job.name] = replace(
                sessions._jobs[job.name], touched=time.time() - 120
            )
        assert sessions.sweep(idle=60, min_age=0) == [job.name]
        assert sessions.take() == []


def test_a_resumed_delegate_is_taken_again(parent, store):
    """A new answer landed, so there is something new to hand over."""
    runner = Scripted(store, {}, text="first pass")
    with Sessions(parent, runner) as sessions:
        job = sessions.ask("go")
        landed(sessions, job.name)
        assert len(sessions.take()) == 1

        runner.text = "second pass"
        sessions.ask("again", resume=job.name)
        deadline = time.time() + 10
        while sessions.list()[0].task != "again" or sessions.list()[0].status == (
            "running"
        ):
            assert time.time() < deadline
            time.sleep(0.01)

        taken = sessions.take()
        assert [(name, str(a)) for name, a in taken] == [(job.name, "second pass")]


# -- hearing back: on_answer, wait, outstanding ----------------------------------


def test_on_answer_is_called_once_the_job_has_settled(parent, store):
    """The hook is how an embedder hears an answer landed without
    polling, so when it runs the answer must already be collectable and
    the branch free for a resume."""
    heard = []
    done = threading.Event()

    def on_answer(name, answer):
        job = next(j for j in sessions.list() if j.name == name)
        heard.append((name, str(answer), job.status, sessions.outstanding()))
        done.set()

    runner = Scripted(store, {"/workspace/report.md": "polished\n"}, text="polished")
    with Sessions(parent, runner, on_answer=on_answer) as sessions:
        job = sessions.ask("polish it")
        assert done.wait(10)
        assert heard == [(job.name, "polished", "answered", [job.name])]
        # and it is still there for whoever collects it
        assert [n for n, _ in sessions.take()] == [job.name]


def test_on_answer_may_be_set_later_and_hears_a_failure_too(parent, store):
    class Broken:
        def run(self, session, task, *, budget=None):
            raise RuntimeError("the model went away")

    heard = []
    with Sessions(parent, Broken()) as sessions:
        sessions.on_answer = lambda name, answer: heard.append(answer.status)
        sessions.ask("try", wait=True)
    assert heard == ["failed"]


def test_on_answer_is_not_called_for_a_cancelled_job(parent, store):
    """A cancelled job's answer is discarded, so it is not news."""
    release = threading.Event()
    heard = []
    runner = Echo(release=release)
    with Sessions(parent, runner, on_answer=lambda *a: heard.append(a)) as sessions:
        job = sessions.ask("go")
        sessions.cancel(job.name)
        release.set()
        sessions.close()  # joins the worker
    assert heard == []


def test_a_hook_that_raises_leaves_the_job_alone(parent, store, caplog):
    def broken(name, answer):
        raise ValueError("embedder bug")

    runner = Scripted(store, {}, text="fine")
    with caplog.at_level("WARNING", logger="nontainer.sessions"):
        with Sessions(parent, runner, on_answer=broken) as sessions:
            job = sessions.ask("go")
            landed(sessions, job.name)
            sessions.close()
            assert str(sessions.result(job.name)) == "fine"
    assert "on_answer raised" in caplog.text


def test_wait_returns_when_an_answer_lands_and_collects_nothing(parent, store):
    release = threading.Event()
    runner = Echo(release=release)
    with Sessions(parent, runner) as sessions:
        job = sessions.ask("go")
        threading.Timer(0.2, release.set).start()
        assert sessions.wait(timeout=10) == [job.name]
        # still waiting to be collected, so a second wait returns at once
        assert sessions.wait(timeout=10) == [job.name]
        assert [n for n, _ in sessions.take()] == [job.name]
        # collected, and nothing runs: nothing to wait for
        assert sessions.wait(timeout=10) == []


def test_wait_ends_on_a_timeout_or_a_cancel(parent, store):
    release = threading.Event()
    runner = Echo(release=release)
    with Sessions(parent, runner) as sessions:
        job = sessions.ask("go")
        started = time.monotonic()
        assert sessions.wait(timeout=0.2) == []
        assert time.monotonic() - started >= 0.2

        threading.Timer(0.2, sessions.cancel, args=(job.name,)).start()
        started = time.monotonic()
        assert sessions.wait(timeout=10) == []
        assert time.monotonic() - started < 5
        release.set()


def test_outstanding_runs_from_asked_to_collected(parent, store):
    release = threading.Event()
    runner = Echo(release=release)
    with Sessions(parent, runner) as sessions:
        assert sessions.outstanding() == []
        job = sessions.ask("go")
        assert sessions.outstanding() == [job.name]  # running
        release.set()
        landed(sessions, job.name)
        assert sessions.outstanding() == [job.name]  # answered, not collected
        sessions.take()
        assert sessions.outstanding() == []


def test_a_resume_between_landing_and_the_hook_does_not_silence_it(
    parent, store, monkeypatch
):
    """``_record`` frees the branch before the run is done, so a resume
    can start in between and replace the job's current answer. The first
    answer was recorded all the same, so the hook still hears it: once
    per recorded answer, the second one included."""
    heard = []
    two = threading.Event()
    original = Sessions._land
    resumed = []

    def land_then_resume(self, name, task, answer, token):
        out = original(self, name, task, answer, token)
        # Claimed before asking: the resumed run lands through this
        # wrapper too, possibly before ``ask`` has returned here.
        if not resumed:
            resumed.append(name)
            self.ask("again", resume=name)
        return out

    def on_answer(name, answer):
        heard.append(str(answer))
        if len(heard) == 2:
            two.set()

    monkeypatch.setattr(Sessions, "_land", land_then_resume)
    with Sessions(parent, Echo(), max_workers=2, on_answer=on_answer) as sessions:
        sessions.ask("first")
        assert two.wait(10)
    assert sorted(heard) == ["answering again", "answering first"]
