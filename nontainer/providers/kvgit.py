"""KvgitProvider: the versioned substrate (default backend).

One shared kvgit store; each session is a branch. Files (via monkeyfs
``VirtualFS``), the agent cache, and framework keys (the working
directory, the ws-git blob) all live in one flat ``Staged`` mapping —
so one ``commit()`` commits the whole world atomically, and
``checkout()`` restores all of it (including where the agent's cwd
was) as a new commit.

Key coexistence in the flat mapping: ``VirtualFS`` encodes file paths
under its own key prefix (plus ``__vfs_metadata__``), while cache keys
live under ``__cache__/`` — no collisions by construction.

Capabilities: ``staging`` (writes are invisible until commit;
``discard()`` drops them), ``cheap_fork`` (branches share storage via
kvgit's content-addressed HAMT), ``merge`` (CAS + key-level three-way
with conflict markers), ``index`` (keyed commits — ``commit_keys``
takes a subset of the tree, which is what lets the agent-facing git
fiction hold an index of its own; see ``nontainer/agentgit.py``).

``info`` dicts attached to commits must be JSON-serializable
(kvgit hashes them into the commit id).

Tag storage: kvgit tags live in one flat namespace per store, so
nontainer prefixes every tag with its scope. A session tag ``v1`` made
by session ``alice`` is stored as ``alice/v1``, a store tag ``v1`` as
``@store/v1`` — both under kvgit's reserved ``refs/tags/``. That is what
lets two sessions each hold a ``v1``, lets ``tags()`` list a session's
own without seeing anyone else's, and lets session teardown drop a
session's tags by prefix while store tags stay. Callers never see the
prefix: names go in and come out bare.

The store prefix leads with ``@`` so that no session can reach it: a
session id must begin with a letter, digit, underscore or hyphen, so
``@store`` is not a name any session can have, and the two prefixes
cannot collide however a session is named.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Mapping, MutableMapping
from pathlib import Path
from typing import Any

from ..agentgit import BLOB_KEY as _WS_BLOB_KEY
from ..errors import CommitNotFoundError, NotSupportedError, WorkspaceError
from ..planes import CACHE_PREFIX, CONVERSATION_PREFIX
from ..protocol import (
    Capabilities,
    CommitInfo,
    MergeOutcome,
    TagInfo,
    WorkspaceDiff,
    validate_session_id,
)
from ..views import VIEW_KEY as _VIEW_KEY
from ..views import parse_seed

#: How many commits ``checkout`` will make to land the target's exact
#: state before it gives up. One is the quiet case; a second is one
#: concurrent writer losing a race with it. Past that, something is
#: committing to this session in a loop and the caller has to be told.
_CHECKOUT_ATTEMPTS = 5

_KVGIT_CAPS = Capabilities(
    versioned=True,
    staging=True,
    cheap_fork=True,
    merge=True,
    index=True,
    sql_audit=False,
    fuse_mount=False,
    tags=True,
)

_LEGACY_CWD_KEY = "__cwd__"
"""The cwd key nontainer kept beside the filesystem's own, before the
two were folded into one. Only ever swept now — a workspace drops it on
open, and a merge hands it to whichever side is doing the merging."""


def _keep_ours(old: Any, ours: Any, theirs: Any) -> Any:
    """Positional session state (cwd): the merger's context wins."""
    return ours if ours is not None else theirs


def _merge_vfs_metadata(old: Any, ours: Any, theirs: Any) -> bytes:
    """Field-aware merge for the monkeyfs file table.

    The table (``{path: {size, created_at, modified_at, is_dir}}``)
    contests on every two-sided change through timestamp noise alone,
    so line-merge is the wrong tool: merge per path instead. Unchanged
    on one side takes the other; changed on both takes the latest
    ``modified_at`` with the earliest ``created_at`` (timestamps are
    advisory — content truth lives in the file blobs this table
    describes); deleted on one side and unmodified on the other drops;
    deleted-against-modified keeps the modified record (the content
    markers flag the path anyway); ``is_dir`` disagreement and
    unparseable tables raise ``CantMark``: filed as a hard conflict
    (the merge aborts untouched) like binary.
    """
    from kvgit.merges import CantMark

    def table(value: Any) -> dict:
        if value is None:
            return {}
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError) as e:
            raise CantMark(f"VFS metadata not JSON: {e}") from e
        if not isinstance(parsed, dict):
            raise CantMark("VFS metadata not a table")
        return parsed

    old_t, ours_t, theirs_t = table(old), table(ours), table(theirs)
    merged: dict[str, Any] = {}
    for path in old_t.keys() | ours_t.keys() | theirs_t.keys():
        o, u, t = old_t.get(path), ours_t.get(path), theirs_t.get(path)
        if u == t:
            if u is not None:
                merged[path] = u
        elif o == u:
            if t is not None:
                merged[path] = t
        elif o == t:
            if u is not None:
                merged[path] = u
        else:
            if u is None:
                merged[path] = t
            elif t is None:
                merged[path] = u
            else:
                if u.get("is_dir", False) != t.get("is_dir", False):
                    raise CantMark(f"is_dir disagreement on {path!r}: not mergeable")
                winner = (
                    u if u.get("modified_at", "") >= t.get("modified_at", "") else t
                )
                created = [
                    r.get("created_at", "")
                    for r in (o, u, t)
                    if isinstance(r, dict) and r.get("created_at")
                ]
                merged[path] = {
                    "size": winner.get("size", 0),
                    "created_at": min(created) if created else "",
                    "modified_at": winner.get("modified_at", ""),
                    "is_dir": winner.get("is_dir", False),
                }
    return json.dumps(merged, sort_keys=True).encode()


