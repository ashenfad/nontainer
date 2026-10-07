"""agno sessions stored in the workspace (``[agno]`` extra).

One kvgit commit holds everything a turn touched: files, ``cache``,
cwd, and the agent's conversation. ``ws.checkout(commit)`` rewinds all
four together; ``fork_session(ws, name)`` branches all four together.

Wire it as agno's ``db``::

    ws = workspace("chat-42")
    db = KvgitSessionDb(ws, db_path="/var/agno")   # non-session tables
    tk = WorkspaceTools(ws, commit="turn", session_db=db)
    agent = Agent(model=..., db=db, session_id=ws.session, tools=[tk])

Layout: the harness-neutral conversation plane (see
:mod:`nontainer.conversation`), one key per run, not one blob::

    __conversation__/index         core's index: harness "agno", the
                                   session, run ids in order, lineage
    __conversation__/record        agno's session dict minus its runs
    __conversation__/runs/<id>     one run dict each

The session id, its fork lineage (``session_data[
"forked_from_session_id"]``) and ``run_ids`` are read from the index,
which core owns and a fork rebinds; the record holds the rest of what
agno stores. A branch written before the plane was neutral keeps the
conversation under ``__agno__/``; it is read from there and moved by
its next write.

kvgit dedups per key, so a turn writes one new run key and rewrites
the small index and record while every earlier run is shared by hash
with each prior commit, fork and branch. One key holding the whole
conversation would instead rewrite every turn and share nothing.

Values are the JSON-shaped dicts agno hands over, never pickled agno
objects, so a branch never depends on agno's class layout and dumping
one to plain files stays trivial.

The ``__conversation__/`` prefix follows the framework convention
(``__vfs_cwd__``, ``__cache__/``): the agent's ``cache`` view rejects
``__`` keys at write time, so agent code cannot reach the conversation.

Only the sessions table lives in the branch. User memories, metrics,
traces, evals and knowledge are inherited from agno's ``JsonDb`` and
stay on disk at ``db_path``: they are cross-session by design (a
user's memories span many conversations) and must not version with one
branch. Session state versions, world state does not.
"""

from __future__ import annotations

import copy
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agno.db.json import JsonDb
from agno.session import AgentSession, Session

from .. import conversation
from ..compaction import is_ours
from ..conversation import Index
from ..errors import NotSupportedError, WorkspaceError
from ..planes import COMPACTION_PREFIX, CONVERSATION_RUN_PREFIX
from ..workspace import Workspace

if TYPE_CHECKING:
    from ..store import Store

HARNESS = "agno"
"""The harness name this adapter writes into the conversation index."""


def _kv(ws: Workspace) -> Any:
    """The provider's small-value mapping — the same store the files,
    the cache and the cwd live in, which is what makes one commit
    cover all of them. Reached through ``ws.provider`` because it is
    the substrate's surface, not the workspace's; the ``__`` prefix on
    these keys is what keeps them out of the agent's ``cache`` view."""
    return ws.provider.kv


def _runs_from(found: dict[str, Any], run_ids: Any) -> list[dict[str, Any]]:
    """The runs ``found`` holds (by id, as ``conversation.read_runs``
    answers), in ``run_ids`` order.

    ``read_runs`` hands back deep copies: agno's ``from_dict`` rewrites
    the nested dicts it is handed in place (a message's ``metrics``
    becomes a ``MessageMetrics`` object), and the store can hand back the
    very dict it holds. A shallow copy let one read turn the stored run
    into agno objects, so the next turn's write saw it as changed and
    stored it again."""
    return [found[str(rid)] for rid in run_ids if isinstance(found.get(str(rid)), dict)]


def _agno_record(index: Index, record: Any) -> dict[str, Any]:
    """agno's session dict, minus its runs, as this db answers with it:
    the stored record, with the session it belongs to, its fork lineage
    and its run ids read from the index. The index is core's, and a fork
    rebinds it without touching the record, so it is the one to trust."""
    data = copy.deepcopy(record) if isinstance(record, dict) else {}
    data["session_id"] = index.session
    if index.forked_from:
        session_data = dict(data.get("session_data") or {})
        session_data["forked_from_session_id"] = index.forked_from
        data["session_data"] = session_data
    data["run_ids"] = list(index.runs)
    return data


