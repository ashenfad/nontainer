"""agno adapter: a Toolkit exposing one Workspace to an agno Agent.

Usage::

    from agno.agent import Agent
    from nontainer import workspace
    from nontainer.adapters.agno import WorkspaceTools

    ws = workspace("user-42")
    agent = Agent(model=..., tools=[WorkspaceTools(ws)])

One toolkit == one workspace == one session. For per-conversation
sessions, construct a fresh ``workspace(session_id)`` + toolkit per
conversation (kvgit branches make this cheap).

Concurrency: agno's ``arun()`` executes sync tools CONCURRENTLY on
separate threads — including parallel tool calls from a single model
turn. ``Workspace`` enforces its own single-writer invariant (mutating
calls hold an internal lock), so parallel calls serialize safely (each
atomic + committed) even without adapter help. The tools come from a
shared :class:`~nontainer.adapters.tools.Toolset`, whose per-workspace
``threading.Lock`` additionally fences work around the call
(``test_app`` + screenshot reads) and, here, the turn-commit checks; it
is uncontended under sync ``run()``. The
toolkit ``instructions`` carry the one-call-per-turn convention so
serialization stays a backstop, not the norm.

Run-level seams, all optional and all bound as instance attributes so
an embedder names them at the call site: ``tk.end_turn`` (post hook)
commits the turn, ``tk.begin_turn`` (pre hook) re-queues notes a
retried attempt threw away and warns when agno restarts a run, and
``tk.deliver`` / ``tk.adeliver`` (tool hooks, one per tool-entrypoint
style — ``tk.deliver`` for sync tools, which is everything this toolkit
registers) append ``tk.inbox``'s notes to the next tool result — the
only place a running agent reads new text.

A run that errors or is cancelled is an interrupt, not a rollback: the
workspace keeps what it wrote, and ``keep_aborted_run(db, session_id,
run_id, note)`` keeps the run in the agent's memory to match.

Exposure follows ``resolve_tools_mode`` (``"auto"`` default): a plain
python environment gets a single ``terminal`` tool (with the `python`
builtin); an augmented one (cache / host objects) additionally gets a
dedicated ``run_python`` tool whose description announces the magic.
"""

from __future__ import annotations

import logging
from typing import Any

from agno.media import Image
from agno.tools import Toolkit
from agno.tools.function import ToolResult

from ..inbox import Inbox, Note
from ..turns import RunStatus, Turn
from ..workspace import Workspace
from .render import PYTHON_UI_NOTE, ToolsMode, toolkit_instructions
from .tools import ToolOutput, Toolset

_logger = logging.getLogger("nontainer.adapters.agno")


def _media_note(count: int) -> str:
    """Provenance line for tool results that carry images.

    agno can't put image blocks on a tool-role message (many chat APIs
    reject them), so it re-delivers tool media as a synthetic USER
    message ("Take note of the following content"). Humble models read
    that as the human sharing an image — mint-satyr thought "the user
    is showing me the report I generated". The tool result text lands
    immediately above that injected message, so it pre-claims
    provenance."""
    if count == 1:
        return (
            "\n[the image in the next message is this tool call's "
            "result — the human did not send it]"
        )
    return (
        f"\n[the {count} images in the next message are this tool "
        "call's results — the human did not send them]"
    )


def _tool_result(out: ToolOutput) -> ToolResult:
    """A toolset result as agno's ``ToolResult``: its images become agno
    media, and the text claims them as the tool's (:func:`_media_note`)."""
    if not out.images:
        return ToolResult(content=out.text)
    return ToolResult(
        content=out.text + _media_note(len(out.images)),
        images=[
            Image(content=image.data, format=image.format, id=image.source)
            for image in out.images
        ],
    )


