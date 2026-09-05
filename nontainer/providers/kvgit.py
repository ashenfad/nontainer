"""KvgitProvider: the versioned substrate (default backend).

One shared kvgit store; each session is a branch. Files (via monkeyfs
``VirtualFS``), the agent cache, and framework keys (``__cwd__``) all
live in one flat ``Staged`` mapping — so one ``checkpoint()`` commits
the whole world atomically, and ``restore()`` rewinds all of it
(including where the agent's cwd was).

Key coexistence in the flat mapping: ``VirtualFS`` encodes file paths
under its own key prefix (plus ``__vfs_metadata__``), while cache keys
live under ``__cache__/`` — no collisions by construction.

Capabilities: ``staging`` (writes are invisible until checkpoint;
``discard()`` drops them), ``cheap_fork`` (branches share storage via
kvgit's content-addressed HAMT), ``merge`` (CAS + key-level three-way
with conflict markers), ``index`` (a staged set with selective
``commit``; staging suspends autocheckpoint until the composition
lands or is abandoned).

``info`` dicts attached to checkpoints must be JSON-serializable
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
from collections.abc import Iterable, Iterator, MutableMapping
from pathlib import Path
from typing import Any

from ..errors import CheckpointNotFoundError, NotSupportedError, WorkspaceError
from ..protocol import (
    Capabilities,
    CheckpointInfo,
    MergeOutcome,
    StageResult,
    TagInfo,
    WorkspaceDiff,
    WorkspaceStatus,
    validate_session_id,
)

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

_WS_BLOB_KEY = "__ws_git__"
_WS_BLOB_VERSION = 1


def _merge_ws_blob(old: Any, ours: Any, theirs: Any) -> bytes:
    """Union merge for the ws-git staging blob.

    Both sides' composition intent survives: the index unions, and a
    suspended autocheckpoint on either side stays suspended. Unchanged
    on one side takes the other (so commit/unstage on one side win
    over a quiet other); unparseable raises ``CantMark`` like binary.
    """
    from kvgit.merges import CantMark

    def blob(value: Any) -> dict:
        if value is None:
            return {"index": [], "suspended": False}
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError) as e:
            raise CantMark(f"staging blob not JSON: {e}") from e
        if not isinstance(parsed, dict):
            raise CantMark("staging blob not a table")
        index = parsed.get("index") or []
        return {
            "index": sorted(k for k in index if isinstance(k, str)),
            "suspended": bool(parsed.get("suspended")),
        }

    o, u, t = blob(old), blob(ours), blob(theirs)
    if u == t:
        result = u
    elif o == u:
        result = t
    elif o == t:
        result = u
    else:
        result = {
            "index": sorted(set(u["index"]) | set(t["index"])),
            "suspended": u["suspended"] or t["suspended"],
        }
    return json.dumps(
        {
            "version": _WS_BLOB_VERSION,
            "index": result["index"],
            "suspended": result["suspended"],
        },
        sort_keys=True,
    ).encode()


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


SESSION_SCOPE = "session"
STORE_SCOPE = "store"
# Leading "@": session ids cannot start with one (SESSION_ID_RE), so no
# session's tag namespace can ever be the store's.
_STORE_PREFIX = "@store/"


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
        validate_session_id(session)
        self._session = session
        self._staged = staged
        self._frozen_at = frozen_at
        self._fs: Any | None = None
        self._closed = False

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

        validate_session_id(session)
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
    def delete(cls, path: str | Path, sessions: Iterable[str]) -> None:
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
        kvgit.delete_branches(names, kind="disk", path=str(p))

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

    def checkpoint(self, info: dict[str, Any] | None = None) -> str:
        """Commit staged fs + kv writes atomically; returns the commit
        hash. No staged changes → no new commit (returns current)."""
        self._refuse_frozen("checkpoint")
        if not self._staged.has_changes:
            return self._staged.current_commit
        result = self._staged.commit(info=info)
        if not result.merged:
            raise WorkspaceError(
                f"checkpoint failed: conflicting concurrent commit on branch "
                f"{self._session!r} (CAS): {result}"
            )
        return self._staged.current_commit

    def restore(self, checkpoint_id: str) -> None:
        self._refuse_frozen("restore")
        if not self._staged.reset_to(checkpoint_id):
            raise CheckpointNotFoundError(f"No such checkpoint: {checkpoint_id!r}")
        self._invalidate_fs()

    def _invalidate_fs(self) -> None:
        """Drop VirtualFS's lazy caches after state changed underneath
        it (restore/discard). The SAME fs instance must survive —
        Workspace and the sandbox hold references to it.
        """
        if self._fs is not None:
            self._fs.invalidate()

    def history(self, *, limit: int | None = None) -> Iterable[CheckpointInfo]:
        return self._history_iter(limit)

    def _history_iter(self, limit: int | None) -> Iterator[CheckpointInfo]:
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
            yield CheckpointInfo(
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

        Without ``at``, pending staged changes are checkpointed first so
        the fork sees current state. With ``at`` the fork starts from
        that earlier checkpoint instead, and the staged changes are left
        alone: they belong to this session's present, not to the past
        the fork is branching from.
        """
        self._refuse_frozen("fork")
        validate_session_id(name)
        if name in self._staged.list_branches():
            raise WorkspaceError(f"Branch already exists: {name!r}")
        if at is None:
            if self._staged.has_changes:
                self.checkpoint(info={"tool": "fork", "target": name})
            forked = self._staged.create_branch(name)
        else:
            try:
                forked = self._staged.create_branch(name, at=at)
            except ValueError as e:
                if "does not exist" in str(e):
                    raise CheckpointNotFoundError(at) from e
                raise
        return KvgitProvider(forked, session=name)

    def discard(self) -> None:
        self._staged.reset()
        self._invalidate_fs()

    # -- staged mode (the index) -----------------------------------------

    @staticmethod
    def _parse_blob(raw: Any) -> dict[str, Any]:
        """Blob value → normalized dict (tolerant of absence and shapes).

        Accepts whatever a handle ``get`` returns — bytes, str, or an
        already-decoded dict — so staged and HEAD reads compare equal.
        """
        if raw is None:
            return {"version": _WS_BLOB_VERSION, "index": [], "suspended": False}
        if isinstance(raw, dict):
            parsed = raw
        else:
            try:
                parsed = json.loads(raw)
            except (ValueError, TypeError):
                return {"version": _WS_BLOB_VERSION, "index": [], "suspended": False}
        if not isinstance(parsed, dict):
            return {"version": _WS_BLOB_VERSION, "index": [], "suspended": False}
        index = parsed.get("index") or []
        return {
            "version": _WS_BLOB_VERSION,
            "index": sorted(k for k in index if isinstance(k, str)),
            "suspended": bool(parsed.get("suspended")),
        }

    def _read_blob(self) -> dict[str, Any]:
        """The staging blob (tolerant of absence and older shapes)."""
        return self._parse_blob(self._staged.get(_WS_BLOB_KEY))

    def _write_blob(self, index: Iterable[str], suspended: bool) -> None:
        """Stage (not commit) index bookkeeping.

        The write sits in the staging buffer next to file writes; it
        lands through the CAS-guarded commit path like everything else
        (see the contention spike in ``scratch/ws-git-impl.md`` gap 3).
        When the bytes equal HEAD, retract instead of writing: kvgit
        has no per-key unstage, so an identical write would linger as
        phantom dirt (and a later read-only op would checkpoint it).
        The blob key is provider-owned — never user-addressed — so
        dropping its staged update is safe; staged removals are left
        alone (only direct handle use records those). Guarded: without
        these internals, fall back to the plain staged write.
        """
        value = json.dumps(
            {
                "version": _WS_BLOB_VERSION,
                "index": sorted(set(index)),
                "suspended": suspended,
            },
            sort_keys=True,
        ).encode()
        head_handle = self._staged.checkout(self._staged.current_commit)
        head_raw = head_handle.get(_WS_BLOB_KEY) if head_handle is not None else None
        if self._parse_blob(head_raw) == self._parse_blob(value):
            updates = getattr(self._staged, "_updates", None)
            cache = getattr(self._staged, "_cache", None)
            if updates is not None and cache is not None:
                updates.pop(_WS_BLOB_KEY, None)
                cache.pop(_WS_BLOB_KEY, None)
                return
        self._staged[_WS_BLOB_KEY] = value

    def _resolve_stage_paths(self, paths: Iterable[str]) -> dict[str, str]:
        """Display path → store key, for stageable paths.

        Encoding is pure, so a path deleted from the live tree still
        resolves (verified against HEAD); a path that names nothing —
        live, at HEAD, or a directory — raises ``ValueError``.
        """
        live = {
            display: key
            for key, display in self._file_keys(self._staged.keys()).items()
        }
        head_handle = self._staged.checkout(self._staged.current_commit)
        head_displays = (
            set(self._file_keys(head_handle.keys()).values())
            if head_handle is not None
            else set()
        )
        vfs = self.fs
        out: dict[str, str] = {}
        for path in paths:
            if path in live:
                out[path] = live[path]
                continue
            if path in head_displays:
                # Encoding is pure (no tree lookup), so a deleted file
                # re-derives the key VFS wrote — same private-access
                # justification as ``_file_keys``' decode.
                out[path] = vfs._encode_path(path)
                continue
            if vfs.isdir(path):
                raise ValueError(f"cannot stage {path!r}: stage files, not directories")
            raise ValueError(
                f"unknown path {path!r}: no such file at HEAD or in the working tree"
            )
        return out

    def _modified_displays(self) -> set[str]:
        """Live-tree display paths differing from HEAD (add/rm/edit)."""
        live = {
            display: key
            for key, display in self._file_keys(self._staged.keys()).items()
        }
        head_handle = self._staged.checkout(self._staged.current_commit)
        if head_handle is None:
            return set(live)
        head = self._file_keys(head_handle.keys())
        head_displays = set(head.values())
        live_displays = set(live)
        modified = (live_displays - head_displays) | (head_displays - live_displays)
        head_by_display = {display: key for key, display in head.items()}
        for display in live_displays & head_displays:
            if self._staged.get(live[display]) != head_handle.get(
                head_by_display[display]
            ):
                modified.add(display)
        return modified

    def stage(self, paths: Iterable[str]) -> StageResult:
        """Stage workspace file paths; first call suspends autocheckpoint.

        The index names store keys, not snapshots: edits made after
        staging ride along into the selective ``commit``.
        """
        self._refuse_frozen("stage")
        if isinstance(paths, str):
            paths = [paths]
        paths = list(dict.fromkeys(paths))
        if not paths:
            return StageResult(staged=(), suspended=False)
        resolved = self._resolve_stage_paths(paths)
        blob = self._read_blob()
        index = set(blob["index"])
        added = [p for p in paths if resolved[p] not in index]
        new_index = index | set(resolved.values())
        suspending = not blob["suspended"]
        if not added and not suspending:
            return StageResult(staged=(), suspended=False)
        self._write_blob(new_index, True)
        return StageResult(staged=tuple(added), suspended=suspending)

    def unstage(self, paths: Iterable[str]) -> tuple[str, ...]:
        """Remove paths from the index; emptying it resumes autocheckpoint."""
        self._refuse_frozen("unstage")
        if isinstance(paths, str):
            paths = [paths]
        paths = list(dict.fromkeys(paths))
        if not paths:
            return ()
        resolved = self._resolve_stage_paths(paths)
        blob = self._read_blob()
        index = set(blob["index"])
        removed = [p for p in paths if resolved[p] in index]
        if not removed:
            return ()
        index -= {resolved[p] for p in removed}
        old_suspended = blob["suspended"]
        self._write_blob(index, old_suspended and bool(index))
        return tuple(removed)

    def commit(self, info: dict[str, Any] | None = None) -> str:
        """Commit staged keys plus index bookkeeping; unstaged stays dirty.

        Framework keys (the VFS table, cwd, the blob itself) ride along
        when staged — the table is fixed up first so the new commit's
        rows describe its own blobs exactly, not uncommitted work.
        Cache keys never ride: unrelated agent state stays dirty.
        """
        from monkeyfs import VirtualFS

        self._refuse_frozen("commit")
        blob = self._read_blob()
        index = set(blob["index"])
        pending = {key for key in index if self._staged.is_staged(key)}
        head_handle = self._staged.checkout(self._staged.current_commit)
        head_blob = (
            self._parse_blob(head_handle.get(_WS_BLOB_KEY))
            if head_handle is not None
            else self._parse_blob(None)
        )
        cleared = {"version": _WS_BLOB_VERSION, "index": [], "suspended": False}
        if not pending and head_blob == cleared:
            # Nothing staged and the bookkeeping is already clean:
            # refuse instead of minting an empty commit (decided before
            # writing anything, so there is nothing to unwind).
            raise WorkspaceError("nothing staged to commit")
        self._write_blob(set(), False)
        # Snapshot live rows before the fixup: the subset commit below
        # clears the table key from the buffer, which would leave the
        # fixed-up table as live truth — unstaged additions rowless,
        # unstaged edits HEAD-stale, and the next checkpoint persisting
        # the mismatch. Restoring afterwards keeps reads truthful.
        live_table = self._staged.get(VirtualFS.METADATA_KEY)
        self._commit_table(index)
        keys = set(pending) | {_WS_BLOB_KEY}
        for extra in (VirtualFS.METADATA_KEY, VirtualFS.CWD_KEY, "__cwd__"):
            if self._staged.is_staged(extra):
                keys.add(extra)
        result = self._staged.commit(
            keys=keys, info={"tool": "ws-git.commit", **(info or {})}
        )
        if not result.merged:
            raise WorkspaceError(
                f"commit failed: conflicting concurrent commit on branch "
                f"{self._session!r} (CAS): {result}"
            )
        if live_table is not None:
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
        # Fixup and restore both wrote around the VFS: drop its caches
        # once, at the end, so post-commit reads see live state.
        self._invalidate_fs()
        return self._staged.current_commit

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

    def discard_staged(self) -> None:
        """Abandon the composition; working-tree writes stay dirty."""
        self._refuse_frozen("discard_staged")
        blob = self._read_blob()
        if not blob["index"] and not blob["suspended"]:
            return
        self._write_blob(set(), False)

    def status(self) -> WorkspaceStatus:
        """Staged vs unstaged paths plus live merge context. Pure read."""
        modified = self._modified_displays() if self.dirty else set()
        index = set(self._read_blob()["index"])
        # Decode index keys directly: a staged deletion has no live
        # display, so mapping through the live tree would drop it.
        vfs = self.fs
        indexed = set()
        for key in index:
            try:
                indexed.add("/" + vfs._decode_path(key).lstrip("/"))
            except Exception:  # noqa: BLE001 - undecodable key is not a file
                continue
        staged = sorted(modified & indexed)
        unstaged = sorted(modified - indexed)
        merge_source, merge_unresolved = self._merge_context()
        return WorkspaceStatus(
            branch=self._staged.current_branch,
            staged=tuple(staged),
            unstaged=tuple(unstaged),
            merge_source=merge_source,
            merge_unresolved=tuple(merge_unresolved),
        )

    def stage_suspended(self) -> bool:
        """Whether staging currently suspends autocheckpoint here."""
        return self._read_blob()["suspended"]

    def _merge_context(self) -> tuple[str | None, list[str]]:
        """Newest merge source with markers still in HEAD, if any.

        Markers are scanned at read time (committed ones count — an
        agent mid-resolution shows clean-tree progress in ``unstaged``
        while the merge stays outstanding until the clearing commit).
        History is consulted only when markers exist.
        """
        head_handle = self._staged.checkout(self._staged.current_commit)
        if head_handle is None:
            return None, []
        marked = sorted(
            display
            for key, display in self._file_keys(head_handle.keys()).items()
            for value in (head_handle.get(key),)
            if isinstance(value, bytes) and b"<<<<<<< " in value
        )
        if not marked:
            return None, []
        for entry in self.history():
            if entry.info.get("tool") == "ws-git.merge":
                return entry.info.get("source"), marked
        return None, marked

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
        """Name a checkpoint immutably; returns the commit it names.

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
            raise CheckpointNotFoundError(
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
            raise CheckpointNotFoundError(f"No such tag: {name!r} in scope {scope!r}")
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
        )

    def merge(self, source: str) -> MergeOutcome:
        """Merge another branch into this one.

        Reads the source HEAD commit (anything uncommitted there is not
        included), three-way merges file content with marker merge (see
        ``kvgit.merges``), and commits the result tagged as a merge.
        Overlapping text lands with conflict markers in the working tree
        and the outcome reports it. Framework state merges by rule, not
        by text: the VFS metadata table merges field-aware (timestamps
        are advisory), cwd keys keep ours, and anything else contested
        raises as a hard conflict rather than merging silently. After
        the merge, VFS sizes are corrected from the merged blobs
        (metadata-only commit, only when they differ) and the
        filesystem caches are invalidated; marker conflicts are
        reported by provenance, so pre-existing marker-like bytes in a
        brought file don't count as conflicts.
        """
        from kvgit import MergeConflict
        from kvgit.merges import text as text_merge
        from monkeyfs import VirtualFS

        self._refuse_frozen("merge")
        if self.dirty:
            raise WorkspaceError(
                "uncommitted changes on this branch; checkpoint or discard them first"
            )
        if source == self._staged.current_branch:
            raise ValueError("cannot merge a branch into itself")
        other = self._open_branch(source)
        source_head = other.current_commit

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
        merge_fns[_WS_BLOB_KEY] = _merge_ws_blob
        for cwd_key in ("__cwd__", VirtualFS.CWD_KEY):
            merge_fns[cwd_key] = _keep_ours

        # Markers commit WITH the merge (flagged in the outcome), they
        # don't block it: the agent resolves with ordinary edit tools
        # and checkpoints, so no merge-state machine is needed. Only
        # non-file hard conflicts abort untouched (no fn can resolve
        # them). Hence no post_check here — kvgit keeps the hook for
        # callers that genuinely want blocking; we want markers.
        try:
            self._staged.merge(
                source_head,
                merge_fns=merge_fns,
                info={"tool": "ws-git.merge", "source": source},
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
        self._fix_merged_sizes(brought.added | brought.modified, rev, source)
        # HEAD moved underneath the VirtualFS object: drop its caches
        # like restore()/discard() do, or listings and stat go stale.
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
        self, paths: set[str], rev: dict[str, str], source: str
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
        self.checkpoint(
            {"tool": "ws-git.merge", "source": source, "sizes": "recomputed"}
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
                # and ``ws.fs`` use (``/workspace/data/in.csv``).
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