def _read_agno(kv: Any) -> dict[str, Any] | None:
    """The stored session, as :func:`_agno_record` assembles it, or
    ``None`` where none is stored. A conversation another harness wrote
    is not agno's to read, and reads as none."""
    index = conversation.read_index(kv)
    if index is None or index.harness != HARNESS:
        return None
    return _agno_record(index, conversation.read_record(kv, index))


def _store(
    kv: Any,
    data: dict[str, Any],
    *,
    runs: dict[str, Any] | None = None,
    drop: Any = (),
) -> None:
    """Write agno's session dict (with ``run_ids``) to the plane: the
    index from its session, lineage and run ids, the record from the
    rest, and ``runs`` by id. Staged; on a branch still on the legacy
    plane, this is its migration. A conversation another harness wrote
    is refused rather than overwritten."""
    held = conversation.read_index(kv)
    if held is not None and held.harness != HARNESS:
        raise NotSupportedError(
            f"This branch holds a conversation written by {held.harness!r}; "
            "the agno db does not write over another harness's conversation."
        )
    lineage = (data.get("session_data") or {}).get("forked_from_session_id")
    index = Index(
        harness=HARNESS,
        session=data.get("session_id"),
        runs=tuple(data.get("run_ids") or ()),
        forked_from=lineage,
    )
    record = {k: v for k, v in data.items() if k != "run_ids"}
    conversation.write(kv, index, record=record, runs=runs, drop=drop)


def _same(held: Any, run: Any) -> bool:
    """Whether a run agno writes back says what the stored one says.

    agno's own load-and-save of a session turns some of a run's empty
    fields from ``None`` into ``[]`` (``events``, for one), so a run
    written on one turn and handed back unchanged on the next differs
    from what is stored by nothing but that. Read as a change, every
    run was stored a second time a turn after it landed. ``None``, an
    absent key and an empty list or dict are taken to say the same
    thing; anything else is compared as it is.
    """

    def empty(value: Any) -> bool:
        return value is None or value == [] or value == {}

    if empty(held) and empty(run):
        return True
    if isinstance(held, dict) and isinstance(run, dict):
        return all(_same(held.get(k), run.get(k)) for k in held.keys() | run.keys())
    if isinstance(held, list) and isinstance(run, list):
        return len(held) == len(run) and all(map(_same, held, run))
    return held == run


def _commit_framework(ws: Workspace, info: dict) -> str | None:
    """Commit the framework's own write, tolerantly.

    Everything uncommitted goes with it, which is the point: the code
    around a workspace needs a commit whose scope does not depend on
    what the agent has staged, and ws-git's own status is measured
    against the agent's last commit, so nothing the agent is composing
    is disturbed by this. ``None`` when there was nothing to commit or
    the workspace cannot take one.
    """
    if ws.frozen or not ws.caps.versioned or not ws.uncommitted:
        return None
    return ws.commit(info=info)


def _without_compaction(run: dict) -> dict:
    """``run`` without the messages compaction inserted.

    A fold's summary pair is put in what the model is sent, flagged as
    history, and agno leaves history out of a stored run only while the
    agent's ``store_history_messages`` is off. The pair's ids carry
    compaction's mark, so it is dropped here whatever the agent's
    settings are: a stored run holds what was said in it, never a
    summary of what came before. Every path that stores a run passes
    through here: a session upsert, agno 3's ``upsert_run``, and a
    seeded fork.
    """
    messages = run.get("messages")
    if isinstance(messages, list) and any(
        isinstance(m, dict) and is_ours(m.get("id")) for m in messages
    ):
        run["messages"] = [
            m for m in messages if not (isinstance(m, dict) and is_ours(m.get("id")))
        ]
    return run


