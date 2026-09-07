"""Store: what outlives a session.

A :class:`Workspace` is one session's world. A :class:`Store` is the
place those sessions live in — the kvgit store, the directory of
per-session trees, the folder of AgentFS db files — and it owns the
verbs that are about the *set* of sessions rather than about any one
of them: opening and listing them, deleting one, sweeping storage
nothing reaches any more, and the store-scoped tags that deliberately
survive the session that made them.

Those verbs used to be module functions (``nontainer.workspace``,
``nontainer.delete_workspace``) or methods grafted onto a session
handle (``ws.tag(..., scope="store")``). Naming the object makes the
split visible: session-level state is on ``Workspace``, store-level
state is here, and a caller can tell which is which by where the verb
lives.

``nontainer.workspace(session, ...)`` remains, as sugar for
``Store(...).open(session, ...)``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from .errors import NotSupportedError, WorkspaceError
from .protocol import SESSION_ID_RE, TagInfo, WorkspaceProvider, validate_session_id

if TYPE_CHECKING:
    from .protocol import Executor
    from .workspace import Mount, PythonConfig, Workspace

Backend = Literal["kvgit", "dir", "agentfs"]

# kvgit's own reserved branch namespaces, plus the legacy anchor branch
# older nontainer versions minted during teardown. None of them is a
# session, so none of them belongs in a session listing.
_RESERVED_BRANCHES = frozenset({"__void__"})
_RESERVED_BRANCH_PREFIXES = ("refs/tags/", "@")


@dataclass(frozen=True)
class Ref:
    """A pointer into a store: ``session@commit`` with an optional
    path, spelled ``session@commit:/path``.

    A commit id alone says *what* the state is but not *where* it came
    from, and a session id alone names a moving head. A ref names one
    exact state on one session, which is what a snapshot, a
    publication, or a cross-session read has to quote.

    ``path`` is carried but not yet interpreted: it is the spelling a
    later sparse read (``store.resolve("a@b:/app")``) will use.
    """

    session: str
    commit: str
    path: str | None = None

    @classmethod
    def parse(cls, text: "str | Ref") -> "Ref":
        """Parse ``session@commit`` or ``session@commit:/path``.

        A :class:`Ref` passes through, so callers can accept either
        spelling without branching.
        """
        if isinstance(text, Ref):
            return text
        if not isinstance(text, str) or "@" not in text:
            raise ValueError(
                f"Not a ref: {text!r} — expected 'session@commit' "
                "(optionally 'session@commit:/path')"
            )
        session, rest = text.split("@", 1)
        commit, _, path = rest.partition(":")
        if not session or not commit:
            raise ValueError(
                f"Not a ref: {text!r} — both a session and a commit are required"
            )
        return cls(session=session, commit=commit, path=path or None)

    def __str__(self) -> str:
        base = f"{self.session}@{self.commit}"
        return f"{base}:{self.path}" if self.path else base


class StoreTags:
    """Store-scoped tags: names that belong to no session.

    Every workspace on the store can read one, and it survives the
    deletion of the session that made it — that is the whole point of
    the scope. A publication, the state an app serves, the snapshot a
    report links to.

    Session-scoped tags stay on the workspace (``ws.tag``,
    ``ws.tags``, ``ws.at_tag``): they belong to a session and go when
    it does.

    Reached as ``store.tags``; kvgit only, since it is the one backend
    with tags.
    """

    def __init__(self, store: "Store") -> None:
        self._store = store

    def add(
        self,
        source: "Workspace | Ref | str",
        name: str,
        *,
        info: dict[str, Any] | None = None,
    ) -> str:
        """Name a commit store-scoped; returns the commit it names.

        ``source`` is the workspace whose current state to name (its
        staged changes are committed first, so the name means what the
        caller saw), or a ref naming an exact commit on a session.

        A workspace tags through its own provider, so it must be one
        this store opened — a workspace from somewhere else would write
        the tag to ITS store and leave this one's listing empty, which
        reads as a silent no-op. Such a call is refused instead. Use
        that store's own ``tags``, or name the commit by ref.

        Tags never move: an existing name raises rather than being
        repointed.
        """
        from .workspace import Workspace

        if isinstance(source, Workspace):
            self._require_own(source)
            return source.tag(name, info=info, scope="store")
        ref = Ref.parse(source)
        provider = self._store._session_provider(ref.session)
        try:
            provider.check_tag(name, scope="store")
            if provider.tag_info(name, scope="store") is not None:
                raise WorkspaceError(
                    f"Tag already exists: {name!r} in scope 'store' — tags "
                    "never move; delete it first if you mean to repoint it"
                )
            return provider.tag(name, at=ref.commit, info=info, scope="store")
        finally:
            provider.close()

    def _require_own(self, ws: "Workspace") -> None:
        """Refuse a workspace this store did not open."""
        owner = getattr(ws, "_store", None)
        if owner is self._store:
            return
        whose = (
            "was built straight from a provider, so no store owns it"
            if owner is None
            else f"belongs to {owner!r}"
        )
        raise WorkspaceError(
            f"Cannot tag through workspace {ws.session!r}: it {whose}, not to "
            f"{self._store!r}. Tagging goes through the workspace's own "
            "provider, so this would write to that store and leave this one "
            "unchanged. Use that store's tags, or name the commit by ref."
        )

    def list(self) -> dict[str, str]:
        """Tag name → commit id, for every store-scoped tag."""
        prefix = self._store._store_tag_prefix()
        return {
            stored[len(prefix) :]: commit
            for stored, commit in self._store._raw_tags().items()
            if stored.startswith(prefix)
        }

    def info(self, name: str) -> TagInfo | None:
        """Describe one store-scoped tag, or ``None`` if there is
        no such tag."""
        return self._store._raw_tag_info(name)

    def delete(self, name: str) -> None:
        """Drop a store-scoped tag, then sweep commits nothing else
        reaches. What it named survives only while something else
        still reaches it — a branch, or another tag."""
        self._store._delete_store_tag(name)

    def at(self, name: str) -> "Workspace":
        """A frozen workspace over the tagged state.

        Reads see the tagged files, cache and cwd; nothing can be
        written or committed. Close it when done — it holds an executor
        and a store handle of its own.

        A store-scoped tag belongs to no session, so ``ws.session``
        names whichever live branch the read was anchored on rather
        than an origin: the tag is the identity here, not the session.
        """
        return self._store._frozen_workspace(self._store._provider_at_store_tag(name))


class Store:
    """Where sessions live, and everything that outlives one of them.

    Args:
        path: The store directory. ``None`` means ``~/.nontainer``,
            the same default :func:`nontainer.workspace` has always
            had. Each backend lays out its own tree underneath: kvgit
            keeps one shared store at ``<path>/kvgit`` with a branch
            per session, ``dir`` keeps ``<path>/<session>/``, and
            ``agentfs`` keeps ``<path>/<session>.db``.
        backend: Which substrate the sessions live on.
        provider_factory: Bring your own substrate — a callable taking
            a session id and returning a
            :class:`~nontainer.protocol.WorkspaceProvider`. When given
            it replaces ``backend``/``path`` for :meth:`open`, and the
            store-level verbs (:meth:`sessions`, :meth:`delete`,
            :meth:`clean`, :attr:`tags`) refuse: the layout is the
            factory's business, and guessing it would be worse than
            saying so.
    """

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        backend: Backend = "kvgit",
        provider_factory: "Callable[[str], WorkspaceProvider] | None" = None,
    ) -> None:
        self._path = Path(path).expanduser() if path else Path.home() / ".nontainer"
        self._backend = backend
        self._provider_factory = provider_factory

    # ------------------------------------------------------------------
    # identity
    # ------------------------------------------------------------------

    @property
    def path(self) -> Path:
        """The store directory. Layout underneath is the backend's."""
        return self._path

    @property
    def backend(self) -> str:
        return self._backend

    def __repr__(self) -> str:
        if self._provider_factory is not None:
            return f"Store(provider_factory={self._provider_factory!r})"
        return f"Store({str(self._path)!r}, backend={self._backend!r})"

    # ------------------------------------------------------------------
    # sessions
    # ------------------------------------------------------------------

    def open(
        self,
        session: str,
        *,
        python: "PythonConfig | None" = None,
        mounts: "Mapping[str, Mount] | None" = None,
        commands: Mapping[str, Callable[..., Any]] | None = None,
        cache: bool = True,
        autocheckpoint: bool = True,
        max_observation: int = 32_000,
        executor_factory: "Callable[[], Executor] | None" = None,
        root: str = "/workspace",
    ) -> "Workspace":
        """Open (or create) a session's :class:`Workspace`.

        A new session id starts an empty one; an existing id resumes
        it. ``session`` is validated against ``SESSION_ID_RE`` unless
        this store was built with a ``provider_factory``, which brings
        its own naming rules.

        ``executor_factory`` selects the execution backend for this
        session and every fork of it (default: the in-process
        ``LocalExecutor``). Pass ``lambda: DudExecutor()`` to run on a
        real machine — see ``nontainer.executor_dud`` and its ``[dud]``
        extra.

        ``root`` is the workspace root — the absolute VFS path agent
        code sees its files under (default ``/workspace``). One value
        per session, inherited by forks.
        """
        from .workspace import Workspace

        ws = Workspace(
            self._session_provider(session),
            python=python,
            mounts=mounts,
            commands=commands,
            cache=cache,
            autocheckpoint=autocheckpoint,
            max_observation=max_observation,
            executor_factory=executor_factory,
            root=root,
        )
        ws._store = self
        return ws

    def sessions(self) -> list[str]:
        """Every session id present on the store, sorted.

        Reserved names are not sessions and never appear: kvgit's
        ``refs/tags/`` tag refs, anything under the ``@`` store
        namespace, and the ``__void__`` anchor branch older versions
        minted during teardown. Anything else that is shaped like a
        session id is one — including branches a caller made itself
        (a published snapshot, say), because from the store's side
        that is exactly what they are.
        """
        self._require_own_layout("sessions")
        base = self._path
        if self._backend == "kvgit":
            names: Iterable[str] = self._branches()
        elif self._backend == "dir":
            names = (p.name for p in base.iterdir()) if base.is_dir() else ()
        elif self._backend == "agentfs":
            names = (p.stem for p in base.glob("*.db")) if base.is_dir() else ()
        else:
            raise ValueError(f"Unknown backend: {self._backend!r}")
        return sorted(n for n in names if self._is_session_name(n))

    def exists(self, session: str) -> bool:
        """Whether this session has state on the store. A session that
        was never opened, or one that was deleted, is absent — opening
        it would create it."""
        return session in set(self.sessions())

    def delete(self, sessions: str | Iterable[str], *, min_age: float = 3600) -> None:
        """Delete one or more sessions' entire stored state.

        The teardown counterpart to :meth:`open`, resolving the same
        per-backend layout:

        - ``"kvgit"``: deletes the named branches from the shared store
          at ``<path>/kvgit``, and with each branch the session-scoped
          tags it owns (everything under ``<session>/``). Store-scoped
          tags are left alone: that scope exists so a publication can
          outlive the session that made it, and its checkpoints stay
          reachable through the tag even once every branch that reached
          them is gone.
        - ``"dir"``: removes the ``<path>/<session>/`` directory trees.
        - ``"agentfs"``: unlinks the ``<path>/<session>.db`` files.

        Plural because a caller often owns more than one branch/dir/db
        per logical session (an app that publishes snapshot branches, a
        batch cleanup). ``sessions`` may be a single id or any iterable
        of ids. Deleting a name that doesn't exist is a no-op; so is
        deleting from a store that was never created — teardown is
        idempotent.

        ``min_age`` is the orphan sweep's grace period in seconds
        (kvgit only): commits younger than this are left alone, so a
        concurrent writer mid-commit is never swept out from under.
        The other backends delete whole files and have nothing to
        sweep.

        This is a store-level operation, not a live-session one: close
        any open :class:`Workspace` on these sessions first (a kvgit
        store handle pins its branch). It does not touch anything a
        caller keeps *beside* the workspace store (an app's own db
        files, transcripts) — that bookkeeping is the caller's.
        """
        self._require_own_layout("delete")
        names = {sessions} if isinstance(sessions, str) else set(sessions)
        if self._backend == "dir":
            from .providers.dir import DirProvider

            DirProvider.delete(self._path, names)
        elif self._backend == "kvgit":
            from .providers.kvgit import KvgitProvider

            KvgitProvider.delete(self._path / "kvgit", names, min_age=min_age)
        elif self._backend == "agentfs":
            from .providers.agentfs import AgentFSProvider

            AgentFSProvider.delete(self._path, names)
        else:
            raise ValueError(f"Unknown backend: {self._backend!r}")

    def clean(self, *, min_age: float = 3600) -> int:
        """Sweep storage nothing reaches any more; returns how many
        commits were removed.

        A checkpoint stays alive while a branch head or a tag reaches
        it. Rolling back, restoring, or deleting a tag can leave
        commits behind that nothing reaches — deletion sweeps as it
        goes, but a long-lived store accumulates them anyway. This is
        the standalone sweep.

        ``min_age`` (seconds) is a grace period: commits younger than
        it are left alone, so a concurrent writer mid-commit is never
        swept out from under. Backends that store a session as a whole
        file have nothing to sweep and return 0.
        """
        self._require_own_layout("clean")
        if self._backend != "kvgit":
            return 0
        p = self._kvgit_path()
        if not p.is_dir():
            return 0
        from kvgit.kv.disk import Disk
        from kvgit.versioned.kv import clean_orphans

        backend = Disk(str(p))
        try:
            return clean_orphans(backend, min_age=min_age)
        finally:
            self._close_backend(backend)

    def resolve(self, ref: "str | Ref") -> "Workspace":
        """A frozen workspace at the exact commit a ref names.

        ``ref`` is ``session@commit`` (see :class:`Ref`). Reads see
        that commit's files, cache and cwd; nothing can be written or
        committed. Close it when done — it holds an executor and a
        store handle of its own.

        The session must exist: resolving a ref into a session the
        store has never held raises rather than creating the branch.
        """
        self._require_own_layout("resolve")
        parsed = Ref.parse(ref)
        if self._backend != "kvgit":
            raise NotSupportedError(
                f"The {self._backend!r} backend is not versioned, so it has no "
                "commits to resolve a ref against. Use the kvgit backend."
            )
        if not self.exists(parsed.session):
            raise WorkspaceError(
                f"No such session on this store: {parsed.session!r} "
                f"(resolving {str(parsed)!r})"
            )
        return self._frozen_workspace(
            self._provider_at_commit(parsed.session, parsed.commit)
        )

    # ------------------------------------------------------------------
    # store-scoped tags
    # ------------------------------------------------------------------

    @property
    def tags(self) -> StoreTags:
        """Store-scoped tags — see :class:`StoreTags`."""
        self._require_own_layout("tags")
        if self._backend != "kvgit":
            raise NotSupportedError(
                f"The {self._backend!r} backend has no tags. Use the kvgit "
                "backend for named checkpoints."
            )
        return StoreTags(self)

    # ------------------------------------------------------------------
    # not yet: the shared plane and publication (see scratch/api-v2.md)
    # ------------------------------------------------------------------

    def shared(self, name: str) -> "Workspace":
        """The shared plane: a multi-writer workspace on
        ``@store/shared/<name>``, visible to every session.

        Planned; see the api-v2 spec. Not implemented.
        """
        raise NotImplementedError(
            "Store.shared is not implemented yet — see the api-v2 spec for "
            "the shared plane."
        )

    def publish(self, ws: "Workspace", name: str, **kwargs: Any) -> Any:
        """Derive a rootless, store-tagged commit holding a subset of a
        session's files — what an app serves, independent of the
        session that built it.

        Planned; see the api-v2 spec. Not implemented.
        """
        raise NotImplementedError(
            "Store.publish is not implemented yet — see the api-v2 spec for "
            "publications."
        )

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Release store-level resources.

        A ``Store`` holds no long-lived handle today: every verb opens
        what it needs and hands ownership to the workspace it returns,
        or closes it before returning. The method exists so callers can
        write the symmetric shape now (and so ``with`` works) rather
        than adding it to every call site later.
        """

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _require_own_layout(self, op: str) -> None:
        """Refuse a store-level verb when a ``provider_factory`` owns
        session resolution. The factory decides where state lives, so
        this store cannot list, delete or sweep it."""
        if self._provider_factory is not None:
            raise NotSupportedError(
                f"{op}() needs the store's own layout, but this Store was "
                "built with a provider_factory, which owns where sessions "
                "live. Do it through that substrate."
            )

    @staticmethod
    def _is_session_name(name: str) -> bool:
        if name in _RESERVED_BRANCHES:
            return False
        if name.startswith(_RESERVED_BRANCH_PREFIXES):
            return False
        return bool(SESSION_ID_RE.match(name))

    def _session_provider(self, session: str) -> WorkspaceProvider:
        """The provider for one session — the resolution
        :func:`nontainer.workspace` used to do inline."""
        if self._provider_factory is not None:
            return self._provider_factory(session)
        validate_session_id(session)
        if self._backend == "dir":
            from .providers.dir import DirProvider

            return DirProvider(self._path / session, session=session)
        if self._backend == "kvgit":
            from .providers.kvgit import KvgitProvider

            return KvgitProvider.open(self._kvgit_path(), session=session)
        if self._backend == "agentfs":
            from .providers.agentfs import AgentFSProvider

            return AgentFSProvider(self._path / f"{session}.db", session=session)
        raise ValueError(f"Unknown backend: {self._backend!r}")

    def _frozen_workspace(self, provider: WorkspaceProvider) -> "Workspace":
        from .workspace import Workspace

        ws = Workspace(provider)
        ws._store = self
        return ws

    # -- kvgit specifics -------------------------------------------------
    #
    # The store layer already knows each backend's layout (it is what
    # ``open`` and ``delete`` dispatch on), so the handle-free admin
    # reads live here rather than on the session-scoped provider.

    def _kvgit_path(self) -> Path:
        return self._path / "kvgit"

    @staticmethod
    def _close_backend(backend: Any) -> None:
        close = getattr(backend, "close", None)
        if callable(close):
            close()

    def _branches(self) -> list[str]:
        p = self._kvgit_path()
        if not p.is_dir():
            return []  # never-materialized store: no branches
        from kvgit.kv.disk import Disk
        from kvgit.versioned.kv import VersionedKV

        backend = Disk(str(p))
        try:
            return list(VersionedKV.branches(backend))
        finally:
            self._close_backend(backend)

    @staticmethod
    def _store_tag_prefix() -> str:
        from .providers.kvgit import _STORE_PREFIX

        return _STORE_PREFIX

    def _raw_tags(self) -> dict[str, str]:
        """Every tag in the kvgit store, stored (prefixed) names and
        all. Read without a branch handle: opening one by name would
        create that branch."""
        p = self._kvgit_path()
        if not p.is_dir():
            return {}
        from kvgit.kv.disk import Disk
        from kvgit.versioned.kv import tags as raw_tags

        backend = Disk(str(p))
        try:
            return dict(raw_tags(backend))
        finally:
            self._close_backend(backend)

    def _raw_tag_info(self, name: str) -> TagInfo | None:
        p = self._kvgit_path()
        if not p.is_dir():
            return None
        from kvgit.encoding import safe_loads
        from kvgit.kv.disk import Disk
        from kvgit.versioned.kv import tag_info as raw_tag_info

        stored = self._scoped_store_tag(name)
        backend = Disk(str(p))
        try:
            info = raw_tag_info(backend, stored)
            if info is None:
                return None
            # The commit's keyset root hash, read off the store key that
            # holds it — the same raw read KvgitProvider makes for a
            # checkpoint's tree, since kvgit has no public accessor.
            raw = backend.get(f"__commit_root__{info.commit}")
            root = safe_loads(raw) if raw is not None else None
            return TagInfo(
                name=name,
                scope="store",
                id=info.commit,
                tree=root if isinstance(root, str) else None,
                time=info.time,
                info=info.info,
                dangling=info.dangling,
            )
        finally:
            self._close_backend(backend)

    def _delete_store_tag(self, name: str) -> None:
        from .errors import CheckpointNotFoundError

        stored = self._scoped_store_tag(name)
        if self._raw_tag_info(name) is None:
            raise CheckpointNotFoundError(f"No such tag: {name!r} in scope 'store'")
        import kvgit

        kvgit.delete_tags([stored], kind="disk", path=str(self._kvgit_path()))

    def _scoped_store_tag(self, name: str) -> str:
        """The stored name for a caller's store-scoped tag, with the
        same rules the provider applies: non-empty, no ``%``, and not
        already carrying the scope prefix."""
        prefix = self._store_tag_prefix()
        if not isinstance(name, str) or not name:
            raise ValueError("Tag name must be a non-empty string")
        if "%" in name:
            raise ValueError(f"Tag name must not contain '%': {name!r}")
        if name.startswith(prefix):
            raise ValueError(
                f"Tag name {name!r} starts with the store scope prefix; pass "
                "the bare name"
            )
        return f"{prefix}{name}"

    def _anchor_session(self, op: str) -> str:
        """A live branch to open the store's kvgit handle on.

        kvgit has no handle without a branch, and opening one by a name
        that does not exist creates it — so a read at a commit or a
        store tag borrows an existing session's branch. Which one does
        not matter: a checkout is by commit or tag, both of which are
        store-wide.
        """
        names = self.sessions()
        if not names:
            raise WorkspaceError(
                f"{op} needs a session on the store to read through, and this "
                "store has none. Open a session first."
            )
        return names[0]

    def _provider_at_commit(self, session: str, commit: str) -> WorkspaceProvider:
        from .errors import CheckpointNotFoundError
        from .providers.kvgit import KvgitProvider

        provider = KvgitProvider.open(self._kvgit_path(), session=session)
        handle = provider._staged.checkout(commit)
        if handle is None:
            provider.close()
            raise CheckpointNotFoundError(commit)
        # The frozen provider and the handle it came from share one
        # backend, so closing the workspace this ends up in closes the
        # store exactly once. The opening provider is not closed here:
        # that would close the backend the caller is about to read.
        return KvgitProvider(handle, session=session, frozen_at=commit)

    def _provider_at_store_tag(self, name: str) -> WorkspaceProvider:
        from .providers.kvgit import KvgitProvider

        anchor = self._anchor_session(f"store.tags.at({name!r})")
        provider = KvgitProvider.open(self._kvgit_path(), session=anchor)
        try:
            frozen = provider.at_tag(name, scope="store")
        except BaseException:
            provider.close()
            raise
        # Shares a backend with the provider it came from, which is
        # therefore not closed here (see _provider_at_commit).
        return frozen


def store(
    path: str | Path | None = None,
    *,
    backend: Backend = "kvgit",
    provider_factory: "Callable[[str], WorkspaceProvider] | None" = None,
) -> Store:
    """Build a :class:`Store` (sugar, matching ``nontainer.workspace``)."""
    return Store(path, backend=backend, provider_factory=provider_factory)
