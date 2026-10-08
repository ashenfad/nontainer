"""Delegation with an async runner, and from coroutines.

A runner whose ``run`` is ``async def`` runs on the embedder's event
loop rather than a worker thread: ``max_workers`` bounds the runs in
flight, landing runs on a thread, and a cancel stops the run. From a
coroutine, ``aask`` asks, ``await_ready`` waits, and ``answers`` yields
each answer as it lands, once.
"""

import asyncio
import threading

import pytest

from nontainer import Answer, SessionsError, Store
from nontainer.sessions import Sessions
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
    ws.files.write("/workspace/report.md", "# Rates\n")
    ws.index.commit("seed")
    yield ws
    ws.close()


class Writing:
    """An async runner that writes its task into the child, after
    ``gate`` opens when one is given."""

    def __init__(self, store, gate: asyncio.Event | None = None):
        self.store = store
        self.gate = gate
        self.running = 0
        self.most = 0
        self.loops: set[int] = set()
        self.stopped: list[str] = []

    async def run(self, session, task, *, budget=None):
        self.loops.add(id(asyncio.get_running_loop()))
        self.running += 1
        self.most = max(self.most, self.running)
        try:
            if self.gate is not None:
                await self.gate.wait()
            child = self.store.open(session)
            try:
                child.files.write(f"/workspace/{task}", task)
            finally:
                child.close()
            return f"did {task}"
        except asyncio.CancelledError:
            self.stopped.append(task)
            raise
        finally:
            self.running -= 1


async def resume_when_free(sessions, name, task):
    """Resume ``name`` once its stopped run has finished ending: a cancel
    stops an async run through its own path, which frees the branch a
    moment later."""
    for _ in range(200):
        try:
            return await sessions.aask(task, resume=name, wait=True)
        except SessionsError as exc:
            if "still finishing" not in str(exc):
                raise
            await asyncio.sleep(0.01)
    raise AssertionError(f"{name} was never freed")


def test_an_async_runner_runs_on_the_loop_and_aask_awaits_its_answer(parent, store):
    runner = Writing(store)

    async def main():
        sessions = Sessions(parent, runner)
        try:
            answer = await sessions.aask("api.py", wait=True)
            assert isinstance(answer, Answer) and answer.text == "did api.py"
            assert answer.changed["seed"] == ("/workspace/api.py",)
            assert runner.loops == {id(asyncio.get_running_loop())}
        finally:
            await sessions.aclose()

    asyncio.run(main())


def test_max_workers_bounds_the_runs_in_flight(parent, store):
    async def main():
        gate = asyncio.Event()
        runner = Writing(store, gate)
        sessions = Sessions(parent, runner, max_workers=2)
        try:
            for name in ("a.py", "b.py", "c.py"):
                await sessions.aask(name)
            await asyncio.sleep(0.2)
            assert runner.running == 2
            gate.set()
            seen = []
            async for name, answer in sessions.answers():
                seen.append(answer.text)
                if len(seen) == 3:
                    break
            assert sorted(seen) == ["did a.py", "did b.py", "did c.py"]
            assert runner.most == 2
        finally:
            await sessions.aclose()

    asyncio.run(main())


def test_answers_hands_each_answer_over_once(parent, store):
    async def main():
        sessions = Sessions(parent, Writing(store))
        try:
            job = await sessions.aask("api.py")
            async for name, answer in sessions.answers():
                assert (name, answer.text) == (job.name, "did api.py")
                break
            assert sessions.take() == []
            assert sessions.outstanding() == []
        finally:
            await sessions.aclose()

    asyncio.run(main())


def test_answers_ends_when_the_helper_closes(parent, store):
    async def main():
        sessions = Sessions(parent, Writing(store))

        async def drain():
            return [pair async for pair in sessions.answers()]

        draining = asyncio.create_task(drain())
        await asyncio.sleep(0.05)
        await sessions.aclose()
        assert await asyncio.wait_for(draining, 5) == []

    asyncio.run(main())


def test_await_ready_is_wait_from_a_coroutine(parent, store):
    async def main():
        gate = asyncio.Event()
        sessions = Sessions(parent, Writing(store, gate))
        try:
            assert await sessions.await_ready() == []  # nothing to wait for
            job = await sessions.aask("api.py")
            assert await sessions.await_ready(timeout=0.1) == []  # timed out
            gate.set()
            assert await sessions.await_ready(timeout=5) == [job.name]
            assert sessions.result(job.name).text == "did api.py"
        finally:
            await sessions.aclose()

    asyncio.run(main())


def test_await_ready_works_with_a_synchronous_runner(parent, store):
    class Sync:
        def run(self, session, task, *, budget=None):
            return f"did {task}"

    async def main():
        with Sessions(parent, Sync()) as sessions:
            job = sessions.ask("api.py")
            assert await sessions.await_ready(timeout=5) == [job.name]

    asyncio.run(main())


