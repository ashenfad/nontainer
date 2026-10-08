"""Delegation, host-side: fork a session, hand it to a runner, keep
the job table.

A subagent is a branch. Everything delegation needs already exists —
``fork`` gives the child the whole world at a real commit, ``merge``
and ``checkout(ref, paths=)`` bring its work back, ``diff`` says what
it touched — and what was missing is the calling convention over them:
who mints the child's name, who hands it to the loop, who collects the
answer, and who makes the child's work mergeable.

That is this module. :class:`Sessions` is built by the embedder over a
parent workspace and a :class:`~nontainer.SessionRunner`, and the
adapters expose it to a model as the ``sessions`` tool. It runs
entirely on the host: nothing here enters an executor, so nothing about
it changes per rung and nothing is relayed.

Two rules earn their place here rather than in the runner:

- **The fork point is a commit, and the helper takes it.** ``fork``
  lands the parent's uncommitted writes first, so the child's base is
  a state that existed and the merge base is not older than the
  parent's own edits. The runner is handed that commit (``forked_at``)
  before the first turn, because the provenance header a delegate
  arrives with names it.
- **Everything a delegate wrote is its answer.** A delegate need not
  think about ws-git at all: autocommit puts every write on its branch,
  and a delegate that never touched ws-git answers at its branch head.
  One that did commit and then wrote more would otherwise answer with
  less than it did — its last commit — and leave its merge refused. So
  when it answers, the helper commits that remainder for it, as one
  commit whose message says the delegation mechanism made it, and the
  answer names that commit. Its own commits stay in its log as the
  checkpoints they were. Authoring output (app logs, test_app
  captures; see :mod:`nontainer.ignore`) is never part of it.

Delivery is pull: :meth:`Sessions.ask` returns a :class:`Job` and the
answer is collected later with :meth:`Sessions.result`. How a parent
LEARNS that a delegate finished — a dot in a rail, a message injected
into the next turn — is the embedder's, not nontainer's.
:meth:`Sessions.take` is the collection point for an embedder that
pushes: every answer that landed and has not been collected, so the
same answer never arrives twice however it is read.

An ask starts from this session unless it is told otherwise. ``fork_from``
names another fork point — a commit somebody else tends, spelled
``session@commit`` or named by a store tag — and the child is forked
THERE instead, with a conversation of its own, since a conversation
that is not the asker's cannot be continued. The branch it was forked
from is only read: every ask forks, and the child is what the runner
drives. What reaches the parent either way is the answer and a branch;
merging it, taking files out of it or leaving it alone is the parent's
own step, and nothing here takes it.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import logging
import random
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal

from .errors import (
    BranchExpired,
    CommitNotFoundError,
    JobRunning,
    SessionsError,
    WorkspaceError,
)
from .ignore import Patterns, drop_ignored
from .protocol import SESSION_ID_RE, Answer, Job

if TYPE_CHECKING:
    from .inbox import Inbox, Note
    from .protocol import SessionRunner
    from .workspace import Workspace

_logger = logging.getLogger("nontainer.sessions")

#: What joins a parent's name to a child's pet name. A slash would read
#: better and is what the design asked for, but a session id becomes a
#: branch name and a storage path, so ``SESSION_ID_RE`` allows no
#: separator at all; a dot is in the alphabet and keeps the scoping
#: visible and prefix-deletable ("every branch starting ``parent.``").
SEPARATOR = "."

#: How many names to try before giving up. Each miss appends a numeric
#: suffix, so this bounds the suffix too.
_MINT_TRIES = 8

#: The pet-name vocabulary, kept in the module so delegation costs no
#: dependency. Two words is enough to be typable and quotable across a
#: turn ("merge sleepy-otter"); a numeric suffix settles collisions.
_ADJECTIVES = (
    "amber brave brisk calm clever curious dapper eager fond gentle glad "
    "keen lucky merry mild noble patient plucky quiet rapid ready sleepy "
    "snug solemn spry steady sunny swift tidy vivid warm wise"
).split()
_ANIMALS = (
    "badger bison crane cricket dingo falcon ferret finch gecko heron ibis "
    "jackal koala lemur lynx magpie marten mole narwhal otter owl panda "
    "puffin quail raven seal shrew stoat tapir tiger walrus wombat"
).split()


def pet_name(rng: "random.Random | None" = None) -> str:
    """Two words, hyphenated — a child's name as a human types it."""
    pick = (rng or random).choice
    return f"{pick(_ADJECTIVES)}-{pick(_ANIMALS)}"


def _first_line(*sources: str) -> str:
    """The first non-empty line of the first source that has one."""
    for source in sources:
        for line in source.splitlines():
            stripped = line.strip().lstrip("# ").strip()
            if stripped:
                return stripped[:72]
    return "delegated work"


def _error_text(exc: BaseException) -> str:
    """A runner's failure as the caller reads it: the exception, named.

    No traceback: the frames belong to the embedder's loop, and the
    text of an answer is read by a model that can act on "the runner
    raised TimeoutError" and cannot act on a stack.
    """
    return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__


def _running_loop() -> "asyncio.AbstractEventLoop | None":
    """The event loop running in this thread, if one is."""
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def _accepts_forked_at(runner: "SessionRunner") -> bool:
    """Whether ``runner.run`` takes the fork point.

    A runner belongs to the embedder, and one written against a
    signature that has no ``forked_at`` must keep working — so the fork
    point is passed only where the signature accepts it, by name or
    through ``**kwargs``. A signature does not change, so this is read
    once per runner rather than once per job.
    """
    try:
        params = inspect.signature(runner.run).parameters
    except (TypeError, ValueError):
        return False  # no signature to read: the call without it is safe
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return True
    found = params.get("forked_at")
    return found is not None and found.kind in (
        inspect.Parameter.KEYWORD_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
    )


