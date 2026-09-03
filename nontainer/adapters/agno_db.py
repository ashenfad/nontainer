"""agno sessions stored in the workspace (``[agno]`` extra).

One kvgit commit holds everything a turn touched: files, ``cache``,
cwd, and the agent's conversation. ``ws.restore(commit)`` rewinds all
four together; ``fork_session(ws, name)`` branches all four together.

Wire it as agno's ``db``::

    ws = workspace("chat-42")
    db = KvgitSessionDb(ws, db_path="/var/agno")   # non-session tables
    tk = WorkspaceTools(ws, checkpoint="turn", session_db=db)
    agent = Agent(model=..., db=db, session_id=ws.session, tools=[tk])

Layout — one key per run, not one blob::

    __agno__/session          the session dict minus runs, plus
                              "run_ids": [...] in order
    __agno__/runs/<run_id>    one run dict each

kvgit dedups per key, so a turn writes one new run key and rewrites
the small session key while every earlier run is shared by hash with
each prior commit, fork and branch. One key holding the whole
conversation would instead rewrite every turn and share nothing.

Values are the JSON-shaped dicts agno hands over, never pickled agno
objects, so a branch never depends on agno's class layout and dumping
one to plain files stays trivial.

The ``__agno__/`` prefix follows the framework convention (``__cwd__``,
``__cache__/``): the agent's ``cache`` view rejects ``__`` keys at
write time, so agent code cannot reach the conversation.

Only the sessions table lives in the branch. User memories, metrics,
traces, evals and knowledge are inherited from agno's ``JsonDb`` and
stay on disk at ``db_path``: they are cross-session by design (a
user's memories span many conversations) and must not version with one
branch. Session state versions, world state does not.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agno.db.json import JsonDb
from agno.session import AgentSession, Session

from ..errors import NotSupportedError, WorkspaceError
from ..workspace import Workspace

SESSION_KEY = "__agno__/session"
RUN_PREFIX = "__agno__/runs/"


def _kv(ws: Workspace) -> Any:
    """The provider's small-value mapping — the same store the files,
    the cache and the cwd live in, which is what makes one checkpoint
    cover all of them. Not on ``Workspace``'s public surface; the
    ``__`` prefix on these keys is what keeps them out of the agent's
    ``cache`` view."""
    return ws._provider.kv


def _run_keys(kv: Any) -> list[str]:
    return [k for k in list(kv.keys()) if k.startswith(RUN_PREFIX)]


class KvgitSessionDb(JsonDb):
    """agno ``BaseDb`` holding one agent session in one workspace branch.

    A db instance is bound to one workspace and holds exactly one
    session: the one whose ``session_id`` is recorded in
    ``__agno__/session``. There is no routing — whatever agno writes
    through it lands in that branch — so an id that does not match the
    branch's is refused rather than stored beside it.

    Subclassing ``JsonDb`` rather than implementing ``BaseDb`` from
    scratch is deliberate: ``BaseDb`` is ~150 abstract methods and the
    sessions table is seven of them. Those seven are the whole exposure
    to agno churn; everything else is inherited untouched.

    **The commit trigger.** ``upsert_session`` writes its keys and then,
    when the upsert added or changed a run, checkpoints the workspace
    with ``info={"tool": "turn"}``. So the turn's files, cache, cwd and
    conversation land in ONE commit, at the moment agno persists the
    run. The db and not a post hook fires it because agno's run loop
    executes post hooks BEFORE it persists the session: a hook-driven
    commit would capture the turn's files but not its conversation, and
    the conversation would ride into the next turn's commit — exactly
    the files-versus-memory divergence this class exists to remove.
    Anchoring the commit to the persist makes agno's internal ordering
    irrelevant.

    An upsert that carries no new or changed run (agno creating the
    record before the first run) does not commit; it is staged and
    rides into the turn's commit. Under ``checkpoint="call"`` the
    mutating tool calls have already committed and this is the turn's
    trailing write, so the head at the next user message includes the
    conversation either way.

    Pass the db to the toolkit as ``WorkspaceTools(ws,
    checkpoint="turn", session_db=db)``. That is what tells the
    toolkit's ``end_turn`` hook to stand down, so wiring the hook stays
    harmless and existing embedder code keeps working.

    **Rewind.** ``ws.restore(commit)`` rewinds the run keys with
    everything else, and agno re-reads the session from the db at the
    start of every run (``Agent.cache_session`` defaults to False), so
    the next run sees the rewound conversation with no invalidation
    step. ``cache_session=True`` would break that — agno would append
    to the stale in-memory run list and write the rewound turns back —
    so an upsert whose prior runs are not exactly the branch's
    ``run_ids`` is refused and writes nothing.

    Only ``AgentSession`` is supported; team and workflow sessions
    raise. Give those their own db.

    **What sees one session through this.** agno's run loop never
    lists sessions, so persisting, loading and rewinding a conversation
    are unaffected. The features that do list — the opt-in
    ``search_past_sessions`` tool and the AgentOS session routers —
    get this branch's one session. ``KvgitStoreDb`` is the db over the
    whole store for embedders that want those features.
    """

    def __init__(
        self,
        workspace: Workspace,
        db_path: str | None = None,
        **kwargs: Any,
    ) -> None:
        """``db_path`` is where the INHERITED tables (memories, metrics,
        traces, evals, knowledge) write their JSON files; the session
        never goes there. Remaining kwargs pass through to ``JsonDb``
        (table names, ``id``)."""
        super().__init__(db_path=db_path, **kwargs)
        self._ws = workspace

    @property
    def workspace(self) -> Workspace:
        """The branch this db is bound to."""
        return self._ws

    def owns(self, workspace: Workspace) -> bool:
        """Whether this db commits the turns of ``workspace`` — what
        ``WorkspaceTools(session_db=...)`` checks before standing its
        own turn hook down."""
        return workspace is self._ws

    def seed(self, session: AgentSession, *, deserialize: bool | None = True) -> Any:
        """Write a whole session into a branch that holds no runs yet.

        The import path for a conversation that already exists
        elsewhere: an embedder moving sessions out of another agno db,
        or the store honouring agno's own ``Agent.fork_session``, which
        hands over a complete copy of the parent's runs under fresh ids.
        Neither is one new run on the branch's history, so neither can
        pass ``upsert_session``'s guard. Seeding is allowed only where
        that guard has nothing to protect — a branch with no runs — and
        commits so a fresh open of the branch sees the conversation.

        The session lands under THIS branch's id, whatever id it
        carried where it came from: a branch holds the session named
        after it, and an agent opened on ``session_id=ws.session`` must
        find what was imported. The runs are re-bound the same way."""
        data = session.to_dict()
        data["session_id"] = self._ws.session
        runs = [
            dict(run, session_id=self._ws.session) for run in (data.get("runs") or [])
        ]
        run_ids = [str(run.get("run_id")) for run in runs]
        if any(not rid or rid == "None" for rid in run_ids):
            raise WorkspaceError(
                "A run without a run_id cannot be stored: runs are "
                "addressed by id in the workspace."
            )
        with self._ws.lock:
            record = self._record()
            if record is not None and record.get("run_ids"):
                raise NotSupportedError(
                    "Refusing to seed a branch that already holds a "
                    "conversation; seeding is for a branch with no runs."
                )
            kv = _kv(self._ws)
            for run, rid in zip(runs, run_ids):
                kv[RUN_PREFIX + rid] = run
            now = int(time.time())
            stored = {k: v for k, v in data.items() if k != "runs"}
            stored["session_type"] = "agent"
            stored["run_ids"] = run_ids
            stored["created_at"] = data.get("created_at") or now
            stored["updated_at"] = now
            kv[SESSION_KEY] = stored
            if self._ws.caps.versioned and self._ws.dirty:
                self._ws.checkpoint(
                    info={"tool": "fork_session", "conversation": "copy"}
                )
        if not deserialize:
            out = dict(stored)
            out.pop("run_ids", None)
            out["runs"] = runs or None
            return out
        return AgentSession.from_dict({**stored, "runs": runs or None})

    # -- keys ----------------------------------------------------------

    def _record(self) -> dict[str, Any] | None:
        """The stored session dict (minus runs), or None on a branch
        that has never held a session."""
        record = _kv(self._ws).get(SESSION_KEY)
        return dict(record) if isinstance(record, dict) else None

    def _read_runs(self, run_ids: list[str]) -> list[dict[str, Any]]:
        kv = _kv(self._ws)
        runs = []
        for rid in run_ids:
            run = kv.get(RUN_PREFIX + rid)
            if isinstance(run, dict):
                runs.append(dict(run))
        return runs

    def _assembled(self, record: dict[str, Any]) -> dict[str, Any]:
        """The session dict agno expects: the record with its
        ``run_ids`` resolved back into a ``runs`` list.

        An empty conversation is ``None`` rather than ``[]``, which is
        the shape ``AgentSession.to_dict()`` emits and what every agno
        db therefore stores; some agno versions index ``runs[0]`` after
        a bare ``is not None`` check and raise on the empty list."""
        data = {k: v for k, v in record.items() if k != "run_ids"}
        data["runs"] = self._read_runs(list(record.get("run_ids") or [])) or None
        return data

    @staticmethod
    def _matches(
        record: dict[str, Any],
        *,
        session_id: str | None = None,
        user_id: str | None = None,
        session_type: Any = None,
        component_id: str | None = None,
    ) -> bool:
        if session_id is not None and record.get("session_id") != session_id:
            return False
        if user_id is not None and record.get("user_id") != user_id:
            return False
        if session_type is not None:
            value = getattr(session_type, "value", session_type)
            if value != "agent":
                return False
        if component_id is not None and record.get("agent_id") != component_id:
            return False
        return True

    # -- sessions ------------------------------------------------------

    def get_session(
        self,
        session_id: str,
        session_type: Any = None,
        user_id: str | None = None,
        deserialize: bool | None = True,
        runs_limit: int | None = None,
        **_: Any,
    ) -> Any:
        """The branch's session when the id matches, else None.

        A ``session_type`` that is not the agent one finds nothing;
        ``runs_limit``, where agno passes it, trims the answer to the
        most recent N runs. The branch keeps the full list: a session
        read this way and written back after a turn reconciles against
        the branch's ``run_ids`` in ``upsert_session``."""
        record = self._record()
        if record is None or not self._matches(
            record, session_id=session_id, user_id=user_id, session_type=session_type
        ):
            return None
        data = self._assembled(record)
        if runs_limit is not None and data["runs"]:
            data["runs"] = data["runs"][-runs_limit:] or None
        if not deserialize:
            return data
        return AgentSession.from_dict(data)

    def get_sessions(
        self,
        session_type: Any = None,
        user_id: str | None = None,
        component_id: str | None = None,
        session_name: str | None = None,
        start_timestamp: int | None = None,
        end_timestamp: int | None = None,
        limit: int | None = None,
        page: int | None = None,
        sort_by: str | None = None,
        sort_order: str | None = None,
        deserialize: bool | None = True,
        **_: Any,
    ) -> Any:
        """At most one session: the branch's. Filters that exclude it
        yield an empty result; sorting and pagination are moot over one
        row and are accepted but ignored."""
        record = self._record()
        matched = record is not None and self._matches(
            record,
            user_id=user_id,
            session_type=session_type,
            component_id=component_id,
        )
        if matched and session_name is not None:
            stored = (record.get("session_data") or {}).get("session_name", "")  # type: ignore[union-attr]
            matched = session_name.lower() in (stored or "").lower()
        if matched and start_timestamp is not None:
            matched = (record.get("created_at") or 0) >= start_timestamp  # type: ignore[union-attr]
        if matched and end_timestamp is not None:
            matched = (record.get("created_at") or 0) <= end_timestamp  # type: ignore[union-attr]
        if matched and limit is not None and limit < 1:
            matched = False
        if matched and page is not None and page > 1:
            matched = False

        # agno's interface returns a list (with a count when raw dicts are
        # asked for); this branch holds one session, so the list is empty
        # or that one.
        if not matched:
            return ([], 0) if not deserialize else []
        data = self._assembled(record)  # type: ignore[arg-type]
        if not deserialize:
            return [data], 1
        return [AgentSession.from_dict(data)]

    def upsert_session(
        self, session: Session, deserialize: bool | None = True, **_: Any
    ) -> Any:
        """Write the session's keys, then commit the turn.

        Refuses, without writing anything, a session that is not this
        branch's (see ``fork_session``) and a session whose runs do not
        continue the branch's history (the ``cache_session=True``
        shape). Commits when a run was added or changed.
        """
        if not isinstance(session, AgentSession):
            raise NotSupportedError(
                f"{type(session).__name__} is not supported: a workspace "
                "branch holds one agent session. Give teams and workflows "
                "their own db."
            )

        data = session.to_dict()
        runs = [dict(run) for run in (data.get("runs") or [])]
        incoming: list[str] = []
        for run in runs:
            run_id = run.get("run_id")
            if not run_id:
                raise WorkspaceError(
                    "A run without a run_id cannot be stored: runs are "
                    "addressed by id in the workspace."
                )
            incoming.append(str(run_id))

        with self._ws.lock:
            kv = _kv(self._ws)
            record = self._record()
            bound = record.get("session_id") if record else None
            session_id = data.get("session_id")
            if bound is not None and bound != session_id:
                raise NotSupportedError(
                    f"This workspace branch holds session {bound!r}; refusing "
                    f"to write {session_id!r} beside it. To branch a "
                    "conversation use fork_session(ws, name), which forks the "
                    "files and the conversation together — agno's own "
                    "Agent.fork_session assumes one db holding many sessions."
                )

            known = list(record.get("run_ids") or []) if record else []
            # The turn appends at most one run. agno may have read the
            # session with a run limit, so the write can carry only the
            # most recent runs: everything before the new run must be a
            # contiguous tail of what the branch holds. Runs the branch
            # does not hold mean the caller is writing from a session
            # object that predates a restore. The branch keeps its full
            # list either way; a limited read never shortens history.
            prior = (
                incoming[:-1] if incoming and incoming[-1] not in known else incoming
            )
            kept = len(known) - len(prior)
            if kept < 0 or known[kept:] != prior:
                raise NotSupportedError(
                    "Refusing to write a conversation this branch's history "
                    f"does not lead to: the branch holds {len(known)} run(s) "
                    f"and the write carries {len(prior)} prior run(s) that are "
                    "not its most recent ones. This is what "
                    "cache_session=True produces after ws.restore() — agno "
                    "keeps the pre-rewind session in memory and appends to "
                    "it. Leave cache_session at its default."
                )
            run_ids = known[:kept] + incoming

            changed = False
            for run in runs:
                key = RUN_PREFIX + str(run["run_id"])
                if kv.get(key) != run:
                    kv[key] = run
                    changed = True

            now = int(time.time())
            stored = {k: v for k, v in data.items() if k != "runs"}
            stored["session_type"] = "agent"
            stored["run_ids"] = run_ids
            if record is None:
                stored["created_at"] = data.get("created_at") or now
                stored["updated_at"] = stored["created_at"]
            else:
                stored["created_at"] = record.get("created_at") or now
                stored["updated_at"] = now
            kv[SESSION_KEY] = stored

            if changed and self._ws.caps.versioned:
                self._ws.checkpoint(info={"tool": "turn"})

        if not deserialize:
            out = dict(stored)
            out.pop("run_ids", None)
            out["runs"] = runs or None
            return out
        return session

    def upsert_sessions(
        self,
        sessions: list[Session],
        deserialize: bool | None = True,
        preserve_updated_at: bool = False,
        **_: Any,
    ) -> list[Any]:
        results = []
        for session in sessions:
            if session is None:
                continue
            result = self.upsert_session(session, deserialize=deserialize)
            if result is not None:
                results.append(result)
        return results

    def upsert_run(
        self,
        run: Any,
        session_id: str,
        user_id: str | None = None,
        run_index: int | None = None,
        **_: Any,
    ) -> None:
        """Write one run key.

        agno versions with a separate runs table call this right after
        ``upsert_session``, with the run that upsert already stored — so
        the normal path finds the key unchanged and writes nothing. It
        still carries a standalone run update (a status transition)
        into the branch, staged for the next commit: only a session
        upsert closes a turn.
        """
        run_data = run if isinstance(run, dict) else run.to_dict()
        run_id = run_data.get("run_id")
        if not run_id:
            return
        with self._ws.lock:
            kv = _kv(self._ws)
            record = self._record()
            if record is None or record.get("session_id") != session_id:
                return
            key = RUN_PREFIX + str(run_id)
            if kv.get(key) != run_data:
                kv[key] = dict(run_data)
            run_ids = list(record.get("run_ids") or [])
            if run_id not in run_ids:
                run_ids.append(str(run_id))
                record["run_ids"] = run_ids
                kv[SESSION_KEY] = record

    def _commit_management_write(self, tool: str, *, was_clean: bool) -> None:
        """Commit a delete or rename made between turns.

        These arrive from outside the run loop — a session list, an
        admin action — and nothing else would commit them, so the
        store's listing (which reads committed heads) and a reopen of
        the branch would both still show the old conversation. When the
        workspace already holds a turn's staged work, the write stays
        staged with it: committing then would close the turn early
        with the agent's half-finished files in it."""
        if was_clean and self._ws.caps.versioned and self._ws.dirty:
            self._ws.checkpoint(info={"tool": tool})

    def delete_session(self, session_id: str, user_id: str | None = None) -> bool:
        """Clear the session and run keys, committed when the workspace
        was clean; otherwise staged with the turn in flight."""
        with self._ws.lock:
            record = self._record()
            if record is None or not self._matches(
                record, session_id=session_id, user_id=user_id
            ):
                return False
            was_clean = not self._ws.dirty
            kv = _kv(self._ws)
            for key in _run_keys(kv):
                del kv[key]
            del kv[SESSION_KEY]
            self._commit_management_write("delete_session", was_clean=was_clean)
            return True

    def delete_sessions(
        self, session_ids: list[str], user_id: str | None = None
    ) -> None:
        for session_id in session_ids:
            self.delete_session(session_id, user_id=user_id)

    def rename_session(
        self,
        session_id: str,
        session_type: Any,
        session_name: str,
        user_id: str | None = None,
        deserialize: bool | None = True,
        **_: Any,
    ) -> Any:
        with self._ws.lock:
            record = self._record()
            if record is None or not self._matches(
                record,
                session_id=session_id,
                user_id=user_id,
                session_type=session_type,
            ):
                return None
            was_clean = not self._ws.dirty
            session_data = dict(record.get("session_data") or {})
            session_data["session_name"] = session_name
            record["session_data"] = session_data
            _kv(self._ws)[SESSION_KEY] = record
            self._commit_management_write("rename_session", was_clean=was_clean)
        data = self._assembled(record)
        if not deserialize:
            return data
        return AgentSession.from_dict(data)