def _run_rows(record: dict[str, Any], found: dict[str, Any]) -> list[dict[str, Any]]:
    """The session's runs as agno 3's runs table holds them: one row
    each (``run_id``, ``session_id``, ``run_type``, ``status``,
    ``run_index``, ``run_data`` and the rest), built the way agno's own
    adapters build them, so ``get_run``/``get_runs`` answer in agno's
    shape. ``run_index`` is the run's place in the session's
    ``run_ids``. On agno 2, which has no runs table and none of agno 3's
    row helpers, the rows are built here in the same shape: agno 2 never
    calls these methods, but a caller probing for them (``getattr(db,
    "get_run", ...)``) must get an answer, not an ImportError."""
    try:
        from agno.db.utils import build_single_run_row
    except ImportError:  # agno 2
        build_single_run_row = _run_row

    rows = []
    for index, rid in enumerate(record.get("run_ids") or []):
        run = found.get(str(rid))
        if isinstance(run, dict):
            rows.append(
                build_single_run_row(
                    run=copy.deepcopy(run),
                    session_id=record.get("session_id"),
                    user_id=record.get("user_id"),
                    run_index=index,
                )
            )
    return rows


def _run_out(row: dict[str, Any], deserialize: bool | None) -> Any:
    """One row as ``get_run`` answers: the row itself, or the run
    object agno deserializes it into."""
    if not deserialize:
        return row
    try:
        from agno.db.utils import deserialize_run
    except ImportError:  # agno 2: see _run_rows
        deserialize_run = _deserialize_run

    return deserialize_run(row.get("run_type"), row["run_data"])


def _validate_pagination(limit: int | None, page: int | None) -> None:
    """agno 3's ``validate_pagination``, for agno 2: a page needs a
    limit, and pages count from 1."""
    if page is not None and limit is None:
        raise ValueError("`page` was provided without `limit`")
    if page is not None and page < 1:
        raise ValueError(f"`page` must be >= 1; got {page}")


def _run_type(run: dict[str, Any]) -> str:
    """agno 3's ``get_run_type`` for a stored run dict, for agno 2."""
    if run.get("agent_id") or run.get("agent_name"):
        return "agent"
    if run.get("team_id") or run.get("team_name"):
        return "team"
    return "workflow"


def _run_row(
    run: dict[str, Any],
    session_id: str | None,
    user_id: str | None = None,
    run_index: int | None = None,
) -> dict[str, Any]:
    """agno 3's ``build_single_run_row`` for a stored run dict, for agno 2:
    the same keys, so ``get_runs``' filters and ``deserialize=False``
    answer the same on both."""
    now = int(time.time())
    return {
        "run_id": run.get("run_id"),
        "session_id": session_id,
        "run_type": _run_type(run),
        "agent_id": run.get("agent_id"),
        "team_id": run.get("team_id"),
        "workflow_id": run.get("workflow_id"),
        "user_id": user_id,
        "parent_run_id": run.get("parent_run_id"),
        "status": run.get("status"),
        "run_index": run_index if run_index is not None else run.get("run_index"),
        "run_data": run,
        "created_at": run.get("created_at") or now,
        "updated_at": now,
    }


def _deserialize_run(run_type: str | None, run_data: dict[str, Any]) -> Any:
    """agno 3's ``deserialize_run``, for agno 2: the run class by type."""
    from agno.run.agent import RunOutput
    from agno.run.team import TeamRunOutput
    from agno.run.workflow import WorkflowRunOutput

    kind = run_type or _run_type(run_data)
    cls = {"agent": RunOutput, "team": TeamRunOutput}.get(kind, WorkflowRunOutput)
    return cls.from_dict(run_data)


def _select_runs(
    rows: list[dict[str, Any]],
    *,
    user_id: str | None = None,
    agent_id: str | None = None,
    team_id: str | None = None,
    workflow_id: str | None = None,
    status: Any = None,
    limit: int | None = None,
    page: int | None = None,
    sort_by: str | None = None,
    sort_order: str | None = None,
    deserialize: bool | None = True,
) -> Any:
    """``get_runs``' filters, order and pagination, as agno's own
    ``JsonDb`` applies them, over rows the caller gathered."""
    from agno.db.json.utils import apply_sorting

    try:
        from agno.db.utils import validate_pagination
    except ImportError:  # agno 2: see _run_rows
        validate_pagination = _validate_pagination

    validate_pagination(limit, page)
    for field, wanted in (
        ("user_id", user_id),
        ("agent_id", agent_id),
        ("team_id", team_id),
        ("workflow_id", workflow_id),
    ):
        if wanted is not None:
            rows = [r for r in rows if r.get(field) == wanted]
    if status is not None:
        value = getattr(status, "value", status)
        rows = [r for r in rows if r.get("status") == value]
    total = len(rows)
    if sort_by is not None:
        rows = apply_sorting(rows, sort_by, sort_order)
    else:
        rows = sorted(
            rows,
            key=lambda r: (r.get("session_id") or "", r.get("run_index") or 0),
        )
    if limit is not None:
        start = (page - 1) * limit if page is not None else 0
        rows = rows[start : start + limit]
    if not deserialize:
        return rows, total
    return [_run_out(row, True) for row in rows]


