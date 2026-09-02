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
        most recent N runs."""
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

        found = [self._assembled(record)] if matched else []  # type: ignore[arg-type]
        if not deserialize:
            return found, len(found)
        return [AgentSession.from_dict(data) for data in found]

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
            # The turn appends at most one run, so everything before it
            # must be exactly what the branch holds. A longer tail means
            # the caller is writing from a session object that predates a
            # restore.
            prior = (
                incoming[:-1] if incoming and incoming[-1] not in known else incoming
            )
            if prior != known:
                raise NotSupportedError(
                    "Refusing to write a conversation this branch's history "
                    f"does not lead to: the branch holds {len(known)} run(s), "
                    f"the write carries {len(prior)} prior run(s). This is "
                    "what cache_session=True produces after ws.restore() — "
                    "agno keeps the pre-rewind session in memory and appends "
                    "to it. Leave cache_session at its default."
                )

            changed = False
            for run in runs:
                key = RUN_PREFIX + str(run["run_id"])
                if kv.get(key) != run:
                    kv[key] = run
                    changed = True

            now = int(time.time())
            stored = {k: v for k, v in data.items() if k != "runs"}
            stored["session_type"] = "agent"
            stored["run_ids"] = incoming
            if record is None:
                stored["created_at"] = data.get("created_at") or now
                stored["updated_at"] = stored["created_at"]
            else:
                stored["created_at"] = record.get("created_at") or now
                stored["updated_at"] = now
                # agno's session object has no lineage field, so a plain
                # copy of what it hands over would erase the fork marker
                # on the first turn after a fork.
                lineage = record.get("forked_from_session_id")
                if lineage:
                    stored["forked_from_session_id"] = lineage
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

    def delete_session(self, session_id: str, user_id: str | None = None) -> bool:
        """Clear the session and run keys. The delete is staged like any
        other write — the next checkpoint captures it."""
        with self._ws.lock:
            record = self._record()
            if record is None or not self._matches(
                record, session_id=session_id, user_id=user_id
            ):
                return False
            kv = _kv(self._ws)
            for key in _run_keys(kv):
                del kv[key]
            del kv[SESSION_KEY]
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
            session_data = dict(record.get("session_data") or {})
            session_data["session_name"] = session_name
            record["session_data"] = session_data
            _kv(self._ws)[SESSION_KEY] = record
        data = self._assembled(record)
        if not deserialize:
            return data
        return AgentSession.from_dict(data)

    def get_tool_results_for_session(
        self, session_id: str, limit: int | None = None
    ) -> list[dict[str, Any]]:
        """The session's tool calls and their results, newest first.

        Reassembled from the run keys: the inherited implementation
        reads a table on disk that this db never writes, and would
        report a conversation with tools in it as having none. Rows
        carry the tool call as the run recorded it (``result_id`` is
        agno's ``tool_call_id``), not the offloaded-payload index other
        backends keep under this name — offloading stores payloads
        outside the workspace and is not wired here.
        """
        record = self._record()
        if record is None or record.get("session_id") != session_id:
            return []
        rows: list[dict[str, Any]] = []
        for run in self._read_runs(list(record.get("run_ids") or [])):
            for call in run.get("tools") or []:
                rows.append(
                    {
                        "result_id": call.get("tool_call_id"),
                        "session_id": session_id,
                        "run_id": run.get("run_id"),
                        "tool_name": call.get("tool_name"),
                        "tool_args": call.get("tool_args"),
                        "result": call.get("result"),
                        "created_at": call.get("created_at"),
                    }
                )
        rows.reverse()
        if limit is not None:
            rows = rows[:limit]
        return rows


def fork_session(
    ws: Workspace, name: str, *, conversation: str = "inherit"
) -> Workspace:
    """Branch the files, the cache, the cwd AND the conversation.

    Forking is a workspace verb here, not an agno one. ``ws.fork(name)``
    gives the new branch every key; this then rewrites
    ``__agno__/session`` so the fork carries its own ``session_id``
    (the branch name) and records ``forked_from_session_id``, and
    checkpoints that rewrite so the fork's head is consistent.

    ``conversation="inherit"`` keeps the parent's runs — the branch is
    the same chat over its own files from here on. ``"fresh"`` deletes
    the run keys and clears ``run_ids``: a clean chat over the forked
    files. Run ids are left alone; agno mints fresh ones on its own
    fork only to avoid collisions inside a shared db, and branches
    never share one.

    Rewind first to branch from any checkpoint with the conversation as
    it was there. Drive the fork with an agent whose ``session_id`` is
    ``name``.
    """
    if conversation not in ("inherit", "fresh"):
        raise ValueError(f"conversation must be 'inherit' or 'fresh': {conversation!r}")

    parent = _kv(ws).get(SESSION_KEY)
    parent_id = parent.get("session_id") if isinstance(parent, dict) else None

    child = ws.fork(name)
    with child.lock:
        kv = _kv(child)
        record = kv.get(SESSION_KEY)
        if isinstance(record, dict):
            record = dict(record)
            record["session_id"] = name
            if parent_id:
                record["forked_from_session_id"] = parent_id
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