def keep_aborted_run(db: Any, session_id: str, run_id: str | None, note: str) -> bool:
    """Keep a run that errored or was cancelled in the agent's memory.

    agno's history builder skips runs whose status is error or
    cancelled. A run cut short by a provider failure or a stop
    therefore vanishes from what the model is shown next turn, while
    every file its tool calls wrote is still in the workspace — memory
    and files disagree, and a model that cannot remember its own work
    confabulates around it rather than saying it does not know. The
    messages up to the cut are real work, so this treats the abort as
    an interrupt: the run is marked completed and closed with an
    assistant message, ``[turn aborted early: {note} — the work above
    this point is real and completed]``, so the model keeps what it did
    AND knows the turn ended early. Nothing in the workspace changes.

    Call it after an errored or cancelled run, once the embedder has
    stopped trying to finish that run. Ordering matters: an embedder
    that resumes an errored run in place (agno 3's ``continue_run``
    resumes an ERROR run where it stopped, and forks a COMPLETED one
    instead) must attempt that first, because this marks the run
    completed.

    ``db`` is the agent's db (``agent.db``). Returns whether anything
    changed: ``False`` for no db, no run id, a session or run the db
    does not hold, or a run in any other status — and then nothing is
    written. Older agno releases store no aborted run at all (a
    provider error raises out of ``run()``), which leaves nothing to
    keep and returns ``False``.

    Both storage layouts are written. agno 2.x keeps a session's runs
    inline, so ``upsert_session`` stores the change; agno 3 keeps runs
    in their own table and ``upsert_session`` writes the session row
    alone, so the run also goes through ``upsert_run`` — skipped, as
    agno's own storage layer skips it, by a db that raises
    ``NotImplementedError`` there because it still stores runs inline.
    """
    if db is None or run_id is None:
        return False
    from agno.db.base import SessionType
    from agno.models.message import Message
    from agno.run.base import RunStatus

    session = db.get_session(session_id=session_id, session_type=SessionType.AGENT)
    if session is None or not getattr(session, "runs", None):
        return False
    run = next((r for r in session.runs if getattr(r, "run_id", None) == run_id), None)
    if run is None or run.status not in (RunStatus.error, RunStatus.cancelled):
        return False
    run.status = RunStatus.completed
    run.messages = list(run.messages or []) + [
        Message(
            role="assistant",
            content=f"[turn aborted early: {note} — the work above this point "
            "is real and completed]",
        )
    ]
    db.upsert_session(session)
    upsert_run = getattr(db, "upsert_run", None)
    if upsert_run is not None:
        try:
            upsert_run(
                run=run,
                session_id=session_id,
                user_id=getattr(session, "user_id", None),
            )
        except NotImplementedError:
            pass  # runs are stored inline, and upsert_session wrote this one
    return True


#: agno's ``error_type`` on a ``RunError`` for a failed provider call
#: (an overloaded or rate-limited endpoint): the errors worth resuming.
PROVIDER_ERRORS = frozenset({"model_provider_error", "model_rate_limit_error"})


def status_of_error(error_type: str | None) -> RunStatus:
    """How a run that ended in agno's ``RunError`` ended, by the event's
    ``error_type``: ``"interrupted"`` for a provider error, which the run
    can resume from in place (``acontinue_run``), and ``"failed"`` for
    anything else."""
    return "interrupted" if error_type in PROVIDER_ERRORS else "failed"


def finish_turn(
    turn: Turn,
    db: Any,
    session_id: str,
    status: RunStatus,
    message: str | None = None,
) -> str | None:
    """End the turn of an agno run; the commit it landed, or ``None``.

    A cancelled or failed run is kept first (:func:`keep_aborted_run`),
    with ``message`` as its closing note, so the model remembers the
    work it did before the cut. Under the open turn that write is
    staged, so the turn's end lands the run, the files and the settled
    inbox in one commit, stamped with ``status``. An interrupted run is
    left as agno stored it, for a resume to continue.

    Keeping the run is best-effort: a failure there is logged and the
    turn still ends, because losing the note costs the model some
    memory while leaving the turn open would refuse every turn after.
    """
    if status in ("cancelled", "failed"):
        try:
            keep_aborted_run(db, session_id, turn.run_id, message or status)
        except Exception:  # noqa: BLE001 - the turn must still end
            _logger.warning(
                "could not keep aborted run %s of session %s",
                turn.run_id,
                session_id,
                exc_info=True,
            )
    return turn.end(status, message=message)