class Sessions:
    """Delegation over one parent workspace.

    ``Sessions(ws, runner)`` gives the parent the four verbs a
    delegating agent needs — :meth:`ask`, :meth:`list`, :meth:`result`,
    :meth:`cancel` — plus :meth:`keep`. Every one is host-side and
    thread-safe: the parent workspace enforces a single writer, and
    this holds that lock exactly where it mutates the parent (the
    fork, and the diff that reads the child's changes back).

    ``budget`` is the default handed to the runner when :meth:`ask`
    names none; nontainer never interprets it. ``max_workers`` bounds
    how many delegates run at once, and ``chain`` is the provenance
    this session's OWN work descends from — a runner that builds a
    ``Sessions`` for a delegate passes the delegate's chain, so an
    answer three hops down still names every source.

    ``on_answer(name, answer)`` is called once for every answer that is
    recorded, on the worker thread that ran the job, once the job has
    settled: the answer is collectable, the branch is free for a
    resume, and the child handle is closed. An answer a resume has
    replaced by then is still reported: it was recorded. It is how an
    embedder hears that an answer landed without polling — to start
    the parent's next turn, say, when nobody is talking to it. A cancelled job's answer
    is discarded rather than recorded, so it calls nothing. The hook
    may also be set after construction; one that raises is logged and
    the job is unaffected. :meth:`wait` is the same news for a caller
    that would rather block for it.

    **An async runner** (``run`` is ``async def``) runs on an event loop
    rather than a worker thread: ``loop``, or the loop the helper is
    built in. ``max_workers`` then bounds how many of its runs are in
    flight at once, and landing an answer, which does store work, runs
    on a thread so the loop never waits on it. :meth:`cancel` stops such
    a run, where a synchronous runner can only have its answer
    discarded. From a coroutine, :meth:`aask` asks, :meth:`await_ready`
    is :meth:`wait`, and :meth:`answers` yields every answer as it
    lands.

    Not a context the parent workspace owns: build it, and
    :meth:`close` it (or use it as a context manager) when the session
    ends. Closing joins the workers; the branches stay.
    """

    def __init__(
        self,
        workspace: "Workspace",
        runner: "SessionRunner",
        *,
        budget: Any = None,
        max_workers: int = 4,
        chain: Iterable[str] = (),
        on_answer: "Callable[[str, Answer], None] | None" = None,
        loop: "asyncio.AbstractEventLoop | None" = None,
    ) -> None:
        self.on_answer = on_answer
        self._ws = workspace
        self._runner = runner
        self._forked_at_ok = _accepts_forked_at(runner)
        self._budget = budget
        self._chain = tuple(chain)
        self._pool = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="nontainer-sessions"
        )
        #: The loop an async runner's runs are scheduled on, and how many
        #: of them may be in flight; ``None`` for a synchronous runner.
        self._loop: asyncio.AbstractEventLoop | None = None
        self._slots: asyncio.Semaphore | None = None
        if inspect.iscoroutinefunction(getattr(runner, "run", None)):
            self._loop = loop or _running_loop()
            if self._loop is None:
                raise SessionsError(
                    "an async runner needs an event loop to run on: build the "
                    "helper inside one, or pass loop="
                )
            self._slots = asyncio.Semaphore(max_workers)
        self._lock = threading.Lock()
        #: Notified, under the lock, whenever an answer becomes
        #: collectable or a job stops running without one (a cancel, a
        #: close), which is everything :meth:`wait` waits for.
        self._settled = threading.Condition(self._lock)
        self._jobs: dict[str, Job] = {}
        self._answers: dict[str, Answer] = {}
        self._futures: dict[str, Future] = {}
        self._children: dict[str, Workspace] = {}
        self._base: dict[str, str | None] = {}
        # child -> the ref its answer descends from, when it was forked
        # from elsewhere: settled when the ask resolved the fork point,
        # since a bare name read again later may mean something else
        self._sources: dict[str, str] = {}
        #: The jobs whose answer has landed and has not been collected
        #: since it landed. A name goes in when an answer is recorded
        #: and comes out when a caller collects it, through either
        #: :meth:`result` or :meth:`take` — one mark, so a caller that
        #: reads an answer between turns and a caller that takes them
        #: mid-turn never see the same answer twice. A resume records
        #: a new answer, which puts the name back.
        self._uncollected: set[str] = set()
        #: The branches with a run in flight, each held by the run that
        #: reserved it. A branch runs one job at a time: the runner
        #: cannot be interrupted, so a second run on one branch would
        #: drive the child the first is still driving and land its
        #: answer against the other's job. The value is the run's
        #: token, because a hold belongs to ONE run: a run that has
        #: recorded its answer still has its exit to make, and a
        #: resume that starts in between takes the branch over — the
        #: earlier run's exit must not then hand it away.
        self._busy: dict[str, int] = {}
        self._token = 0
        #: The runs whose answer ``_record`` recorded, by token, until
        #: ``on_answer`` has been told. Kept apart from ``_answers``,
        #: which a resume may already have emptied by then.
        self._recorded: set[int] = set()
        #: An async runner's runs in flight, by run token, so a cancel
        #: stops the run that holds the branch now and never one a
        #: resume has superseded.
        self._tasks: dict[int, asyncio.Task] = {}
        #: The coroutines waiting for news (:meth:`await_ready`,
        #: :meth:`answers`): each one's loop and the event to set, under
        #: the lock, when :attr:`_settled` is notified.
        self._watchers: set[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = set()
        self._closed = False

    def __repr__(self) -> str:
        with self._lock:
            running = sum(1 for j in self._jobs.values() if j.status == "running")
            total = len(self._jobs)
        return f"<sessions of {self._ws.session!r}: {total} job(s), {running} running>"

    # -- the verbs -----------------------------------------------------

    def ask(
        self,
        task: str,
        *,
        name: str | None = None,
        paths: "Iterable[str] | str | None" = None,
        inherit: 'Literal["full", "fresh"] | None' = None,
        fork_from: "str | Any | None" = None,
        resume: str | None = None,
        wait: bool = False,
        budget: Any = None,
    ) -> "Job | Answer":
        """Delegate ``task`` to a fork of this session.

        Returns a :class:`Job` — the child's name, and the handle for
        every later verb — or, with ``wait=True``, blocks until the
        runner is done and returns the :class:`Answer`.

        The child is a fork under a pet name scoped to this session
        (``parent.sleepy-otter``), taken at a real commit: uncommitted
        writes here land first and THAT commit is the merge base.
        ``name`` asks for a particular name, and one whose branch is
        already taken is REFUSED — a caller naming the child it means
        to address would otherwise be handed a different branch and
        send every later verb to the wrong one; the refusal says which
        branch, and that ``resume=`` is how the existing child gets its
        next task. A minted pet name carries no such intent, so a
        collision there is settled with a numeric suffix.
        ``paths`` narrows what the child's filesystem shows without
        narrowing its branch, and
        ``inherit`` decides whether the conversation at the fork point
        comes along. Unset, it follows where the child is forked: from
        this session it starts ``"fresh"``, doing work of its own on
        a brief; from anywhere else (``fork_from``) it is ``"full"``.

        ``fork_from`` starts the child somewhere else: another session as
        it is now, by its bare name; a commit named by a store tag; or
        one spelled ``session@commit`` (a short commit id and a
        session's own tag resolve here the way they do for every other
        verb). A store tag wins over a session of the same name. The
        child is forked THERE rather than here, and the branch it came
        from is only read. It arrives, unless told otherwise, with the
        conversation the fork point holds: the delegate is then the
        agent that was there, as of that commit, and the task is its
        next turn. That is what forking from elsewhere is for: another
        session's files are already in reach (``ws-git show``,
        ``checkout``, ``worktree``), and its agent's memory is not.
        ``inherit="fresh"`` gives it a conversation of its own over
        that state instead.

        ``resume`` gives a new task to a child this session already
        has: the same branch with its conversation kept, and no second
        fork. Everything that belongs to the CHILD carries over — where
        it was forked from, its base, whether it is kept — and only the
        run is new. A child answers ONE task at a time: a run still in
        flight refuses the next one rather than sharing the child with
        it, and a cancelled run is in flight until its runner stops,
        since cancel discards an answer without interrupting the
        embedder's loop.

        The runner drives the child on a worker thread and its answer
        is collected with :meth:`result`. A runner that raises does not
        lose the job: it resolves as ``failed`` with the exception's
        text as the answer. Whatever the child does stays on its
        branch: merging it, taking files from it or leaving it is the
        caller's own step.
        """
        self._check_open()
        if wait and self._loop is not None and _running_loop() is self._loop:
            raise SessionsError(
                "ask(wait=True) would block the loop its runner runs on: "
                "await sessions.aask(..., wait=True) instead"
            )
        started = time.time()
        if inherit is None:
            inherit = "full" if fork_from is not None and resume is None else "fresh"
        if inherit != "fresh" and resume is not None:
            raise SessionsError(
                "inherit must be 'fresh' when resuming: a child resumed keeps "
                "the conversation it already has, and there is no second fork "
                "to seed. A brief is content the task carries"
            )
        resuming = resume is not None
        if resume is not None:
            previous, origin = self._resumed(resume, fork_from)
            child_name, base = previous.name, None
            child = self._reopen(child_name)
        else:
            source = None
            if fork_from is not None:
                given, commit, source = self._origin(fork_from)
                origin = (given, commit)
            else:
                origin = None
            try:
                child, child_name, base = self._fork(
                    name, paths=paths, inherit=inherit, at=origin[1] if origin else None
                )
            except CommitNotFoundError as exc:
                # The funnel classifies the word; the store is what
                # knows whether it holds that commit, and it says so
                # with the commit alone.
                raise SessionsError(
                    f"{origin[0]!r} names commit {exc}, which this store does "
                    "not hold, so there is nothing to fork"
                ) from exc
            if source is not None:
                with self._lock:
                    self._sources[child_name] = source
        job = Job(
            name=child_name,
            task=task,
            status="running",
            started=started,
            touched=started,
            origin=origin,
        )
        try:
            job, future = self._submit(
                job,
                child,
                base,
                self._budget if budget is None else budget,
                resuming=resuming,
            )
        except BaseException:
            # The branch was taken by another run between the check and
            # the reservation, the helper closed, or the pool is gone:
            # the handle opened for a run that will not happen is closed
            # here. The branch stays, as every branch does.
            try:
                child.close()
            except Exception:  # noqa: BLE001 - the refusal wins
                pass
            raise
        if wait:
            future.result()  # _work resolves every failure into an Answer
            return self.result(child_name)
        return job

    async def aask(
        self, task: str, *, wait: bool = False, **options: Any
    ) -> "Job | Answer":
        """:meth:`ask`, from a coroutine: the fork, which takes the
        parent's lock and writes, runs on a thread, and ``wait=True``
        awaits the answer rather than blocking the loop."""
        job = await asyncio.to_thread(self.ask, task, wait=False, **options)
        assert isinstance(job, Job)
        if not wait:
            return job
        with self._lock:
            future = self._futures[job.name]
        await asyncio.wrap_future(future)
        return self.result(job.name)

    def list(self) -> list[Job]:
        """Every job this session has asked for, oldest first."""
        with self._lock:
            return sorted(self._jobs.values(), key=lambda j: (j.started, j.name))

    def result(self, name: str) -> Answer:
        """The answer for job ``name``.

        Raises :class:`~nontainer.JobRunning` while the delegate is
        still working — delivery is pull, so an unfinished job gives
        the turn back rather than blocking it —
        :class:`~nontainer.BranchExpired` for a job whose branch
        :meth:`sweep` has taken, and
        :class:`~nontainer.SessionsError` for a name no job has, or a
        job whose answer was discarded by :meth:`cancel`.

        Reading an answer TOUCHES the job. Retention for a delegate's
        branch is an idle TTL, and collecting the answer is the caller
        dealing with the job, so the branch of a delegate whose answer
        is read every turn is never idle however long ago its run
        ended.
        """
        with self._lock:
            name = self._resolve(name)
            job = self._jobs.get(name)
            if job is None:
                raise SessionsError(self._unknown(name))
            if job.status == "running":
                raise JobRunning(
                    f"job {name!r} is still running; ask again on a later turn "
                    "(sessions list shows what is outstanding)"
                )
            if job.status == "expired":
                raise BranchExpired(
                    f"job {name!r} expired: its branch was swept after going "
                    "unread long enough for the retention sweep to take it, so "
                    "there is nothing left to read. Ask again (sessions ask), "
                    "and keep the next one (sessions keep) if it should "
                    "outlive that."
                )
            answer = self._answers.get(name)
            if answer is None:
                raise SessionsError(
                    f"job {name!r} was cancelled, so its answer was discarded. "
                    f"The branch is still there as the delegate left it: "
                    f"ws-git diff {name} shows what it had done."
                )
            self._uncollected.discard(name)
            self._jobs[name] = replace(job, touched=time.time())
            return answer

    def take(self) -> "list[tuple[str, Answer]]":
        """Every answer that landed and has not been collected yet, as
        ``(job name, answer)`` pairs in landing order.

        The push half of delivery, for a caller that wants to hand
        answers to the parent the moment they exist rather than
        waiting for it to ask: an embedder polls this where it can
        reach the model — nontainer's agno adapter drains it into the
        next tool result. Taking never blocks; ``on_answer`` and
        :meth:`wait` are how a caller learns there is something to take.

        Collecting through this TOUCHES each job exactly as
        :meth:`result` does, and marks it collected, so an answer is
        taken once. A job whose delegate is asked again (``resume``)
        records a new answer and appears again — and a resume DISCARDS
        the previous answer, exactly as it does for ``result``: a job
        resumed before its answer was read has nothing to take until
        the new one lands. A cancelled job never appears, since its
        answer was discarded, and an expired one drops out with its
        branch.
        """
        now = time.time()
        taken: list[tuple[str, Answer]] = []
        with self._lock:
            names = sorted(
                self._uncollected,
                key=lambda n: (self._jobs[n].finished or 0.0, n),
            )
            for name in names:
                self._uncollected.discard(name)
                job = self._jobs.get(name)
                answer = self._answers.get(name)
                if job is None or answer is None or job.status == "expired":
                    continue
                self._jobs[name] = replace(job, touched=now)
                taken.append((name, answer))
        return taken

    def outstanding(self) -> list[str]:
        """The jobs this session has not heard the end of: still
        running, or answered and not yet collected. Oldest ask first.

        A runner asks this of a child that has just replied. A reply
        given while the child's own delegates are outstanding is the
        child waiting for them, not its answer (see ``SessionRunner``).
        """
        with self._lock:
            names = [
                name
                for name, job in self._jobs.items()
                if job.status == "running"
                or (name in self._uncollected and job.status != "expired")
            ]
            return sorted(names, key=lambda n: (self._jobs[n].started, n))

    def wait(self, timeout: float | None = None) -> list[str]:
        """Block until an answer is waiting to be collected, or until no
        job is running; the names with an answer waiting, in landing
        order.

        Collects nothing: :meth:`take` or :meth:`result` does that, and
        the names returned are the ones they will hand over. An empty
        list means there is nothing to wait for (no job running and
        none answered), or that ``timeout`` seconds passed first, or
        that the helper closed. A job cancelled while this waits stops
        counting as running, so a wait on it alone ends.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._settled:
            while True:
                ready = self._ready_locked()
                if ready or not self._waiting_locked():
                    return ready
                left = None if deadline is None else deadline - time.monotonic()
                if left is not None and left <= 0:
                    return []
                self._settled.wait(left)

    async def await_ready(self, timeout: float | None = None) -> list[str]:
        """:meth:`wait`, from a coroutine: it awaits, and blocks nothing."""
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + timeout
        while True:
            event = asyncio.Event()
            watcher = (loop, event)
            with self._lock:
                ready = self._ready_locked()
                if ready or not self._waiting_locked():
                    return ready
                self._watchers.add(watcher)
            try:
                left = None if deadline is None else deadline - loop.time()
                if left is not None and left <= 0:
                    return []
                try:
                    await asyncio.wait_for(event.wait(), left)
                except asyncio.TimeoutError:
                    return []
            finally:
                with self._lock:
                    self._watchers.discard(watcher)

    async def answers(self) -> "AsyncIterator[tuple[str, Answer]]":
        """Every answer as it lands, as ``(job name, answer)`` pairs,
        for as long as the helper is open: the push half of delivery,
        from a coroutine. Each is collected as :meth:`take` collects it,
        so an answer is handed over once however it is read."""
        loop = asyncio.get_running_loop()
        while True:
            event = asyncio.Event()
            watcher = (loop, event)
            with self._lock:
                closed = self._closed
                self._watchers.add(watcher)
            try:
                for pair in self.take():
                    yield pair
                if closed:
                    return
                await event.wait()
            finally:
                with self._lock:
                    self._watchers.discard(watcher)

    @property
    def closed(self) -> bool:
        """Whether :meth:`close` has begun. A closed helper asks nothing
        more, and a wait on it ends at once."""
        with self._lock:
            return self._closed

    def _ready_locked(self) -> list[str]:
        """The jobs with an answer waiting, in landing order. Under the
        lock."""
        ready = [
            name
            for name in self._uncollected
            if self._jobs[name].status != "expired" and name in self._answers
        ]
        return sorted(ready, key=lambda n: (self._jobs[n].finished or 0.0, n))

    def _waiting_locked(self) -> bool:
        """Whether there is anything to wait for: a job running, on a
        helper still open. Under the lock."""
        running = any(job.status == "running" for job in self._jobs.values())
        return running and not self._closed

    def _notify_locked(self) -> None:
        """Wake everything waiting for news, threads and coroutines.
        Under the lock."""
        self._settled.notify_all()
        for loop, event in self._watchers:
            try:
                loop.call_soon_threadsafe(event.set)
            except RuntimeError:  # its loop has closed: nothing to wake
                pass

    def base(self, name: str) -> str | None:
        """The commit job ``name`` was forked from.

        The parent's head at the moment the child was taken, which is
        the merge base for everything the delegate does; ``None`` when
        the parent's provider keeps no commits. The runner is handed
        the same value as ``forked_at``, and this is how a caller that
        holds only the job name asks for it.
        """
        with self._lock:
            if name not in self._jobs:
                raise SessionsError(self._unknown(name))
            return self._base.get(name)

    def cancel(self, name: str) -> Job:
        """Stop caring about job ``name``; returns the job.

                A synchronous runner that is already working cannot be
                interrupted — it is the embedder's loop, and nontainer has no
                handle on it — so cancel means this: the answer will be
                DISCARDED when it arrives, and the child's branch is left as it
                is. An async runner's run is a task on the helper's loop, and
                cancel also stops it; its branch is free once the run has ended,
                a moment later. Either way nothing is committed for it and
                nothing is deleted; ``ws-git diff <name>`` still shows whatever
                it managed to do.

        Cancelling and recording an answer are one state transition, taken
                under one lock, so a cancel either wins outright — the answer is
                dropped — or finds a job that has already answered and returns
                it unchanged. There is nothing to undo in the second case: the
                helper writes nothing to the child's branch either way.
        """
        with self._lock:
            name = self._resolve(name)
            job = self._jobs.get(name)
            if job is None:
                raise SessionsError(self._unknown(name))
            if job.status != "running":
                return job
            job = replace(job, status="cancelled", finished=time.time())
            self._jobs[name] = job
            self._notify_locked()
            future = self._futures.get(name)
            token = self._busy.get(name)
            task = None if token is None else self._tasks.get(token)
        if task is not None and self._loop is not None:
            # An async run is stopped, not just disowned: it ends through
            # its own path, which discards the answer and frees the
            # branch.
            self._loop.call_soon_threadsafe(task.cancel)
            return job
        if future is not None and future.cancel():
            # It bit: the worker never started, so no run will land for
            # this job and none will release what it reserved. The
            # branch is free for another task, and the handle taken for
            # the run that will not happen is closed here — the branch
            # itself is untouched, as it is for a cancel that comes too
            # late.
            with self._lock:
                if token is not None:
                    self._release_locked(name, token)
                child = self._children.pop(name, None)
            if child is not None:
                try:
                    child.close()
                except Exception:  # noqa: BLE001 - the cancel wins
                    pass
        return job

    def keep(self, name: str) -> Job:
        """Promote job ``name`` out of the retention sweep; returns it.

        :meth:`sweep` deletes the branch of a finished job that has
        gone unread for long enough; this is the flag that exempts one
        from it, for good. A caller says so at the moment it decides —
        while it is reading the answer — rather than whenever a sweep
        happens to be scheduled, and the job is touched here too,
        because asking to keep a delegate is dealing with it.

        Raises :class:`~nontainer.BranchExpired` for a job the sweep
        has already taken: its branch is gone, and recording a flag
        over nothing would read as a promise this cannot make.
        """
        with self._lock:
            name = self._resolve(name)
            job = self._jobs.get(name)
            if job is None:
                raise SessionsError(self._unknown(name))
            if job.status == "expired":
                raise BranchExpired(
                    f"job {name!r} expired: its branch was already swept, so "
                    "there is nothing to keep. Ask again (sessions ask) and "
                    "keep the new job while its answer is fresh."
                )
            job = replace(job, kept=True, touched=time.time())
            self._jobs[name] = job
            return job

    def sweep(self, idle: float, *, min_age: float = 3600) -> list[str]:
        """Delete the branches of jobs idle for ``idle`` seconds;
        returns the names swept, sorted.

        Retention for a delegate's branch is an idle TTL, and this is
        the reaper the embedder schedules for it — a
        ``sessions.sweep(idle=24 * 3600)`` beside ``store.clean()`` and
        whatever else it sweeps on a timer. Nothing calls it for the
        embedder: an ask that swept would make the retention of one
        delegate depend on how often another is asked for.

        A branch goes when all of this holds: its job has finished (a
        run still driving it is not idle, whatever the clock says), no
        caller has asked to :meth:`keep` it, and ``Job.touched`` — the
        moment the caller last dealt with it, moved by :meth:`result` and
        :meth:`keep` — is at least ``idle`` seconds ago. The job's row
        stays and its status becomes ``expired``, keeping the time its
        run finished; its answer is dropped, since the branch the
        answer describes is gone. :meth:`base` still answers, because
        the fork point is recorded here rather than read off the
        branch.

        **Only this helper's own jobs.** A delegate's own delegates —
        ``<child>.<pet>`` — belong to the helper the runner built for
        that child, and are that embedder's to sweep. Ownership is
        recorded by the embedder that asked, never inferred from the
        dot in a name: a human may create ``foo.notes`` beside ``foo``,
        and a sweep that deleted by prefix would take it.

        ``min_age`` is :meth:`Store.delete`'s grace period for the
        orphan commits a deleted branch leaves behind.
        """
        store = self._ws.store
        if store is None:
            raise SessionsError(
                f"cannot sweep the branches of {self._ws.session!r}: that "
                "session was not opened from a store, so there is no store to "
                "delete them from"
            )
        cutoff = time.time() - idle
        # One critical section for the whole sweep, deletion included:
        # the branches are chosen, taken and marked without the table
        # being read or written in between, so a resume cannot start on
        # a branch this is about to delete and then be recorded over as
        # expired.
        with self._lock:
            names = sorted(
                name
                for name, job in self._jobs.items()
                if job.status not in ("running", "expired")
                and name not in self._busy
                and not job.kept
                and job.touched <= cutoff
            )
            if not names:
                return []
            store.delete(names, min_age=min_age)
            for name in names:
                self._answers.pop(name, None)
                self._uncollected.discard(name)
                self._jobs[name] = replace(self._jobs[name], status="expired")
            return names

    # -- lifecycle -----------------------------------------------------

    def close(self) -> None:
        """Join the workers and release the child handles.

        The branches stay: a delegate's work is in the store, and
        closing the helper that asked for it must not throw it away.
        """
        if self._loop is not None and _running_loop() is self._loop:
            if self._pending_runs():
                raise SessionsError(
                    "an async runner's runs are still in flight on this loop, "
                    "and closing joins them: await sessions.aclose() instead"
                )
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._notify_locked()
        concurrent.futures.wait(self._pending_runs())
        self._pool.shutdown(wait=True)
        self._close_children()

    async def aclose(self) -> None:
        """:meth:`close`, from a coroutine: it awaits the runs in flight
        rather than blocking the loop they run on."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._notify_locked()
        for future in self._pending_runs():
            try:
                await asyncio.wrap_future(future)
            except BaseException:  # noqa: BLE001 - closing waits, it does not judge
                pass
        await asyncio.to_thread(self._pool.shutdown, wait=True)
        self._close_children()

    def _pending_runs(self) -> list[Future]:
        """An async runner's runs not yet done."""
        if self._loop is None:
            return []
        with self._lock:
            return [f for f in self._futures.values() if not f.done()]

    def _close_children(self) -> None:
        with self._lock:
            children = list(self._children.values())
            self._children.clear()
        for child in children:
            try:
                child.close()
            except Exception:  # noqa: BLE001 - closing the rest wins
                pass

    def __enter__(self) -> "Sessions":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- internals -----------------------------------------------------

    def _check_open(self) -> None:
        if self._closed:
            raise SessionsError(
                f"the sessions helper for {self._ws.session!r} is closed; "
                "its workers are joined and no new job can be asked"
            )

    def _resolve(self, name: str) -> str:
        """The job ``name`` means: itself when a job has that name,
        else the child this session scoped under it.

        An agent that asked with ``name="backend"`` addresses the child
        it named, and the child is ``<session>.backend`` — so the name
        it gave works wherever a job's name is taken, rather than
        costing it a refusal that lists the full one."""
        name = str(name).strip()
        if name in self._jobs:
            return name
        scoped = f"{self._ws.session}{SEPARATOR}{name}"
        return scoped if scoped in self._jobs else name

    def _unknown(self, name: str) -> str:
        known = ", ".join(sorted(self._jobs)) or "none"
        return f"no job named {name!r} on session {self._ws.session!r} (have: {known})"

    def _fork(
        self,
        name: str | None,
        *,
        paths: "Iterable[str] | str | None",
        inherit: str,
        at: str | None = None,
    ) -> "tuple[Workspace, str, str | None]":
        """``(child, its name, the fork point)``.

        The name is minted and the fork taken under the parent's own
        lock, so two asks racing on one session cannot pick the same
        name — and the fork itself is the authority on collision, since
        a branch may exist that no job here remembers.

        ``at`` names the commit to fork FROM, for a child that starts
        somewhere other than this session's head. It is the child's
        base, and this session's uncommitted writes are left alone,
        because they are no part of where that child came from.

        A name the CALLER gave is refused when its branch is taken,
        rather than suffixed: the caller is naming the child it means to
        address, and handing back a different branch sends every later
        verb to the wrong one. A minted pet name has no such meaning, so
        a collision there is settled with a suffix and reported to
        nobody.
        """
        minted = name is None
        wanted = name or pet_name()
        with self._ws.lock:
            for attempt in range(_MINT_TRIES if minted else 1):
                candidate = self._scoped(wanted, attempt)
                try:
                    child = self._ws.fork(
                        candidate, at=at, inherit=inherit, paths=paths
                    )
                except WorkspaceError as exc:
                    if "already exists" not in str(exc):
                        raise
                    if not minted:
                        raise SessionsError(
                            f"session {candidate!r} already exists, so "
                            f"name={wanted!r} cannot start a new child there. "
                            f"To give that child its next task, "
                            f"resume={wanted!r}; to start another one, ask "
                            f"under a different name or leave name off and "
                            f"take a minted one."
                        ) from exc
                    continue
                return child, candidate, self._ws.head if at is None else at
        raise SessionsError(
            f"could not mint a free name for a child of {self._ws.session!r} "
            f"in {_MINT_TRIES} tries (last: {candidate!r})"
        )

    def _scoped(self, wanted: str, attempt: int) -> str:
        """``parent.pet`` — plus ``.2``, ``.3`` … past attempt zero."""
        candidate = f"{self._ws.session}{SEPARATOR}{wanted}"
        if attempt:
            candidate = f"{candidate}{SEPARATOR}{attempt + 1}"
        if not SESSION_ID_RE.match(candidate):
            raise SessionsError(
                f"{wanted!r} cannot name a child session: {candidate!r} is not "
                "a session id (letters, digits, '_', '-' and '.', and a "
                "session id is a branch name, so it holds no separators)"
            )
        return candidate

    def _submit(
        self,
        job: Job,
        child: "Workspace",
        base: str | None,
        budget: Any,
        *,
        resuming: bool = False,
    ) -> "tuple[Job, Future]":
        """Reserve the job's branch, install the job, put it on a
        worker; returns the job as installed and its future.

        The reservation and the install are ONE critical section, which
        is what makes the handoff of a branch from one run to the next
        atomic: two callers resuming one child cannot both find it
        free, because the one that finds it free takes it in the same
        breath. The loser is refused rather than sharing the child.

        A resumed branch keeps everything of the child's and replaces
        everything of the run's: the previous answer goes, because it
        is not the answer to this task, and ``result`` says the job is
        running until the new one lands. What the child's own row
        holds — where it was forked from, its base, whether a caller
        has kept it — is READ HERE, in the section that installs the
        replacement, so a ``keep`` that lands while the resume is being
        prepared is not overwritten by a copy taken a moment earlier.
        """
        name = job.name
        with self._lock:
            # Open is checked again here, in the section that installs
            # the run: an ask that passed the first check while a close
            # began must not start a run the close will not wait for.
            self._check_open()
            if name in self._busy:
                raise SessionsError(self._still_running(name))
            if resuming:
                current = self._jobs.get(name)
                if current is not None:
                    job = replace(
                        job, kept=current.kept, origin=job.origin or current.origin
                    )
                base = self._base.get(name, base)
            self._token += 1
            token = self._busy[name] = self._token
            self._jobs[name] = job
            # The previous answer goes with its uncollected mark: a
            # resumed job has nothing to take until the new one lands.
            self._answers.pop(name, None)
            self._uncollected.discard(name)
            self._children[name] = child
            self._base[name] = base
            # Scheduled in the same section, so a cancel never finds the
            # branch reserved without the run's future: both only
            # schedule, and the run itself starts by waiting on this lock.
            try:
                if self._loop is not None:
                    future = asyncio.run_coroutine_threadsafe(
                        self._awork(name, job.task, budget, token), self._loop
                    )
                else:
                    future = self._pool.submit(
                        self._work, name, job.task, budget, token
                    )
            except BaseException:
                self._children.pop(name, None)
                self._release_locked(name, token)
                raise
            self._futures[name] = future
        return job, future

    def _still_running(self, name: str) -> str:
        """Why a branch cannot take a task yet."""
        return (
            f"a run on {name!r} is still finishing; ask again on a later turn. "
            "A delegate does one task at a time, and a runner already working "
            "cannot be interrupted (sessions list shows what is outstanding)"
        )

    def _release(self, name: str, token: int) -> None:
        """Give up a branch — if this run is the one still holding it."""
        with self._lock:
            self._release_locked(name, token)

    def _release_locked(self, name: str, token: int) -> None:
        """:meth:`_release` for a caller already under the lock.

        A run releases the branch it reserved and no other: by the time
        a run exits, the branch may belong to the run that took it over
        when this one's answer landed, and freeing THAT would let a
        third run drive the same child.
        """
        if self._busy.get(name) == token:
            del self._busy[name]

    def _resumed(
        self, resume: str, fork_from: "str | Any | None"
    ) -> "tuple[Job, tuple[str, str] | None]":
        """``(the job on the child, where the child was forked from)``
        for an ask that continues a child this session already has.

        The job comes back whole because what belongs to the CHILD
        rather than to one run of it carries over to the run that
        continues it. A child carries the conversation of the state it
        was forked from, so it answers from there and nowhere else:
        naming a different fork point is refused rather than quietly
        answered by the child at hand.
        """
        with self._lock:
            resume = self._resolve(resume)
            job = self._jobs.get(resume)
            # Whether a run is in flight, which is not what the job's
            # status says: ``cancel`` marks a job cancelled at once and
            # the runner it cannot interrupt goes on driving the child.
            running = resume in self._busy
        if job is None:
            raise SessionsError(self._unknown(resume))
        if job.status == "expired":
            raise BranchExpired(
                f"{resume!r} expired: its branch was swept, so there is "
                "nothing to resume — a resume continues a child, and the "
                "child is the branch. Ask afresh (sessions ask), and keep "
                "the next one (sessions keep) if it should outlive the sweep."
            )
        if running:
            raise SessionsError(self._still_running(resume))
        if fork_from is not None:
            given = str(fork_from).strip()
            if job.origin is None:
                raise SessionsError(
                    f"{resume!r} was forked from this session, not from "
                    f"{given!r}: leave the fork point out to continue it, or "
                    f"ask afresh from {given!r}"
                )
            if given != job.origin[0]:
                raise SessionsError(
                    f"{resume!r} was forked from {job.origin[0]!r} and carries "
                    f"that state's conversation, so it cannot answer from "
                    f"{given!r}: leave the fork point out to continue it, or "
                    f"ask afresh from {given!r}"
                )
        return job, job.origin

    def _reopen(self, name: str) -> "Workspace":
        """A second handle on a branch this session forked earlier.

        The handle taken at the fork is closed once its answer lands —
        a job holds no store open between turns — so continuing a child
        opens the branch again. Reads only: the runner drives the child
        through a handle of its own, and this one reports what the
        branch holds afterwards. It carries the parent's ignore rules, as
        a fork does: what a delegate's answer counts as work (its
        changed paths, its remainder commit) is judged by them.
        """
        store = self._ws.store
        if store is None:
            raise SessionsError(
                f"cannot continue {name!r}: session {self._ws.session!r} was not "
                "opened from a store, so its branches cannot be opened again"
            )
        return store.open(name, root=self._ws.root, ignore=self._ws.ignore)

    def _origin(self, fork_from: "str | Any") -> "tuple[str, str, str]":
        """``(the fork point as the caller spelled it, its commit, the
        ref an answer from there descends from)``.

        A ``session@commit`` ref — or a :class:`~nontainer.Ref` —
        resolves the way every other cross-session read does, so a short
        commit id and a session's own tag are spellings this takes too.
        A bare name is a store tag, the name a commit outlives its
        session under, or else a session as it is now.

        The third part is settled here, with the name: a store tag
        enters a provenance chain as its bare commit (it belongs to no
        session), and a session as ``session@commit``. Reading the
        name again when the answer lands could classify it differently,
        if a tag or session of that name came or went during the run.
        """
        from .store import Ref

        given = str(fork_from).strip()
        if not given:
            raise SessionsError(
                "a fork point must be named: a store tag, or a 'session@commit' "
                "ref. Leave it out to fork this session"
            )
        if not self._ws.caps.versioned:
            raise SessionsError(
                f"session {self._ws.session!r} is on a provider that keeps no "
                "commits, and a fork point is a commit; forking from elsewhere "
                "needs a versioned provider"
            )
        if isinstance(fork_from, Ref) or "@" in given:
            with self._ws.lock:
                commit = self._ws.expand_ref(fork_from).commit
            return given, commit, str(Ref(Ref.parse(given).session, commit))
        tags = self._store_tags()
        if given in tags:
            return given, tags[given], tags[given]
        head = self._session_head(given)
        if head is not None:
            return given, head, str(Ref(given, head))
        known = ", ".join(sorted(tags)) or "none"
        sessions = self._session_names()
        shown = ", ".join(sessions[:12]) + (" …" if len(sessions) > 12 else "")
        raise SessionsError(
            f"nothing named {given!r} on this store (sessions: {shown or 'none'}; "
            f"store tags: {known}). A fork point is a session (as it is now), "
            "a store tag, or a commit spelled 'session@commit'"
        )

    def _store_tags(self) -> dict[str, str]:
        """The store's tags, name -> commit; empty with no store or a
        store that keeps none.

        A store tag belongs to no session and outlives the one that
        made it, which is what lets a commit be a fork point long after
        the session that reached it is gone.
        """
        from .errors import NotSupportedError

        store = self._ws.store
        if store is None:
            return {}
        try:
            return dict(store.tags.list())
        except NotSupportedError:
            return {}

    def _session_names(self) -> list[str]:
        store = self._ws.store
        if store is None:
            return []
        try:
            return sorted(store.sessions())
        except Exception:  # noqa: BLE001 - a listing for a message
            return []

    def _session_head(self, name: str) -> str | None:
        """Session ``name``'s latest commit, or None for a name no
        session has. The commit holds the session's files and its
        conversation alike, so a fork there with ``inherit="full"`` is
        its agent as of its last turn."""
        from .errors import NotSupportedError

        try:
            with self._ws.lock:
                return self._ws.provider.branch_head(name)
        except (ValueError, NotSupportedError):
            return None

    def _work(self, name: str, task: str, budget: Any, token: int) -> Answer:
        """The worker body: run, then land. Never raises.

        A job that dies with its thread is a job the caller waits on
        forever, so both halves resolve into an :class:`Answer` — the
        runner's failure as ``failed`` with the exception's text, and a
        failure to land the answer as the answer plus what went wrong.
        """
        extra = self._extra(name)
        try:
            try:
                out = self._runner.run(
                    self._session_of(name), task, budget=budget, **extra
                )
                answer = out if isinstance(out, Answer) else Answer(text=str(out))
            except BaseException as exc:  # noqa: BLE001 - the job must resolve
                answer = Answer(text=_error_text(exc), status="failed")
            return self._settle(name, task, answer, token)
        finally:
            # The branch is free again however this went, including the
            # ways that record no answer: a job the caller cancelled
            # releases the child when its runner finally stops, not
            # when the cancel is asked for. Already released, and
            # possibly taken over by a resume, when the answer landed —
            # so this frees the branch only while it is still this
            # run's to free.
            self._release(name, token)

    async def _awork(self, name: str, task: str, budget: Any, token: int) -> Answer:
        """:meth:`_work` for an async runner, on the helper's loop: the
        run awaits a slot (``max_workers``), and the landing, which does
        store work, runs on a thread. A cancel stops the run (see
        :meth:`cancel`); its answer is then discarded as any cancelled
        job's is. Never raises."""
        extra = self._extra(name)
        with self._lock:
            current = asyncio.current_task()
            if current is not None:
                self._tasks[token] = current
        try:
            try:
                assert self._slots is not None
                async with self._slots:
                    out = await self._runner.run(
                        self._session_of(name), task, budget=budget, **extra
                    )
                answer = out if isinstance(out, Answer) else Answer(text=str(out))
            except asyncio.CancelledError:
                answer = Answer(text="the run was stopped", status="failed")
            except BaseException as exc:  # noqa: BLE001 - the job must resolve
                answer = Answer(text=_error_text(exc), status="failed")
            return await asyncio.to_thread(self._settle, name, task, answer, token)
        finally:
            with self._lock:
                self._tasks.pop(token, None)
            self._release(name, token)

    def _extra(self, name: str) -> dict[str, Any]:
        """The fork point, for a runner whose signature accepts it: an
        embedder's runner written without the parameter gets the call it
        was written for."""
        with self._lock:
            return {"forked_at": self._base.get(name)} if self._forked_at_ok else {}

    def _settle(self, name: str, task: str, answer: Answer, token: int) -> Answer:
        """Land the run's answer and announce it. Never raises: a failure
        to land is the answer plus what went wrong."""
        try:
            settled = self._land(name, task, answer, token)
        except BaseException as exc:  # noqa: BLE001 - the landing must resolve
            settled = replace(
                answer,
                status="failed",
                text=(
                    f"{answer.text}\n\n[the delegate answered, and its work "
                    f"could not be landed: {_error_text(exc)}]"
                ),
                branch=name,
            )
            self._record(name, settled, token)
        self._announce(name, settled, token)
        return settled

    def _announce(self, name: str, answer: Answer, token: int) -> None:
        """Call ``on_answer`` for an answer this run recorded. Not for a
        job cancelled first: its answer was discarded, and that is not
        news. Asked of the run rather than of the job's current answer,
        because a resume between the recording and here has already
        replaced that, and the answer was recorded all the same. Never
        raises, because the job has already resolved."""
        with self._lock:
            if token not in self._recorded:
                return
            self._recorded.discard(token)
        callback = self.on_answer
        if callback is None:
            return
        try:
            callback(name, answer)
        except Exception:  # noqa: BLE001 - the job is settled either way
            _logger.warning("sessions: on_answer raised for %s", name, exc_info=True)

    def _session_of(self, name: str) -> str:
        with self._lock:
            return self._children[name].session

    def _land(self, name: str, task: str, answer: Answer, token: int) -> Answer:
        """Describe what the delegate did and record the answer.

        Nothing is committed here. The runner drove the child through a
        handle of its own, so this one is re-read from the branch first
        and then only reports: the commit the answer names, the paths
        that changed, and whether the delegate left work its own merge
        would refuse.

        The child handle is closed either way: its branch is what the
        parent merges from, and holding a second handle on it past the
        job keeps a store open for nothing.
        """
        with self._lock:
            child = self._children.pop(name, None)
            base = self._base.get(name)
            cancelled = self._jobs[name].status == "cancelled"
        if child is None:
            # a cancel freed the run before it began, and its handle with
            # it: the answer is discarded, as any cancelled job's is
            return answer
        try:
            if cancelled:
                # Discarded, and the branch left exactly as the
                # delegate left it.
                return answer
            self._commit_remainder(name, child)
            commit = self._landed(child)
            answer = replace(
                answer,
                ref=f"{child.session}@{commit}" if commit else None,
                branch=child.session,
                changed=self._changed(base, child),
                uncommitted=self._uncommitted(child),
                provenance=self._provenance(answer, child, name, base, commit),
            )
            self._record(name, answer, token)
            return answer
        finally:
            try:
                child.close()
            except Exception:  # noqa: BLE001 - recording the answer wins
                pass

    def _commit_remainder(self, name: str, child: "Workspace") -> None:
        """Commit what the delegate wrote past its own last ws-git
        commit, so its answer is everything it did.

        Only a delegate that used ws-git has a remainder: one that never
        did answers at its branch head, where autocommit already put
        every write. Authoring output is not work and never counts.

        Not while a merge of the delegate's own is unresolved: its
        conflict markers would go into the commit and on to whoever
        merges the answer, with nothing saying they were there. That
        remainder is left as it is, and the answer reports it as
        uncommitted, so the merge refuses it, as it always has. A
        commit that fails for any other reason is left the same way.
        """
        refresh = getattr(child.provider, "refresh", None)
        if refresh is not None and child.caps.versioned:
            refresh()
        if not child.caps.index or child.index.head is None:
            return
        try:
            status = child.index.status()
            if not (status.staged or status.unstaged):
                return
            if status.merge_unresolved:
                _logger.info(
                    "sessions: %s answered mid-merge; leaving its work uncommitted",
                    name,
                )
                return
            if status.unstaged:
                child.index.stage(status.unstaged)
            child.index.commit(
                "Work the delegate had not committed when it answered, "
                "committed by the delegation mechanism",
                info={"committed_for": name},
            )
        except Exception as e:  # noqa: BLE001 - the answer reports the rest
            _logger.warning(
                "sessions: could not commit what %s left uncommitted: %s", name, e
            )

    def _landed(self, child: "Workspace") -> str | None:
        """The commit the answer names: what the delegate LANDED.

        Its last ws-git commit when it made one, which by now holds
        everything it wrote (:meth:`_commit_remainder`). Its branch head
        when it never touched ws-git,
        because autocommit put every write there and the fiction reads
        such a session at its store head, which is the honest answer
        for it. Either way this is the same commit ``merge`` and
        ``checkout(<child>, paths=)`` resolve the name to, so what the
        answer cites and what the terminal brings back cannot disagree.

        The runner used a handle of its own, so the branch is re-read
        before any of it is asked.
        """
        refresh = getattr(child.provider, "refresh", None)
        if refresh is not None and child.caps.versioned:
            refresh()
        if not child.caps.index:
            return child.head
        return child.index.head or child.head

    def _uncommitted(self, child: "Workspace") -> bool:
        """Whether the delegate wrote past its own last ws-git commit.
        Only when :meth:`_commit_remainder` could not commit the rest.

        Asked in the merge's own terms, so the answer cannot say one
        thing and ``ws-git merge <name>`` another: it is the same check
        that refuses a source with work its agent has not committed.
        False for a delegate that never used ws-git, which has no commit
        of its own to differ from.

        Read through the child's handle (which carries the parent's
        ignore rules), never the parent's. Landing reads committed
        history only, so it takes no lock of the parent's: agent code
        waiting for this answer inside a ``run_python`` call holds it.
        """
        if not self._ws.caps.index:
            return False
        from .agentgit import AgentGit

        return bool(AgentGit(child).source_uncommitted(child.session))

    def _changed(self, base: str | None, child: "Workspace") -> dict:
        """The child's changed paths, grouped seed vs elsewhere.

        Where the parent and child last met (the fork point, until the
        parent merges the child) against the child's head — one store,
        so a commit on either branch is legible from both — and grouped
        the way ``WorkspaceDiff`` groups: under what the child was
        seeded with, and everywhere else. A delegate that touched a
        caller in ``main.py`` is the common case, and the grouping is
        what keeps that from going unnoticed.

        Read through the child's handle, the parent's head included:
        see :meth:`_uncommitted`.
        """
        head = child.head
        if base is None or head is None:
            return {"seed": (), "elsewhere": ()}
        # From where the two last met, when that is since the fork: a
        # resumed delegate whose earlier answer was merged would
        # otherwise list that work again as what this task changed (the
        # base `ws-git diff <name>` and a merge use). A meeting point
        # that is not past the fork — a child forked from another
        # session's state shares little or nothing with this one —
        # leaves the fork point, which is ITS start.
        start = base
        find = getattr(child.provider, "merge_base", None)
        ours = self._parent_head(child)
        if find is not None and ours is not None:
            met = find(ours, head)
            if met and met != base and find(base, met) == base:
                start = met
        diff = child.diff(start, head)
        # the parent's rules, whatever handle the child was read through
        rules = Patterns(self._ws.ignore)
        kept = drop_ignored(diff.paths, self._ws.root, rules)
        return {
            "seed": tuple(sorted(diff.in_seed & kept)),
            "elsewhere": tuple(sorted(diff.elsewhere & kept)),
        }

    def _parent_head(self, reader: "Workspace") -> str | None:
        """This session's commit as the store has it, read through
        ``reader`` (a child's handle) rather than the parent's own."""
        branch_head = getattr(reader.provider, "branch_head", None)
        if branch_head is None:
            return None
        try:
            return branch_head(self._ws.session)
        except ValueError:
            return None

    def _provenance(
        self,
        answer: Answer,
        child: "Workspace",
        name: str,
        base: str | None,
        commit: str | None,
    ) -> dict:
        """Where the answer came from. The runner's own record (model,
        turns, tokens) is kept; what only the host knows is added.

        ``chain`` is the full path of refs and not the last hop: where
        the child came from, then the child at the commit it answered
        from, behind whatever chain this session's own work already
        carried. An answer must not be able to launder its sources by
        passing through one more delegate.

        Where the child came from is this session at the commit it
        delegated from, or the fork point it was given instead: an
        answer grown from somebody else's state says so rather than
        naming the asker. A fork point named by a store tag enters the
        chain as the bare commit, because a store tag belongs to no
        session and ``session@commit`` has no honest session half for
        it.
        """
        with self._lock:
            job = self._jobs[name]
        source = self._descends_from(job, base)
        hop = [source] if source else []
        if commit:
            hop.append(f"{child.session}@{commit}")
        extra: dict[str, Any] = {}
        if job.origin:
            extra["from"] = job.origin[0]
            if not self._holds(job.origin[1], child):
                extra["outside"] = True
        return {
            **answer.provenance,
            "session": child.session,
            "commit": commit,
            "started": job.started,
            "finished": time.time(),
            "chain": (*self._chain, *hop),
            **extra,
        }

    def _holds(self, commit: str, reader: "Workspace") -> bool:
        """Whether ``commit`` is in this session's history, so that a
        child forked there merges back as any fork does. ``True`` where
        the provider cannot say: the ordinary guidance is the default.
        Read through ``reader``, as :meth:`_changed` reads."""
        find = getattr(reader.provider, "merge_base", None)
        if find is None:
            return True
        head = self._parent_head(reader)
        return head is not None and find(commit, head) == commit

    def _descends_from(self, job: Job, base: str | None) -> str | None:
        """The ref a job's answer descends FROM."""
        if job.origin is not None:
            # what the ask resolved, not the name read again now: a tag
            # or session made or removed during the run would change it
            with self._lock:
                return self._sources.get(job.name, job.origin[1])
        return f"{self._ws.session}@{base}" if base else None

    def _record(self, name: str, answer: Answer, token: int) -> None:
        """Store the answer and close the job out.

        Cancellation wins: a job the caller stopped caring about does
        not un-cancel because its runner finished a moment later. The
        check and the write are one critical section, which is the
        whole of the race — nothing is written to the child's branch,
        so there is nothing else for a cancel to be too late for.
        """
        with self._lock:
            job = self._jobs[name]
            if job.status == "cancelled":
                return
            # Released with the answer, in the same critical section
            # that records it: a caller that reads a finished status
            # can give the child its next task at once, and nothing
            # after this point touches the job's table rows.
            self._release_locked(name, token)
            self._answers[name] = answer
            self._uncollected.add(name)
            self._recorded.add(token)
            self._notify_locked()
            self._jobs[name] = replace(
                job,
                status=answer.status,
                ref=answer.ref,
                finished=time.time(),
                changed=dict(answer.changed),
                uncommitted=answer.uncommitted,
            )


# ======================================================================
# Settling: a delegate answers once nothing it waits on is outstanding
# ======================================================================


#: How often a settling loop looks at the inbox while it waits for an
#: answer: notes have no wait of their own.
POLL = 0.5


@dataclass(frozen=True)
class Settled:
    """How a delegate's turns ended: its last ``reply``, the woken turns
    it took (``wakes``), and what was still outstanding when it ran out
    of them: the delegates it had not heard back from (``unread``) and
    the notes left undelivered (``notes``)."""

    reply: str
    wakes: int = 0
    unread: tuple[str, ...] = ()
    notes: int = 0

    @property
    def text(self) -> str:
        """The reply as an answer: as it is, or saying which delegates
        it was given before hearing from."""
        if not self.unread:
            return self.reply
        names = ", ".join(self.unread)
        return (
            f"{self.reply}\n\n[answered before hearing back from {names}: "
            "their work is on their branches, and `sessions result` reads "
            "an answer once it lands]"
        )


def until_settled(
    run_turn: "Callable[[str | None], str]",
    sessions: "Sessions | None" = None,
    inbox: "Inbox | None" = None,
    *,
    prompt: str,
    max_wakes: int = 10,
    poll: float = POLL,
) -> Settled:
    """Run a delegate's turns until nothing it waits on is outstanding.

    ``run_turn(prompt)`` runs one turn and returns its reply;
    ``run_turn(None)`` is a WOKEN turn, which opens with what is waiting
    to be delivered (see :meth:`~nontainer.turns.Turn.opening`). The
    first turn is the task's. After each reply the delegate is settled
    when its own delegates (``sessions``) have nothing outstanding and
    its ``inbox`` holds no note: a reply given while either is pending is
    the delegate waiting, not its answer. While delegates are still
    running it waits for one to answer, looking at the inbox every
    ``poll`` seconds, since a note from the delegate's caller counts too;
    then it wakes the delegate to read what came.

    ``max_wakes`` bounds the woken turns. Once they are spent it stops
    waiting and returns the last reply, with what was left outstanding:
    :attr:`Settled.text` names the unread delegates. A ``sessions`` that
    closes while its delegates still run ends the wait the same way.

    What else a turn is (nudges, what counts as a reply, budgets) is the
    runner's: this is only the waiting every runner needs, done once.
    """
    reply = run_turn(prompt)
    wakes = 0
    while True:
        pending = bool(inbox is not None and inbox.pending())
        outstanding = sessions.outstanding() if sessions is not None else []
        if not pending and not outstanding:
            return Settled(reply, wakes)
        if wakes >= max_wakes:
            return _spent(reply, wakes, outstanding, inbox)
        ready = sessions.wait(timeout=0) if sessions is not None else []
        if not pending and not ready:
            assert sessions is not None
            if sessions.closed:
                # Nothing will come: a closed helper's waits end at once.
                return _spent(reply, wakes, outstanding, inbox)
            sessions.wait(timeout=poll)
            continue
        reply = run_turn(None)
        wakes += 1


async def auntil_settled(
    run_turn: "Callable[[str | None], Awaitable[str]]",
    sessions: "Sessions | None" = None,
    inbox: "Inbox | None" = None,
    *,
    prompt: str,
    max_wakes: int = 10,
    poll: float = POLL,
) -> Settled:
    """:func:`until_settled`, from a coroutine: ``run_turn`` is awaited,
    and so is the wait for an answer."""
    reply = await run_turn(prompt)
    wakes = 0
    while True:
        pending = bool(inbox is not None and inbox.pending())
        outstanding = sessions.outstanding() if sessions is not None else []
        if not pending and not outstanding:
            return Settled(reply, wakes)
        if wakes >= max_wakes:
            return _spent(reply, wakes, outstanding, inbox)
        ready = sessions.wait(timeout=0) if sessions is not None else []
        if not pending and not ready:
            assert sessions is not None
            if sessions.closed:
                return _spent(reply, wakes, outstanding, inbox)
            await sessions.await_ready(timeout=poll)
            continue
        reply = await run_turn(None)
        wakes += 1


def _spent(
    reply: str, wakes: int, outstanding: "list[str]", inbox: "Inbox | None"
) -> Settled:
    return Settled(
        reply,
        wakes,
        unread=tuple(outstanding),
        notes=len(inbox.pending()) if inbox is not None else 0,
    )


# ======================================================================
# The tool half: what a model sends, and what it reads back
# ======================================================================
#
# The adapters register ONE tool named ``sessions`` with an ``action``
# argument (the shape ``test_app`` has), so a model learns one spelling
# for delegation and one — ws-git, in the terminal — for versioning.
# Both adapters call the dispatch below, so the two surfaces cannot
# drift into saying different things about the same job.


def coerce_paths(paths: Any) -> "list[str] | None":
    """Normalize a loosely-typed ``paths`` argument.

    Models routinely send a list as a JSON string, and a single path as
    a bare string. Both mean what they look like; anything else raises
    ``ValueError`` with a message the model can act on.
    """
    import json

    if paths is None or paths == "":
        return None
    if isinstance(paths, str):
        text = paths.strip()
        if text.startswith("["):
            try:
                paths = json.loads(text)
            except ValueError as e:
                raise ValueError(f"paths must be a JSON list of paths ({e})") from e
        else:
            return [text]
    if isinstance(paths, (list, tuple)) and all(isinstance(p, str) for p in paths):
        return [p for p in paths if p]
    raise ValueError(
        'paths must be a list of workspace paths like ["report.md", "src/"] '
        f"— got {type(paths).__name__}"
    )


def _summary(task: str) -> str:
    """A task on one line, for a listing."""
    return _first_line(task)


def render_job(job: Job) -> str:
    """The line a caller reads when a delegate is sent off."""
    where = f", forked from {job.origin[0]}" if job.origin else ""
    return (
        f"delegated to {job.name}{where}: {_summary(job.task)}\n"
        "the answer arrives on a later turn; `sessions list` shows progress "
        f"and `sessions result {job.name}` collects it."
    )


def render_jobs(jobs: "list[Job]") -> str:
    """Every job this session has, one per line."""
    if not jobs:
        return (
            "no delegated jobs yet. `sessions ask` with a task forks this "
            "session and puts an agent on it."
        )
    lines = [f"{len(jobs)} job(s):"]
    for job in jobs:
        line = f"  {job.name}  {job.status}  {_summary(job.task)}"
        if job.status == "running":
            line += "  (no answer yet)"
        elif job.status == "expired":
            # Pointing at `sessions result` here would name a verb that
            # refuses: the answer went with the branch.
            line += "  (branch swept; sessions ask to run it again)"
        else:
            paths = sum(len(v) for v in job.changed.values())
            line += f"  ({paths} path(s); sessions result {job.name})"
            if job.uncommitted:
                line += " [left work uncommitted]"
        if job.origin:
            line += f" [from {job.origin[0]}]"
        if job.kept:
            line += " [kept]"
        lines.append(line)
    return "\n".join(lines)


def answer_notes(helper: Any, inbox: Inbox) -> list[Note]:
    """Every delegate answer that has landed since the last collection,
    as notes being delivered (``inbox.deliver_now``), in landing order.

    ``helper`` is the session's :class:`Sessions` (anything with its
    ``take()``). A delegate's answer is framed as the mechanism rather
    than the principal, because that is what it is: prose another model
    wrote, evidence rather than instruction. Taking it here, at a tool
    result, is what makes an answer arrive mid-turn at all; the
    ``sessions`` tool's ``result`` action still reads answers on demand,
    and neither hands over the same answer twice. Recorded as delivered,
    the notes are settled with the turn or put back by a requeue, like
    any other.

    A turn takes this as a source: ``ws.turn(..., sources=[lambda inbox:
    answer_notes(helper, inbox)])``.
    """
    return [
        inbox.deliver_now(
            render_answer(answer),
            kind="mechanism",
            label=f"delegate {name}",
            job=name,
            answer=answer,
        )
        for name, answer in helper.take()
    ]


def render_answer(answer: Answer) -> str:
    """The delegate's reply, then what it changed, then the terminal
    verb that brings the work back.

    The changed paths come after the prose and before the next step
    because that is the order the decision is made in: what it says,
    what it touched, what to do about it. Nothing is merged here — the
    parent decides, in the terminal.
    """
    name = answer.branch or "the delegate"
    seed = answer.changed.get("seed", ())
    elsewhere = answer.changed.get("elsewhere", ())
    lines = [answer.text.rstrip(), "", f"-- {name} {answer.status} at {answer.ref}"]
    if not seed and not elsewhere:
        lines.append("it changed no files; its answer is the whole result.")
        return "\n".join(lines)
    if seed:
        lines.append(f"changed, in what you sent it to do: {', '.join(seed)}")
    if elsewhere:
        lines.append(f"changed, elsewhere: {', '.join(elsewhere)}")
    if answer.uncommitted:
        # Say what will happen, not what would be nice: the delegate
        # committed some of this and wrote past it, and a merge refuses
        # a source in that state rather than bringing back a version it
        # has moved on from.
        lines.append(
            f"{name} left work uncommitted, so what it committed is not "
            f"everything it did; ws-git merge will refuse it — take paths "
            f"with ws-git checkout {name} -- <paths>, or ask again."
        )
    elif answer.provenance.get("outside"):
        # Forked from a state this session never held: a merge brings
        # that state's files along with the delegate's work, which is
        # rarely what an asker means by taking the answer.
        source = answer.provenance.get("from", "another session")
        lines.append(
            f"next, in the terminal: ws-git diff {name} --stat (what it "
            f"touched), then ws-git diff {name} -- <paths> (read it) | "
            f"ws-git checkout {name} -- <paths> (take it). It began at "
            f"{source}, which your history does not hold: ws-git merge "
            f"{name} would bring {source}'s files too"
        )
    else:
        lines.append(
            # The summary first: a delegate's whole diff, or several
            # of them in one call, is cut off long before it is read.
            f"next, in the terminal: ws-git diff {name} --stat (what it "
            f"touched), then ws-git diff {name} -- <paths> (read it) | "
            f"ws-git merge {name} (take all of it) | "
            f"ws-git checkout {name} -- <paths> (take some)"
        )
    return "\n".join(lines)


def run_action(
    sessions: Sessions,
    action: str,
    *,
    task: str = "",
    name: str = "",
    paths: Any = None,
    inherit: str = "",
    fork_from: str = "",
    resume: str = "",
    wait: bool = False,
) -> str:
    """Dispatch one ``sessions`` tool call and render its result.

    Every refusal comes back as text rather than an exception: a tool
    result is what the model reads, and "no job named x" is something
    it can act on where a traceback is not. The one refusal that is not
    a failure — the delegate has not answered yet — says so in those
    words.
    """
    try:
        if action == "resume":
            # What the description's `resume=<name>` reads as to a model
            # that has an action to fill in: the ask that continues a
            # delegate, under the name it typed in either field.
            resume = resume or name
            if not resume.strip():
                return (
                    "sessions resume needs the delegate's name: "
                    'action="ask", resume="<name>", task="..."'
                )
            action, name = "ask", ""
        if action == "ask":
            if not task.strip():
                return "sessions ask needs a task: what should the delegate do?"
            out = sessions.ask(
                task,
                name=name or None,
                paths=coerce_paths(paths),
                inherit=inherit or None,  # unset: follows fork_from
                fork_from=fork_from or None,
                resume=resume or None,
                wait=wait,
            )
            return render_answer(out) if isinstance(out, Answer) else render_job(out)
        if action == "list":
            return render_jobs(sessions.list())
        if action in ("result", "cancel", "keep"):
            if not name.strip():
                return f"sessions {action} needs a name (`sessions list` has them)."
            if action == "result":
                return render_answer(sessions.result(name))
            # The job's own name, not the one typed: a short name finds
            # its child, and the verbs these lines suggest take the whole
            # branch name (ws-git knows no short one).
            if action == "cancel":
                job = sessions.cancel(name)
                if job.status != "cancelled":
                    return (
                        f"{job.name} had already finished ({job.status}); "
                        "nothing to stop."
                    )
                return (
                    f"{job.name} cancelled: its answer will be discarded. A "
                    "delegate already working cannot be interrupted, and its "
                    f"branch is left as it is (ws-git diff {job.name})."
                )
            job = sessions.keep(name)
            return (
                f"{job.name} kept: the retention sweep will leave its branch "
                "alone from now on."
            )
        return (
            f"unknown action {action!r} — sessions takes ask, list, result, "
            "cancel or keep. A delegate you already have gets its next task "
            'with action="ask", resume="<name>".'
        )
    except JobRunning as e:
        return str(e)
    except (SessionsError, ValueError, WorkspaceError) as e:
        return f"sessions {action} failed: {e}"
