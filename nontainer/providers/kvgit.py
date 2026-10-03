"""KvgitProvider: the versioned substrate (default backend).

One shared kvgit store; each session is a branch. Files (via monkeyfs
``VirtualFS``), the agent cache, and framework keys (the working
directory, the ws-git blob) all live in one flat kvgit ``Worktree`` —
so one ``commit()`` commits the whole world atomically, and
``checkout()`` restores all of it (including where the agent's cwd
was) as a new commit.

Key coexistence in the flat mapping: ``VirtualFS`` encodes file paths
under its own key prefix — one key per file, plus a metadata row beside
it under the same encoding — while cache keys live under ``__cache__/``:
no collisions by construction. A row is written whenever, and only when,
its blob is written or its metadata changes, so a row travels with the
bytes it describes through every commit and merge in this file.

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
from collections.abc import Callable, Iterable, Iterator, Mapping, MutableMapping
from pathlib import Path
from typing import Any

from ..agentgit import BLOB_KEY as _WS_BLOB_KEY
from ..errors import CommitNotFoundError, NotSupportedError, WorkspaceError
from ..planes import CACHE_PREFIX, CONVERSATION_PREFIX
from ..protocol import (
    SHORT_ID_RE,
    Capabilities,
    CommitInfo,
    MergeOutcome,
    TagInfo,
    WorkspaceDiff,
    expand_commit,
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


def _keep_ours(old: Any, ours: Any, theirs: Any) -> Any:
    """Positional session state (cwd): the merger's context wins."""
    return ours if ours is not None else theirs


def _same(a: Any, b: Any) -> bool:
    """Whether two stored values are the same value.

    Values from the file planes are bytes and compare by content; a key
    from somewhere else can hold anything at all, including an object
    whose ``==`` raises or answers with something that is not a bool.
    Such a pair is reported as different, which files it as contested
    rather than assuming an agreement nothing established.
    """
    try:
        return bool(a == b)
    except Exception:  # noqa: BLE001 - an answer that raises is not equality
        return False


def _row(value: Any) -> dict | None:
    """One metadata row as a dict, or ``None`` where there is no row.

    Raises ``CantMark`` on a body that is not a JSON object: the merge
    machinery files that as a conflict rather than inventing metadata
    for a file it cannot describe.
    """
    from kvgit.merges import CantMark

    if value is None:
        return None
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError) as e:
        raise CantMark(f"metadata row not JSON: {e}") from e
    if not isinstance(parsed, dict):
        raise CantMark("metadata row not an object")
    return parsed


def _merge_metadata_row(
    old: Any, ours: Any, theirs: Any, size: int | None = None
) -> bytes:
    """Field-aware merge for one file's metadata row.

    Rows contest on timestamp noise alone, so line-merge is the wrong
    tool: merge the fields instead. A row present on one side only is
    that side's (a file deleted here and kept there keeps the surviving
    record — the content merge is what decides the bytes). Both present
    takes the later ``modified_at`` with the earliest ``created_at``,
    since timestamps are advisory next to the blob they describe.
    ``is_dir`` disagreement — a file on one side, a directory on the
    other — raises ``CantMark``: no field merge can resolve that, so it
    is filed as a hard conflict and the merge aborts untouched.

    ``size`` describes the merged bytes when the caller knows them, and
    it is applied whether the row came from one side or from both: a
    size copied from a side would be wrong wherever the content merge
    produced bytes that match neither, which is exactly what a file
    deleted on one side and edited on the other lands.
    """
    from kvgit.merges import CantMark

    o, u, t = _row(old), _row(ours), _row(theirs)
    if u is None and t is None:
        raise CantMark("no metadata row on either side")
    if u is None or t is None:
        survivor = dict(u if t is None else t)
        if size is not None:
            survivor["size"] = size
        return json.dumps(survivor, sort_keys=True).encode()
    if u.get("is_dir", False) != t.get("is_dir", False):
        raise CantMark("a file on one side and a directory on the other")
    winner = u if u.get("modified_at", "") >= t.get("modified_at", "") else t
    created = [
        r.get("created_at", "")
        for r in (o, u, t)
        if isinstance(r, dict) and r.get("created_at")
    ]
    return json.dumps(
        {
            "size": winner.get("size", 0) if size is None else size,
            "created_at": min(created) if created else "",
            "modified_at": winner.get("modified_at", ""),
            "is_dir": winner.get("is_dir", False),
        },
        sort_keys=True,
    ).encode()


def _commit_info(commit: Any) -> CommitInfo:
    """A kvgit ``Commit`` record as this layer's :class:`CommitInfo`."""
    return CommitInfo(
        id=commit.hash,
        time=float(commit.time) if commit.time is not None else 0.0,
        info=commit.info if isinstance(commit.info, dict) else {},
        tree=commit.root,
        parents=tuple(commit.parents),
    )


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

    def get_many(self, paths: Iterable[str]) -> dict[str, Any]:
        """The values of those ``paths`` this tree holds, in one read.

        What comparing or rendering many files at once should use: one
        batched fetch from the store rather than one per path. Each
        path's key is ENCODED rather than looked up, so this never lists
        the tree: a few paths out of thousands cost those few reads.
        """
        vfs = self._provider.fs
        keys: dict[str, list[str]] = {}
        for path in paths:
            # The VFS owns the path encoding, as it owns the decoding
            # the path index uses. A content key cannot collide with a
            # framework key under the same prefix (the metadata rows,
            # the cwd): those are spelled in lowercase, which base32
            # never produces.
            keys.setdefault(vfs._encode_path(path), []).append(path)
        if not keys:
            return {}
        found = self._handle.get_many(*keys)
        return {path: value for key, value in found.items() for path in keys[key]}

    def __iter__(self) -> Iterator[str]:
        return iter(self._index)

    def __len__(self) -> int:
        return len(self._index)

    def __repr__(self) -> str:
        return f"<files: {len(self)} paths>"