class _FileView(Mapping):
    """One tree's files as ``path -> stored value``, read on demand.

    Backs :meth:`KvgitProvider.files_at` and
    :meth:`KvgitProvider.working_files`. The path index is built once
    per view (a key listing plus the VFS decode); values are read from
    the handle as they are asked for, so comparing two trees costs the
    blobs it actually reads.
    """

    __slots__ = ("_provider", "_handle", "_paths")

    def __init__(self, provider: "KvgitProvider", handle: Any) -> None:
        self._provider = provider
        self._handle = handle
        self._paths: dict[str, str] | None = None

    @property
    def _index(self) -> dict[str, str]:
        if self._paths is None:
            self._paths = {
                path: key
                for key, path in self._provider._file_keys(self._handle.keys()).items()
            }
        return self._paths

    def __getitem__(self, path: str) -> Any:
        return self._handle.get(self._index[path])

    def __iter__(self) -> Iterator[str]:
        return iter(self._index)

    def __len__(self) -> int:
        return len(self._index)

    def __repr__(self) -> str:
        return f"<files: {len(self)} paths>"


SESSION_SCOPE = "session"
STORE_SCOPE = "store"
# Leading "@": session ids cannot start with one (SESSION_ID_RE), so no
# session's tag namespace can ever be the store's.
_STORE_PREFIX = "@store/"


def _validate_branch(name: str) -> str:
    """A session id, or one of the store's own reserved branches.

    A session id cannot begin with ``@`` (``SESSION_ID_RE``), which is
    what makes ``@store/`` safe as the store's own branch namespace: a
    publication lives on ``@store/pub/<name>/<version>``. Those
    branches are not sessions — ``Store.sessions()`` never lists one —
    but a provider still has to be constructible over one, since that
    is how a publication's tree is written and read back.
    """
    if name.startswith(_STORE_PREFIX):
        return name
    return validate_session_id(name)


