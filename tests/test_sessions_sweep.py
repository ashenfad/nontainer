"""Retention for delegate branches: idle TTL, touch-on-read, and keep.

A delegate leaves a branch behind and nothing here deletes one on its
own — the embedder schedules ``sessions.sweep(idle=...)`` beside its
other reapers, the way it schedules ``store.clean()``. What the file
asserts is which branches that sweep may take (finished, unheld,
unkept, unread for long enough), what it leaves alone (a running job, a
kept one, a job the caller just read, and every branch this helper's
own job table does not name), and how a job whose branch is gone
answers afterwards.
"""

import threading
import time
from dataclasses import replace

import pytest

from nontainer import BranchExpired, SessionsError, Store, Workspace
from nontainer.providers import KvgitProvider
from nontainer.sessions import Sessions, render_jobs, run_action
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
    ws.index.commit("seed")
    yield ws
    ws.close()


class Scripted:
    """A runner that opens the child, writes one file, and answers."""

    def __init__(self, store, text="done"):
        self.store = store
        self.text = text

    def run(self, session, task, *, budget=None):
        child = self.store.open(session)
        try:
            child.files.write("/workspace/notes.md", f"{task}\n")
        finally:
            child.close()
        return self.text


def age(sessions, name, seconds):
    """Rewind when the caller last dealt with a job.

    A sweep measures idleness from ``Job.touched``, so a test that
    wants an old job either sleeps or says when the job was last
    touched. This says it.
    """
    with sessions._lock:
        job = sessions._jobs[name]
        sessions._jobs[name] = replace(job, touched=time.time() - seconds)


def test_a_finished_unkept_job_past_idle_is_swept(parent, store):
    """The branch goes, the job stays and says why it has nothing to
    read."""
    with Sessions(parent, Scripted(store)) as sessions:
        answer = sessions.ask("write notes", wait=True)
        name = answer.branch
        assert name in store.sessions()

        age(sessions, name, 120)
        swept = sessions.sweep(idle=60, min_age=0)

        assert swept == [name]
        assert name not in store.sessions()
        job = sessions.list()[0]
        assert job.status == "expired"
        assert job.finished is not None  # when the run ended, not when it went
        with pytest.raises(BranchExpired, match="sessions ask"):
            sessions.result(name)


def test_a_kept_job_is_promoted_out_of_the_sweep(parent, store):
    with Sessions(parent, Scripted(store)) as sessions:
        name = sessions.ask("write notes", wait=True).branch
        sessions.keep(name)
        age(sessions, name, 120)

        assert sessions.sweep(idle=60, min_age=0) == []
        assert name in store.sessions()
        assert sessions.result(name).text == "done"


def test_a_running_job_is_not_swept(parent, store):
    """A branch a run is driving is not idle, whatever the clock says:
    the runner is still writing to it."""
    started = threading.Event()
    release = threading.Event()

    class Slow:
        def run(self, session, task, *, budget=None):
            started.set()
            release.wait(5)
            return "late"

    with Sessions(parent, Slow()) as sessions:
        job = sessions.ask("go")
        started.wait(5)
        age(sessions, job.name, 120)

        assert sessions.sweep(idle=60, min_age=0) == []
        assert job.name in store.sessions()

        release.set()
        sessions.close()


def test_reading_an_answer_touches_the_job(parent, store):
    """Retention is idle TTL: collecting the answer is dealing with the
    job, so the clock starts again from the read."""
    with Sessions(parent, Scripted(store)) as sessions:
        name = sessions.ask("write notes", wait=True).branch
        age(sessions, name, 120)

        sessions.result(name)  # the touch

        assert sessions.sweep(idle=60, min_age=0) == []
        assert name in store.sessions()


