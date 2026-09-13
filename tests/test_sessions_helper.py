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

from nontainer import (
    Answer,
    Job,
    JobRunning,
    SessionsError,
    Store,
    WorkspaceError,
)
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


def test_a_delegate_that_left_work_uncommitted_says_so(parent, store):
    """A delegate that used ws-git is the author of its own commits:
    what it committed is what it submitted. The rest is reported, not
    committed for it and not hidden — merge refuses such a source, so
    the answer says that before the caller tries."""

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
    assert sessions.list()[0].uncommitted is True
    # the answer names what it LANDED, and the paths still name it all
    child = store.open(answer.branch)
    try:
        assert answer.ref == f"{answer.branch}@{child.index.head}"
    finally:
        child.close()
    assert answer.changed["seed"] == ("/workspace/notes.md", "/workspace/report.md")

    # merge refuses it, in its own words, and the tool said so first
    text = render_answer(answer)
    assert "left work uncommitted" in text
    assert f"ws-git checkout {answer.branch} -- <paths>" in text
    assert "ws-git merge will refuse it" in text
    assert "take all of it" not in text
    with pytest.raises(WorkspaceError, match="uncommitted ws-git work on"):
        parent.merge(answer.branch)

    # taking paths is the way through, and it works
    parent.checkout(answer.branch, paths=["report.md"])
    assert parent.files.read("/workspace/report.md") == b"polished\n"
    assert "left work uncommitted" in run_action(sessions, "list")


# -- a fork point somewhere else -----------------------------------------------


@pytest.fixture
def fork_point(store):
    """State that is not this session's: one session, one commit, a
    store tag naming it. Returns the commit.

    The tree is deliberately not the parent's, so a fork of it can be
    told apart from a fork of the asking session.
    """
    ws = store.open("sage")
    ws.files.write("/workspace/rates.md", "# Rates\n\nnorth 4, south 7\n")
    ws.index.commit("curated")
    commit = store.tags.add(ws, "rates-2026")
    ws.close()
    return commit


def test_a_job_forked_from_this_session_names_no_origin():
    """The field is at the end with a default, so a job built the way
    it always was is a fork of the asking session."""
    job = Job("analyst.sleepy-otter", "polish it", "running", None, 1.0)
    assert job.origin is None


def test_ask_from_a_store_tag_forks_that_state(parent, store, fork_point):
    """from_ moves the fork point: the child holds the named state and
    nothing of the asker's."""
    runner = Scripted(store, {}, text="north is 4")
    with Sessions(parent, runner) as sessions:
        answer = sessions.ask("what is north?", from_="rates-2026", wait=True)

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
    """Every spelling the store hands out is one from_ takes back: the
    funnel that resolves a ref for every other verb resolves this one."""
    from nontainer import Ref

    runner = Scripted(store, {}, text="answered")
    with Sessions(parent, runner) as sessions:
        sessions.ask("whole id", from_=f"sage@{fork_point}", wait=True)
        sessions.ask("short id", from_=f"sage@{fork_point[:7]}", wait=True)
        sessions.ask("a Ref", from_=Ref("sage", fork_point), wait=True)
        asked = [j.origin for j in sessions.list()]

    assert asked == [
        (f"sage@{fork_point}", fork_point),
        (f"sage@{fork_point[:7]}", fork_point),
        (f"sage@{fork_point}", fork_point),
    ]
    # a ref-spelled fork point is a ref in the chain, short id expanded
    with Sessions(parent, runner) as sessions:
        answer = sessions.ask("go", from_=f"sage@{fork_point[:7]}", wait=True)
    assert answer.provenance["chain"][0] == f"sage@{fork_point}"
    assert answer.provenance["from"] == f"sage@{fork_point[:7]}"


def test_ask_from_refuses_a_fork_point_nothing_names(parent, store, fork_point):
    with Sessions(parent, Scripted(store, {})) as sessions:
        with pytest.raises(SessionsError, match="rates-2026"):
            sessions.ask("q", from_="rates-2025")
        with pytest.raises(ValueError, match="unknown session"):
            sessions.ask("q", from_="nobody@abcdef1")
        with pytest.raises(SessionsError, match="nothing to fork"):
            sessions.ask("q", from_="sage@abcdef1")


def test_ask_from_refuses_to_carry_your_conversation(parent, store, fork_point):
    """A conversation that is not yours cannot be continued, so a fork
    from somewhere else starts one of its own."""
    with Sessions(parent, Scripted(store, {})) as sessions:
        with pytest.raises(SessionsError, match="fresh"):
            sessions.ask("q", from_="rates-2026", inherit="full")


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
        answer = sessions.ask("rewrite it", from_="rates-2026", wait=True)

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
        answer = sessions.ask("what is north?", from_="rates-2026", wait=True)
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
        answer = sessions.ask("explain everything", from_="rates-2026", wait=True)
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
        answer = sessions.ask("write it down", from_="rates-2026", wait=True)

    parent.files.attach(answer.branch, "/workspace/theirs")
    assert parent.files.read("/workspace/theirs/answer.md") == b"north is 4\n"
    parent.files.detach("/workspace/theirs")

    parent.checkout(answer.branch, paths=["answer.md"])
    assert parent.files.read("/workspace/answer.md") == b"north is 4\n"