def fork_session(
    ws: Workspace, name: str, *, conversation: str = "inherit", at: str | None = None
) -> Workspace:
    """Branch the files, the cache, the cwd AND the conversation.

    Forking is a workspace verb here, not an agno one. ``ws.fork(name)``
    gives the new branch every key; this then rewrites
    ``__agno__/session`` so the fork carries its own ``session_id``
    (the branch name) and records the parent in
    ``session_data["forked_from_session_id"]``, where agno keeps fork
    lineage, and checkpoints that rewrite so the fork's head is
    consistent.

    ``conversation="inherit"`` keeps the parent's runs — the branch is
    the same chat over its own files from here on. ``"fresh"`` deletes
    the run keys and clears ``run_ids``: a clean chat over the forked
    files. Run ids are left alone; agno mints fresh ones on its own
    fork only to avoid collisions inside a shared db, and branches
    never share one.

    ``at`` branches from an earlier checkpoint of this session — files
    and conversation as they stood there — without rewinding this
    session to get there; it is what "branch from where I published"
    wants. Drive the fork with an agent whose ``session_id`` is
    ``name``.
    """
    if conversation not in ("inherit", "fresh"):
        raise ValueError(f"conversation must be 'inherit' or 'fresh': {conversation!r}")

    parent = _kv(ws).get(SESSION_KEY)
    parent_id = parent.get("session_id") if isinstance(parent, dict) else None

    child = ws.fork(name, at=at)
    with child.lock:
        kv = _kv(child)
        record = kv.get(SESSION_KEY)
        if isinstance(record, dict):
            record = dict(record)
            record["session_id"] = name
            if parent_id:
                # Where agno keeps fork lineage, so agno's own readers
                # find it and the field rides along on every upsert.
                session_data = dict(record.get("session_data") or {})
                session_data["forked_from_session_id"] = parent_id
                record["session_data"] = session_data
            if conversation == "fresh":
                for key in _run_keys(kv):
                    del kv[key]
                record["run_ids"] = []
            kv[SESSION_KEY] = record
        if child.caps.versioned and child.dirty:
            child.checkpoint(
                info={"tool": "fork_session", "conversation": conversation}
            )
    return child


