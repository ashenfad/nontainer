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

import pytest

from nontainer import Answer, Job, JobRunning, SessionsError, Store
from nontainer.sessions import SEPARATOR, Sessions, pet_name
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


def test_two_concurrent_asks_get_distinct_names(parent, store):
    runner = Scripted(store, {"/workspace/notes.md": "note\n"})
    with Sessions(parent, runner, max_workers=2) as sessions:
        jobs = [sessions.ask("one", name="twin"), sessions.ask("two", name="twin")]
        names = {j.name for j in jobs}
        sessions.close()

    assert len(names) == 2
    assert f"analyst{SEPARATOR}twin" in names
    assert f"analyst{SEPARATOR}twin{SEPARATOR}2" in names


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


def test_a_child_that_never_committed_still_merges(parent, store):
    """A fork inherits the parent's ws-git blob, so everything the
    delegate writes reads as uncommitted agent work and merge refuses
    it. The helper commits for it."""
    runner = Scripted(
        store,
        {"/workspace/report.md": "# Rates\n\nNorth 4, South 7.\n"},
        text="capitalized the regions",
    )
    with Sessions(parent, runner) as sessions:
        answer = sessions.ask("polish the report", wait=True)

    out = parent.merge(answer.branch)
    assert out.merged and not out.conflicts
    assert parent.files.read("/workspace/report.md").decode().endswith("South 7.\n")


def test_the_commit_message_is_the_answers_first_line(parent, store):
    runner = Scripted(
        store,
        {"/workspace/report.md": "x\n"},
        text="# Polished the report\n\nand more prose\n",
    )
    with Sessions(parent, runner) as sessions:
        answer = sessions.ask("polish", wait=True)

    child = store.open(answer.branch)
    try:
        assert child.index.log(limit=1)[0].info["message"] == "Polished the report"
    finally:
        child.close()


def test_take_from_the_child_works_too(parent, store):
    runner = Scripted(store, {"/workspace/notes.md": "how I did it\n"})
    with Sessions(parent, runner) as sessions:
        answer = sessions.ask("write notes", wait=True)

    parent.checkout(answer.branch, paths=["notes.md"])
    assert parent.files.read("/workspace/notes.md") == b"how I did it\n"
    assert next(iter(parent.log(limit=1))).info["taken_from"] == answer.ref


def test_a_delegate_that_committed_itself_is_not_committed_twice(parent, store):
    runner = Scripted(store, {"/workspace/report.md": "own\n"}, commits=True)
    with Sessions(parent, runner) as sessions:
        answer = sessions.ask("polish", wait=True)

    child = store.open(answer.branch)
    try:
        messages = [c.info["message"] for c in child.index.log()]
    finally:
        child.close()
    assert messages[0] == "the delegate's own commit"
    assert messages.count("the delegate's own commit") == 1


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
        assert child.index.head == parent.index.head  # nothing committed for it
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


def test_a_partial_commit_by_the_delegate_still_lands_everything(parent, store):
    """The answer lands everything the delegate did. A delegate that
    staged half its work would otherwise leave the other half as
    uncommitted agent work — which merge refuses and a take from the
    answer's ref silently omits, while the job reads as answered."""

    class Partial:
        def run(self, session, task, *, budget=None):
            child = store.open(session)
            try:
                child.files.write("/workspace/report.md", "polished\n")
                child.files.write("/workspace/notes.md", "why I did it\n")
                child.index.stage(["/workspace/report.md"])
                # the index is a key like any other, and a delegate's
                # staged set reaches the branch on the next durability
                # point — a turn hook here, a tool call in a real loop
                child.commit(info={"tool": "turn"})
            finally:
                child.close()
            return "polished, with a note"

    with Sessions(parent, Partial()) as sessions:
        answer = sessions.ask("polish the report", wait=True)

    assert answer.changed["seed"] == ("/workspace/notes.md", "/workspace/report.md")
    child = store.open(answer.branch)
    try:
        committed = child._provider.files_at(child.index.head)
    finally:
        child.close()
    assert "/workspace/report.md" in committed
    assert "/workspace/notes.md" in committed

    out = parent.merge(answer.branch)
    assert out.merged and not out.conflicts
    assert parent.files.read("/workspace/notes.md") == b"why I did it\n"


def test_a_delegate_that_deleted_a_file_lands_the_deletion(parent, store):
    """Deletions are part of the working set the answer lands, whether
    or not the delegate staged them."""

    class Remover:
        def run(self, session, task, *, budget=None):
            child = store.open(session)
            try:
                child.files.write("/workspace/report.md", "kept\n")
                child.index.stage(["/workspace/report.md"])
                child.terminal("rm main.py")  # commits the delete and the index
            finally:
                child.close()
            return "dropped main.py"

    with Sessions(parent, Remover()) as sessions:
        answer = sessions.ask("drop main.py", wait=True)

    child = store.open(answer.branch)
    try:
        committed = child._provider.files_at(child.index.head)
    finally:
        child.close()
    assert "/workspace/main.py" not in committed
    parent.merge(answer.branch)
    assert not parent.files.exists("/workspace/main.py")
