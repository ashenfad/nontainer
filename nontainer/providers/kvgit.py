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
— unused by the v1 toolkit, reserved for concurrent-session presets).

``info`` dicts attached to checkpoints must be JSON-serializable
(kvgit hashes them into the commit id).
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, MutableMapping
from pathlib import Path
from typing import Any

from ..errors import CheckpointNotFoundError, WorkspaceError
from ..protocol import Capabilities, CheckpointInfo, validate_session_id

_KVGIT_CAPS = Capabilities(
    versioned=True,
    staging=True,
    cheap_fork=True,
    merge=True,
    sql_audit=False,
    fuse_mount=False,
)


class KvgitProvider:
    """``WorkspaceProvider`` over a kvgit ``Staged`` branch.

    Construct via :meth:`open` (path-based, branch-per-session) or
    directly from a ``Staged`` you built yourself (custom codecs,
    memory stores for tests, an existing store).
    """

    def __init__(self, staged: Any, *, session: str) -> None:
        validate_session_id(session)
        self._session = session
        self._staged = staged
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

    # -- versioning ----------------------------------------------------

    def checkpoint(self, info: dict[str, Any] | None = None) -> str:
        """Commit staged fs + kv writes atomically; returns the commit
        hash. No staged changes → no new commit (returns current)."""
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
        if not self._staged.reset_to(checkpoint_id):
            raise CheckpointNotFoundError(
                f"No such checkpoint: {checkpoint_id!r}"
            )
        self._invalidate_fs()

    def _invalidate_fs(self) -> None:
        """Drop VirtualFS's lazy caches after state changed underneath
        it (restore/discard). The SAME fs instance must survive —
        Workspace and the sandbox hold references to it.

        TODO(monkeyfs): promote to a public ``VirtualFS.invalidate()``.
        """
        fs = self._fs
        if fs is not None:
            fs._dir_cache = None
            fs._metadata_cache = None
            fs._current_size = None

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
            )
            count += 1

    def fork(self, name: str) -> "KvgitProvider":
        """O(1) branch sharing storage. Pending staged changes are
        checkpointed first so the fork sees current state."""
        validate_session_id(name)
        if name in self._staged.list_branches():
            raise WorkspaceError(f"Branch already exists: {name!r}")
        if self._staged.has_changes:
            self.checkpoint(info={"tool": "fork", "target": name})
        forked = self._staged.create_branch(name)
        return KvgitProvider(forked, session=name)

    def discard(self) -> None:
        self._staged.reset()
        self._invalidate_fs()

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