class WorkspaceTools(Toolkit):
    """agno Toolkit over a nontainer :class:`Workspace`."""

    def _begin_turn(self, *args: Any, run_context: Any = None, **kwargs: Any) -> None:
        """Re-queue notes an abandoned attempt delivered. Tolerant
        signature so it slots into agno ``pre_hooks``.

        A pre hook runs once per attempt, so on a run's first attempt
        nothing has been delivered yet and this does nothing. It earns
        its place on the second: a provider-error retry rebuilds the
        run from the user message and drops the attempt's tool calls,
        so notes that rode out on one of those tool results went with
        them and the model never saw them. Delivered-but-unsettled is
        exactly that set, and it goes back to the front of the queue
        for the retry to deliver again.

        Cancellation needs the same care from the other side: agno
        runs no post hook for a cancelled run, so an embedder that
        keeps a cancelled run's messages — the model DID see the
        notes — should call ``tk.inbox.settle()`` in its cancel path.
        Otherwise the next turn re-delivers them.

        It also says, once per run, when agno has restarted one. A pre
        hook that sees a ``run_context`` whose ``run_id`` it has
        already seen is on a run-level retry (``Agent(retries=N)``),
        and that retry forgets the failed attempt's tool calls while
        the files they wrote stay in the workspace — memory and files
        no longer agree. Nothing is undone here: the workspace may hold
        writes that are not the attempt's, and a hook cannot tell them
        apart. The warning names the fix instead: ``Agent(retries=0)``,
        with ``retries`` set on the model (agno's ``Model.retries``,
        which defaults to 0) to absorb transient provider errors — it
        retries one model call and keeps the turn's tool results. agno releases that hand pre hooks no run context skip
        the check and only re-queue.
        """
        run_id = getattr(run_context, "run_id", None)
        if run_id is not None:
            if run_id != self._run_seen:
                self._run_seen = run_id
            elif run_id != self._run_warned:
                self._run_warned = run_id
                _logger.warning(
                    "agno restarted run %s (Agent(retries=...)): the restart "
                    "forgets the failed attempt's tool calls, but the files "
                    "they wrote remain in the workspace, so the model's memory "
                    "and the files disagree. Set Agent(retries=0) and put the "
                    "retries on the model (Model.retries), which retries one "
                    "model call and keeps the turn's tool results; call "
                    "keep_aborted_run after a run that errors.",
                    run_id,
                )
        self.inbox.requeue()

    def _deliver(
        self,
        function_name: str,
        function: Any,
        arguments: dict[str, Any],
    ) -> Any:
        """agno ``tool_hook``, sync: run the tool, append the inbox.

        A tool result is the one place a running agent reads new text,
        so it is where a mid-run message is delivered. Nothing is
        interrupted and no message is rewritten: the notes queued
        while this call ran are appended to the text it returns.

        A tool that RAISES delivers nothing — the exception propagates
        untouched and the notes stay pending for the next result that
        lands, rather than being spent on a message the model may
        never see.

        This is the right hook whenever the tool entrypoints are
        sync, which is every tool this toolkit registers — under
        ``run()`` and under ``arun()`` alike. agno picks the execution
        path per TOOL CALL, not per run loop: a sync entrypoint runs
        in a thread via ``asyncio.to_thread`` unless one of the tool
        hooks is a coroutine function, in which case the whole call
        runs inline on the event loop. An async hook over sync tools
        therefore holds the loop for the length of every tool call: a
        cancel cannot reach the run, and nothing else on that loop
        moves. Pair with :meth:`_adeliver` only for async tools.
        """
        result = function(**arguments)
        notes = self._collect()
        if not notes:
            return result
        out = self._attach(result, notes, function_name)
        if out is None:
            return result
        self._announce(notes, allow_async=False)
        return out

    async def _adeliver(
        self,
        function_name: str,
        function: Any,
        arguments: dict[str, Any],
    ) -> Any:
        """agno ``tool_hook``, async: :meth:`_deliver` for ASYNC tool
        entrypoints, awaiting the tool and any awaitable the inbox's
        ``on_delivered`` returns.

        Match the hook to the tool entrypoints, not to ``run()``
        versus ``arun()``. agno's async chain hands a hook a coroutine
        ``next_func`` that only a coroutine hook can drive, so a tool
        whose entrypoint is async needs this spelling. Every tool this
        toolkit registers is sync, so this one is for an embedder that
        registers async tools of its own beside the toolkit — and then
        it is bound on THOSE functions (``@tool(tool_hooks=[...])``),
        never on the agent: an agent-level ``tool_hooks`` replaces every
        function's own hooks rather than adding to them, and this hook
        over a sync tool makes agno run the call inline on the event
        loop. The toolkit's own functions take :meth:`_deliver` on
        each of them in that setup, and both hooks drain one inbox.
        """
        result = await function(**arguments)
        notes = self._collect()
        if not notes:
            return result
        out = self._attach(result, notes, function_name)
        if out is None:
            return result
        await self._aannounce(notes)
        return out

    def _collect(self) -> "list[Note]":
        """Everything to deliver with this tool result: the queued
        notes, then any delegate answer that has landed since the last
        collection (:func:`~nontainer.sessions.answer_notes`)."""
        notes = self.inbox.drain()
        if self.sessions is None:
            return notes
        from ..sessions import answer_notes

        return notes + answer_notes(self.sessions, self.inbox)

    def _with_notes(self, result: Any, notes: "list[Note]") -> Any:
        """The tool's result with the rendered notes appended, keeping
        whatever shape it had.

        A ``ToolResult`` is copied with its text replaced rather than
        mutated, and everything else on it comes across untouched:
        dropping the images off a screenshot's result to deliver a
        sentence would cost the model the thing it called the tool
        for. The copy carries fields by name rather than being rebuilt
        from a fixed list, so a field one agno version has and another
        does not is neither dropped nor demanded.
        """
        rendered = self.inbox.render(notes)
        if isinstance(result, ToolResult):
            content = result.content
            if not isinstance(content, str):
                content = "" if content is None else str(content)
            return result.model_copy(update={"content": content + rendered})
        if isinstance(result, str):
            return result + rendered
        return str(result) + rendered

    def _attach(self, result: Any, notes: "list[Note]", function_name: str) -> Any:
        """:meth:`_with_notes`, or ``None`` when the notes could not be
        attached — and then the notes are pending again.

        The notes were drained before this ran, so a failure here (a
        frame that raises, a result shape the text cannot be added
        to) would otherwise spend them on a message the model never
        sees AND replace a tool result that succeeded with an
        exception. The tool result wins: it goes back untouched, and
        the notes go back to the head of the queue for the next result
        that lands.
        """
        try:
            return self._with_notes(result, notes)
        except Exception:  # noqa: BLE001 - the tool result wins
            _logger.warning(
                "inbox: %d note(s) could not be attached to the result of %s; "
                "they stay pending for the next tool result",
                len(notes),
                function_name,
                exc_info=True,
            )
            self.inbox.restore(notes)
            return None

    def _announce(self, notes: "list[Note]", *, allow_async: bool) -> Any:
        """Tell the inbox's ``on_delivered`` that these notes landed
        (:meth:`~nontainer.inbox.Inbox.announce`)."""
        return self.inbox.announce(notes, allow_async=allow_async)

    async def _aannounce(self, notes: "list[Note]") -> None:
        outcome = self._announce(notes, allow_async=True)
        if outcome is None:
            return
        try:
            await outcome
        except Exception:  # noqa: BLE001 - the tool result wins
            _logger.warning("inbox on_delivered raised", exc_info=True)

    def _end_turn(self, *args: Any, **kwargs: Any) -> str | None:
        """Commit the turn's staged work (one commit per agent turn).
        Tolerant signature so it slots into agno ``post_hooks``; also
        callable directly by embedders after ``agent.run(...)``.
        Returns the turn's commit id, or None when nothing changed or
        the workspace is unversioned.

        A no-op when the toolkit was given a ``session_db``: that db
        commits the turn itself, at the moment agno persists the run,
        so the conversation lands in the same commit as the files.
        Post hooks run BEFORE the session is persisted, so committing
        here would leave the conversation for the next turn's commit.

        An agent that left a ws-git composition open across the turn
        boundary keeps it: the turn commit is the framework's, and
        ws-git measures against the agent's own last commit, so its
        staged set is still staged and its work in progress still
        uncommitted afterwards.

        A no-op too while a turn is open on the workspace (``ws.turn``):
        the turn's end commits.

        It also settles the inbox — the turn that read this turn's
        notes is over, so nothing will redeliver them. That happens
        first, whether or not this toolkit owns the commit, because
        settling is about the conversation rather than the files. A
        cancelled run runs no post hook at all, so an embedder that
        keeps a cancelled run's messages should call
        ``tk.inbox.settle()`` in its cancel path; otherwise the next
        turn re-delivers what the model has already read."""
        self.inbox.settle()
        if self._session_db is not None or self._ws.turns.current is not None:
            return None
        with self._lock:
            ws = self._ws
            if ws.frozen or not ws.caps.versioned or not ws.uncommitted:
                return None
            return ws.commit(info={"tool": "turn"})

    def __init__(
        self,
        workspace: Workspace,
        *,
        tools: ToolsMode = "auto",
        apps: Any = None,
        sessions: Any = None,
        commit: str = "call",
        session_db: Any = None,
        inbox: Inbox | None = None,
        terminal_primer: str | None = None,
        python_primer: str | None = None,
        vision: bool = True,
        **kwargs,
    ) -> None:
        """``apps``: an ``AppRuntime`` (from ``nontainer.apps.
        enable_apps``) — when given, a ``test_app`` tool is registered
        whose screenshots come back as real images (agno ``ToolResult``
        media) in addition to being saved under ``<root>/app/screenshots/``.

        ``sessions``: a ``SessionRunner`` (the embedder's loop) or an
        already-built ``nontainer.sessions.Sessions`` — when given, a
        ``sessions`` tool is registered, and the agent can delegate to
        forks of this session. No runner, no tool: an agent that cannot
        delegate is never told about delegation. Which ACTIONS a
        deployment allows is the embedder's policy on top of this.
        The built helper is on the toolkit as ``tk.sessions``; pass a
        ``Sessions`` yourself when you need to close it (closing joins
        the delegate workers and leaves their branches).

        ``vision``: whether the driving model accepts image input.
        With ``False``, ``view_image`` isn't registered and ``test_app``
        screenshots stay path-only (still saved to the workspace) —
        attaching media a model can't take errors the whole next call
        ("no endpoints support image input"), losing the turn.

        ``commit``: commit granularity on versioned workspaces.
        ``"call"`` (default) commits after each mutating tool call —
        maximum durability, chattier history. ``"turn"`` is the agex
        model — one commit per agent turn; wire :meth:`end_turn` as an
        agno run-level hook::

            tk = WorkspaceTools(ws, commit="turn")
            agent = Agent(model=..., tools=[tk], post_hooks=[tk.end_turn])

        Turn mode defers commits to the hook, so a crash mid-turn can
        lose that turn's staged work (kvgit staging is in-memory).

        ``session_db``: a ``nontainer.adapters.agno_db.KvgitSessionDb``
        over the SAME workspace, or a ``KvgitStoreDb`` whose ``open``
        hands back this workspace for its session, when the conversation
        is stored in the branch too. Naming it here is what makes
        :meth:`end_turn` stand down — the db commits the turn when agno
        persists the run, so files and conversation land in one commit —
        and it is wired explicitly rather than sniffed off the workspace
        so a reader of the call site can see which object owns the
        commit. The db's ``owns(workspace)`` is the check.

        ``inbox``: the :class:`~nontainer.inbox.Inbox` whose notes
        :meth:`deliver` appends to tool results. One is created when
        none is given (``tk.inbox``); pass your own to share a queue
        an embedder already fills, or to set its framing and its
        ``on_delivered`` callback. Delivery needs the hook wired::

            tk = WorkspaceTools(ws, commit="turn")
            agent = Agent(model=..., tools=[tk],
                          pre_hooks=[tk.begin_turn],
                          post_hooks=[tk.end_turn],
                          # sync tools take the sync hook, under
                          # run() and arun() alike
                          tool_hooks=[tk.deliver])

        Without ``tool_hooks`` the queue simply fills and the
        embedder reads it between turns."""
        self._ws = workspace
        self.inbox = inbox if inbox is not None else Inbox()
        self._run_seen: str | None = None
        self._run_warned: str | None = None
        if session_db is not None:
            owns = getattr(session_db, "owns", None)
            if owns is None or not owns(workspace):
                raise ValueError(
                    "session_db must cover this same workspace; it holds the "
                    "conversation in the branch the tools write to."
                )
        self._session_db = session_db
        if commit not in ("call", "turn"):
            raise ValueError(f"commit must be 'call' or 'turn': {commit!r}")
        self._turn_commits = commit == "turn"
        if self._turn_commits:
            workspace.autocommit = False
        toolset = Toolset(
            workspace,
            tools=tools,
            apps=apps,
            sessions=sessions,
            terminal_primer=terminal_primer,
            python_primer=python_primer,
            vision=vision,
        )
        self._toolset = toolset
        # The toolset's fence is the toolkit's: end_turn commits under
        # the same lock the tools work under.
        self._lock = toolset.lock
        split = toolset.split
        if python_primer and not split:
            import warnings

            warnings.warn(
                "python_primer set but tools resolved to terminal-only "
                "(no run_python tool); it will appear in the terminal "
                "tool's python section. Put python-tool guidance in "
                "terminal_primer if that's not intended.",
                stacklevel=2,
            )

        # Thin typed wrappers: agno derives each tool's schema from the
        # signature, and the toolset does the work.
        def terminal(command: str) -> str:
            """Run a shell script in the persistent workspace."""
            return toolset.terminal(command).text

        terminal.__doc__ = toolset.description("terminal")

        def file_write(path: str, content: str) -> str:
            """Write a file in the workspace."""
            return toolset.file_write(path, content).text

        file_write.__doc__ = toolset.description("file_write")

        def file_edit(
            path: str,
            old_string: str,
            new_string: str,
            replace_all: bool = False,
        ) -> str:
            """Exact-string replacement in a workspace file."""
            return toolset.file_edit(
                path, old_string, new_string, replace_all=replace_all
            ).text

        file_edit.__doc__ = toolset.description("file_edit")

        def view_image(path: str) -> ToolResult:
            """View an image file from the workspace."""
            return _tool_result(toolset.view_image(path))

        view_image.__doc__ = toolset.description("view_image")

        registered = [terminal, file_write, file_edit]
        if vision:
            registered.append(view_image)

        if split:

            def run_python(code: str) -> str:
                """Run Python in the sandboxed workspace environment."""
                return toolset.run_python(code).text

            # The ui note promises that artifacts display beside the
            # reply, which a host rendering this toolkit's conversation
            # does; it is this adapter's to make, not the toolset's.
            run_python.__doc__ = toolset.description(
                "run_python"
            ) + PYTHON_UI_NOTE.replace(
                "__WS__", "" if workspace.root == "/" else workspace.root
            )
            registered.append(run_python)

        if apps is not None:
            # actions and viewport are annotated loose: models routinely
            # send a list or an object as a JSON STRING, and agno's
            # pydantic layer would reject it on the annotation BEFORE the
            # toolset's coercion gets its chance
            def test_app(
                actions: "list[dict] | str",
                viewport: "str | dict" = "desktop",
                bind: "dict | str | None" = None,
            ) -> ToolResult:
                """Verify the app headlessly."""
                return _tool_result(toolset.test_app(actions, viewport, bind))

            test_app.__doc__ = toolset.description("test_app")
            registered.append(test_app)

        self.sessions = toolset.sessions
        if toolset.sessions is not None:
            # One tool with an action argument, as test_app has: the
            # model learns one spelling for delegation, and ws-git keeps
            # the versioning verbs. paths is annotated loose for the
            # same reason test_app's actions are — models send lists as
            # JSON strings, and pydantic would reject one on the
            # annotation before coerce_paths got its chance.
            # ``fork_from`` rather than ``from``: a JSON argument's
            # name is the python parameter's name in both adapters,
            # and ``from`` is a python keyword, so it can name neither
            # the parameter here nor the argument a model sends. The
            # tool takes the word the host API takes, one spelling
            # everywhere.
            def sessions_tool(
                action: str,
                task: str = "",
                name: str = "",
                paths: "list[str] | str | None" = None,
                inherit: str = "",
                fork_from: str = "",
                resume: str = "",
                wait: bool = False,
            ) -> str:
                """Delegate to a fork of this session, and read it back."""
                return toolset.sessions_action(
                    action,
                    task=task,
                    name=name,
                    paths=paths,
                    inherit=inherit,
                    fork_from=fork_from,
                    resume=resume,
                    wait=wait,
                ).text

            sessions_tool.__name__ = "sessions"
            sessions_tool.__doc__ = toolset.description("sessions")
            registered.append(sessions_tool)

        instructions = toolkit_instructions(
            workspace, split=split, turn_commits=self._turn_commits
        )

        self.end_turn = self._end_turn  # bindable as an agno post_hook
        self.begin_turn = self._begin_turn  # bindable as an agno pre_hook
        self.deliver = self._deliver  # agno tool_hook, sync tools
        self.adeliver = self._adeliver  # agno tool_hook, async tools

        super().__init__(
            name="nontainer_workspace",
            tools=registered,
            instructions=instructions,
            add_instructions=True,
            **kwargs,
        )