class _Overlay(MutableMapping):
    """Writes held in memory over a read-only snapshot.

    A frozen provider reads one commit and commits nothing, but its
    filesystem can still write: ``chdir`` records the cwd in the state,
    and code run against a snapshot may write files it never keeps.
    Those writes land here and read back, as they would in a worktree,
    and go nowhere — there is no commit to take them.
    """

    def __init__(self, snapshot: Any) -> None:
        self.snapshot = snapshot
        self._updates: dict[str, Any] = {}
        self._removed: set[str] = set()

    @property
    def dirty(self) -> bool:
        return bool(self._updates or self._removed)

    @property
    def pending(self) -> set[str]:
        """The keys written or deleted over the snapshot."""
        return set(self._updates) | self._removed

    def discard(self) -> None:
        self._updates.clear()
        self._removed.clear()

    def __getitem__(self, key: str) -> Any:
        if key in self._removed:
            raise KeyError(key)
        if key in self._updates:
            return self._updates[key]
        return self.snapshot[key]

    def get(self, key: str, default: Any = None) -> Any:
        if key in self._removed:
            return default
        if key in self._updates:
            return self._updates[key]
        return self.snapshot.get(key, default)

    def get_many(self, *keys: str) -> dict[str, Any]:
        found = {k: self._updates[k] for k in keys if k in self._updates}
        rest = [k for k in keys if k not in self._updates and k not in self._removed]
        if rest:
            found.update(self.snapshot.get_many(*rest))
        return found

    def __contains__(self, key: object) -> bool:
        if key in self._removed:
            return False
        return key in self._updates or key in self.snapshot

    def __setitem__(self, key: str, value: Any) -> None:
        self._removed.discard(key)
        self._updates[key] = value

    def __delitem__(self, key: str) -> None:
        if key not in self:
            raise KeyError(key)
        self._updates.pop(key, None)
        if key in self.snapshot:
            self._removed.add(key)

    def _keys(self) -> set[str]:
        held = {key for key in self.snapshot if key not in self._removed}
        return held | set(self._updates)

    def __iter__(self) -> Iterator[str]:
        return iter(self._keys())

    def __len__(self) -> int:
        return len(self._keys())


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
    """``WorkspaceProvider`` over a kvgit branch.

    Construct via :meth:`open` (path-based, branch-per-session) or
    directly from a kvgit ``Repo`` and a ``Worktree`` on it that you
    built yourself (another backend, a codec, memory stores for tests).
    """

    def __init__(
        self,
        repo: Any,
        handle: Any,
        *,
        session: str,
        frozen_at: str | None = None,
        owns_repo: bool = True,
    ) -> None:
        """``handle`` is a kvgit ``Worktree`` on the session's branch or,
        for a frozen provider, a ``Snapshot`` of the commit it reads.
        ``frozen_at`` names what that snapshot was opened at and makes
        the provider a snapshot: see :meth:`at_tag`. Only a frozen
        provider takes one — a provider on a branch head never is.

        ``owns_repo`` says whether :meth:`close` closes ``repo``. A
        provider opened on its own owns the repository it opened; one a
        ``Store`` opens borrows the store's, which the store closes.
        """
        _validate_branch(session)
        self._session = session
        self._repo = repo
        self._owns_repo = owns_repo
        self._frozen_at = frozen_at
        self._wt: Any | None = None
        self._state: Any
        if frozen_at is None:
            self._wt = handle
            self._state = handle
        else:
            self._state = handle if isinstance(handle, _Overlay) else _Overlay(handle)
        self._fs: Any | None = None
        self._closed = False
        if self._wt is not None:
            from kvgit import MergeChoice
            from monkeyfs import VirtualFS

            # The framework's own keys, with the policy the merge verb
            # already uses for them, registered for ORDINARY commits too.
            # A commit whose CAS is lost to another handle on this session
            # three-way merges by key, and these are the keys two writers
            # are bound to contend on: the cwd and the ws-git blob, and the
            # metadata row of any file both of them wrote. Without a policy
            # each of those turns a routine race into a raised conflict.
            # Rows are registered by prefix because their names are the
            # encoded paths, unknown until a file is written; a merge
            # function under a prefix is consulted only where both sides
            # changed a key, so registering it disturbs nothing else.
            self._wt.set_merge_fn(VirtualFS.CWD_KEY, _keep_ours)
            self._wt.set_merge_fn(_WS_BLOB_KEY, MergeChoice.OURS)
            self._wt.set_merge_fn(_VIEW_KEY, MergeChoice.OURS)
            self._wt.set_merge_prefix(VirtualFS.META_PREFIX, _merge_metadata_row)

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
            codecs: Optional kvgit codec (e.g. ``"scientific"`` for
                numpy/pandas chunk dedup); pickle when omitted.
        """
        from kvgit import Repo
        from kvgit.kv.memory import Memory

        _validate_branch(session)
        if path is None:
            backend: Any = Memory()
        else:
            from kvgit.kv.disk import Disk

            p = Path(path).expanduser()
            p.mkdir(parents=True, exist_ok=True)
            backend = Disk(str(p))
        repo = Repo(backend, codec=codecs or "pickle")
        try:
            worktree = repo.worktree(session, create=True)
        except BaseException:
            repo.close()
            raise
        return cls(repo, worktree, session=session)

    @classmethod
    def on(cls, repo: Any, session: str) -> "KvgitProvider":
        """A provider on ``session``'s branch of a repository someone
        else owns — a ``Store``'s — created empty if it is new. Closing
        the provider leaves the repository open."""
        _validate_branch(session)
        worktree = repo.worktree(session, create=True)
        return cls(repo, worktree, session=session, owns_repo=False)

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
        passes through, and ``Store.unpublish`` drops a publication's
        reserved branch through here. The session rule is the store
        verb's: ``Store.delete`` refuses anything that is not a session
        id, which is what keeps the ``@store/`` branches out of reach
        of a teardown.

        A session's own tags go with it: every tag stored under
        ``<name>/`` is deleted alongside the branch, because a
        session-scoped tag belongs to that session. Store-scoped tags
        (``@store/``) are left exactly where they are — that scope exists
        so a publication can outlive the session that made it, and
        teardown is where that promise is kept; no session id can spell
        that prefix, so no name passed here can reach them.

        The tags and branches go first and one garbage-collection sweep
        follows, taking the commits that only they reached.
        ``min_age`` is that sweep's grace period in seconds: commits
        younger than it are left for a later sweep. Any value is safe
        beside concurrent writers; it only decides how long abandoned
        work lingers.
        """
        from kvgit import Repo
        from kvgit.kv.disk import Disk

        requested = set(sessions)  # once: ``sessions`` may be one-shot
        if not requested:
            return  # nothing asked: don't touch the store
        p = Path(path).expanduser()
        if not p.is_dir():
            return  # never-materialized (or non-kvgit) store: nothing here
        repo = Repo(Disk(str(p)))
        try:
            cls.delete_in(repo, requested, min_age=min_age)
        finally:
            repo.close()

    @staticmethod
    def delete_in(repo: Any, sessions: Iterable[str], *, min_age: float = 3600) -> None:
        """:meth:`delete`, on a repository already open: the named
        branches, their session tags, then one sweep."""
        requested = set(sessions)
        if not requested:
            return
        # Legacy cleanup: stores that ran the old code minted a hidden
        # ``__void__`` anchor branch that pinned a dead session's entire
        # history (create_branch forks from the current commit), silently
        # defeating orphan GC. It carries no wanted state, so it always
        # joins the doomed set, erasing that retention bug on the next
        # delete.
        names = requested | {"__void__"}
        prefixes = tuple(f"{s}/" for s in requested)
        for tag in [t for t in repo.tags if t.startswith(prefixes)]:
            repo.tags.delete(tag)
        # Missing names (including __void__ on stores that never had one)
        # are skipped, and a store with no kvgit data has no branches to
        # match, so the old tolerance is preserved.
        for name in names:
            if name in repo.branches:
                repo.branches.delete(name)
        repo.gc(min_age=min_age)

    # -- identity ------------------------------------------------------

    @property
    def session(self) -> str:
        return self._session

    @property
    def caps(self) -> Capabilities:
        return _KVGIT_CAPS

    @property
    def worktree(self) -> Any:
        """The underlying kvgit ``Worktree`` (host-side power tool), or
        ``None`` on a frozen provider, which reads a ``Snapshot``."""
        return self._wt

    @property
    def repo(self) -> Any:
        """The kvgit ``Repo`` this provider's branch lives in."""
        return self._repo

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

            self._fs = VirtualFS(self._state)
        return self._fs

    @property
    def kv(self) -> MutableMapping[str, Any]:
        return self._state

    @property
    def dirty(self) -> bool:
        if self._wt is None:
            return self._state.dirty
        return bool(self._wt.status())

    # -- versioning ----------------------------------------------------

    @property
    def head(self) -> str:
        if self._wt is None:
            return self._state.snapshot.commit
        return self._wt.head

    def commit(self, info: dict[str, Any] | None = None) -> str:
        """Commit staged fs + kv writes atomically; returns the commit
        hash. No staged changes → no new commit (returns current)."""
        self._refuse_frozen("commit")
        if not self._wt.status():
            return self._wt.head
        result = self._wt.commit(info=info)
        if not result.merged:
            raise WorkspaceError(
                f"commit failed: conflicting concurrent commit on branch "
                f"{self._session!r} (CAS): {result}"
            )
        return self._wt.head

    def checkout(self, commit_id: str, *, info: dict[str, Any] | None = None) -> str:
        """Append a commit whose keyset equals that commit's; return it.

        A restore, not a rewind: the target's state is WRITTEN into the
        working buffer and committed, so the branch head only ever
        moves forward and every commit made since the target stays in
        ``history()`` — which is what makes an undo redo-able. kvgit's
        ``reset_to`` is deliberately not used.

        The whole keyset moves, not the files alone: the cache, the
        cwd, the metadata rows, the ws-git blob and the stored
        conversation are keys like any other, and the host restored the
        whole world. The keys to touch come from kvgit's keyset diff
        between the head and the target, so the cost is the size of the
        change and not the size of the tree.

        A target written before per-file metadata rows lands in the
        current layout (see :mod:`nontainer.migrate`): its table
        entries arrive as rows and its old cwd key in the cwd slot, and
        neither legacy key is written to the head. The old commit
        itself is not touched.

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
        target = self._snapshot(commit_id)
        # The diff below is between two COMMITS: a buffer left in place
        # would ride into the restore commit as though it were part of
        # the target's state.
        if self._wt.status():
            self._wt.discard()
        landed = 0
        while True:
            head = self._wt.head
            self._stage_restore(target, head, commit_id)
            # HEAD moved (or the buffer did) under the VirtualFS
            # object: drop its caches the way discard() does.
            self._invalidate_fs()
            if not self._wt.status():
                # Nothing left to write: this head IS the target's
                # state, whether it took one commit or several.
                return head
            if landed >= _CHECKOUT_ATTEMPTS:
                self._wt.discard()
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

        The target is read in the current layout. Its converted keys are
        not in kvgit's diff (the target commit does not hold them), so
        they are compared as well; a legacy key the diff names is absent
        from the converted target and is therefore never written.
        """
        from ..migrate import converted_keys, current_layout

        target = current_layout(target)
        changed = self._repo.diff(head, commit_id)
        keys = (
            set(changed.added)
            | set(changed.modified)
            | set(changed.removed)
            | converted_keys(target)
        )
        for key in keys:
            if key in target:
                value = target.get(key)
                if key not in self._wt or self._wt.get(key) != value:
                    self._wt[key] = value
            elif key in self._wt:
                del self._wt[key]

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
        """This branch's own line, newest first: the first parent of
        each commit, so a merge's other side is not walked."""
        from kvgit import UnknownCommitError

        if limit is not None and limit <= 0:
            return
        count = 0
        try:
            for commit in self._repo.log(commit=self.head, first_parent=True):
                yield _commit_info(commit)
                count += 1
                if limit is not None and count >= limit:
                    return
        except UnknownCommitError:
            return  # history ends where the store stops holding it

    def _tree(self, commit_hash: str) -> str | None:
        """The commit's keyset root hash — the identity of its content."""
        record = self._commit_record(commit_hash)
        return record.tree if record is not None else None

    def _snapshot(self, commit: str) -> Any:
        """A read-only view of one commit, or ``CommitNotFoundError``."""
        from kvgit import UnknownCommitError

        try:
            return self._repo.snapshot(commit=commit)
        except UnknownCommitError:
            raise CommitNotFoundError(f"No such commit: {commit!r}") from None

    def fork(self, name: str, *, at: str | None = None) -> "KvgitProvider":
        """O(1) branch sharing storage.

        Without ``at``, pending staged changes are committed first so
        the fork sees current state. With ``at`` the fork starts from
        that earlier commit instead, and the staged changes are left
        alone: they belong to this session's present, not to the past
        the fork is branching from.

        The fork's head is always in the current layout: a fork point
        written before per-file metadata rows is migrated on the new
        branch, one commit after the fork point (see
        :mod:`nontainer.migrate`). The fork point itself is untouched.
        """
        from kvgit import UnknownCommitError

        from ..migrate import legacy_keys, migrate_provider

        self._refuse_frozen("fork")
        validate_session_id(name)
        if name in self._repo.branches:
            raise WorkspaceError(f"Branch already exists: {name!r}")
        if at is None:
            if self._wt.status():
                self.commit(info={"tool": "fork", "target": name})
            self._repo.branches.create(name, at=self._wt.head)
        else:
            try:
                self._repo.branches.create(name, at=at)
            except UnknownCommitError as e:
                raise CommitNotFoundError(at) from e
        child = KvgitProvider(
            self._repo, self._repo.worktree(name), session=name, owns_repo=False
        )
        if legacy_keys(child.kv):
            migrate_provider(child)
        return child

    def discard(self) -> None:
        if self._wt is None:
            self._state.discard()
        else:
            self._wt.discard()
        self._invalidate_fs()

    # -- read views ------------------------------------------------------

    def files_at(self, commit: str) -> Mapping[str, Any]:
        """This session's files at one commit, by workspace path.

        A frozen read view: a mapping whose keys are the paths and
        whose values are the stored file values, read on demand. What
        the agent-facing git needs to compare a tree against another
        one without materializing either.
        """
        return _FileView(self, self._snapshot(commit))

    def working_files(self) -> Mapping[str, Any]:
        """The live working tree's files, by workspace path — including
        writes not committed yet, and excluding deletions not committed
        yet."""
        return _FileView(self, self._state)

    def working_diff(self, commit: str) -> WorkspaceDiff:
        """File-level changes from one commit to the live working tree.

        What :meth:`diff` answers between two commits, with the working
        tree — uncommitted writes and deletions included — as the second
        side. The candidates are the keys the store's own diff names
        between ``commit`` and this branch's head, plus the keys written
        or deleted since that head; only those are read, in one batched
        fetch per side, and a candidate is kept only if its bytes
        differ. So asking what changed costs the change, not the tree.
        """
        from kvgit import UnknownCommitError

        if self._wt is None:
            head = self._state.snapshot.commit
            pending = self._state.pending
        else:
            head = self._wt.head
            status = self._wt.status()
            pending = set(status.updated) | set(status.removed)
        candidates = pending
        if commit != head:
            try:
                raw = self._repo.diff(commit, head)
            except UnknownCommitError as e:
                raise CommitNotFoundError(str(e)) from None
            candidates |= set(raw.added) | set(raw.removed) | set(raw.modified)
        keys = self._file_keys(candidates)
        if not keys:
            return WorkspaceDiff(frozenset(), frozenset(), frozenset())
        before = self._snapshot(commit).get_many(*keys)
        after = self._state.get_many(*keys)
        added, removed, modified = set(), set(), set()
        for key, path in keys.items():
            if key not in before:
                if key in after:
                    added.add(path)
            elif key not in after:
                removed.add(path)
            elif before[key] != after[key]:
                modified.add(path)
        return WorkspaceDiff(frozenset(added), frozenset(removed), frozenset(modified))

    def key_at(self, commit: str, key: str) -> Any:
        """One store key's value at a commit, or ``None`` if absent.

        The non-file half of :meth:`files_at`: what reads bookkeeping
        (the ws-git blob) out of a commit without materializing a tree.
        """
        return self._snapshot(commit).get(key)

    def branch_head(self, session: str) -> str:
        """Another session's current commit on this store. Unknown
        names raise ``ValueError``."""
        from kvgit import UnknownBranchError

        try:
            return self._repo.branches[session]
        except UnknownBranchError:
            raise ValueError(f"unknown branch {session!r}") from None

    def expand_commit(self, commit: str, *, session: str | None = None) -> str:
        """A commit id typed short → the whole one it names.

        Every ref spelling nontainer prints is one it accepts back, and
        what it prints is seven characters of a commit id. A unique
        prefix of seven hex characters or more expands here, against
        the full history of the session that holds it — the framework's
        commits included, since a ref names a state and takes no place
        in anyone's graph.

        An ambiguous prefix raises ``ValueError`` naming the commits it
        could mean. A prefix nothing matches is returned unchanged, so
        the read that follows refuses it with the same not-found error
        a whole id nothing matches earns. Anything that is not a short
        id — a whole one, a name — is returned unchanged too, and costs
        no walk.

        ``session`` names whose history to search; the default is this
        provider's own.
        """
        if not isinstance(commit, str) or not SHORT_ID_RE.fullmatch(commit):
            return commit
        return expand_commit(
            commit,
            self._commit_ids(session),
            where=f"on session {session or self.session!r}",
        )

    def _commit_ids(self, session: str | None) -> Iterable[str]:
        """Every commit id on a session's branch, newest first: its own
        line, the first parent of each commit."""
        if session is None or session == self.session:
            start = self.head
        else:
            start = self.branch_head(session)
        return (c.hash for c in self._repo.log(commit=start, first_parent=True))

    def refresh(self) -> None:
        """Re-read the branch head, DISCARDING uncommitted writes.

        Recovery, not routine: what a caller reaches for when a commit
        lost its CAS to another handle on the same session and the
        work has to be re-applied against the head that won.
        """
        self._refuse_frozen("refresh")
        self._wt.refresh()
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

        A file's metadata row rides along with its blob, and only with
        its blob: a committed file whose row stayed behind would have no
        size and no timestamps, and a reader of that commit would not
        see the file at all. A staged deletion carries its row's
        deletion the same way.
        """
        self._refuse_frozen("commit_keys")
        status = self._wt.status()
        staged = status.updated | status.removed
        pending = {key for key in self._resolve_keys(keys) if key in staged}
        if not pending:
            return None
        vfs = self.fs
        for path in list(self._file_keys(pending).values()):
            row = vfs.metadata_key(path)
            if row in staged:
                pending.add(row)
        from kvgit import MergeConflict

        try:
            try:
                result = self._wt.commit(keys=pending, info=info)
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
            self._invalidate_fs()
        return self._wt.head

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

    # -- tags ------------------------------------------------------------

    def _scoped(self, name: str, scope: str) -> str:
        """The stored tag name for a caller's name in a scope.

        Rejects a name that already carries a scope prefix, so a caller
        cannot reach the store scope by spelling ``@store/x`` as a
        session tag, or its own session's namespace from the store
        scope. Beyond that the rule is kvgit's: any non-empty name
        without ``%``.

        Reading a tag applies the same rules as writing one, minus what
        only a new name has to answer for (:meth:`_refuse_unaddressable`).
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

    @staticmethod
    def _refuse_unaddressable(name: str, scope: str) -> None:
        """Refuse a STORE tag name no ref could spell.

        A store tag is a ref: it is written where
        ``session@commit[:/path]`` is written, and those two delimiters
        belong to the grammar, so a name holding either could be stored
        and never addressed. A session tag is reached by name alone
        (``ws.tags.at``) and takes no such rule.

        Asked of a name being created, not of one being read: a tag
        already on a store is a fact, and it stays listable and
        openable by name whatever it is called.
        """
        if scope == STORE_SCOPE and ("@" in name or ":" in name):
            raise ValueError(
                f"Store tag name must not contain '@' or ':': {name!r} — a "
                "ref spells session@commit[:/path], so a store tag holding "
                "either delimiter could be stored and never addressed as "
                "one."
            )

    def check_tag(self, name: str, *, scope: str = SESSION_SCOPE) -> None:
        """Apply the name and scope rules without writing anything."""
        self._scoped(name, scope)
        self._refuse_unaddressable(name, scope)

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
        from kvgit import KvgitError

        self._refuse_frozen("tag")
        stored = self._scoped(name, scope)
        self._refuse_unaddressable(name, scope)
        target = at or self.head
        try:
            self._repo.tags.create(stored, target, info=info)
            return target
        except (KvgitError, ValueError) as e:
            # kvgit's message carries the stored (prefixed) name, which
            # is not the name the caller used.
            raise WorkspaceError(f"cannot tag {name!r} in scope {scope!r}: {e}") from e

    def tags(self, *, scope: str = SESSION_SCOPE) -> dict[str, str]:
        """Tag name → commit, for one scope, with the prefix stripped."""
        prefix = self._prefix(scope)
        return {
            stored[len(prefix) :]: commit
            for stored, commit in self._repo.tags.items()
            if stored.startswith(prefix)
        }

    def tag_info(self, name: str, *, scope: str = SESSION_SCOPE) -> TagInfo | None:
        from kvgit import UnknownTagError

        try:
            info = self._repo.tags.info(self._scoped(name, scope))
        except UnknownTagError:
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
        """Drop a tag, then sweep commits nothing else reaches (past the
        sweep's default grace period)."""
        from kvgit import UnknownTagError

        self._refuse_frozen("delete_tag")
        stored = self._scoped(name, scope)
        try:
            self._repo.tags.delete(stored)
        except UnknownTagError as e:
            raise CommitNotFoundError(
                f"No such tag: {name!r} in scope {scope!r}"
            ) from e
        self._repo.gc()

    def at_tag(self, name: str, *, scope: str = SESSION_SCOPE) -> "KvgitProvider":
        """A frozen provider over the tagged commit.

        It reads a snapshot of that commit, which takes no commits: the
        frozen provider has no worktree to write through.
        """
        from kvgit import UnknownCommitError, UnknownTagError

        stored = self._scoped(name, scope)
        try:
            snapshot = self._repo.snapshot(tag=stored)
        except (UnknownTagError, UnknownCommitError):
            raise CommitNotFoundError(
                f"No such tag: {name!r} in scope {scope!r}"
            ) from None
        return KvgitProvider(
            self._repo, snapshot, session=self._session, frozen_at=name, owns_repo=False
        )

    def diff(self, a: str, b: str) -> WorkspaceDiff:
        """File-level changes between two commits.

        kvgit diffs keys; this keeps the ones that are files and drops
        the rest, so an embedder sees the workspace's own paths and
        never a framework key (the cache, cwd, the stored conversation,
        the filesystem's metadata).

        ``modified`` answers the content question, not the write
        question. kvgit compares blob pointers, and a pointer written
        before kvgit's content-addressed layout names the commit that
        wrote it, so equal bytes can sit under two pointers; here the
        bytes are read at both commits and the path is kept only if they
        differ. That costs one read per candidate key per side — paid
        only for files the pointer diff already flagged — and it is what
        makes "what changed since I published" mean what it says.
        """
        from kvgit import UnknownCommitError

        try:
            raw = self._repo.diff(a, b)
        except UnknownCommitError as e:
            raise CommitNotFoundError(str(e)) from None
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
        ignore: Callable[[str], bool] | None = None,
    ) -> MergeOutcome:
        """Merge another branch into this one.

        Reads the source HEAD commit — or ``at``, a commit on that
        branch, which is how the agent-facing git merges a source's
        last AGENT commit rather than whatever the framework committed
        after it — three-way merges file content with marker merge (see
        ``kvgit.merges``), and commits the result tagged as a merge.
        Overlapping text lands with conflict markers in the working tree
        and the outcome reports it. Framework state merges by rule, not
        by text: a file's metadata row merges field-aware beside its
        blob (timestamps are advisory) and carries the size of the
        merged bytes, the cwd keeps ours, and anything else contested
        raises as a hard conflict rather than merging silently. A file
        is reported once, by path, however many of its keys were
        contested. The filesystem caches are invalidated afterwards;
        marker conflicts are reported by provenance, so pre-existing
        marker-like bytes in a brought file don't count as conflicts.

        Both sides must be in the current layout, and a side that is not
        raises :class:`~nontainer.errors.LegacyLayoutError` naming its
        branch. One case is not refused: ``at`` in the old layout, on a
        source whose head was migrated since and holds the same files.
        That is a migrated session whose agent has not committed since,
        and its head is merged instead — the same files, with rows.

        ``ignore`` names paths that are never work (see
        :mod:`nontainer.ignore`): this side keeps its own copy of each,
        whatever the source did to it.
        """
        from kvgit import MergeConflict

        self._refuse_frozen("merge")
        if self.dirty:
            raise WorkspaceError(
                "uncommitted changes on this branch; commit or discard them first"
            )
        if source == self._wt.branch:
            raise ValueError("cannot merge a branch into itself")
        other = self._open_branch(source)
        source_head = other.commit if at is None else at
        if at is not None and at != other.commit:
            # Merging an earlier commit on that branch: read its side
            # from THERE, so the key enumeration and the marker
            # provenance describe what is being merged.
            other = self._snapshot(at)
        other, source_head = self._current_layout_side(source, other, source_head)

        # The rules are the same three-way rules an apply resolves by,
        # read against the base kvgit's own merge will use: the merged
        # bytes of a contested file are a function of that base, and a
        # row whose size was computed against a different one would
        # describe bytes nothing holds.
        base = self._merge_base(self._wt.head, source_head)
        at_base = self._snapshot(base) if base is not None else None
        merge_fns, merge_prefixes = self._three_way_rules(other, at_base, ignore)

        # Markers commit WITH the merge (flagged in the outcome), they
        # don't block it: the agent resolves with ordinary edit tools
        # and commits, so no merge-state machine is needed. Only
        # non-file hard conflicts abort untouched (no fn can resolve
        # them). Hence no post_check here — kvgit keeps the hook for
        # callers that genuinely want blocking; we want markers.
        #
        # Always a merge commit, never a fast-forward: the merge is a
        # step in this session's history, carrying the tool's info, and
        # what it brought is read below as the diff from our pre-merge
        # head to that commit.
        before = self._wt.head
        try:
            result = self._wt.merge(
                commit=source_head,
                merge_fns=merge_fns,
                merge_prefixes=merge_prefixes,
                info={"tool": "ws-git.merge", "source": source, **(info or {})},
                fast_forward=False,
            )
        except MergeConflict as e:
            return MergeOutcome(
                merged=False,
                commit=None,
                conflicts=tuple(sorted(self._display_conflicts(e))),
                auto_merged=(),
            )
        if result.strategy == "no_op":
            # Our history already holds theirs: nothing to bring in.
            return MergeOutcome(
                merged=True, commit=before, conflicts=(), auto_merged=()
            )
        # What the merge brought in: the diff from our pre-merge head
        # lists exactly the merge's work. Conflict markers are reported
        # by provenance, not by scan: a brought file may legitimately
        # contain marker-like bytes, so only markers absent from both
        # parents' versions count.
        brought = self.diff(before, self._wt.head)
        at_base = self._snapshot(before)
        rev = {
            display: key for key, display in self._file_keys(self._wt.keys()).items()
        }
        candidates = brought.added | brought.removed | brought.modified
        conflicts = tuple(
            sorted(
                path
                for path in candidates
                if self._markers_introduced(rev.get(path), at_base, other)
            )
        )
        # HEAD moved underneath the VirtualFS object: drop its caches
        # like checkout()/discard() do, or listings and stat go stale.
        self._invalidate_fs()
        return MergeOutcome(
            merged=True,
            commit=self._wt.head,
            conflicts=conflicts,
            auto_merged=tuple(sorted(set(candidates) - set(conflicts))),
        )

    def _current_layout_side(
        self, source: str, other: Any, source_head: str
    ) -> tuple[Any, str]:
        """Both sides of a merge in the current layout, or a refusal.

        A merge is kvgit's own three-way over two commits, so a side in
        the old layout cannot be converted on the way in the way a
        restore converts one; it is refused instead. The exception is a
        source commit that its own branch has migrated since without
        changing a file: the branch head is then the same tree in the
        current layout, and that is what is merged.
        """
        from ..errors import LegacyLayoutError
        from ..migrate import legacy_keys

        ours = legacy_keys(self._state)
        if ours:
            raise LegacyLayoutError(self._session, ours)
        found = legacy_keys(other)
        if not found:
            return other, source_head
        head = self._open_branch(source)
        if (
            head.commit != source_head
            and not legacy_keys(head)
            and self._same_files(source_head, head.commit)
        ):
            return head, head.commit
        raise LegacyLayoutError(source, found)

    def _same_files(self, a: str, b: str) -> bool:
        """Whether two commits hold the same files with the same bytes."""
        change = self.diff(a, b)
        return not (change.added or change.removed or change.modified)

    def _three_way_rules(
        self, other: Any, at_base: Any, ignore: Callable[[str], bool] | None = None
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """The rules a three-way over this session resolves by.

        One table, two callers: the merge verb and the apply verb
        resolve the same trees by the same rules, and a rule that lived
        in only one of them would make "cherry-pick" and "merge" mean
        different things about the same two files. ``other`` is the
        handle holding THEIRS and ``at_base`` the one holding the base
        the three-way resolves against (``None`` where there is none).

        Returns per-key merge functions and whole-prefix policies:

        - file content merges as text, with markers where both sides
          changed the same lines. Registered per key rather than as a
          default, because a merge function is handed values and not
          the key it was registered for, so a blanket default would
          marker-merge cache blobs. Enumerated from ours and theirs;
          the union is harmless, since an uncontested key never
          consults a function.
        - a file's metadata row merges field-aware beside its blob,
          carrying the size of the merged bytes. Bound per key for the
          same reason: the function has to know which file it describes.
        - the ws-git blob, the view seed and the cwd take ours: they
          are this session's position, not content to reconcile.
        - THE PLANE POLICY, whole: this is filesystem-only. The cache
          and the stored conversation are session-scoped by
          construction — a delegate's working memory and a chat that
          never happened here — so this side keeps every key under
          those prefixes, and that has to include keys the other side
          merely ADDED. A merge function would not do it: kvgit
          consults one only where both sides changed a key, so a new
          ``__agno__/runs/<id>`` would ride in untouched. A
          ``MergeChoice`` over the prefix is the whole-side policy that
          also drops their-only adds and ignores their-only removes.
          Built for these two verbs ONLY, never registered on the
          handle: an ordinary commit that loses its CAS to a second
          handle on THIS session must still take that handle's
          conversation, which is this session's own.
        - a path ``ignore`` names keeps ours, blob and row alike: it is
          authoring output (see :mod:`nontainer.ignore`), never work to
          bring across. Per key, from the enumeration above, because
          paths are base32-encoded and a directory's key is no prefix
          of its files' keys.

        No try/except around the enumeration: failing there means store
        trouble, and that must surface rather than silently narrow what
        the rules cover.
        """
        from kvgit import MergeChoice
        from kvgit.merges import text as text_merge
        from monkeyfs import VirtualFS

        file_keys: set[str] = set()
        row_keys: set[str] = set()
        for handle in (self._state, other):
            if handle is None:
                continue
            keys = list(handle.keys())
            file_keys.update(self._file_keys(keys).keys())
            row_keys.update(k for k in keys if VirtualFS.is_metadata_key(k))
        merge_fns: dict[str, Any] = {key: text_merge for key in file_keys}
        for key in row_keys:
            merge_fns[key] = self._row_merge_fn(key, other, at_base)
        if ignore is not None:
            for key, path in self._file_keys(file_keys).items():
                if ignore(path):
                    merge_fns[key] = MergeChoice.OURS
            for key in row_keys:
                try:
                    path = "/" + VirtualFS.path_for_metadata_key(key).lstrip("/")
                except ValueError:
                    continue
                if ignore(path):
                    merge_fns[key] = MergeChoice.OURS
        merge_fns[_WS_BLOB_KEY] = MergeChoice.OURS
        merge_fns[_VIEW_KEY] = MergeChoice.OURS
        merge_fns[VirtualFS.CWD_KEY] = _keep_ours
        merge_prefixes: dict[str, Any] = {
            CACHE_PREFIX: MergeChoice.OURS,
            CONVERSATION_PREFIX: MergeChoice.OURS,
        }
        return merge_fns, merge_prefixes

    def apply(
        self,
        base: str | None,
        theirs: str | None,
        *,
        info: dict[str, Any] | None = None,
        ignore: Callable[[str], bool] | None = None,
    ) -> MergeOutcome:
        """Apply the change between two commits to the working tree.

        The one engine behind revert and cherry-pick, which are this
        call with the two commits swapped: for every key that differs
        between ``base`` and ``theirs``, three-way merge the working
        tree against those two sides by the rules a merge resolves by
        (:meth:`_three_way_rules`), and land the result as ONE appended
        commit whose only parent is the current head. ``None`` on
        either side names the tree before anything, so the change a
        root commit made can be applied and undone like any other.

        Where the two commits differ in nothing — or in nothing this
        tree does not already hold — nothing is committed and the
        outcome says so: ``merged`` False with no commit and no
        conflicts.

        The commit is not a merge commit in the graph: ``theirs`` and
        ``base`` are recorded in its info as ``applied_from`` and
        ``applied_base``, soft references rather than parents, because
        applying one commit's change does not make its history this
        session's. Conflicts are spelled exactly as a merge's are:
        markers land IN the commit and are reported in the outcome,
        with only contested state no rule resolves aborting untouched.

        Either commit may predate per-file metadata rows: both are read
        in the current layout (see :mod:`nontainer.migrate`), so the
        change applied is a change of blobs and rows, and no legacy key
        is ever written to this head.
        """
        from kvgit import MergeChoice, MergeConflict
        from kvgit.versioned.keyset import KeysetEntry, MetaEntry
        from kvgit.versioned.merge import Change, pick_merge_policy, resolve_merge

        from ..migrate import current_layout

        self._refuse_frozen("apply")
        if self.dirty:
            raise WorkspaceError(
                "uncommitted changes on this branch; commit or discard them first"
            )
        at_base = current_layout(self._at_commit(base))
        at_theirs = current_layout(self._at_commit(theirs))
        merge_fns, merge_prefixes = self._three_way_rules(at_theirs, at_base, ignore)
        # A key a policy gives to ours outright is not read at all: the
        # planes that take ours keep every key under them, and their
        # values are the one part of a session's state that is not
        # content to compare.
        changed = {
            key
            for key in self._changed_keys(base, theirs, at_base, at_theirs)
            if pick_merge_policy(key, merge_fns, merge_prefixes, None)
            is not MergeChoice.OURS
        }
        if not changed:
            return MergeOutcome(merged=False, commit=None, conflicts=(), auto_merged=())

        # kvgit resolves a three-way from each side's CHANGES since the
        # base — per key, an entry there and an entry here, each naming
        # content by id, with a reader from id to value — and it
        # compares those ids only within one key. So the three trees are
        # handed to it as ids of values: one per distinct value of a key,
        # and a reader that hands the value back. That keeps the
        # resolution kvgit's own (equal-content shortcuts, removals, the
        # order a policy is picked in) while the base is ours to name,
        # which is the whole difference between this and a merge.
        values: dict[str, Any] = {}
        base_keys: dict[str, str] = {}
        our_keys: dict[str, str] = {}
        their_keys: dict[str, str] = {}
        for key in changed:
            slots: list[tuple[Any, str]] = []
            for keyset, handle in (
                (base_keys, at_base),
                (our_keys, self._wt),
                (their_keys, at_theirs),
            ):
                if handle is None or key not in handle:
                    continue
                value = handle.get(key)
                for held, name in slots:
                    if _same(held, value):
                        keyset[key] = name
                        break
                else:
                    name = f"{key}#{len(slots)}"
                    slots.append((value, name))
                    values[name] = value
                    keyset[key] = name

        def entry(name: str | None) -> Any:
            # Every entry carries the same metadata, so two entries are
            # equal exactly when they name the same value.
            return (
                None if name is None else KeysetEntry(blob=name, meta=MetaEntry(size=0))
            )

        def changes(side: dict[str, str]) -> dict[str, Any]:
            return {
                key: Change(entry(base_keys.get(key)), entry(side.get(key)))
                for key in changed
                if side.get(key) != base_keys.get(key)
            }

        try:
            resolution = resolve_merge(
                changes(our_keys),
                changes(their_keys),
                values.get,
                merge_fns,
                None,
                merge_prefixes,
            )
        except MergeConflict as e:
            return MergeOutcome(
                merged=False,
                commit=None,
                conflicts=tuple(sorted(self._display_conflicts(e))),
                auto_merged=(),
            )

        written = self._stage_applied(changed, resolution, values)
        if not written:
            return MergeOutcome(merged=False, commit=None, conflicts=(), auto_merged=())
        before = self._wt.head
        try:
            landed = self.commit(
                info={
                    "applied_from": theirs,
                    "applied_base": base,
                    **(info or {}),
                }
            )
        except BaseException:
            # Nothing of anyone's is at stake in the buffer: it holds
            # only what this call put there.
            self._wt.discard()
            self._invalidate_fs()
            raise
        # The tree moved under the VirtualFS object: drop its caches
        # like checkout()/discard() do, or listings and stat go stale.
        self._invalidate_fs()
        # Conflicts by provenance, not by scan: a file the change
        # brings may legitimately contain marker-like bytes, so only
        # markers absent from both sides count.
        at_before = self._snapshot(before)
        paths = self._file_keys(written)
        conflicts = tuple(
            sorted(
                path
                for key, path in paths.items()
                if self._markers_introduced(key, at_before, at_theirs)
            )
        )
        return MergeOutcome(
            merged=True,
            commit=landed,
            conflicts=conflicts,
            auto_merged=tuple(sorted(set(paths.values()) - set(conflicts))),
        )

    def _stage_applied(
        self, changed: "Iterable[str]", resolution: Any, values: dict[str, Any]
    ) -> set[str]:
        """Stage a resolved apply; returns the keys it wrote.

        The resolution says what to change on our side: a key a merge
        function produced a value for, a key that takes the value one
        side holds, a key that goes. A key it leaves alone keeps ours. A
        key whose resolution is what the tree already holds is not
        written at all, so a change already present costs no commit.
        """
        written: set[str] = set()
        for key in changed:
            if key in resolution.merged_values:
                value = resolution.merged_values[key]
            elif key in resolution.updates:
                value = values[resolution.updates[key].blob]
            elif key in resolution.removals:
                if key in self._wt:
                    del self._wt[key]
                    written.add(key)
                continue
            else:
                continue
            if key in self._wt and _same(self._wt.get(key), value):
                continue
            self._wt[key] = value
            written.add(key)
        return written

    def _at_commit(self, commit: str | None) -> Any:
        """A read handle on one commit, or ``None`` for the empty tree.

        ``None`` names the tree before anything — what the first commit
        of a session changed the world from — and is a side a three-way
        can take. Any other id the store does not hold is an error.
        """
        if commit is None:
            return None
        return self._snapshot(commit)

    def _changed_keys(
        self, base: str | None, theirs: str | None, at_base: Any, at_theirs: Any
    ) -> set[str]:
        """The keys one commit's change touches, from the store's own
        diff — and every key of the one side there is where the other
        side is the empty tree.

        A side read in the current layout adds the keys its conversion
        wrote, which the store's diff cannot name, and never contributes
        a legacy key.
        """
        from ..migrate import LEGACY_KEYS, converted_keys

        if base is not None and theirs is not None:
            change = self._repo.diff(base, theirs)
            keys = set(change.added) | set(change.removed) | set(change.modified)
            keys |= converted_keys(at_base) | converted_keys(at_theirs)
            return keys - set(LEGACY_KEYS)
        handle = at_theirs if at_base is None else at_base
        return set(handle.keys()) if handle is not None else set()

    def commit_at(
        self, commit: str, *, session: str | None = None
    ) -> CommitInfo | None:
        """One commit's record by id; ``None`` when there is no such
        commit to read.

        ``history()`` walks this session's branch; this answers for any
        commit the store holds, which is what reading the change
        another session's commit made takes.

        ``session`` narrows that to one branch: the commit must be one
        that session's history reaches, and a commit the store holds
        somewhere else reads as absent. A ref names a session AND a
        commit, so a commit a sibling branch made is not the one a ref
        under this name means, however whole the id. A session the
        store does not have raises ``ValueError`` naming it — no such
        session is a different answer from no such commit.
        """
        if session is not None and not any(
            held == commit for held in self._commit_ids(session)
        ):
            return None
        return self._commit_record(commit)

    def _commit_record(self, commit_hash: str) -> CommitInfo | None:
        """One commit as a :class:`CommitInfo`, or ``None`` if the store
        holds no such commit."""
        from kvgit import UnknownCommitError

        try:
            return _commit_info(self._repo.get_commit(commit_hash))
        except UnknownCommitError:
            return None

    def _open_branch(self, source: str) -> Any:
        """A read-only snapshot of another branch's head.

        Unknown branches raise ValueError naming them.
        """
        from kvgit import UnknownBranchError

        try:
            return self._repo.snapshot(branch=source)
        except UnknownBranchError:
            raise ValueError(f"unknown branch {source!r}") from None

    def _display_conflicts(self, exc: Exception) -> set[str]:
        """Conflicting keys as display paths (raw keys for non-files).

        A file is one conflict however many of its keys contested: its
        blob and its metadata row are two keys describing one path, and
        an agent asked to resolve the same file twice has been told
        something untrue. Both fold onto the path, and the set does the
        rest.
        """
        from monkeyfs import VirtualFS

        keys: set[str] = set(getattr(exc, "conflicting_keys", ()))
        paths = self._file_keys(keys)
        out: set[str] = set()
        for key in keys:
            if VirtualFS.is_metadata_key(key):
                try:
                    out.add("/" + VirtualFS.path_for_metadata_key(key).lstrip("/"))
                    continue
                except (ValueError, UnicodeDecodeError):
                    pass
            out.add(paths.get(key, key))
        return out

    def _merge_base(self, ours: str, theirs: str) -> str | None:
        """The commit a three-way merge of these two heads resolves against.

        It must be the base kvgit's own merge uses: the merged bytes of
        a contested file are a function of that base, and a row whose
        size was computed against a different one would describe bytes
        nothing holds. kvgit's ``merge_base`` is that base by contract.
        ``None`` when there is none to be had, and the sizes then fall
        back to a side's own.
        """
        try:
            return self._repo.merge_base(ours, theirs)
        except Exception:  # noqa: BLE001 - no base is an answer, not a failure
            return None

    def _row_merge_fn(self, row_key: str, other: Any, at_lca: Any):
        """The merge function for one file's metadata row.

        Fields merge by rule; ``size`` is recomputed from the bytes the
        content merge produces for this same path, so the row that lands
        in the merge commit describes the blob that lands with it. A
        size copied from a side would be wrong for every file whose
        merged bytes match neither — a clean union of two edits is the
        common case — and correcting it afterwards would put a second
        commit inside one merge.
        """
        from monkeyfs import VirtualFS

        def merge_row(old: Any, ours: Any, theirs: Any) -> bytes:
            return _merge_metadata_row(
                old, ours, theirs, size=self._merged_size(row_key, other, at_lca)
            )

        try:
            VirtualFS.path_for_metadata_key(row_key)
        except (ValueError, UnicodeDecodeError):
            # A row key whose path will not decode describes no file
            # this provider can find bytes for; merge the fields alone.
            return _merge_metadata_row
        return merge_row

    def _merged_size(self, row_key: str, other: Any, at_lca: Any) -> int | None:
        """Length of the bytes a content merge produces for this row's file.

        Resolves the file's three sides exactly as the content merge
        does — identical sides stand, a side that did not change takes
        the other, and anything else is the marker merge — so the size
        is the merged blob's, whether the merge was clean or marked.
        A side given as no handle at all is the empty tree, and holds
        no bytes for any file: the same reading every other rule here
        takes of a missing side.

        ``None`` where the size cannot be answered (a directory row, an
        unreadable side, bytes no marker merge can mark): the row then
        keeps a side's own size, which is the best that is known.
        """
        from kvgit.merges import CantMark
        from kvgit.merges import text as text_merge
        from monkeyfs import VirtualFS

        try:
            path = VirtualFS.path_for_metadata_key(row_key)
            blob_key = self.fs._encode_path("/" + path.lstrip("/"))
        except (ValueError, UnicodeDecodeError):
            return None
        base = at_lca.get(blob_key) if at_lca is not None else None
        ours = self._state.get(blob_key)
        theirs = other.get(blob_key) if other is not None else None
        for side in (base, ours, theirs):
            if side is not None and not isinstance(side, bytes):
                return None
        if ours == theirs:
            merged = ours
        elif ours == base:
            merged = theirs
        elif theirs == base:
            merged = ours
        else:
            try:
                merged = text_merge(base, ours, theirs)
            except CantMark:
                return None
        return len(merged) if merged is not None else None

    def _markers_introduced(self, key: str | None, at_base: Any, other: Any) -> bool:
        """Whether this merge introduced conflict markers into a key.

        Marker-like bytes can predate the merge (docs, fixtures), so
        current bytes carrying them are not enough: the markers must
        be absent from both parents' versions. Reads stay scoped to
        scan-positive candidates, so the common case costs one blob.
        """
        if key is None:
            return False
        value = self._state.get(key)
        if not isinstance(value, bytes) or b"<<<<<<< " not in value:
            return False
        for handle in (at_base, other):
            if handle is None:
                continue
            parent = handle.get(key)
            if isinstance(parent, bytes) and b"<<<<<<< " in parent:
                return False
        return True

    def _changed_content(self, keys: dict[str, str], a: str, b: str) -> frozenset[str]:
        """Of these file keys, the paths whose bytes actually differ."""
        if not keys:
            return frozenset()
        from kvgit import UnknownCommitError

        try:
            at_a = self._repo.snapshot(commit=a)
            at_b = self._repo.snapshot(commit=b)
        except UnknownCommitError:
            # A commit that cannot be opened cannot be compared; the
            # pointer diff is then the most this can honestly say.
            return frozenset(keys.values())
        before = at_a.get_many(*keys)
        after = at_b.get_many(*keys)
        return frozenset(
            path for key, path in keys.items() if before.get(key) != after.get(key)
        )

    def _file_keys(self, keys: Iterable[str]) -> dict[str, str]:
        """Store key → workspace file path, for the keys that are files."""
        from monkeyfs import VirtualFS

        from ..migrate import LEGACY_TABLE_KEY

        vfs = self.fs
        out: dict[str, str] = {}
        for key in keys:
            if not isinstance(key, str) or not key.startswith(VirtualFS.PREFIX):
                continue
            # A metadata row starts with the file prefix too, so the
            # scan has to say so: it describes a file, it is not one.
            # The cwd slot is another key under the prefix that holds no
            # file content, and so is the single metadata table monkeyfs
            # kept before per-file rows: an old commit a frozen read
            # opens can still hold it, and it is never a file.
            if VirtualFS.is_metadata_key(key):
                continue
            if key in (LEGACY_TABLE_KEY, VirtualFS.CWD_KEY):
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
        if self._owns_repo:
            self._repo.close()
