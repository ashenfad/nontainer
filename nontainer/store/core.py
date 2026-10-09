"""Store: what outlives a session.

A :class:`Workspace` is one session's world. A :class:`Store` is the
place those sessions live in — the kvgit store, the directory of
per-session trees, the folder of AgentFS db files — and it owns the
verbs that are about the *set* of sessions rather than about any one
of them: opening and listing them, forking one into another,
resolving a ref to a frozen state, deleting one, sweeping storage
nothing reaches any more, the store-scoped tags that deliberately
survive the session that made them, and the publications
(``publish``/``unpublish``) an app is served from.

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

import os
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..errors import NotSupportedError, WorkspaceError
from ..protocol import (
    SESSION_ID_RE,
    SHORT_ID_RE,
    TagInfo,
    WorkspaceProvider,
    expand_commit,
    validate_session_id,
)
from .layout import (
    _ANCHOR_BRANCH,
    _PUB_BRANCH_PREFIX,
    _RESERVED_BRANCH_PREFIXES,
    _RESERVED_BRANCHES,
    KV_ENV,
    Backend,
    _backend_for,
    _check_frozen_mounts,
    _check_frozen_settings,
    _disk_backend,
)
from .publications import _Publishing
from .refs import Ref, StoreTags

if TYPE_CHECKING:
    from ..migrate import LayoutMigration
    from ..protocol import Executor
    from ..workspace import Mount, Profile, PythonConfig, Workspace


class Store(_Publishing):
    """Where sessions live, and everything that outlives one of them.

    Args:
        path: The store directory. ``None`` means ``~/.nontainer``,
            the same default :func:`nontainer.workspace` has always
            had. Each backend lays out its own tree underneath: kvgit
            keeps one shared store at ``<path>/kvgit`` with a branch
            per session, and ``agentfs`` keeps ``<path>/<session>.db``.
        backend: Which substrate the sessions live on.
        kv: Where a ``"kvgit"`` store keeps its data: a kvgit
            ``KVStore`` (``kvgit.kv.postgres.Postgres(...)``, say), or a
            PostgreSQL URL (``postgresql://...``; needs the ``postgres``
            extra). ``None`` reads the ``NONTAINER_KV`` environment
            variable, and with that unset keeps the data on disk under
            ``<path>/kvgit``. A PostgreSQL URL's table is
            ``NONTAINER_KV_TABLE`` (default ``kvgit``). The store opens
            the backend once, on first use, and every session and verb
            shares it. :meth:`close` closes a backend the store built
            (from a URL, or on disk); a ``KVStore`` passed here stays
            yours to close. ``path`` still holds the publication
            registry.
        provider_factory: Bring your own substrate — a callable taking
            a session id and returning a
            :class:`~nontainer.protocol.WorkspaceProvider`. When given
            it replaces ``backend``/``path`` for :meth:`open`, and the
            store-level verbs (:meth:`sessions`, :meth:`delete`,
            :meth:`clean`, :attr:`tags`) refuse: the layout is the
            factory's business, and guessing it would be worse than
            saying so.
        memory: Keep everything in this process's memory and nothing on
            disk: the sessions, their history, and the publication
            registry. The data lives as long as this ``Store`` object
            does; :meth:`close` releases the repository handle but not
            the data. For scratch worlds and tests. ``path``, ``kv`` and
            ``provider_factory`` each say where state lives, so each is
            refused alongside it, and only the ``"kvgit"`` backend has a
            memory form. ``NONTAINER_KV`` is not consulted.
    """

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        backend: Backend = "kvgit",
        kv: Any = None,
        provider_factory: "Callable[[str], WorkspaceProvider] | None" = None,
        memory: bool = False,
    ) -> None:
        if memory:
            given = [
                name
                for name, value in (
                    ("path", path),
                    ("kv", kv),
                    ("provider_factory", provider_factory),
                )
                if value is not None
            ]
            if given:
                raise ValueError(
                    f"memory=True keeps state in this process, so {', '.join(given)} "
                    "has nowhere to apply: pass one or the other"
                )
            if backend != "kvgit":
                raise ValueError(
                    f"memory=True needs the 'kvgit' backend, not {backend!r}: "
                    "agentfs keeps each session in a file"
                )
        self._memory = memory
        # The in-memory backend, built on first use and kept for the
        # Store's lifetime, so closing the repository (which a Memory
        # backend survives) and reopening it finds the same data.
        self._memory_backend: Any = None
        self._path: Path | None = (
            None
            if memory
            else Path(path).expanduser()
            if path
            else Path.home() / ".nontainer"
        )
        if backend == "dir":
            raise ValueError(
                "the 'dir' backend was removed in nontainer 0.8.3: use the "
                "default 'kvgit' backend (on disk, or PostgreSQL with kv=), "
                "or 'agentfs'"
            )
        self._backend = backend
        self._provider_factory = provider_factory
        self._kv = kv
        # The one kvgit repository every session and verb of this store
        # shares, opened on first use: one pool of connections to a
        # networked backend rather than one per call.
        self._kvgit: Any = None
        self._kvgit_lock = threading.Lock()
        # Whether the store built the backend under that repository (from
        # a URL, or on disk) and so closes it. A KVStore handed in as
        # ``kv=`` stays the caller's.
        self._owns_backend = False
        # The publication registry for a store that has no directory of
        # its own: a provider_factory decides where state lives, and a
        # memory store has no directory, so there is nowhere to put the
        # file. It then lives for as long as this Store object does.
        # Every other store keeps it in ``<path>/publications.json``.
        self._memory_registry: dict[str, Any] = {}
        # ...and since nothing else can reach that dict, its
        # read-modify-write serializes on a lock of this object's own.
        self._memory_registry_lock = threading.Lock()

    # ------------------------------------------------------------------
    # identity
    # ------------------------------------------------------------------

    @property
    def path(self) -> Path | None:
        """The store directory. Layout underneath is the backend's.
        ``None`` for a memory store, which has none."""
        return self._path

    @property
    def memory(self) -> bool:
        """Whether this store keeps everything in memory (see the
        ``memory`` argument)."""
        return self._memory

    @property
    def backend(self) -> str:
        return self._backend

    @property
    def repo(self) -> Any:
        """The kvgit ``Repo`` this store's sessions live in (host-side
        power tool): opened on first use, shared by every session and
        verb of this store, and closed by :meth:`close`. kvgit stores
        only."""
        self._require_own_layout("repo")
        self._require_kvgit("repo")
        return self._kvgit_repo(create=True)

    def __repr__(self) -> str:
        if self._provider_factory is not None:
            return f"Store(provider_factory={self._provider_factory!r})"
        if self._memory:
            return "Store(memory=True)"
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
        autocommit: bool = True,
        max_observation: int = 32_000,
        executor_factory: "Callable[[], Executor] | None" = None,
        root: str | None = None,
        ignore: "Iterable[str] | None" = None,
        profile: "Profile | None" = None,
    ) -> "Workspace":
        """Open (or create) a session's :class:`Workspace`.

        A new session id starts an empty one; an existing id resumes
        it. ``session`` is validated against ``SESSION_ID_RE`` unless
        this store was built with a ``provider_factory``, which brings
        its own naming rules.

        ``profile`` is the session's profile as one value
        (:class:`~nontainer.workspace.Profile`): it stands in for
        ``python``, ``mounts``, ``commands``, ``executor_factory``,
        ``root`` and ``ignore``, and passing it with any of them is
        refused. Its ``variables`` are applied to ``ws.runtime.env``.

        ``executor_factory`` selects the execution backend for this
        session and every fork of it (default: the in-process
        ``LocalExecutor``). Pass ``lambda: DudExecutor()`` to run on a
        real machine — see ``nontainer.executor_dud`` and its ``[dud]``
        extra.

        ``root`` is the workspace root — the absolute VFS path agent
        code sees its files under (default ``/workspace``). One value
        per session, inherited by forks.

        ``ignore`` is the embedder's ``.gitignore``: patterns for what
        is never work, besides everything outside the root (see
        :mod:`nontainer.ignore`). Inherited by forks.

        A session whose head was written before monkeyfs 0.1.10 is
        refused with :class:`~nontainer.errors.LegacyLayoutError`, which
        names the :meth:`migrate_layout` call that fixes it.
        """
        from ..errors import LegacyLayoutError
        from ..workspace import Workspace, _profile_fields

        fields = _profile_fields(
            profile,
            python=python,
            mounts=mounts,
            commands=commands,
            executor_factory=executor_factory,
            root=root,
            ignore=ignore,
        )
        provider = self._session_provider(session)
        try:
            ws = Workspace(
                provider,
                cache=cache,
                autocommit=autocommit,
                max_observation=max_observation,
                **fields,
            )
        except LegacyLayoutError as e:
            provider.close()
            raise e.for_store(self) from None
        ws._store = self
        if profile is not None:
            ws.runtime.env.update(profile.variables)
        return ws

    def fork(
        self,
        src: str,
        dst: str,
        *,
        at: str | None = None,
        inherit: str = "full",
        paths: "Iterable[str] | str | None" = None,
        **open_kwargs: Any,
    ) -> "Workspace":
        """Branch ``src`` into a new session ``dst`` and open it.

        :meth:`Workspace.fork` for host code that has no workspace open
        — a scheduler seeding a delegate, a script branching a session
        it never has to run. Same semantics, keyword for keyword (a
        fork point is a commit; ``inherit`` decides the conversation;
        ``paths`` narrows the view), and the same refusal on a
        substrate that cannot fork cheaply.

        The source is opened only to fork it and is closed again, so
        nothing is left holding it. ``open_kwargs`` are
        :meth:`open`'s, applied to the workspace this returns.

        A lineage shares one workspace root, so ``root`` (when given,
        directly or as ``profile.root``) opens the SOURCE too: ``paths`` is
        normalized against the root the child will use, and a view
        recorded against some other root would name paths the child
        cannot see.
        """
        from ..workspace import _profile_fields

        # A profile passed with one of its own fields is refused here,
        # before the destination branch exists: the final open refuses
        # it too, but by then the fork would already be on the store.
        fields = _profile_fields(
            open_kwargs.get("profile"),
            **{
                name: open_kwargs.get(name)
                for name in (
                    "python",
                    "mounts",
                    "commands",
                    "executor_factory",
                    "root",
                    "ignore",
                )
            },
        )
        source = self.open(src, root=fields["root"])
        try:
            child = source.fork(dst, at=at, inherit=inherit, paths=paths)
            # Its provider shares the source's store handle, which goes
            # away with the source; the branch is what matters, and it
            # is reopened below on a handle of its own.
            child.close()
        finally:
            source.close()
        return self.open(dst, **open_kwargs)

    def sessions(self) -> list[str]:
        """Every session id present on the store, sorted.

        Reserved names are not sessions and never appear: kvgit's
        ``refs/tags/`` tag refs, anything under the ``@`` store
        namespace (a publication's branch, the store's own read
        anchor), and the ``__void__`` branch older versions minted
        during teardown. Anything else that is shaped like a
        session id is one — including branches a caller made itself
        (a published snapshot, say), because from the store's side
        that is exactly what they are.
        """
        self._require_own_layout("sessions")
        base = self._path
        if self._backend == "kvgit":
            names: Iterable[str] = self._branches()
        elif self._backend == "agentfs":
            # A session is one db FILE; anything else in the directory
            # (an embedder's own files, a stray note) is not a session.
            names = (
                (p.stem for p in base.glob("*.db") if p.is_file())
                if base.is_dir()
                else ()
            )
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
          outlive the session that made it, and its commits stay
          reachable through the tag even once every branch that reached
          them is gone.
        - ``"agentfs"``: unlinks the ``<path>/<session>.db`` files.

        Plural because a caller often owns more than one branch/dir/db
        per logical session (an app that publishes snapshot branches, a
        batch cleanup). ``sessions`` may be a single id or any iterable
        of ids. Deleting a name that doesn't exist is a no-op; so is
        deleting from a store that was never created — teardown is
        idempotent.

        **Sessions only.** Every name must be a session id, checked
        before any of them reaches the backend, so one rule covers
        every branch the store keeps for itself: no session id may
        begin with ``@``, so nothing under ``@store/`` — a
        publication's branch, the read anchor — can be named here. A
        publication is removed with :meth:`unpublish`, which drops its
        tag and its branch together and keeps the registry in step.

        ``min_age`` is the orphan sweep's grace period in seconds
        (kvgit only): commits younger than this are left alone, so a
        concurrent writer mid-commit is never swept out from under.
        The other backends delete whole files and have nothing to
        sweep.

        This is a store-level operation, not a live-session one: close
        any open :class:`Workspace` on these sessions first. Nothing
        stops a delete under an open one, and that workspace's next
        commit then fails — its branch is gone. It does not touch
        anything a caller keeps *beside* the workspace store (an app's
        own db files, transcripts) — that bookkeeping is the caller's.
        """
        self._require_own_layout("delete")
        names = {sessions} if isinstance(sessions, str) else set(sessions)
        self._require_session_names(names)
        if self._backend == "kvgit":
            from ..providers.kvgit import KvgitProvider

            repo = self._kvgit_repo()
            if repo is not None:
                KvgitProvider.delete_in(repo, names, min_age=min_age)
        elif self._backend == "agentfs":
            from ..providers.agentfs import AgentFSProvider

            AgentFSProvider.delete(self._path, names)
        else:
            raise ValueError(f"Unknown backend: {self._backend!r}")

    def clean(self, *, min_age: float = 3600) -> int:
        """Sweep storage nothing reaches any more; returns how many
        commits were removed.

        A commit stays alive while a branch head or a tag reaches it,
        and a session's own history is append-only — a checkout appends
        a restore rather than moving the head back, so it strands
        nothing. What does: deleting a session or
        a tag. Both sweep as they go, but their grace period spares
        commits younger than it and a long-lived store accumulates
        what they left. This is the standalone sweep.

        ``min_age`` (seconds) is a grace period: commits younger than
        it are left alone, so a concurrent writer mid-commit is never
        swept out from under. Backends that store a session as a whole
        file have nothing to sweep and return 0.
        """
        self._require_own_layout("clean")
        if self._backend != "kvgit":
            return 0
        with self._repo() as repo:
            if repo is None:
                return 0
            return repo.gc(min_age=min_age)

    def migrate_layout(
        self,
        sessions: "str | Iterable[str] | None" = None,
        *,
        dry_run: bool = False,
    ) -> "dict[str, LayoutMigration]":
        """Rewrite session heads written before monkeyfs 0.1.10 into the
        current layout; returns a report per session examined.

        Such a head keeps every file's metadata in one
        ``__vfs_metadata__`` table and its working directory under
        ``__cwd__``, and a writable open refuses it. Migrating one is a
        single commit on its branch: each table entry becomes the
        file's own metadata row (a row already there wins), the old cwd
        moves into the filesystem's slot when that is empty, and both
        keys are deleted. A head in the current layout is left alone
        and costs no commit, so running this twice is running it once.
        See :mod:`nontainer.migrate` for the rules.

        ``sessions`` names the sessions to migrate — one id or several;
        each must be a session this store holds. ``None`` is every
        session :meth:`sessions` lists. ``dry_run`` computes the same
        reports and writes nothing.

        What is never rewritten: publication branches, tags, and old
        commits in any session's history. A tag and a publication name
        one frozen state that something may be serving, and history is
        append-only. They keep reading right, because monkeyfs still
        reads the table for any path without a row, and whatever brings
        an old commit's state into a live head converts it on the way
        (a restore, a fork from it, a revert or cherry-pick of it).

        Close any workspace open on these sessions first, as for
        :meth:`delete`. The ``agentfs`` backend keeps no table, so
        there only the cwd key moves, with no commit to name.

        Returns ``{session: LayoutMigration}`` for every session
        examined, clean ones included (``report.clean``).
        """
        from ..migrate import migrate_provider

        self._require_own_layout("migrate_layout")
        held = set(self.sessions())
        if sessions is None:
            names = sorted(held)
        else:
            names = sorted({sessions} if isinstance(sessions, str) else set(sessions))
            self._require_session_names(names)
            missing = [name for name in names if name not in held]
            if missing:
                raise ValueError(
                    f"no session {', '.join(repr(n) for n in missing)} on this "
                    "store: migrate_layout rewrites sessions that exist, and "
                    "opening one that does not would create it"
                )
        reports: dict[str, LayoutMigration] = {}
        for name in names:
            provider = self._session_provider(name)
            try:
                reports[name] = migrate_provider(provider, dry_run=dry_run)
            finally:
                provider.close()
        return reports

    def resolve(
        self, ref: "str | Ref", *, root: str | None = None, **settings: Any
    ) -> "Workspace":
        """A frozen workspace at the exact commit a ref names.

        ``ref`` is ``session@commit`` (see :class:`Ref`). Reads see
        that commit's files, cache and cwd; nothing can be written or
        committed. Close it when done — it holds an executor and a
        store handle of its own.

        ``root`` is the workspace root to read the commit under, for a
        session that was not opened at the default one. A commit holds
        files at whatever root the session that made them used, so
        resolving at the wrong root reads an empty tree; the caller
        that knows the lineage passes its own.

        The session must exist: resolving a ref into a session the
        store has never held raises rather than creating the branch.
        A ref naming a STORE TAG (``ref.tag``) needs no session at
        all — the tag names the commit and holds it against collection,
        so it opens the way :meth:`StoreTags.at` does, for as long as
        the tag exists — and it must still name the commit the ref
        carries. Tags never move, so the two halves disagree only where
        the name was deleted and added again, which is a different
        state under the same word; that is refused rather than read.

        ``settings`` are :meth:`open`'s remaining construction keywords
        — ``python``, ``mounts``, ``commands``, ``cache``,
        ``max_observation``, ``executor_factory`` — applied to the
        workspace this returns. A commit holds files, never the live
        objects a handler calls, so an embedder that runs code against
        a resolved state supplies them here.
        """
        self._require_own_layout("resolve")
        _check_frozen_settings("Store.resolve()", settings)
        parsed = Ref.parse(ref)
        if self._backend != "kvgit":
            raise NotSupportedError(
                f"The {self._backend!r} backend is not versioned, so it has no "
                "commits to resolve a ref against. Use the kvgit backend."
            )
        if parsed.tag is not None:
            self._require_tag_at(parsed.tag, parsed)
            return self._frozen_workspace(
                self._provider_at_store_tag(parsed.tag), root=root, **settings
            )
        if not self._is_publication_branch(parsed.session):
            if parsed.session not in set(self._branches()):
                raise WorkspaceError(
                    f"No such session on this store: {parsed.session!r} "
                    f"(resolving {str(parsed)!r})"
                )
        # A publication's ref names its own reserved branch, which is
        # deliberately not a session and never appears in sessions().
        # The registry is what says whether that version is still
        # published; the branch outliving it would not make it servable
        # again. The check and the open share the registry lock, so an
        # unpublish cannot land between them. The workspace is built
        # after: it holds a handle checked out at the commit, which is
        # pinned by the version's tag for the sweep's grace period and
        # names no branch, so nothing it does later can mint one.
        with self._ref_source_held(parsed, "resolve") as source:
            provider = self._provider_at_commit(source.session, source.commit)
        return self._frozen_workspace(provider, root=root, **settings)

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
                "backend for named commits."
            )
        return StoreTags(self)

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Release store-level resources: the kvgit repository every
        session of this store shares, and with it — when the store built
        it, from a URL or on disk — the backend's connections. A
        ``KVStore`` handed in as ``kv=`` is left open: it is the
        caller's to close.

        Close the workspaces this store opened first — they read and
        commit through that repository. That includes frozen ones being
        served (``pub.open()``, ``resolve``, ``tags.at``): each reads
        through this repository without holding it open, so on a
        pool-backed store, closing it under a request in flight fails
        that request. Stop serving, then close. The store can be used
        again afterwards; the repository reopens on its next use.
        """
        with self._kvgit_lock:
            repo, self._kvgit = self._kvgit, None
            owned = self._owns_backend
        if repo is not None and owned:
            repo.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _require_kvgit(self, op: str) -> None:
        """Refuse a verb that needs commits, branches and tags on a
        backend that has none of them."""
        if self._backend != "kvgit":
            raise NotSupportedError(
                f"{op}() needs a versioned store with branches and tags, and "
                f"the {self._backend!r} backend has none. Use the kvgit "
                "backend."
            )

    def _require_own_workspace(self, ws: "Workspace", op: str) -> None:
        """Refuse a workspace this store did not open.

        A store-level verb that writes does it through the workspace's
        OWN provider, so a workspace from somewhere else would write to
        that store and leave this one unchanged — which reads as a
        silent no-op. Such a call is refused instead.
        """
        owner = getattr(ws, "_store", None)
        if owner is self:
            return
        whose = (
            "was built straight from a provider, so no store owns it"
            if owner is None
            else f"belongs to {owner!r}"
        )
        raise WorkspaceError(
            f"Cannot {op} through workspace {ws.session!r}: it {whose}, not to "
            f"{self!r}. The write goes through the workspace's own provider, "
            "so this would write to that store and leave this one unchanged. "
            "Use that store, or name the commit by ref."
        )

    @staticmethod
    def _require_session_names(names: Iterable[str]) -> None:
        """Refuse a teardown of anything but sessions.

        Names are checked as a set before any of them reaches a
        backend, so a batch either deletes or does nothing — and the
        one rule covers every branch the store keeps for itself,
        because no session id may begin with ``@`` and the store's own
        branches all live under ``@store/``.
        """
        bad = sorted(
            n for n in names if not isinstance(n, str) or not SESSION_ID_RE.match(n)
        )
        if not bad:
            return
        named = ", ".join(repr(n) for n in bad)
        raise ValueError(
            f"Store.delete deletes sessions, and {named} is not a session id "
            f"(must match {SESSION_ID_RE.pattern}). The store's own branches "
            "live under '@store/', which no session id can reach: remove a "
            "publication with store.unpublish(name, version), and leave the "
            "read anchor where it is — it holds an empty commit and costs "
            "the store nothing."
        )

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

    @staticmethod
    def _is_publication_branch(name: str) -> bool:
        """A publication's own reserved branch, which is not a session:
        it fails session-id validation, ``sessions()`` never lists it,
        and the publication registry is what says it may be read."""
        return name.startswith(_PUB_BRANCH_PREFIX)

    @contextmanager
    def _ref_source_held(self, ref: Ref, op: str) -> "Iterator[Ref]":
        """Check the source a ref names, hold it still for the body, and
        hand the body that ref with its commit spelled whole.

        A publication's reserved branch is checked under the registry
        lock, and the caller opens it in the body, so the check and
        the open are one critical section. ``unpublish`` deletes the
        row, the tag and the branch under that same lock; a check that
        passed and an open that followed on either side of it would
        mint the reserved branch again, because kvgit creates a branch
        opened by a name it does not know — and a reserved branch no
        record names refuses to let that version be published again.

        The expansion comes first, and for a publication inside that
        same lock: a ref carries what a caller was shown, which may be
        seven characters of a commit id, while the registry holds whole
        ones — an exact comparison made before the expansion reads a
        published version as unpublished.

        A ref naming a session takes no lock: a session's branch is
        not the registry's to remove.
        """
        if not self._is_publication_branch(ref.session):
            yield self._expanded_ref(ref)
            return
        self._require_own_layout(op)
        self._require_kvgit(op)
        with self._registry_locked():
            whole = self._expanded_ref(ref)
            self._require_registered(whole)
            yield whole

    def _expanded_ref(self, ref: Ref) -> Ref:
        """A ref whose commit is spelled in full.

        Every spelling nontainer prints is one it accepts back, and what
        ws-git prints is seven characters of a commit id — so a ref is
        made whole before anything compares it with what the store
        holds. A commit already whole, or a substrate that keeps no
        commits, costs no open.

        A branch that is not there expands nothing and the ref comes
        back as it went in: opening one by a name kvgit does not know
        would mint it, and the check that follows is what says why the
        branch is missing.
        """
        if self._backend != "kvgit" or self._provider_factory is not None:
            return ref
        if not SHORT_ID_RE.fullmatch(ref.commit):
            return ref
        if ref.session not in set(self._branches()):
            return ref
        return Ref(ref.session, self._expand_commit(ref.session, ref.commit), ref.path)

    @contextmanager
    def _ref_provider(
        self, ref: Ref, op: str
    ) -> "Iterator[tuple[WorkspaceProvider, Ref]]":
        """A provider open on the branch a ref names, and that ref with
        its commit spelled whole, for the body.

        Two kinds of branch answer to a ref. A session opens through
        the store's own session resolution. A publication's own
        reserved branch opens the way :meth:`resolve` reads one: the
        registry says whether that version is still published, and the
        branch is opened only when it is already there. The handle
        that comes back reads — a publication's branch takes no
        commits of its own — which is all a caller naming a commit by
        id needs.

        A context manager because a publication's source is held still
        for as long as the body runs: whatever the body does with the
        handle happens on a version the registry still holds.
        """
        with self._ref_source_held(ref, op) as source:
            if not self._is_publication_branch(source.session):
                yield self._session_provider(source.session), source
                return
            self._require_branch(source.session, op)
            from ..providers.kvgit import KvgitProvider

            yield (
                KvgitProvider.on(self._kvgit_repo(create=True), source.session),
                source,
            )

    def _session_provider(self, session: str) -> WorkspaceProvider:
        """The provider for one session — the resolution
        :func:`nontainer.workspace` used to do inline."""
        if self._provider_factory is not None:
            return self._provider_factory(session)
        validate_session_id(session)
        if self._backend == "kvgit":
            from ..providers.kvgit import KvgitProvider

            return KvgitProvider.on(self._kvgit_repo(create=True), session)
        if self._backend == "agentfs":
            from ..providers.agentfs import AgentFSProvider

            return AgentFSProvider(self._path / f"{session}.db", session=session)
        raise ValueError(f"Unknown backend: {self._backend!r}")

    def _frozen_workspace(
        self,
        provider: WorkspaceProvider,
        *,
        root: str | None = None,
        **settings: Any,
    ) -> "Workspace":
        """A frozen workspace over ``provider``, built with the
        embedder's execution settings.

        ``settings`` are the construction keywords the public frozen
        opens forward (see ``_FROZEN_SETTINGS``), already checked by
        the entry point the caller used — plus, from the publication
        open alone, an ``executor`` the store built itself: a caller
        may not name one (an executor instance is bound to one
        workspace), and a tree with nothing to run is given one that
        refuses. ``root`` is spelled as a named
        parameter so a caller can pass it either way and Python binds
        it here once — there is no second value to conflict with.

        A frozen workspace accepts no writes from anyone, so a mount
        with ``readonly=False`` is refused here rather than coerced:
        every frozen open funnels through this method, which is what
        makes the rule one rule instead of three. The mounted bytes
        still read, and reach the executor read-only like the rest of
        the tree.
        """
        from ..workspace import Workspace

        if root is not None:
            settings["root"] = root
        _check_frozen_mounts(settings.get("mounts"))
        ws = Workspace(provider, **settings)
        ws._store = self
        return ws

    # -- kvgit specifics -------------------------------------------------
    #
    # The store layer already knows each backend's layout (it is what
    # ``open`` and ``delete`` dispatch on), so the handle-free admin
    # reads live here rather than on the session-scoped provider.

    def _kvgit_path(self) -> Path:
        return self._path / "kvgit"

    def _kvgit_repo(self, *, create: bool = False) -> Any:
        """The kvgit repository this store's sessions share, opened on
        first use; ``None`` when the store keeps its data on disk and
        nothing was ever written there (unless ``create``).

        The backend is the store's own memory for a memory store, else
        ``kv=`` when given, else ``NONTAINER_KV``, else the store's
        ``kvgit`` directory. A ``Repo`` never writes by being opened and
        makes no branch by being read, so every admin read goes through
        here as well as every session.
        """
        if self._kvgit is not None:
            return self._kvgit
        with self._kvgit_lock:
            if self._kvgit is None and self._memory:
                from kvgit import Repo
                from kvgit.kv.memory import Memory

                if self._memory_backend is None:
                    self._memory_backend = Memory()
                self._owns_backend = True
                self._kvgit = Repo(self._memory_backend)
            if self._kvgit is None:
                kv = self._kv if self._kv is not None else os.environ.get(KV_ENV)
                self._owns_backend = not kv or isinstance(kv, str)
                if kv:
                    backend = _backend_for(kv)
                else:
                    backend = _disk_backend(self._kvgit_path(), create)
                    if backend is None:
                        return None
                from kvgit import Repo

                self._kvgit = Repo(backend)
            return self._kvgit

    @contextmanager
    def _repo(self, *, create: bool = False) -> "Iterator[Any | None]":
        """:meth:`_kvgit_repo` for a block. The repository is the
        store's and stays open after the block."""
        yield self._kvgit_repo(create=create)

    def _branches(self) -> list[str]:
        with self._repo() as repo:
            return [] if repo is None else list(repo.branches)

    @staticmethod
    def _store_tag_prefix() -> str:
        from ..providers.kvgit import _STORE_PREFIX

        return _STORE_PREFIX

    def _raw_tags(self, repo: Any = None) -> dict[str, str]:
        """Every tag in the kvgit store, stored (prefixed) names and
        all. Reads on ``repo`` when given one, else on an open of its
        own."""
        if repo is not None:
            return dict(repo.tags.items())
        with self._repo() as opened:
            return {} if opened is None else dict(opened.tags.items())

    def _raw_tag_info(self, name: str, repo: Any = None) -> TagInfo | None:
        """Describe one store-scoped tag. Reads on ``repo`` when given
        one, else on an open of its own."""
        if repo is None:
            with self._repo() as opened:
                if opened is None:
                    return None
                return self._raw_tag_info(name, opened)
        from kvgit import UnknownCommitError, UnknownTagError

        stored = self._scoped_store_tag(name)
        try:
            info = repo.tags.info(stored)
        except UnknownTagError:
            return None
        try:
            tree = None if info.dangling else repo.get_commit(info.commit).root
        except UnknownCommitError:
            tree = None
        return TagInfo(
            name=name,
            scope="store",
            id=info.commit,
            tree=tree,
            time=info.time,
            info=info.info,
            dangling=info.dangling,
        )

    def _raw_tag_infos(self) -> dict[str, TagInfo]:
        """Every store-scoped tag described, on one backend open."""
        prefix = self._store_tag_prefix()
        with self._repo() as repo:
            if repo is None:
                return {}
            described = {}
            for stored in self._raw_tags(repo):
                if not stored.startswith(prefix):
                    continue
                name = stored[len(prefix) :]
                info = self._raw_tag_info(name, repo)
                if info is not None:
                    described[name] = info
            return described

    def _delete_store_tag(self, name: str) -> None:
        from ..errors import CommitNotFoundError

        stored = self._scoped_store_tag(name)
        with self._repo() as repo:
            if repo is None or self._raw_tag_info(name, repo) is None:
                raise CommitNotFoundError(f"No such tag: {name!r} in scope 'store'")
            repo.tags.delete(stored)
            # Removing the tag is what makes its commits collectable;
            # the sweep takes them past its default grace period.
            repo.gc()

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
        store tag borrows an existing branch. Which one does not
        matter: a checkout is by commit or tag, both of which are
        store-wide.

        Sessions first, then a publication's own branch, then the
        store's own anchor. Borrowing before minting is why an ordinary
        store never grows a branch it has no use for: the anchor
        appears the first time a store-scoped read finds nothing else
        to read through, which is the case a store with no sessions and
        no publications left is in.
        """
        names = self.sessions()
        if names:
            return names[0]
        reserved = sorted(
            b for b in self._branches() if b.startswith(_PUB_BRANCH_PREFIX)
        )
        if reserved:
            return reserved[0]
        return self._ensure_anchor()

    def _ensure_anchor(self) -> str:
        """The store's own branch to read through, created if absent.

        A store-scoped tag is meant to outlive every session that could
        have made it, so the store must be able to read one with no
        session left — which means owning a branch rather than
        borrowing one. The anchor is that branch: reserved under
        ``@store/`` where no session id can spell it, absent from
        :meth:`sessions`, and holding a single commit with nothing in
        it — the same empty root a publication's branch starts from.

        It is permanent. ``delete`` refuses a name that is not a session
        id, and no session id begins with ``@``, so nothing can name it
        for teardown; nothing sweeps it, because a branch head is a GC
        root; and sparing it costs the store nothing, because the
        commit it holds owns no blobs.

        A store built with a ``provider_factory`` never gets here: the
        factory owns where state lives, so the store-level verbs refuse
        before there is an anchor to mint.
        """
        with self._repo(create=True) as repo:
            if _ANCHOR_BRANCH not in repo.branches:
                repo.branches.create(_ANCHOR_BRANCH)  # at the empty root
        return _ANCHOR_BRANCH

    def _require_registered(self, ref: Ref) -> None:
        """A publication ref names a version the registry still holds.

        Parsed back out of the branch name, since that is what a ref
        carries: ``@store/pub/<name>/<version>``.
        """
        name, _, version = ref.session[len(_PUB_BRANCH_PREFIX) :].partition("/")
        row = ((self._registry_read().get(name) or {}).get("versions") or {}).get(
            version
        )
        if row is None or Ref.parse(row["ref"]).commit != ref.commit:
            raise WorkspaceError(
                f"No longer published: {str(ref)!r} names version "
                f"{version!r} of {name!r}, which this store's publication "
                "registry does not hold."
            )

    def _require_branch(self, name: str, op: str) -> None:
        """Refuse to open a branch that is not there.

        kvgit CREATES a branch opened by a name it does not know, so
        every read that opens one by name has to check first — otherwise
        a stale ref resurrects the branch an unpublish deleted, and the
        leftover then blocks republishing that version.
        """
        if name in set(self._branches()):
            return
        raise WorkspaceError(
            f"{op}: no branch {name!r} on this store. Opening it would create "
            "it, so the read is refused instead."
        )

    def _provider_at_commit(self, session: str, commit: str) -> WorkspaceProvider:
        from kvgit import UnknownCommitError

        from ..errors import CommitNotFoundError
        from ..providers.kvgit import KvgitProvider

        self._require_branch(session, f"read at {session}@{commit}")
        repo = self._kvgit_repo(create=True)
        # What ws-git prints is seven characters of a commit id, and
        # every spelling nontainer prints is one it accepts back.
        commit = self._expand_commit(session, commit)
        try:
            snapshot = repo.snapshot(commit=commit)
        except UnknownCommitError:
            raise CommitNotFoundError(
                f"no commit {commit!r} on session {session!r} "
                f"(ws-git log {session} lists what it holds)"
            ) from None
        return KvgitProvider(
            repo, snapshot, session=session, frozen_at=commit, owns_repo=False
        )

    def _expand_commit(self, session: str, commit: str) -> str:
        """A commit id typed short, expanded against ``session``'s own
        line of history; anything else comes back unchanged (see
        :meth:`KvgitProvider.expand_commit`)."""
        if not isinstance(commit, str) or not SHORT_ID_RE.fullmatch(commit):
            return commit
        repo = self._kvgit_repo(create=True)
        return expand_commit(
            commit,
            (c.hash for c in repo.log(branch=session, first_parent=True)),
            where=f"on session {session!r}",
        )

    def _require_tag_at(self, tag: str, ref: Ref) -> None:
        """Refuse a tag ref the tag no longer answers for.

        A ref names one exact state, and a tag is a name that can be
        taken away: deleting one and adding it again is how a name is
        repointed, and what it names the second time is a different
        state under the same word. A ref minted against the first must
        not quietly open the second — it was kept precisely because it
        meant one commit.
        """
        held = self.tags.list().get(tag)
        if held is None:
            raise WorkspaceError(
                f"No such store tag: {tag!r} (resolving {str(ref)!r}). A "
                "tag that was deleted takes its name with it, so there is "
                "nothing here to read."
            )
        if held != ref.commit:
            raise WorkspaceError(
                f"Store tag {tag!r} names commit {held} now, and this ref "
                f"carries {ref.commit} (resolving {str(ref)!r}). Tags never "
                "move, so the name was deleted and added again: that is a "
                "different state under the same word, and a ref made against "
                "the first one is not a way to read it."
            )

    def _provider_at_store_tag(self, name: str) -> WorkspaceProvider:
        from kvgit import UnknownCommitError, UnknownTagError

        from ..errors import CommitNotFoundError
        from ..providers.kvgit import KvgitProvider

        # The snapshot itself needs no branch, but the workspace it ends
        # up in quotes a ref, ``<session>@<commit>``, and that ref has to
        # resolve: so the provider is labelled with a branch the store
        # holds — a session's, a publication's, or the store's own
        # anchor (see _anchor_session).
        session = self._anchor_session(f"store.tags.at({name!r})")
        stored = self._scoped_store_tag(name)
        repo = self._kvgit_repo()
        try:
            if repo is None:
                raise UnknownTagError(stored)
            snapshot = repo.snapshot(tag=stored)
        except (UnknownTagError, UnknownCommitError):
            raise CommitNotFoundError(
                f"No such tag: {name!r} in scope 'store'"
            ) from None
        return KvgitProvider(
            repo, snapshot, session=session, frozen_at=name, owns_repo=False
        )


def store(
    path: str | Path | None = None,
    *,
    backend: Backend = "kvgit",
    kv: Any = None,
    provider_factory: "Callable[[str], WorkspaceProvider] | None" = None,
    memory: bool = False,
) -> Store:
    """Build a :class:`Store` (sugar, matching ``nontainer.workspace``)."""
    return Store(
        path,
        backend=backend,
        kv=kv,
        provider_factory=provider_factory,
        memory=memory,
    )
