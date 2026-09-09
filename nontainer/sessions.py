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
  parent's own edits.
- **The helper commits the child's work before answering.** A fork
  inherits the parent's ws-git blob, so the child's virtual head is the
  PARENT's last agent commit and every framework commit the child makes
  reads as uncommitted agent work — which ``merge`` and
  ``checkout(ref, paths=)`` refuse, on both sides, by design. One
  host-side ``child.index.commit(message)`` when the answer arrives
  fixes that and is also what makes the delegate's result one agent
  commit with a message on it. No model is involved; the message is the
  answer's first line.

Delivery is pull: :meth:`Sessions.ask` returns a :class:`Job` and the
answer is collected later with :meth:`Sessions.result`. How a parent
LEARNS that a delegate finished — a dot in a rail, a message injected
into the next turn — is the embedder's, not nontainer's.
"""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Iterable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from .errors import JobRunning, SessionsError, WorkspaceError
from .protocol import SESSION_ID_RE, Answer, Job

if TYPE_CHECKING:
    from .protocol import SessionRunner
    from .workspace import Workspace

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
    ) -> None:
        self._ws = workspace
        self._runner = runner
        self._budget = budget
        self._chain = tuple(chain)
        self._pool = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="nontainer-sessions"
        )
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._answers: dict[str, Answer] = {}
        self._futures: dict[str, Future] = {}
        self._children: dict[str, Workspace] = {}
        self._base: dict[str, str | None] = {}
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
        inherit: str = "fresh",
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
        ``name`` asks for a particular pet name and a numeric suffix
        settles a collision either way. ``paths`` narrows what the
        child's filesystem shows without narrowing its branch, and
        ``inherit`` decides whether this session's conversation comes
        along (``"fresh"`` here, where the child is starting work of
        its own, against ``fork``'s own ``"full"`` default).

        The runner drives the child on a worker thread and its answer
        is collected with :meth:`result`. A runner that raises does not
        lose the job: it resolves as ``failed`` with the exception's
        text as the answer.
        """
        self._check_open()
        started = time.time()
        child, child_name, base = self._fork(name, paths=paths, inherit=inherit)
        job = Job(name=child_name, task=task, status="running", started=started)
        with self._lock:
            self._jobs[child_name] = job
            self._children[child_name] = child
            self._base[child_name] = base
        future = self._pool.submit(
            self._work, child_name, task, self._budget if budget is None else budget
        )
        with self._lock:
            self._futures[child_name] = future
        if wait:
            future.result()  # _work resolves every failure into an Answer
            return self.result(child_name)
        return job

    def list(self) -> list[Job]:
        """Every job this session has asked for, oldest first."""
        with self._lock:
            return sorted(self._jobs.values(), key=lambda j: (j.started, j.name))

    def result(self, name: str) -> Answer:
        """The answer for job ``name``.

        Raises :class:`~nontainer.JobRunning` while the delegate is
        still working — delivery is pull, so an unfinished job gives
        the turn back rather than blocking it — and
        :class:`~nontainer.SessionsError` for a name no job has, or a
        job whose answer was discarded by :meth:`cancel`.
        """
        with self._lock:
            job = self._jobs.get(name)
            answer = self._answers.get(name)
        if job is None:
            raise SessionsError(self._unknown(name))
        if job.status == "running":
            raise JobRunning(
                f"job {name!r} is still running; ask again on a later turn "
                "(sessions list shows what is outstanding)"
            )
        if answer is None:
            raise SessionsError(
                f"job {name!r} was cancelled, so its answer was discarded. "
                f"The branch is still there as the delegate left it: "
                f"ws-git diff {name} shows what it had done."
            )
        return answer

    def cancel(self, name: str) -> Job:
        """Stop caring about job ``name``; returns the job.

        A runner that is already working cannot be interrupted — it is
        the embedder's loop, and nontainer has no handle on it — so
        cancel means exactly this: the answer will be DISCARDED when it
        arrives, and the child's branch is left as it is. Nothing is
        committed for it and nothing is deleted; ``ws-git diff <name>``
        still shows whatever it managed to do.

        A job that has already answered is returned unchanged: there is
        nothing left to discard.
        """
        with self._lock:
            job = self._jobs.get(name)
            if job is None:
                raise SessionsError(self._unknown(name))
            if job.status != "running":
                return job
            job = replace(job, status="cancelled", finished=time.time())
            self._jobs[name] = job
            future = self._futures.get(name)
        if future is not None:
            future.cancel()  # only bites if the worker has not started
        return job

    def keep(self, name: str) -> Job:
        """Mark job ``name`` as worth keeping; returns the job.

        A delegate's branch is meant to be swept once it has gone
        unread for long enough. Nothing sweeps yet, and this records
        the flag the sweep will honor, so a caller that wants to keep a
        child around can say so at the moment it decides — which is
        while it is reading the answer, not whenever a sweep is
        eventually scheduled.
        """
        with self._lock:
            job = self._jobs.get(name)
            if job is None:
                raise SessionsError(self._unknown(name))
            job = replace(job, kept=True)
            self._jobs[name] = job
            return job

    # -- lifecycle -----------------------------------------------------

    def close(self) -> None:
        """Join the workers and release the child handles.

        The branches stay: a delegate's work is in the store, and
        closing the helper that asked for it must not throw it away.
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._pool.shutdown(wait=True)
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

    def _unknown(self, name: str) -> str:
        known = ", ".join(sorted(self._jobs)) or "none"
        return f"no job named {name!r} on session {self._ws.session!r} (have: {known})"

    def _fork(
        self,
        name: str | None,
        *,
        paths: "Iterable[str] | str | None",
        inherit: str,
    ) -> "tuple[Workspace, str, str | None]":
        """``(child, its name, the fork point)``.

        The name is minted and the fork taken under the parent's own
        lock, so two asks racing on one session cannot pick the same
        name — and the fork itself is the authority on collision, since
        a branch may exist that no job here remembers.
        """
        wanted = name or pet_name()
        with self._ws.lock:
            for attempt in range(_MINT_TRIES):
                candidate = self._scoped(wanted, attempt)
                try:
                    child = self._ws.fork(candidate, inherit=inherit, paths=paths)
                except WorkspaceError as exc:
                    if "already exists" not in str(exc):
                        raise
                    continue
                return child, candidate, self._ws.head
        raise SessionsError(
            f"could not mint a free name for a child of {self._ws.session!r} "
            f"in {_MINT_TRIES} tries (last: {candidate!r})"
        )

    def _scoped(self, wanted: str, attempt: int) -> str:
        """``parent.pet`` — plus ``.2``, ``.3`` … on collision."""
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

    def _work(self, name: str, task: str, budget: Any) -> Answer:
        """The worker body: run, then land. Never raises.

        A job that dies with its thread is a job the caller waits on
        forever, so both halves resolve into an :class:`Answer` — the
        runner's failure as ``failed`` with the exception's text, and a
        failure to land the answer as the answer plus what went wrong.
        """
        try:
            out = self._runner.run(self._session_of(name), task, budget=budget)
            answer = out if isinstance(out, Answer) else Answer(text=str(out))
        except BaseException as exc:  # noqa: BLE001 - the job must resolve
            answer = Answer(text=_error_text(exc), status="failed")
        try:
            return self._land(name, task, answer)
        except BaseException as exc:  # noqa: BLE001 - as must the landing
            failed = replace(
                answer,
                status="failed",
                text=(
                    f"{answer.text}\n\n[the delegate answered, and its work "
                    f"could not be landed: {_error_text(exc)}]"
                ),
                branch=name,
            )
            self._record(name, failed)
            return failed

    def _session_of(self, name: str) -> str:
        with self._lock:
            return self._children[name].session

    def _land(self, name: str, task: str, answer: Answer) -> Answer:
        """Commit the child's work, describe it, and record the answer.

        The child handle is closed either way: its branch is what the
        parent merges from, and holding a second handle on it past the
        job keeps a store open for nothing.
        """
        with self._lock:
            child = self._children.pop(name)
            base = self._base.get(name)
            cancelled = self._jobs[name].status == "cancelled"
        try:
            if cancelled:
                # Discarded, and the branch left exactly as the
                # delegate left it: no commit of ours goes onto work
                # nobody is going to read.
                return answer
            commit = self._commit_child(child, answer, task)
            answer = replace(
                answer,
                ref=f"{child.session}@{commit}" if commit else None,
                branch=child.session,
                changed=self._changed(base, child),
                provenance=self._provenance(answer, child, name, base, commit),
            )
            self._record(name, answer)
            return answer
        finally:
            try:
                child.close()
            except Exception:  # noqa: BLE001 - recording the answer wins
                pass

    def _commit_child(
        self, child: "Workspace", answer: Answer, task: str
    ) -> str | None:
        """Land the delegate's work as one agent commit; returns the
        commit the answer names.

        The runner drove the child through a handle of its own, so this
        one is re-read from the branch first. Then the fiction's
        question: has the agent anything uncommitted? For a fork the
        answer is yes for everything it wrote, because its virtual head
        is the PARENT's last agent commit — which is exactly why merge
        and take would refuse it without this.
        """
        refresh = getattr(child._provider, "refresh", None)
        if refresh is not None and child.caps.versioned:
            refresh()
        if not child.caps.index:
            return child.head
        status = child.index.status()
        if status.staged or status.unstaged:
            try:
                child.index.commit(_first_line(answer.text, task))
            except WorkspaceError:
                # Another writer landed it between the status and here;
                # its commit is as good as ours would have been.
                pass
        return child.index.head or child.head

    def _changed(self, base: str | None, child: "Workspace") -> dict:
        """The child's changed paths, grouped seed vs elsewhere.

        The fork point against the child's head, read through the
        PARENT — one store, so a commit on either branch is legible
        from both — and grouped the way ``WorkspaceDiff`` groups:
        under what the child was seeded with, and everywhere else. A
        delegate that touched a caller in ``main.py`` is the common
        case, and the grouping is what keeps that from going unnoticed.
        """
        head = child.head
        if base is None or head is None:
            return {"seed": (), "elsewhere": ()}
        with self._ws.lock:
            diff = self._ws.diff(base, head)
        return {
            "seed": tuple(sorted(diff.in_seed)),
            "elsewhere": tuple(sorted(diff.elsewhere)),
        }

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

        ``chain`` is the full path of refs and not the last hop: the
        parent at the commit it delegated from, then the child at the
        commit it answered from, behind whatever chain this session's
        own work already carried. An answer must not be able to launder
        its sources by passing through one more delegate.
        """
        with self._lock:
            job = self._jobs[name]
        hop = [f"{self._ws.session}@{base}"] if base else []
        if commit:
            hop.append(f"{child.session}@{commit}")
        return {
            **answer.provenance,
            "session": child.session,
            "commit": commit,
            "started": job.started,
            "finished": time.time(),
            "chain": (*self._chain, *hop),
        }

    def _record(self, name: str, answer: Answer) -> None:
        """Store the answer and close the job out. Cancellation wins:
        a job the caller stopped caring about does not un-cancel
        because its runner finished a moment later."""
        with self._lock:
            job = self._jobs[name]
            if job.status == "cancelled":
                return
            self._answers[name] = answer
            self._jobs[name] = replace(
                job,
                status=answer.status,
                ref=answer.ref,
                finished=time.time(),
                changed=dict(answer.changed),
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
    return (
        f"delegated to {job.name}: {_summary(job.task)}\n"
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
        else:
            paths = sum(len(v) for v in job.changed.values())
            line += f"  ({paths} path(s); sessions result {job.name})"
        if job.kept:
            line += " [kept]"
        lines.append(line)
    return "\n".join(lines)


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
    lines.append(
        f"next, in the terminal: ws-git diff {name} (read it) | "
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
    inherit: str = "fresh",
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
        if action == "ask":
            if not task.strip():
                return "sessions ask needs a task: what should the delegate do?"
            out = sessions.ask(
                task,
                name=name or None,
                paths=coerce_paths(paths),
                inherit=inherit,
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
            if action == "cancel":
                job = sessions.cancel(name)
                if job.status != "cancelled":
                    return (
                        f"{name} had already finished ({job.status}); nothing to stop."
                    )
                return (
                    f"{name} cancelled: its answer will be discarded. A delegate "
                    "already working cannot be interrupted, and its branch is "
                    f"left as it is (ws-git diff {name})."
                )
            return f"{name} kept: it will not be swept away while it is unread."
        return (
            f"unknown action {action!r} — sessions takes ask, list, result, "
            "cancel or keep."
        )
    except JobRunning as e:
        return str(e)
    except (SessionsError, ValueError, WorkspaceError) as e:
        return f"sessions {action} failed: {e}"