class KvgitProvider:
    """``WorkspaceProvider`` over a kvgit ``Staged`` branch.

    Construct via :meth:`open` (path-based, branch-per-session) or
    directly from a ``Staged`` you built yourself (custom codecs,
    memory stores for tests, an existing store).
    """

    def __init__(
        self, staged: Any, *, session: str, frozen_at: str | None = None
    ) -> None:
        """``frozen_at`` names the tag this handle was opened at, and
        makes it a snapshot: see :meth:`at_tag`. Only that method passes
        it — a handle on a branch head is never frozen."""
        _validate_branch(session)
        self._session = session
        self._staged = staged
        self._frozen_at = frozen_at
        self._fs: Any | None = None
        self._closed = False
        # The framework's own keys, with the policy the merge verb
        # already uses for them, registered for ORDINARY commits too.
        # A commit whose CAS is lost to another handle on this session
        # three-way merges by key, and these three are the keys any two
        # writers are bound to contend on: the file table (every write
        # touches it), the cwd, and the ws-git blob. Without a policy
        # each of those turns a routine race into a raised conflict.
        register = getattr(staged, "set_merge_fn", None)
        if callable(register):
            from kvgit import MergeChoice
            from monkeyfs import VirtualFS

            register(VirtualFS.METADATA_KEY, _merge_vfs_metadata)
            register(VirtualFS.CWD_KEY, _keep_ours)
            register(_WS_BLOB_KEY, MergeChoice.OURS)
            register(_VIEW_KEY, MergeChoice.OURS)
            register(_LEGACY_CWD_KEY, MergeChoice.OURS)

    @classmethod
    def open(
        cls,
        path: str | Path | None = None,
        *,
        session: str,
        codecs: str | None = None,
    ) -> "KvgitProvider":
        """Open (or create) the shared store and this session's branch.

        Args:
            path: Store directory (disk backend). ``None`` = in-memory
                (tests / ephemeral).
            session: Branch name. A new name starts an empty branch;
                an existing name resumes it.
            codecs: Optional kvgit codec preset (e.g. ``"scientific"``
                for numpy/pandas chunk dedup).
        """
        import kvgit

        _validate_branch(session)
        if path is None:
            staged = kvgit.store(kind="memory", branch=session, codecs=codecs)
        else:
            p = Path(path).expanduser()
            p.mkdir(parents=True, exist_ok=True)
            staged = kvgit.store(
                kind="disk", path=str(p), branch=session, codecs=codecs
            )
        return cls(staged, session=session)

    @classmethod
    def delete(
        cls, path: str | Path, sessions: Iterable[str], *, min_age: float = 3600
    ) -> None:
        """Delete the named session branches from the shared store.

        Symmetric with :meth:`open`, and plural on purpose: a caller
        typically owns more branches than the one session id (studio
        also holds published-snapshot branches its own bookkeeping
        knows about), and deleting them is one store's worth of work.

        Deleting a name that doesn't exist is a no-op; so is deleting
        from a store dir that was never materialized. Names are treated
        as branch names, not validated as session ids — snapshot
        branches (``<slug>-pub-<hex>``) are legitimate targets a caller
        passes through.

        A session's own tags go with it: every tag stored under
        ``<name>/`` is deleted alongside the branch, because a
        session-scoped tag belongs to that session. Store-scoped tags
        (``@store/``) are left exactly where they are — that scope exists
        so a publication can outlive the session that made it, and
        teardown is where that promise is kept; no session id can spell
        that prefix, so no name passed here can reach them. Removing a
        tag is what makes its commits collectable, so the sweep runs
        first and the branch deletion's own orphan sweep finishes the
        job.

        This lives in kvgit now: :func:`kvgit.delete_branches` opens the
        raw backend with no current branch, so it can drop any branch —
        including the store's only one, the case a branch-anchored handle
        can't reach.

        ``min_age`` is the orphan sweep's grace period in seconds:
        commits younger than it are left alone, so a concurrent writer
        mid-commit is never swept out from under. Lower it only when
        nothing else is writing (tests, a controlled teardown).
        """
        import kvgit

        requested = set(sessions)
        if not requested:
            return  # nothing asked: don't touch the store
        p = Path(path).expanduser()
        if not p.is_dir():
            return  # never-materialized (or non-kvgit) store: nothing here

        # Legacy cleanup: stores that ran the old code minted a hidden
        # ``__void__`` anchor branch that pinned a dead session's entire
        # history (create_branch forks from the current commit), silently
        # defeating orphan GC. The anchor-free admin API can delete it
        # safely — it carries no wanted state — so we always sweep it into
        # the doomed set, erasing that retention bug on the next delete.
        names = requested | {"__void__"}

        cls._delete_session_tags(p, requested)

        # No branch anchor needed, no probe, no void dance: one call
        # removes each head + its prev-HEAD backup and sweeps orphans.
        # Missing names (including __void__ on stores that never had one)
        # are no-ops, and a dir that isn't a kvgit store has no branch
        # keys to match, so the old tolerance is preserved.
        kvgit.delete_branches(names, kind="disk", path=str(p), min_age=min_age)

    @staticmethod
    def _delete_session_tags(path: Path, sessions: Iterable[str]) -> None:
        """Delete every tag stored under ``<session>/`` for these names.

        The names have to be listed before they can be deleted, and
        kvgit's anchor-free admin surface deletes tags without listing
        them — so the backend is opened directly here, the way
        :func:`kvgit.delete_tags` opens it, rather than through a handle
        that would have to invent a branch to sit on.
        """
        import kvgit
        from kvgit.kv.disk import Disk
        from kvgit.versioned.kv import tags as store_tags

        prefixes = tuple(f"{s}/" for s in sessions)
        if not prefixes:
            return
        backend = Disk(str(path))
        try:
            doomed = [name for name in store_tags(backend) if name.startswith(prefixes)]
        finally:
            close = getattr(backend, "close", None)
            if callable(close):
                close()
        if doomed:
            kvgit.delete_tags(doomed, kind="disk", path=str(path))

    # -- identity ------------------------------------------------------

    @property
    def session(self) -> str:
        return self._session

    @property
    def caps(self) -> Capabilities:
        return _KVGIT_CAPS

    @property
    def staged(self) -> Any:
        """The underlying kvgit ``Staged`` (host-side power tool)."""
        return self._staged

    @property
    def frozen(self) -> bool:
        """This handle is a snapshot at a tag: reads work, nothing
        commits. See :meth:`at_tag`."""
        return self._frozen_at is not None

    @property
    def frozen_at(self) -> str | None:
        """The tag this snapshot was opened at, or ``None``."""
        return self._frozen_at

    def _refuse_frozen(self, op: str) -> None:
        if self._frozen_at is not None:
            raise NotSupportedError(
                f"frozen: this workspace is a snapshot at tag "
                f"{self._frozen_at!r}; it accepts no writes, so {op}() "
                "is not supported"
            )

    # -- surfaces ------------------------------------------------------

    @property
    def fs(self) -> Any:
        if self._fs is None:
            from monkeyfs import VirtualFS

            self._fs = VirtualFS(self._staged)
        return self._fs

    @property
    def kv(self) -> MutableMapping[str, Any]:
        return self._staged

    @property
    def dirty(self) -> bool:
        return bool(self._staged.has_changes)

    # -- versioning ----------------------------------------------------

    @property
    def head(self) -> str:
        return self._staged.current_commit

    def commit(self, info: dict[str, Any] | None = None) -> str:
        """Commit staged fs + kv writes atomically; returns the commit
        hash. No staged changes → no new commit (returns current)."""
        self._refuse_frozen("commit")
        if not self._staged.has_changes:
            return self._staged.current_commit
        result = self._staged.commit(info=info)
        if not result.merged:
            raise WorkspaceError(
                f"commit failed: conflicting concurrent commit on branch "
                f"{self._session!r} (CAS): {result}"
            )
        return self._staged.current_commit

    def checkout(self, commit_id: str, *, info: dict[str, Any] | None = None) -> str:
        """Append a commit whose keyset equals that commit's; return it.

        A restore, not a rewind: the target's state is WRITTEN into the
        working buffer and committed, so the branch head only ever
        moves forward and every commit made since the target stays in
        ``history()`` — which is what makes an undo redo-able. kvgit's
        ``reset_to`` is deliberately not used.

        The whole keyset moves, not the files alone: the cache, the
        cwd, the VFS table, the ws-git blob and the stored conversation
        are keys like any other, and the host restored the whole world.
        The keys to touch come from kvgit's keyset diff between the
        head and the target, so the cost is the size of the change and
        not the size of the tree.

        IT CONVERGES ON THE TARGET. Another handle on this session can
        commit between the diff and the commit; kvgit then three-way
        merges that state into the restore, and a key it added — one
        absent from both the head this started at and the target — is
        in neither list, so it would ride into a commit claiming to be
        the target's state. So the restore is re-diffed against the
        head that actually landed and re-applied until nothing is left
        to write, bounded by ``_CHECKOUT_ATTEMPTS``. Writes that land
        in between are superseded, not lost: they are commits of this
        session like any other, and ``history()`` holds them.

        Convergence is judged by VALUE, not by kvgit's keyset
        pointers: a key rewritten with the bytes it already had gets a
        fresh pointer, so every key the restore wrote reads as
        modified against the target however equal the two are. Which
        is the same reason values are compared before they are written
        where both commits hold a key — a checkout onto state the
        workspace already holds writes nothing and returns the current
        head.

        Uncommitted writes are replaced by the restored state (see the
        protocol; ``discard()`` is the explicit spelling for a caller
        who wants only that).
        """
        self._refuse_frozen("checkout")
        target = self._staged.checkout(commit_id)
        if target is None:
            raise CommitNotFoundError(f"No such commit: {commit_id!r}")
        # The diff below is between two COMMITS: a buffer left in place
        # would ride into the restore commit as though it were part of
        # the target's state.
        if self._staged.has_changes:
            self._staged.reset()
        landed = 0
        while True:
            head = self._staged.current_commit
            self._stage_restore(target, head, commit_id)
            # HEAD moved (or the buffer did) under the VirtualFS
            # object: drop its caches the way discard() does.
            self._invalidate_fs()
            if not self._staged.has_changes:
                # Nothing left to write: this head IS the target's
                # state, whether it took one commit or several.
                return head
            if landed >= _CHECKOUT_ATTEMPTS:
                self._staged.reset()
                self._invalidate_fs()
                raise WorkspaceError(
                    f"checkout of {commit_id!r} on session {self._session!r} "
                    f"could not converge in {_CHECKOUT_ATTEMPTS} commits: the "
                    "branch head keeps moving under it. Another handle is "
                    f"writing to this session; the session is left at "
                    f"{head!r}, and checking out again once that writer is "
                    "done lands the target."
                )
            self.commit(info={"tool": "checkout", "target": commit_id, **(info or {})})
            landed += 1

    def _stage_restore(self, target: Any, head: str, commit_id: str) -> None:
        """Stage the difference between ``head`` and the target commit.

        Leaves the buffer holding exactly what the head has to change
        to become the target's state — nothing when it already is, so
        an empty buffer afterwards is what says the restore is done.
        """
        changed = self._staged.versioned.diff(head, commit_id)
        for key in changed.added:
            self._staged[key] = target.get(key)
        for key in changed.modified:
            value = target.get(key)
            if self._staged.get(key) != value:
                self._staged[key] = value
        for key in changed.removed:
            if key in self._staged:
                del self._staged[key]

    def _invalidate_fs(self) -> None:
        """Drop VirtualFS's lazy caches after state changed underneath
        it (checkout/discard). The SAME fs instance must survive —
        Workspace and the sandbox hold references to it.
        """
        if self._fs is not None:
            self._fs.invalidate()

    def history(self, *, limit: int | None = None) -> Iterable[CommitInfo]:
        return self._history_iter(limit)

    def _history_iter(self, limit: int | None) -> Iterator[CommitInfo]:
        from kvgit.encoding import safe_loads

        store = self._staged.versioned.store
        count = 0
        for commit_hash in self._staged.history():
            if limit is not None and count >= limit:
                return
            raw_time = store.get(f"__commit_time__{commit_hash}")
            raw_info = store.get(f"__info__{commit_hash}")
            time_val = safe_loads(raw_time) if raw_time is not None else None
            info_val = safe_loads(raw_info) if raw_info is not None else None
            yield CommitInfo(
                id=commit_hash,
                time=float(time_val) if time_val is not None else 0.0,
                info=info_val if isinstance(info_val, dict) else {},
                tree=self._tree(commit_hash),
            )
            count += 1

    def _tree(self, commit_hash: str) -> str | None:
        """The commit's keyset root hash — the identity of its content.

        Read straight off the store key that holds it, the same way this
        provider reads a commit's time and info: kvgit has no public
        accessor for the root, and these three raw reads are the one
        place that knows their key names.
        """
        from kvgit.encoding import safe_loads

        raw = self._staged.versioned.store.get(f"__commit_root__{commit_hash}")
        if raw is None:
            return None
        root = safe_loads(raw)
        return root if isinstance(root, str) else None

    def fork(self, name: str, *, at: str | None = None) -> "KvgitProvider":
        """O(1) branch sharing storage.

        Without ``at``, pending staged changes are committed first so
        the fork sees current state. With ``at`` the fork starts from
        that earlier commit instead, and the staged changes are left
        alone: they belong to this session's present, not to the past
        the fork is branching from.
        """
        self._refuse_frozen("fork")
        validate_session_id(name)
        if name in self._staged.list_branches():
            raise WorkspaceError(f"Branch already exists: {name!r}")
        if at is None:
            if self._staged.has_changes:
                self.commit(info={"tool": "fork", "target": name})
            forked = self._staged.create_branch(name)
        else:
            try:
                forked = self._staged.create_branch(name, at=at)
            except ValueError as e:
                if "does not exist" in str(e):
                    raise CommitNotFoundError(at) from e
                raise
        return KvgitProvider(forked, session=name)

    def discard(self) -> None:
        self._staged.reset()
        self._invalidate_fs()

    # -- read views ------------------------------------------------------

    def files_at(self, commit: str) -> Mapping[str, Any]:
        """This session's files at one commit, by workspace path.

        A frozen read view: a mapping whose keys are the paths and
        whose values are the stored file values, read on demand. What
        the agent-facing git needs to compare a tree against another
        one without materializing either.
        """
        handle = self._staged.checkout(commit)
        if handle is None:
            raise CommitNotFoundError(f"No such commit: {commit!r}")
        return _FileView(self, handle)

    def working_files(self) -> Mapping[str, Any]:
        """The live working tree's files, by workspace path — including
        writes not committed yet, and excluding deletions not committed
        yet."""
        return _FileView(self, self._staged)

    def key_at(self, commit: str, key: str) -> Any:
        """One store key's value at a commit, or ``None`` if absent.

        The non-file half of :meth:`files_at`: what reads bookkeeping
        (the ws-git blob) out of a commit without materializing a tree.
        """
        handle = self._staged.checkout(commit)
        if handle is None:
            raise CommitNotFoundError(f"No such commit: {commit!r}")
        return handle.get(key)

    def branch_head(self, session: str) -> str:
        """Another session's current commit on this store. Unknown
        names raise ``ValueError``."""
        return self._open_branch(session).current_commit

    def refresh(self) -> None:
        """Re-read the branch head, DISCARDING uncommitted writes.

        Recovery, not routine: what a caller reaches for when a commit
        lost its CAS to another handle on the same session and the
        work has to be re-applied against the head that won.
        """
        self._refuse_frozen("refresh")
        self._staged.refresh()
        self._invalidate_fs()

    def commit_keys(
        self, info: dict[str, Any] | None = None, *, keys: Iterable[str]
    ) -> str | None:
        """Commit exactly these keys; returns the new commit hash.

        The one keyed-commit primitive, and the only index-shaped thing
        the substrate has to offer: everything else about the agent's
        git is metadata over it (see ``nontainer/agentgit.py``). Keys
        with nothing pending are ignored, and ``None`` comes back when
        that leaves nothing at all.

        ``keys`` are store keys as stored, or absolute workspace paths
        resolved to the keys the VFS wrote them under — the framework
        planes (``__agno__/``, the cache) name themselves, and no store
        key starts with ``/``.

        The VFS table rides along, fixed up the way a partial commit
        fixes it up: rows for the committed files take their live
        versions and every other row stays at HEAD, so the commit
        describes its own blobs and says nothing about work in
        progress. Without it a committed file would have no row, and a
        reader of that commit would not see the file at all.
        """
        from monkeyfs import VirtualFS

        self._refuse_frozen("commit_keys")
        pending = {
            key for key in self._resolve_keys(keys) if self._staged.is_staged(key)
        }
        if not pending:
            return None
        live_table: Any = None
        if self._file_keys(pending):
            live_table = self._staged.get(VirtualFS.METADATA_KEY)
            self._commit_table(pending)
            if self._staged.is_staged(VirtualFS.METADATA_KEY):
                pending.add(VirtualFS.METADATA_KEY)
        from kvgit import MergeConflict

        try:
            try:
                result = self._staged.commit(keys=pending, info=info)
            except MergeConflict as e:
                # kvgit three-way merges a concurrent write on other
                # keys and raises only where both sides touched one.
                # Either way this is the CAS story, in this layer's
                # words: callers unwind on WorkspaceError.
                raise WorkspaceError(
                    f"commit failed: conflicting concurrent commit on branch "
                    f"{self._session!r} (CAS): {e}"
                ) from e
            if not result.merged:
                raise WorkspaceError(
                    f"commit failed: conflicting concurrent commit on branch "
                    f"{self._session!r} (CAS): {result}"
                )
        finally:
            # The fixup describes a commit; live truth is what every
            # read afterwards needs — including the reads a caller
            # makes while unwinding a commit that did not happen.
            self._restore_live_table(live_table)
            self._invalidate_fs()
        return self._staged.current_commit

    def _restore_live_table(self, live_table: Any) -> None:
        """Put the pre-fixup VFS table back after a selective commit.

        The fixup describes the commit's own blobs; live truth is what
        every read afterwards needs — unstaged additions with rows,
        unstaged edits at their live sizes. Nothing to restore when the
        commit touched no files.
        """
        from monkeyfs import VirtualFS

        if live_table is None:
            return
        # Normalize: the restore must stage bytes — a decoded value
        # written back raw would break VFS table reads.
        if not isinstance(live_table, bytes):
            live_table = (
                live_table.encode()
                if isinstance(live_table, str)
                else json.dumps(live_table, sort_keys=True).encode()
            )
        if self._staged.get(VirtualFS.METADATA_KEY) != live_table:
            self._staged[VirtualFS.METADATA_KEY] = live_table

    def _resolve_keys(self, keys: Iterable[str]) -> set[str]:
        """Caller-named state as store keys.

        An absolute path is the workspace's spelling of a file and
        encodes to the key the VFS writes it under; anything else is
        already a store key. Pure encoding, no existence check: a
        caller naming a path that is being deleted means exactly that,
        and a key with nothing pending is ignored by the commit anyway.
        """
        entries = list(keys)
        if not entries:
            return set()
        vfs = self.fs
        return {e if not e.startswith("/") else vfs._encode_path(e) for e in entries}

    def _commit_table(self, staged: set[str]) -> None:
        """Rewrite the VFS table to match the blobs selective commit takes.

        The table is monolithic but the commit is partial: rows for
        files outside the commit keep their HEAD versions, rows for
        staged files take their live versions (staged deletions drop
        their rows). Directory rows stay at HEAD versions — the VFS
        detects implicit dirs from blob keys. Skips the write when the
        table already matches.
        """
        from monkeyfs import VirtualFS

        def table(raw: Any) -> dict:
            if raw is None:
                return {}
            try:
                parsed = json.loads(raw)
            except (ValueError, TypeError):
                return {}
            return parsed if isinstance(parsed, dict) else {}

        vfs = self.fs
        live_table = table(self._staged.get(VirtualFS.METADATA_KEY))
        head_handle = self._staged.checkout(self._staged.current_commit)
        head_table = (
            table(head_handle.get(VirtualFS.METADATA_KEY))
            if head_handle is not None
            else {}
        )
        live_keys = set(self._file_keys(self._staged.keys()))
        committed = dict(head_table)
        for key in staged:
            try:
                row = vfs._decode_path(key).lstrip("/")
            except Exception:  # noqa: BLE001 - undecodable key is not a file
                continue
            if key in live_keys and row in live_table:
                committed[row] = live_table[row]
            else:
                committed.pop(row, None)
        if committed != live_table:
            self._staged[VirtualFS.METADATA_KEY] = json.dumps(
                committed, sort_keys=True
            ).encode()

    # -- tags ------------------------------------------------------------

    def _scoped(self, name: str, scope: str) -> str:
        """The stored tag name for a caller's name in a scope.

        Rejects a name that already carries a scope prefix, so a caller
        cannot reach the store scope by spelling ``@store/x`` as a
        session tag, or its own session's namespace from the store
        scope. Beyond that the rule is kvgit's: any non-empty name
        without ``%``.
        """
        if not isinstance(name, str) or not name:
            raise ValueError("Tag name must be a non-empty string")
        if "%" in name:
            raise ValueError(f"Tag name must not contain '%': {name!r}")
        if name.startswith(_STORE_PREFIX) or name.startswith(f"{self._session}/"):
            raise ValueError(
                f"Tag name {name!r} starts with a scope prefix; pass the bare "
                "name and say scope='store' or scope='session' instead"
            )
        return f"{self._prefix(scope)}{name}"

    def _prefix(self, scope: str) -> str:
        if scope == STORE_SCOPE:
            return _STORE_PREFIX
        if scope == SESSION_SCOPE:
            return f"{self._session}/"
        raise ValueError(
            f"Unknown tag scope {scope!r}: expected "
            f"{SESSION_SCOPE!r} or {STORE_SCOPE!r}"
        )

    def check_tag(self, name: str, *, scope: str = SESSION_SCOPE) -> None:
        """Apply the name and scope rules without writing anything."""
        self._scoped(name, scope)

    def tag(
        self,
        name: str,
        *,
        at: str | None = None,
        info: dict[str, Any] | None = None,
        scope: str = SESSION_SCOPE,
    ) -> str:
        """Name a commit immutably; returns the commit it names.

        Tags the current commit unless ``at`` names another — staged
        changes are in no commit yet, so they are never what gets named.
        """
        self._refuse_frozen("tag")
        stored = self._scoped(name, scope)
        try:
            return self._staged.tag(stored, at=at, info=info)
        except ValueError as e:
            # kvgit's message carries the stored (prefixed) name, which
            # is not the name the caller used.
            raise WorkspaceError(f"cannot tag {name!r} in scope {scope!r}: {e}") from e

    def tags(self, *, scope: str = SESSION_SCOPE) -> dict[str, str]:
        """Tag name → commit, for one scope, with the prefix stripped."""
        prefix = self._prefix(scope)
        return {
            stored[len(prefix) :]: commit
            for stored, commit in self._staged.tags().items()
            if stored.startswith(prefix)
        }

    def tag_info(self, name: str, *, scope: str = SESSION_SCOPE) -> TagInfo | None:
        info = self._staged.tag_info(self._scoped(name, scope))
        if info is None:
            return None
        return TagInfo(
            name=name,
            scope=scope,
            id=info.commit,
            tree=self._tree(info.commit),
            time=info.time,
            info=info.info,
            dangling=info.dangling,
        )

    def delete_tag(self, name: str, *, scope: str = SESSION_SCOPE) -> None:
        """Drop a tag, then sweep commits nothing else reaches."""
        self._refuse_frozen("delete_tag")
        stored = self._scoped(name, scope)
        try:
            self._staged.delete_tag(stored)
        except ValueError as e:
            raise CommitNotFoundError(
                f"No such tag: {name!r} in scope {scope!r}"
            ) from e

    def at_tag(self, name: str, *, scope: str = SESSION_SCOPE) -> "KvgitProvider":
        """A frozen provider over the tagged commit.

        The kvgit handle underneath is a checkout at that commit on this
        session's branch, so it reads the tagged state; the frozen flag
        is what keeps anything from committing through it.
        """
        stored = self._scoped(name, scope)
        handle = self._staged.checkout(tag=stored)
        if handle is None:
            raise CommitNotFoundError(f"No such tag: {name!r} in scope {scope!r}")
        return KvgitProvider(handle, session=self._session, frozen_at=name)

    def diff(self, a: str, b: str) -> WorkspaceDiff:
        """File-level changes between two commits.

        kvgit diffs keys; this keeps the ones that are files and drops
        the rest, so an embedder sees the workspace's own paths and
        never a framework key (the cache, cwd, the stored conversation,
        the filesystem's metadata).

        ``modified`` answers the content question, not the write
        question. kvgit compares per-commit blob pointers, so a file
        re-saved with the same bytes reads as modified there; here the
        bytes are read at both commits and the path is kept only if they
        differ. That costs one read per candidate key per side — paid
        only for files the pointer diff already flagged — and it is what
        makes "what changed since I published" mean what it says. The
        commit's ``tree`` still moves for such a rewrite, since kvgit
        stamps each entry with when it was written: ``tree`` identifies
        this exact write, ``diff`` identifies the content.
        """
        raw = self._staged.versioned.diff(a, b)
        return WorkspaceDiff(
            added=frozenset(self._file_keys(raw.added).values()),
            removed=frozenset(self._file_keys(raw.removed).values()),
            modified=self._changed_content(self._file_keys(raw.modified), a, b),
            seed=parse_seed(self.key_at(b, _VIEW_KEY)),
        )

    def merge(
        self,
        source: str,
        *,
        at: str | None = None,
        info: dict[str, Any] | None = None,
    ) -> MergeOutcome:
        """Merge another branch into this one.

        Reads the source HEAD commit — or ``at``, a commit on that
        branch, which is how the agent-facing git merges a source's
        last AGENT commit rather than whatever the framework committed
        after it — three-way merges file content with marker merge (see
        ``kvgit.merges``), and commits the result tagged as a merge.
        Overlapping text lands with conflict markers in the working tree
        and the outcome reports it. Framework state merges by rule, not
        by text: the VFS metadata table merges field-aware (timestamps
        are advisory), the cwd keeps ours, and anything else contested
        raises as a hard conflict rather than merging silently. After
        the merge, VFS sizes are corrected from the merged blobs
        (metadata-only commit, only when they differ) and the
        filesystem caches are invalidated; marker conflicts are
        reported by provenance, so pre-existing marker-like bytes in a
        brought file don't count as conflicts.
        """
        from kvgit import MergeChoice, MergeConflict
        from kvgit.merges import text as text_merge
        from monkeyfs import VirtualFS

        self._refuse_frozen("merge")
        if self.dirty:
            raise WorkspaceError(
                "uncommitted changes on this branch; commit or discard them first"
            )
        if source == self._staged.current_branch:
            raise ValueError("cannot merge a branch into itself")
        other = self._open_branch(source)
        source_head = other.current_commit if at is None else at
        if at is not None and at != other.current_commit:
            # Merging an earlier commit on that branch: read its side
            # from THERE, so the key enumeration and the marker
            # provenance describe what is being merged.
            probe = self._staged.checkout(at)
            if probe is None:
                raise CommitNotFoundError(f"No such commit: {at!r}")
            other = probe

        # Text merge applies to file-content keys only: the merge fn
        # receives no key, so a blanket default would marker-merge cache
        # blobs. Enumerate from both heads; the union is harmless
        # (uncontested keys never consult a fn). No try/except:
        # enumeration failing means store trouble, and that must
        # surface, not silently narrow the merge.
        file_keys: set[str] = set()
        for handle in (self._staged, other):
            file_keys.update(self._file_keys(handle.keys()).keys())
        merge_fns = {key: text_merge for key in file_keys}
        merge_fns[VirtualFS.METADATA_KEY] = _merge_vfs_metadata
        merge_fns[_WS_BLOB_KEY] = MergeChoice.OURS
        merge_fns[_VIEW_KEY] = MergeChoice.OURS
        merge_fns[VirtualFS.CWD_KEY] = _keep_ours
        # THE PLANE POLICY, whole: a merge is filesystem-only. The
        # cache and the stored conversation are session-scoped by
        # construction — a delegate's working memory and a chat that
        # never happened here — so this side keeps every key under
        # those prefixes, and that has to include keys the other side
        # merely ADDED. A merge function would not do it: kvgit
        # consults one only where both sides changed a key, so a new
        # ``__agno__/runs/<id>`` would ride in untouched. A
        # ``MergeChoice`` over the prefix is the whole-side policy that
        # also drops their-only adds and ignores their-only removes.
        # Registered for the merge verb ONLY, not on the handle: an
        # ordinary commit that loses its CAS to a second handle on THIS
        # session must still take that handle's conversation, which is
        # this session's own.
        merge_prefixes = {
            CACHE_PREFIX: MergeChoice.OURS,
            CONVERSATION_PREFIX: MergeChoice.OURS,
        }
        # The key nontainer used to keep a cwd of its own under. It is
        # dead: a workspace drops it on open. Branches written before
        # that still carry it, and two of them can carry different
        # values, which is a conflict over state neither side reads.
        # OURS ends it every time — whichever side still has the key,
        # the merge takes this side's answer, including its removal.
        merge_fns[_LEGACY_CWD_KEY] = MergeChoice.OURS

        # Markers commit WITH the merge (flagged in the outcome), they
        # don't block it: the agent resolves with ordinary edit tools
        # and commits, so no merge-state machine is needed. Only
        # non-file hard conflicts abort untouched (no fn can resolve
        # them). Hence no post_check here — kvgit keeps the hook for
        # callers that genuinely want blocking; we want markers.
        try:
            self._staged.merge(
                source_head,
                merge_fns=merge_fns,
                merge_prefixes=merge_prefixes,
                info={"tool": "ws-git.merge", "source": source, **(info or {})},
            )
        except MergeConflict as e:
            return MergeOutcome(
                merged=False,
                commit=None,
                conflicts=tuple(sorted(self._display_conflicts(e))),
                auto_merged=(),
            )
        # What the merge brought in: first parent is our pre-merge head
        # (git convention), so this diff lists exactly the merge's work.
        # Conflict markers are reported by provenance, not by scan: a
        # brought file may legitimately contain marker-like bytes, so
        # only markers absent from both parents' versions count.
        parents = self._staged.versioned.parents()
        base = parents[0] if parents else self._staged.current_commit
        brought = self.diff(base, self._staged.current_commit)
        at_base = self._staged.checkout(base)
        rev = {
            display: key
            for key, display in self._file_keys(self._staged.keys()).items()
        }
        candidates = brought.added | brought.removed | brought.modified
        conflicts = tuple(
            sorted(
                path
                for path in candidates
                if self._markers_introduced(rev.get(path), at_base, other)
            )
        )
        # Merged bytes match neither branch, but the metadata merge
        # copied one branch's size: correct sizes from the blobs before
        # reporting. Metadata-only, so ``brought`` still describes this.
        # The follow-up carries the source tag: it is part of the merge,
        # and status derives merge context from history.
        self._fix_merged_sizes(brought.added | brought.modified, rev, source, info)
        # HEAD moved underneath the VirtualFS object: drop its caches
        # like checkout()/discard() do, or listings and stat go stale.
        self._invalidate_fs()
        return MergeOutcome(
            merged=True,
            commit=self._staged.current_commit,
            conflicts=conflicts,
            auto_merged=tuple(sorted(set(candidates) - set(conflicts))),
        )

    def _open_branch(self, source: str):
        """A throwaway read handle on another branch's HEAD.

        kvgit has no "head of branch X" lookup; a fresh checkout switched
        in place lands exactly there without touching our own handle.
        Unknown branches raise ValueError naming them.
        """
        probe = self._staged.checkout(self._staged.current_commit)
        try:
            probe.switch_branch(source)
        except ValueError:
            raise ValueError(f"unknown branch {source!r}") from None
        return probe

    def _display_conflicts(self, exc: Exception) -> list[str]:
        """Conflicting keys as display paths (raw keys for non-files)."""
        keys: set[str] = set(getattr(exc, "conflicting_keys", ()))
        paths = self._file_keys(keys)
        return [paths.get(key, key) for key in keys]

    def _markers_introduced(self, key: str | None, at_base: Any, other: Any) -> bool:
        """Whether this merge introduced conflict markers into a key.

        Marker-like bytes can predate the merge (docs, fixtures), so
        current bytes carrying them are not enough: the markers must
        be absent from both parents' versions. Reads stay scoped to
        scan-positive candidates, so the common case costs one blob.
        """
        if key is None:
            return False
        value = self._staged.get(key)
        if not isinstance(value, bytes) or b"<<<<<<< " not in value:
            return False
        for handle in (at_base, other):
            if handle is None:
                continue
            parent = handle.get(key)
            if isinstance(parent, bytes) and b"<<<<<<< " in parent:
                return False
        return True

    def _fix_merged_sizes(
        self,
        paths: set[str],
        rev: dict[str, str],
        source: str,
        info: dict[str, Any] | None = None,
    ) -> None:
        """Correct VFS metadata sizes from the merged blobs.

        The metadata merge copies ``size`` from one branch, but merged
        file bytes (clean unions, marker content) match neither side.
        Sizes are advisory next to content truth, so rewrite them here
        and commit metadata-only. Commits only when a size actually
        differs, so clean merges stay a single commit. Tagged with the
        merge source: the follow-up is part of the merge.
        """
        from monkeyfs import VirtualFS

        raw = self._staged.get(VirtualFS.METADATA_KEY)
        if raw is None:
            return
        table = json.loads(raw)
        fixed = False
        for path in paths:
            key = rev.get(path)
            if key is None:
                continue
            blob = self._staged.get(key)
            if not isinstance(blob, bytes):
                continue
            entry = table.get(self.fs._decode_path(key))
            if not isinstance(entry, dict):
                continue
            if entry.get("size") != len(blob):
                entry["size"] = len(blob)
                fixed = True
        if not fixed:
            return
        self._staged[VirtualFS.METADATA_KEY] = json.dumps(
            table, sort_keys=True
        ).encode()
        self.commit(
            {
                "tool": "ws-git.merge",
                "source": source,
                "sizes": "recomputed",
                **(info or {}),
            }
        )

    def _changed_content(self, keys: dict[str, str], a: str, b: str) -> frozenset[str]:
        """Of these file keys, the paths whose bytes actually differ."""
        if not keys:
            return frozenset()
        at_a = self._staged.checkout(a)
        at_b = self._staged.checkout(b)
        if at_a is None or at_b is None:
            # A commit that cannot be opened cannot be compared; the
            # pointer diff is then the most this can honestly say.
            return frozenset(keys.values())
        return frozenset(
            path for key, path in keys.items() if at_a.get(key) != at_b.get(key)
        )

    def _file_keys(self, keys: Iterable[str]) -> dict[str, str]:
        """Store key → workspace file path, for the keys that are files."""
        from monkeyfs import VirtualFS

        vfs = self.fs
        out: dict[str, str] = {}
        for key in keys:
            if not isinstance(key, str) or not key.startswith(VirtualFS.PREFIX):
                continue
            if key in (VirtualFS.METADATA_KEY, VirtualFS.CWD_KEY):
                continue
            try:
                # The VFS owns the path encoding; asking it back is the
                # only way to stay right if that encoding ever changes.
                # It stores paths root-relative, so the leading slash
                # goes back on: these are the absolute paths agent code
                # and ``ws.files.fs`` use (``/workspace/data/in.csv``).
                out[key] = "/" + vfs._decode_path(key).lstrip("/")
            except Exception:  # noqa: BLE001 - an undecodable key is not a file
                continue
        return out

    # -- power modes / lifecycle ---------------------------------------

    def mount(self) -> Any:
        from ..errors import NotSupportedError

        raise NotSupportedError(
            "KvgitProvider has no FUSE mount; use the agentfs backend (or a "
            "dir workspace) when real processes must see the files."
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        store = getattr(self._staged.versioned, "store", None)
        close = getattr(store, "close", None)
        if callable(close):
            close()