class KvgitSessionDb(JsonDb):
    """agno ``BaseDb`` holding one agent session in one workspace branch.

    A db instance is bound to one workspace and holds exactly one
    session: the one the conversation's index names (see
    :mod:`nontainer.conversation`). There is no routing — whatever agno writes
    through it lands in that branch — so an id that does not match the
    branch's is refused rather than stored beside it.

    Subclassing ``JsonDb`` rather than implementing ``BaseDb`` from
    scratch is deliberate: ``BaseDb`` is ~150 abstract methods and the
    sessions table is seven of them. Those seven are the whole exposure
    to agno churn; everything else is inherited untouched.

    **The commit trigger.** ``upsert_session`` writes its keys and then,
    when the upsert added or changed a run, commits the workspace
    with ``info={"tool": "turn", "runs": {run_id: status}}`` — the runs
    that write changed, in order, each with the status agno gave it
    (``"COMPLETED"``, or ``"RUNNING"`` for a checkpoint). So the turn's
    files, cache, cwd and conversation land in ONE commit, at the moment
    agno persists the run, and the commit says which run it carried. The db and not a post hook fires it because agno's run loop
    executes post hooks BEFORE it persists the session: a hook-driven
    commit would capture the turn's files but not its conversation, and
    the conversation would ride into the next turn's commit — exactly
    the files-versus-memory divergence this class exists to remove.
    Anchoring the commit to the persist makes agno's internal ordering
    irrelevant.

    **Under a turn.** While a turn is open on the workspace
    (``ws.turn``), nothing here commits: the writes stay staged and the
    turn's end lands them in one commit, stamped with how the run
    ended rather than with agno's status. That is how a cancelled or
    failed run kept with ``keep_aborted_run`` lands once, as what it
    was (see ``nontainer.adapters.agno.finish_turn``).

    An upsert that carries no new or changed run (agno creating the
    record before the first run) does not commit; it is staged and
    rides into the turn's commit. Under ``commit="call"`` the
    mutating tool calls have already committed and this is the turn's
    trailing write, so the head at the next user message includes the
    conversation either way.

    Pass the db to the toolkit as ``WorkspaceTools(ws,
    commit="turn", session_db=db)``. That is what tells the
    toolkit's ``end_turn`` hook to stand down, so wiring the hook stays
    harmless and existing embedder code keeps working.

    **Rewind.** ``ws.checkout(commit)`` rewinds the run keys with
    everything else, and agno re-reads the session from the db at the
    start of every run (``Agent.cache_session`` defaults to False), so
    the next run sees the rewound conversation with no invalidation
    step. The rewind is a restore COMMIT — the branch head moves
    forward to hold the earlier conversation — so the turns it stepped
    off are still in ``ws.log()`` and checking out the commit the rewind
    stepped off puts them back. ``cache_session=True`` would break that — agno would append
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
            _without_compaction(dict(run, session_id=self._ws.session))
            for run in (data.get("runs") or [])
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
            now = int(time.time())
            stored = {k: v for k, v in data.items() if k != "runs"}
            stored["session_type"] = "agent"
            stored["run_ids"] = run_ids
            stored["created_at"] = data.get("created_at") or now
            stored["updated_at"] = now
            _store(_kv(self._ws), stored, runs=dict(zip(run_ids, runs)))
            _commit_framework(
                self._ws, {"tool": "fork_session", "conversation": "copy"}
            )
        if not deserialize:
            out = dict(stored)
            out.pop("run_ids", None)
            out["runs"] = runs or None
            return out
        return AgentSession.from_dict({**stored, "runs": runs or None})

    # -- keys ----------------------------------------------------------

    def _record(self) -> dict[str, Any] | None:
        """The stored session dict (minus runs, with ``run_ids``), or
        None on a branch that has never held a session. A copy agno may
        rewrite in place, as with the runs."""
        return _read_agno(_kv(self._ws))

    def _read_runs(self, run_ids: list[str]) -> list[dict[str, Any]]:
        found = conversation.read_runs(_kv(self._ws), run_ids)
        return _runs_from(found, run_ids)

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
        runs = [_without_compaction(dict(run)) for run in (data.get("runs") or [])]
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
            # object that predates a checkout. The branch keeps its full
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
                    "cache_session=True produces after ws.checkout() — agno "
                    "keeps the pre-rewind session in memory and appends to "
                    "it. Leave cache_session at its default."
                )
            run_ids = known[:kept] + incoming

            # The runs this write changes, each with its status, for the
            # commit that lands them to name: a reader of the history
            # can then tell which commit carried which run without
            # reading the session record at every commit. Usually one
            # run; a checkpointed run is written again, as RUNNING
            # first and then finished.
            written: dict[str, Any] = {}
            changed: dict[str, Any] = {}
            held = conversation.read_runs(kv, [str(run["run_id"]) for run in runs])
            for run in runs:
                rid = str(run["run_id"])
                if not _same(held.get(rid), run):
                    changed[rid] = held[rid] = run
                    written[rid] = run.get("status")

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
            _store(kv, stored, runs=changed)

            if written and self._ws.turns.current is None:
                # The conversation is the framework's own write, and it
                # has to be durable at the moment agno persists the
                # run. An agent that left a ws-git composition open
                # over the turn boundary keeps it: ws-git measures
                # against the agent's own last commit, so this one
                # leaves its staged set staged and its work in progress
                # uncommitted. Under an open turn (ws.turn) the write
                # stays staged: the turn's end commits it once, stamped
                # with how the run ended.
                _commit_framework(self._ws, {"tool": "turn", "runs": written})

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
        run_data = _without_compaction(
            dict(run if isinstance(run, dict) else run.to_dict())
        )
        run_id = run_data.get("run_id")
        if not run_id:
            return
        with self._ws.lock:
            kv = _kv(self._ws)
            record = self._record()
            if record is None or record.get("session_id") != session_id:
                return
            rid = str(run_id)
            held = conversation.read_runs(kv, [rid]).get(rid)
            changed = {} if held == run_data else {rid: dict(run_data)}
            run_ids = list(record.get("run_ids") or [])
            if rid not in run_ids:
                run_ids.append(rid)
            if changed or run_ids != list(record.get("run_ids") or []):
                _store(kv, dict(record, run_ids=run_ids), runs=changed)

    # -- runs (agno 3's runs table) ----------------------------------
    #
    # agno 3 moved runs out of the session row and gave BaseDb
    # get_run/get_runs/delete_run/delete_runs. The runs here are the
    # branch's stored runs, the ones upsert_run writes, so these read and
    # remove those. Inherited from JsonDb, they looked for a runs
    # file under db_path that never exists, answered that no run was
    # there and reported deletes that removed nothing.

    def _run_ids(self) -> tuple[dict[str, Any] | None, list[str]]:
        record = self._record()
        return record, [str(r) for r in (record or {}).get("run_ids") or []]

    def get_run(self, run_id: str, deserialize: bool | None = True, **_: Any) -> Any:
        """The run, from this branch's runs, or None when it holds no
        run by that id."""
        record, run_ids = self._run_ids()
        if record is None or str(run_id) not in run_ids:
            return None
        found = conversation.read_runs(_kv(self._ws), [str(run_id)])
        rows = [
            r for r in _run_rows(record, found) if str(r.get("run_id")) == str(run_id)
        ]
        return _run_out(rows[0], deserialize) if rows else None

    def _rows(self, session_id: str | None = None) -> list[dict[str, Any]]:
        """This branch's runs as rows, when ``session_id`` is its own or
        unasked."""
        record, run_ids = self._run_ids()
        if record is None or (
            session_id is not None and record.get("session_id") != session_id
        ):
            return []
        found = conversation.read_runs(_kv(self._ws), run_ids)
        return _run_rows(record, found)

    def get_runs(
        self,
        session_id: str | None = None,
        user_id: str | None = None,
        agent_id: str | None = None,
        team_id: str | None = None,
        workflow_id: str | None = None,
        status: Any = None,
        limit: int | None = None,
        page: int | None = None,
        sort_by: str | None = None,
        sort_order: str | None = None,
        deserialize: bool | None = True,
        **_: Any,
    ) -> Any:
        """This branch's runs, filtered, ordered and paginated as agno's
        own ``JsonDb`` does it."""
        return _select_runs(
            self._rows(session_id),
            user_id=user_id,
            agent_id=agent_id,
            team_id=team_id,
            workflow_id=workflow_id,
            status=status,
            limit=limit,
            page=page,
            sort_by=sort_by,
            sort_order=sort_order,
            deserialize=deserialize,
        )

    def _delete_runs(self, run_ids: list[str]) -> list[str]:
        """Remove those of ``run_ids`` this branch holds: their keys and
        their ids in the session record. Committed when the workspace
        was clean, as a delete between turns is; otherwise staged with
        the turn in flight, so a run upserted and then deleted before
        the turn's commit never lands. The ids it removed.

        A forward change, like deleting a file: the commits before it
        still hold the run, and a rewind brings it back. A compaction
        fold anchored in a removed run stops applying, and an earlier
        one, or the full history, takes its place."""
        wanted = {str(r) for r in run_ids}
        with self._ws.lock:
            record, held = self._run_ids()
            gone = [rid for rid in held if rid in wanted]
            if record is None or not gone:
                return []
            was_clean = not self._ws.uncommitted
            record["run_ids"] = [rid for rid in held if rid not in wanted]
            record["updated_at"] = int(time.time())
            _store(_kv(self._ws), record, drop=gone)
            self._commit_management_write("delete_run", was_clean=was_clean)
            return gone

    def delete_run(self, run_id: str, **_: Any) -> bool:
        """Remove one run; whether this branch held it."""
        return bool(self._delete_runs([run_id]))

    def delete_runs(self, run_ids: list[str], **_: Any) -> None:
        """Remove the runs of ``run_ids`` this branch holds."""
        self._delete_runs(list(run_ids))

    def _commit_management_write(self, tool: str, *, was_clean: bool) -> None:
        """Commit a delete or rename made between turns.

        These arrive from outside the run loop — a session list, an
        admin action — and nothing else would commit them, so the
        store's listing (which reads committed heads) and a reopen of
        the branch would both still show the old conversation. When the
        workspace already held uncommitted work, the write stays with
        it: committing then would close a turn in flight early, with
        the agent's half-finished files in it."""
        if was_clean and self._ws.turns.current is None:
            _commit_framework(self._ws, {"tool": tool})

    def delete_session(self, session_id: str, user_id: str | None = None) -> bool:
        """Clear the session and run keys, committed when the workspace
        was clean; otherwise staged with the turn in flight."""
        with self._ws.lock:
            record = self._record()
            if record is None or not self._matches(
                record, session_id=session_id, user_id=user_id
            ):
                return False
            was_clean = not self._ws.uncommitted
            conversation.clear(_kv(self._ws))
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
            was_clean = not self._ws.uncommitted
            session_data = dict(record.get("session_data") or {})
            session_data["session_name"] = session_name
            record["session_data"] = session_data
            _store(_kv(self._ws), record)
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
    gives the new branch every key and rebinds the conversation's index,
    so the fork's session reads with its own ``session_id`` (the branch
    name) and the parent in ``session_data["forked_from_session_id"]``,
    where agno keeps fork lineage, in a commit of the fork's own — its
    head is consistent from the start.

    ``conversation="inherit"`` keeps the parent's runs — the branch is
    the same chat over its own files from here on. ``"fresh"`` deletes
    the run keys and compaction's folds over them, and clears
    ``run_ids``: a clean chat over the forked
    files, which is not ``ws.fork(inherit="fresh")`` — that drops the
    record too, and this keeps a session there to write into. Run ids
    are left alone; agno mints fresh ones on its own fork only to
    avoid collisions inside a shared db, and branches never share one.

    ``at`` branches from an earlier commit of this session — files
    and conversation as they stood there — without rewinding this
    session to get there; it is what "branch from where I published"
    wants. Drive the fork with an agent whose ``session_id`` is
    ``name``.
    """
    if conversation not in ("inherit", "fresh"):
        raise ValueError(f"conversation must be 'inherit' or 'fresh': {conversation!r}")

    child = ws.fork(name, at=at, inherit="full")
    if conversation == "inherit":
        # The fork itself carried and rebound the record, in its own
        # commit; there is nothing left to write and nothing to commit.
        return child
    with child.lock:
        kv = _kv(child)
        # compaction's folds over the runs: a summary of a chat the
        # fresh one never had would never apply, only linger
        for key in [
            k
            for k in list(kv.keys())
            if isinstance(k, str) and k.startswith(COMPACTION_PREFIX)
        ]:
            del kv[key]
        record = _read_agno(kv)
        if record is not None:
            stored = [
                k[len(CONVERSATION_RUN_PREFIX) :]
                for k in list(kv.keys())
                if isinstance(k, str) and k.startswith(CONVERSATION_RUN_PREFIX)
            ]
            _store(
                kv,
                dict(record, run_ids=[]),
                drop=set(stored) | set(record["run_ids"]),
            )
    _commit_framework(child, {"tool": "fork_session", "conversation": conversation})
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
    life belongs to the embedder (``Store.delete``). Every other
    ``BaseDb`` table is inherited from ``JsonDb`` and lives at
    ``db_path``, shared across all sessions, which is what agno expects
    of memories and metrics.
    """

    def __init__(
        self,
        store: Store | str | Path,
        *,
        open: Callable[[str], Workspace],
        db_path: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(db_path=db_path, **kwargs)
        # The embedder's own ``Store``, so this db reads the very
        # repository its workspaces write through — whatever backend
        # that store keeps its data in. A path is taken as
        # ``Store(path)``, which finds the backend the same way.
        from ..store import Store

        self._store = store if isinstance(store, Store) else Store(store)
        self._open = open
        self._view_kwargs: dict[str, Any] = {"db_path": db_path, **kwargs}
        # Sessions this db has written through. Their live workspaces
        # can hold runs staged since the last commit, which a branch's
        # committed head does not show, so looking a run up asks them
        # first (see ``_holder``).
        self._written: set[str] = set()

    def owns(self, workspace: Workspace) -> bool:
        """Whether this db commits the turns of ``workspace``: true when
        the embedder's ``open`` hands back that very object for its
        session, which is the live-instance contract above."""
        return self._open(workspace.session) is workspace

    # -- the store -----------------------------------------------------

    def _kvgit(self) -> Any:
        """The store's kvgit repository, shared with its workspaces."""
        return self._store.repo

    def _branches(self) -> list[str]:
        return list(self._kvgit().branches)

    def _peek(self, branch: str) -> tuple[Any, dict[str, Any] | None]:
        """A snapshot of a branch's committed head and the session it
        holds, without opening the branch as a workspace: the snapshot
        reads only the keys asked of it, and reading makes no branch, so
        a name the store does not hold reads as ``(None, None)``. The
        snapshot serves the session's runs too."""
        repo = self._kvgit()
        if branch not in repo.branches:
            return None, None
        snapshot = repo.snapshot(branch=branch)
        return snapshot, _read_agno(snapshot)

    def _exists(self, session_id: str) -> bool:
        return session_id in self._branches()

    def _view(self, session_id: str) -> KvgitSessionDb:
        return KvgitSessionDb(self._open(session_id), **self._view_kwargs)

    def _write_view(self, session_id: str) -> KvgitSessionDb:
        """The view a write goes through, remembered so a later run
        lookup reads that session live (``_holder``)."""
        self._written.add(session_id)
        return self._view(session_id)

    def _holder(self, run_id: str) -> str | None:
        """The session holding run ``run_id``: one this db has written
        through, read live so a run staged since its last commit is
        found, else any branch whose committed session record lists it
        (one key read per branch, as ``get_sessions`` reads). None when
        no session holds it."""
        rid = str(run_id)
        for session_id in sorted(self._written):
            if self._exists(session_id):
                # Only a branch whose record names it holds a session, as
                # below: a plain Workspace.fork() carries its parent's
                # record, run ids and all, and must not stand in for it.
                record, run_ids = self._view(session_id)._run_ids()
                if (
                    record is not None
                    and record.get("session_id") == session_id
                    and rid in run_ids
                ):
                    return session_id
        for branch in self._branches():
            if branch in self._written:
                continue
            _, record = self._peek(branch)
            if (
                isinstance(record, dict)
                and record.get("session_id") == branch
                and rid in [str(r) for r in record.get("run_ids") or []]
            ):
                return branch
        return None

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
        matched: list[tuple[str, Any, dict[str, Any]]] = []
        for branch in self._branches():
            snapshot, record = self._peek(branch)
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
            matched.append((branch, snapshot, record))

        key = sort_by if sort_by in ("created_at", "updated_at") else "created_at"
        matched.sort(
            key=lambda item: item[2].get(key) or 0, reverse=sort_order != "asc"
        )
        total = len(matched)
        if limit is not None:
            start = max((page or 1) - 1, 0) * limit
            matched = matched[start : start + limit]

        found: list[dict[str, Any]] = []
        for _branch, snapshot, record in matched:
            data = {k: v for k, v in record.items() if k != "run_ids"}
            run_ids = record.get("run_ids") or []
            held = conversation.read_runs(snapshot, run_ids)
            data["runs"] = _runs_from(held, run_ids) or None
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
            return self._write_view(session_id).upsert_session(
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

        return self._write_view(session_id).upsert_session(
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
        self._write_view(session_id).upsert_run(
            run, session_id, user_id=user_id, run_index=run_index, **kwargs
        )

    # -- runs (agno 3's runs table) ----------------------------------

    def get_run(self, run_id: str, deserialize: bool | None = True, **_: Any) -> Any:
        """The run, from whichever session holds it (``_holder``), or
        None. agno 3 asks by run id alone, as AgentOS's session service
        does."""
        holder = self._holder(run_id)
        if holder is None:
            return None
        return self._view(holder).get_run(run_id, deserialize=deserialize)

    def get_runs(
        self,
        session_id: str | None = None,
        user_id: str | None = None,
        agent_id: str | None = None,
        team_id: str | None = None,
        workflow_id: str | None = None,
        status: Any = None,
        limit: int | None = None,
        page: int | None = None,
        sort_by: str | None = None,
        sort_order: str | None = None,
        deserialize: bool | None = True,
        **_: Any,
    ) -> Any:
        """One session's runs, or with no ``session_id`` every session's,
        filtered, ordered and paginated as agno's own ``JsonDb`` does
        it. A session this db has written through is read live; the
        rest at their committed heads, as ``get_sessions`` reads them."""
        if session_id is not None:
            branches = [session_id] if self._exists(session_id) else []
        else:
            branches = self._branches()
        rows: list[dict[str, Any]] = []
        for branch in branches:
            if branch in self._written:
                # filtered to its own session, as the committed path is
                rows += self._view(branch)._rows(branch)
                continue
            snapshot, record = self._peek(branch)
            if not isinstance(record, dict) or record.get("session_id") != branch:
                continue
            run_ids = [str(r) for r in record.get("run_ids") or []]
            held = conversation.read_runs(snapshot, run_ids)
            rows += _run_rows(record, held)
        return _select_runs(
            rows,
            user_id=user_id,
            agent_id=agent_id,
            team_id=team_id,
            workflow_id=workflow_id,
            status=status,
            limit=limit,
            page=page,
            sort_by=sort_by,
            sort_order=sort_order,
            deserialize=deserialize,
        )

    def delete_run(self, run_id: str, **_: Any) -> bool:
        """Remove the run from whichever session holds it; whether one
        did."""
        holder = self._holder(run_id)
        if holder is None:
            return False
        return self._write_view(holder).delete_run(run_id)

    def delete_runs(self, run_ids: list[str], **_: Any) -> None:
        """Remove each run from whichever session holds it."""
        by_session: dict[str, list[str]] = {}
        for run_id in run_ids:
            holder = self._holder(run_id)
            if holder is not None:
                by_session.setdefault(holder, []).append(run_id)
        for holder, ids in by_session.items():
            self._write_view(holder).delete_runs(ids)

    def delete_session(self, session_id: str, user_id: str | None = None) -> bool:
        if not self._exists(session_id):
            return False
        return self._write_view(session_id).delete_session(session_id, user_id=user_id)

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