class KvgitStoreDb(JsonDb):
    """agno ``BaseDb`` over a whole kvgit store: one branch per session.

    The embedder owns the workspaces, so this db is built from the
    store path — the same ``store=`` the embedder passes to
    ``workspace()`` — plus its ``open(session_id) -> Workspace``.
    ``open`` must return the LIVE workspace for a session that is open
    (the one its toolkit writes through — a second ``Workspace`` over
    the same branch would split the turn across two staging buffers)
    and resume or create the branch for one that is not. Every session
    call is routed to a ``KvgitSessionDb`` view over that workspace, so
    the commit trigger and the guards are the view's. What the store
    adds:

    - ``get_sessions`` lists the store's branches, reading each
      committed head without opening it. agno's cross-session features
      — ``search_past_sessions``, the AgentOS session routers — see
      every session in the store; with a store per user, that is the
      user's sessions.
    - agno's own ``Agent.fork_session`` works. It writes a session
      under a new id whose ``session_data["forked_from_session_id"]``
      names the parent; the store forks the parent's branch (files,
      cache, cwd, in one kvgit operation) and seeds agno's copy of the
      runs into it. That copy is agno's — every run under a fresh id —
      so the conversation is not shared by hash the way
      ``fork_session(ws, name)`` shares it. The files are.
    - ``get_session`` for an id with no branch returns ``None`` and
      creates nothing; a write to such an id opens it through the
      embedder.

    Listing reads committed heads, so a turn in flight in an open
    session shows there once its commit lands. ``delete_session``
    clears a branch's conversation and leaves the branch; the branch's
    life belongs to the embedder (``delete_workspace``). Every other
    ``BaseDb`` table is inherited from ``JsonDb`` and lives at
    ``db_path``, shared across all sessions, which is what agno expects
    of memories and metrics.
    """

    def __init__(
        self,
        store: str | Path,
        *,
        open: Callable[[str], Workspace],
        db_path: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(db_path=db_path, **kwargs)
        # The same ``store`` the embedder passes to ``workspace()``; the
        # kvgit store lives in its ``kvgit/`` subdirectory.
        self._store = Path(store).expanduser() / "kvgit"
        self._open = open
        self._view_kwargs: dict[str, Any] = {"db_path": db_path, **kwargs}
        self._disk: Any = None
        self._handle: Any = None

    def owns(self, workspace: Workspace) -> bool:
        """Whether this db commits the turns of ``workspace``: true when
        the embedder's ``open`` hands back that very object for its
        session, which is the live-instance contract above."""
        return self._open(workspace.session) is workspace

    # -- the store -----------------------------------------------------

    def _branches(self) -> list[str]:
        from kvgit.kv.disk import Disk
        from kvgit.versioned.kv import VersionedKV

        if self._disk is None:
            self._store.mkdir(parents=True, exist_ok=True)
            self._disk = Disk(str(self._store))
        return VersionedKV.branches(self._disk)

    def _peek(self, branch: str, key: str) -> Any:
        """Read one key at a branch's committed head without opening the
        branch as a workspace. The kvgit handle is opened on an existing
        branch, since opening a name creates it; reads for other
        branches go through ``peek`` and never switch."""
        if self._handle is None:
            import kvgit

            if branch not in self._branches():
                return None
            self._handle = kvgit.store(
                kind="disk", path=str(self._store), branch=branch
            )
        return self._handle.peek(key, branch=branch)

    def _exists(self, session_id: str) -> bool:
        return session_id in self._branches()

    def _view(self, session_id: str) -> KvgitSessionDb:
        return KvgitSessionDb(self._open(session_id), **self._view_kwargs)

    # -- sessions ------------------------------------------------------

    def get_session(
        self,
        session_id: str,
        session_type: Any = None,
        user_id: str | None = None,
        deserialize: bool | None = True,
        **kwargs: Any,
    ) -> Any:
        if not self._exists(session_id):
            return None
        return self._view(session_id).get_session(
            session_id,
            session_type=session_type,
            user_id=user_id,
            deserialize=deserialize,
            **kwargs,
        )

    def get_sessions(
        self,
        session_type: Any = None,
        user_id: str | None = None,
        component_id: str | None = None,
        session_name: str | None = None,
        start_timestamp: int | None = None,
        end_timestamp: int | None = None,
        limit: int | None = None,
        page: int | None = None,
        sort_by: str | None = None,
        sort_order: str | None = None,
        deserialize: bool | None = True,
        **_: Any,
    ) -> Any:
        """Every session in the store that passes the filters, sorted
        (``created_at`` or ``updated_at``, newest first unless asked
        otherwise) and paginated. Runs are read only for the page
        returned."""
        matched: list[tuple[str, dict[str, Any]]] = []
        for branch in self._branches():
            record = self._peek(branch, SESSION_KEY)
            # A plain Workspace.fork() — a published snapshot, say —
            # inherits its parent's record under the parent's id. Only a
            # branch whose record names it holds a session.
            if not isinstance(record, dict) or record.get("session_id") != branch:
                continue
            if not KvgitSessionDb._matches(
                record,
                user_id=user_id,
                session_type=session_type,
                component_id=component_id,
            ):
                continue
            if session_name is not None:
                stored = (record.get("session_data") or {}).get("session_name") or ""
                if session_name.lower() not in stored.lower():
                    continue
            created = record.get("created_at") or 0
            if start_timestamp is not None and created < start_timestamp:
                continue
            if end_timestamp is not None and created > end_timestamp:
                continue
            matched.append((branch, record))

        key = sort_by if sort_by in ("created_at", "updated_at") else "created_at"
        matched.sort(
            key=lambda item: item[1].get(key) or 0, reverse=sort_order != "asc"
        )
        total = len(matched)
        if limit is not None:
            start = max((page or 1) - 1, 0) * limit
            matched = matched[start : start + limit]

        found: list[dict[str, Any]] = []
        for branch, record in matched:
            data = {k: v for k, v in record.items() if k != "run_ids"}
            runs = []
            for rid in record.get("run_ids") or []:
                run = self._peek(branch, RUN_PREFIX + str(rid))
                if isinstance(run, dict):
                    runs.append(dict(run))
            data["runs"] = runs or None
            found.append(data)
        if not deserialize:
            return found, total
        return [AgentSession.from_dict(data) for data in found]

    def upsert_session(
        self, session: Session, deserialize: bool | None = True, **kwargs: Any
    ) -> Any:
        if not isinstance(session, AgentSession):
            raise NotSupportedError(
                f"{type(session).__name__} is not supported: a workspace "
                "branch holds one agent session. Give teams and workflows "
                "their own db."
            )
        session_id = session.session_id
        if not session_id:
            raise WorkspaceError("A session without a session_id cannot be stored.")
        if self._exists(session_id):
            return self._view(session_id).upsert_session(
                session, deserialize=deserialize, **kwargs
            )

        # A new id naming a parent is agno's own fork: branch the parent
        # so the files come along, then seed agno's copy of the runs.
        parent = (session.session_data or {}).get("forked_from_session_id")
        if parent and self._exists(parent):
            child = fork_session(self._open(parent), session_id, conversation="fresh")
            try:
                return KvgitSessionDb(child, **self._view_kwargs).seed(
                    session, deserialize=deserialize
                )
            finally:
                # The embedder's ``open`` resumes the branch from here on;
                # this handle would otherwise be a second one over it.
                child.close()

        return self._view(session_id).upsert_session(
            session, deserialize=deserialize, **kwargs
        )

    def upsert_sessions(
        self,
        sessions: list[Session],
        deserialize: bool | None = True,
        preserve_updated_at: bool = False,
        **_: Any,
    ) -> list[Any]:
        results = []
        for session in sessions:
            if session is None:
                continue
            result = self.upsert_session(session, deserialize=deserialize)
            if result is not None:
                results.append(result)
        return results

    def upsert_run(
        self,
        run: Any,
        session_id: str,
        user_id: str | None = None,
        run_index: int | None = None,
        **kwargs: Any,
    ) -> None:
        if not self._exists(session_id):
            return
        self._view(session_id).upsert_run(
            run, session_id, user_id=user_id, run_index=run_index, **kwargs
        )

    def delete_session(self, session_id: str, user_id: str | None = None) -> bool:
        if not self._exists(session_id):
            return False
        return self._view(session_id).delete_session(session_id, user_id=user_id)

    def delete_sessions(
        self, session_ids: list[str], user_id: str | None = None
    ) -> None:
        for session_id in session_ids:
            self.delete_session(session_id, user_id=user_id)

    def rename_session(
        self,
        session_id: str,
        session_type: Any,
        session_name: str,
        user_id: str | None = None,
        deserialize: bool | None = True,
        **kwargs: Any,
    ) -> Any:
        if not self._exists(session_id):
            return None
        return self._view(session_id).rename_session(
            session_id,
            session_type,
            session_name,
            user_id=user_id,
            deserialize=deserialize,
            **kwargs,
        )