def test_cancel_stops_an_async_run_and_frees_the_branch(parent, store):
    async def main():
        runner = Writing(store, asyncio.Event())  # never opens
        sessions = Sessions(parent, runner)
        try:
            job = await sessions.aask("api.py")
            await asyncio.sleep(0.05)
            assert runner.running == 1
            sessions.cancel(job.name)
            for _ in range(100):
                if runner.stopped:
                    break
                await asyncio.sleep(0.01)
            assert runner.stopped == ["api.py"]
            assert sessions.list()[0].status == "cancelled"
            assert await sessions.await_ready(timeout=1) == []
            # the branch is free again for the child's next task
            runner.gate = None
            answer = await resume_when_free(sessions, job.name, "export.py")
            assert answer.text == "did export.py"
        finally:
            await sessions.aclose()

    asyncio.run(main())


def test_a_cancel_before_the_run_starts_frees_its_branch(parent, store):
    async def main():
        gate = asyncio.Event()
        sessions = Sessions(parent, Writing(store, gate), max_workers=1)
        try:
            first = await sessions.aask("a.py")
            second = await sessions.aask("b.py")  # waits for a slot
            sessions.cancel(second.name)
            gate.set()
            answer = await resume_when_free(sessions, second.name, "c.py")
            assert answer.text == "did c.py"
            assert sessions.result(first.name).text == "did a.py"
        finally:
            await sessions.aclose()

    asyncio.run(main())


def test_a_raising_async_runner_answers_failed(parent, store):
    class Raising:
        async def run(self, session, task, *, budget=None):
            raise RuntimeError("the model went away")

    async def main():
        sessions = Sessions(parent, Raising())
        try:
            answer = await sessions.aask("api.py", wait=True)
            assert answer.status == "failed" and "the model went away" in answer.text
        finally:
            await sessions.aclose()

    asyncio.run(main())


def test_blocking_on_the_runners_own_loop_is_refused(parent, store):
    async def main():
        sessions = Sessions(parent, Writing(store, asyncio.Event()))
        try:
            with pytest.raises(SessionsError, match="aask"):
                sessions.ask("api.py", wait=True)
            job = await sessions.aask("b.py")
            with pytest.raises(SessionsError, match="aclose"):
                sessions.close()
            sessions.cancel(job.name)
        finally:
            await sessions.aclose()

    asyncio.run(main())


def test_an_async_runner_needs_a_loop(parent, store):
    with pytest.raises(SessionsError, match="event loop"):
        Sessions(parent, Writing(store))


def test_a_loop_in_another_thread_takes_asks_from_this_one(parent, store):
    """An embedder's loop on a thread of its own, asked from a plain
    thread: ``ask(wait=True)`` blocks this thread, not the loop."""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        sessions = Sessions(parent, Writing(store), loop=loop)
        answer = sessions.ask("api.py", wait=True)
        assert answer.text == "did api.py"
        sessions.close()
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(5)
        loop.close()


def test_a_cancel_stops_the_run_a_resume_just_started(parent, store):
    """The branch is free as soon as an answer is recorded, before the
    run that recorded it has finished ending: a resume can start the
    next run in that gap (here from ``on_answer``), and a cancel then
    stops that new run, never the old one."""

    async def main():
        runner = Writing(store)
        stopped = asyncio.Event()
        loop = asyncio.get_running_loop()
        sessions = Sessions(parent, runner)
        state = {}

        def resume_and_cancel(name, answer):
            if state:
                return
            runner.gate = asyncio.Event()  # the next run never finishes
            state["job"] = sessions.ask("export.py", resume=name)
            sessions.cancel(name)
            loop.call_soon_threadsafe(stopped.set)

        sessions.on_answer = resume_and_cancel
        try:
            first = await sessions.aask("api.py")
            await asyncio.wait_for(stopped.wait(), 5)
            runner.gate = None
            answer = await resume_when_free(sessions, first.name, "notes.md")
            assert answer.text == "did notes.md"
        finally:
            # bounded: a run the cancel missed would hold aclose forever
            await asyncio.wait_for(sessions.aclose(), 5)

    asyncio.run(main())


def test_an_ask_still_forking_when_aclose_runs_is_refused(parent, store):
    """A fork runs on a thread; a close that begins meanwhile must not be
    outlived by the run that fork would start."""

    async def main():
        runner = Writing(store)
        sessions = Sessions(parent, runner)
        forking = threading.Event()
        proceed = threading.Event()
        fork = sessions._fork

        def slow_fork(*args, **kwargs):
            forking.set()
            proceed.wait(5)
            return fork(*args, **kwargs)

        sessions._fork = slow_fork  # type: ignore[method-assign]
        asking = asyncio.create_task(sessions.aask("api.py"))
        await asyncio.to_thread(forking.wait, 5)
        closing = asyncio.create_task(sessions.aclose())
        await asyncio.sleep(0.05)
        proceed.set()
        await closing
        with pytest.raises(SessionsError, match="closed"):
            await asking
        assert runner.loops == set()  # the runner never ran

    asyncio.run(main())