def test_keep_after_the_sweep_is_refused(parent, store):
    """There is nothing left to keep, and saying so beats recording a
    flag over a branch that is gone."""
    with Sessions(parent, Scripted(store)) as sessions:
        name = sessions.ask("write notes", wait=True).branch
        age(sessions, name, 120)
        sessions.sweep(idle=60, min_age=0)

        with pytest.raises(BranchExpired, match="already swept"):
            sessions.keep(name)
        assert sessions.list()[0].kept is False


def test_resuming_an_expired_job_is_refused(parent, store):
    """A resume needs the branch, and the branch is what the sweep
    took."""
    with Sessions(parent, Scripted(store)) as sessions:
        name = sessions.ask("write notes", wait=True).branch
        age(sessions, name, 120)
        sessions.sweep(idle=60, min_age=0)

        with pytest.raises(BranchExpired, match="nothing to resume"):
            sessions.ask("more notes", resume=name)


def test_base_still_answers_for_an_expired_job(parent, store):
    """The fork point is recorded, not read off the branch, so where a
    delegate started survives the branch it worked on."""
    with Sessions(parent, Scripted(store)) as sessions:
        name = sessions.ask("write notes", wait=True).branch
        base = sessions.base(name)
        age(sessions, name, 120)
        sessions.sweep(idle=60, min_age=0)

        assert sessions.base(name) == base


def test_the_sweep_returns_every_name_it_took_sorted(parent, store):
    with Sessions(parent, Scripted(store)) as sessions:
        first = sessions.ask("one", name="beta", wait=True).branch
        second = sessions.ask("two", name="alpha", wait=True).branch
        third = sessions.ask("three", name="gamma", wait=True).branch
        for name in (first, second):
            age(sessions, name, 120)

        assert sessions.sweep(idle=60, min_age=0) == sorted([first, second])
        assert third in store.sessions()


def test_a_helper_without_a_store_cannot_sweep():
    """Deletion is a store verb, and a workspace built straight from a
    provider belongs to no store to ask."""
    ws = Workspace(KvgitProvider.open(None, session="solo"))
    try:
        with Sessions(ws, Scripted(None)) as sessions:
            with pytest.raises(SessionsError, match="not opened from a store"):
                sessions.sweep(idle=0)
    finally:
        ws.close()


def test_a_delegates_own_delegate_is_not_the_parents_to_sweep(parent, store):
    """Ownership is what the embedder recorded, never what a name looks
    like: the dot in ``analyst.otter.finch`` is a scoping convention,
    and a branch beside a job is nobody's to guess at."""
    with Sessions(parent, Scripted(store)) as sessions:
        name = sessions.ask("write notes", wait=True).branch
        grandchild = store.fork(name, f"{name}.finch")
        grandchild.close()
        beside = store.open(f"{name}-notes")
        beside.files.write("/workspace/mine.md", "a human's\n")
        beside.close()

        age(sessions, name, 120)
        assert sessions.sweep(idle=60, min_age=0) == [name]

        assert name not in store.sessions()
        assert f"{name}.finch" in store.sessions()
        assert f"{name}-notes" in store.sessions()


def test_a_second_sweep_leaves_an_expired_job_alone(parent, store):
    with Sessions(parent, Scripted(store)) as sessions:
        name = sessions.ask("write notes", wait=True).branch
        age(sessions, name, 120)
        assert sessions.sweep(idle=60, min_age=0) == [name]

        age(sessions, name, 120)
        assert sessions.sweep(idle=60, min_age=0) == []


def test_the_tool_surface_reads_an_expired_job(parent, store):
    """What the model sees: the status in the listing, and a refusal in
    prose rather than a traceback."""
    with Sessions(parent, Scripted(store)) as sessions:
        name = sessions.ask("write notes", wait=True).branch
        age(sessions, name, 120)
        sessions.sweep(idle=60, min_age=0)

        assert "expired" in render_jobs(sessions.list())
        text = run_action(sessions, "result", name=name)
        assert "expired" in text and "sessions ask" in text
        assert "Traceback" not in text
