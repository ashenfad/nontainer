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

import json
import os
import re
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
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

# Where a publication's tree lives: one reserved branch per version,
# under the store's own "@" namespace so no session can spell it.
_PUB_BRANCH_PREFIX = "@store/pub/"
# The publication registry: the mutable half (which versions exist,
# which one is current) beside the immutable half (the tags).
_REGISTRY_FILE = "publications.json"
_REGISTRY_LOCK_FILE = "publications.lock"
_VERSION_NUMBER_RE = re.compile(r"^v(\d+)$")
# Keys publish() writes into the commit itself. A caller's ``info`` may
# not spell them: the commit is immutable and is where provenance is
# read from, so a false ``published_from`` would outlive every chance to
# notice it.
_RESERVED_INFO_KEYS = ("tool", "name", "version", "published_from")

# One lock per registry file per process. Two Store objects over the
# same path are two handles on one file, so the lock cannot live on
# either of them; the flock underneath covers other processes, and this
# covers the threads inside this one (flock is per open file
# description, so two threads flocking the same path do not exclude
# each other).
_REGISTRY_LOCKS: dict[str, threading.Lock] = {}
_REGISTRY_LOCKS_GUARD = threading.Lock()


def _registry_lock_for(path: Path) -> threading.Lock:
    key = str(path)
    with _REGISTRY_LOCKS_GUARD:
        lock = _REGISTRY_LOCKS.get(key)
        if lock is None:
            lock = _REGISTRY_LOCKS[key] = threading.Lock()
        return lock


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
        # Split at the LAST "@" of the pointer, not the first: a
        # reserved branch leads with one (``@store/pub/app/v1@abc``),
        # and a commit id never contains one. The path is cut first so
        # an "@" inside it cannot be mistaken for the separator.
        base, sep, path = text.partition(":")
        session, _, commit = base.rpartition("@")
        if not session or not commit:
            raise ValueError(
                f"Not a ref: {text!r} — both a session and a commit are required"
            )
        return cls(session=session, commit=commit, path=(path if sep else None) or None)

    def __str__(self) -> str:
        base = f"{self.session}@{self.commit}"
        return f"{base}:{self.path}" if self.path else base


@dataclass(frozen=True)
class Version:
    """One published version: an immutable state with a name.

    ``tag`` is the store-scoped tag naming the commit (``<name>/<version>``),
    ``ref`` points at it on the publication's own branch, and
    ``published_from`` is the session ref it was derived from — a soft
    reference recorded in the commit's info, not a parent pointer, so
    the version pins none of that session's history.
    """

    name: str
    version: str
    tag: str
    ref: Ref
    published_from: Ref | None
    created: float


@dataclass(frozen=True)
class Publication:
    """A named lineage of published versions, and which one is current.

    Generic on purpose: it knows nothing about handlers, tokens, routes
    or databases. An embedder that serves a publication keeps those in
    its own table, keyed by name — see ``docs/apps.md``.
    """

    name: str
    versions: tuple[Version, ...]
    current: str
    store: "Store" = field(repr=False, compare=False)

    def version(self, name: str) -> Version | None:
        """One version by name, or ``None``."""
        for v in self.versions:
            if v.version == name:
                return v
        return None

    @property
    def current_version(self) -> Version:
        """The version :meth:`open` serves by default."""
        found = self.version(self.current)
        if found is None:  # pragma: no cover - registry invariant
            raise WorkspaceError(
                f"Publication {self.name!r} points at version "
                f"{self.current!r}, which its registry does not hold"
            )
        return found

    def open(self, version: str | None = None) -> "Workspace":
        """A frozen workspace over a published version (default: the
        current one).

        Reads see the published files and nothing else; nothing can be
        written or committed. Close it when done — it holds an executor
        and a store handle of its own. ``ws.session`` names the
        publication's own branch, because that is where the state
        lives: a publication belongs to no session.
        """
        target = self.current_version if version is None else self.version(version)
        if target is None:
            raise WorkspaceError(
                f"No such version of {self.name!r}: {version!r} — have "
                f"{', '.join(v.version for v in self.versions)}"
            )
        return self.store._open_publication(target)


class StoreTags:
    """Store-scoped tags: names that belong to no session.

    Every workspace on the store can read one, and it survives the
    deletion of the session that made it — that is the whole point of
    the scope. A publication, the state an app serves, the snapshot a
    report links to.

    Session-scoped tags stay on the workspace (``ws.tags.add``,
    ``ws.tags.list``, ``ws.tags.at``): they belong to a session and go
    when it does.

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
            return source._tag(name, info=info, scope="store")
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
        self._store._require_own_workspace(ws, "tag")

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
        # The publication registry for a store that has no directory of
        # its own: a provider_factory decides where state lives, so
        # there is nowhere to put the file. It then lives for as long as
        # this Store object does. Every other store keeps it in
        # ``<path>/publications.json``.
        self._memory_registry: dict[str, Any] = {}
        # ...and since nothing else can reach that dict, its
        # read-modify-write serializes on a lock of this object's own.
        self._memory_registry_lock = threading.Lock()

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
        autocommit: bool = True,
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
            autocommit=autocommit,
            max_observation=max_observation,
            executor_factory=executor_factory,
            root=root,
        )
        ws._store = self
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

        A lineage shares one workspace root, so ``root`` (when given)
        opens the SOURCE too: ``paths`` is normalized against the root
        the child will use, and a view recorded against some other root
        would name paths the child cannot see.
        """
        source = self.open(
            src, **{k: open_kwargs[k] for k in ("root",) if k in open_kwargs}
        )
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
            # A session IS a directory here, so only directories are
            # sessions. A store directory holds other things — the
            # kvgit/ subtree when both backends share one, an embedder's
            # own db file, a stray note — and reporting a plain file as
            # a session hands the caller a name that open() cannot use.
            names = (
                (p.name for p in base.iterdir() if p.is_dir()) if base.is_dir() else ()
            )
        elif self._backend == "agentfs":
            # Same rule the other way: a session is one db FILE.
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

        A commit stays alive while a branch head or a tag reaches it,
        and a session's own history is append-only — a checkout or a
        rollback appends a restore rather than moving the head back,
        so neither strands anything. What does: deleting a session or
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
        if parsed.session.startswith(_PUB_BRANCH_PREFIX):
            # A publication's ref names its own reserved branch, which
            # is deliberately not a session and never appears in
            # sessions(). The registry is what says whether that version
            # is still published; the branch outliving it would not make
            # it servable again.
            self._require_registered(parsed)
        elif parsed.session not in set(self._branches()):
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
                "backend for named commits."
            )
        return StoreTags(self)

    # ------------------------------------------------------------------
    # not yet: the shared plane (see scratch/api-v2.md)
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

    # ------------------------------------------------------------------
    # publications
    # ------------------------------------------------------------------

    def publish(
        self,
        ws: "Workspace",
        name: str,
        *,
        paths: "Sequence[str]" = ("app/",),
        version: str | None = None,
        info: dict[str, Any] | None = None,
    ) -> Publication:
        """Publish part of a session's tree as an immutable version.

        What lands is the **subtree**, not the session. The published
        commit holds the files under ``paths`` and the filesystem rows
        that describe them — no cache, no working directory, no ws-git
        bookkeeping, no conversation record, nothing else the session
        happens to carry. Provenance is a soft reference in the commit's
        info (``published_from``), not a parent pointer, so the version
        is self-contained: it reads alone, it exports alone, and it pins
        none of the session's history against the orphan sweep.

        Three things come out of one call: a reserved branch
        ``@store/pub/<name>/<version>`` holding the derived commit (the
        anchor that lets the version be opened without borrowing a live
        session), a store-scoped tag ``<name>/<version>`` naming it, and
        a record in the store's publication registry, which becomes the
        current version of ``name``.

        Args:
            ws: The session to publish from. It must be one this store
                opened, and it must be clean — publish names a commit,
                and staged changes are in no commit yet. Commit them
                (``ws.commit()``) or drop them (``ws.discard()``).
            name: The publication's name — the lineage every version of
                it belongs to. Session-id shaped (no ``/``, no ``%``).
            paths: What to publish, as workspace paths. A relative path
                is taken under ``ws.root``, an absolute one as given; a
                trailing slash is optional. The default publishes the
                app tree an ``[apps]`` handler serves.
            version: The version name. Defaults to ``v<N>``, one past
                the highest ``v``-number this lineage holds. An explicit
                name must be unused: versions are immutable, so a name
                is never repointed.
            info: Extra keys merged into the commit's info, beside the
                ``tool``/``name``/``version``/``published_from`` this
                writes itself.

        Returns:
            The :class:`Publication`, with the new version current.
        """
        self._require_own_layout("publish")
        self._require_kvgit("publish")
        self._require_own_workspace(ws, "publish")
        _validate_publication_name(name)
        reserved = sorted(set(info or ()) & set(_RESERVED_INFO_KEYS))
        if reserved:
            raise ValueError(
                f"info may not set {', '.join(repr(k) for k in reserved)}: "
                "publish writes those into the commit itself, and the commit "
                "is where a reader checks where a version came from. Refused "
                "rather than overridden, because a false provenance in an "
                "immutable commit outlives every chance to notice it."
            )
        if ws.uncommitted:
            raise WorkspaceError(
                f"Cannot publish {ws.session!r}: it has staged changes, and "
                "publish names a commit. Land them with ws.commit() or drop "
                "them with ws.discard(), then publish."
            )
        head = ws.head
        if head is None:
            raise NotSupportedError(
                f"Cannot publish {ws.session!r}: its provider is not "
                "versioned, so it has no commit to publish."
            )

        if version is not None:
            _validate_version_name(version)

        def land(registry: dict[str, Any]) -> Publication:
            # Everything from "which version number is free" to the
            # commit itself happens under the registry lock, so two
            # publishers cannot pick the same number, and no version is
            # written whose branch and tag did not land.
            record = registry.get(name) or {"versions": {}, "current": None}
            versions = dict(record.get("versions") or {})
            chosen = _next_version(versions) if version is None else version
            if chosen in versions:
                raise WorkspaceError(
                    f"Version already published: {name}/{chosen} — versions "
                    "are immutable. Publish a new one, or unpublish that one "
                    "first."
                )
            tag = f"{name}/{chosen}"
            branch = f"{_PUB_BRANCH_PREFIX}{name}/{chosen}"
            if self._scoped_store_tag(tag) in self._raw_tags():
                raise WorkspaceError(
                    f"Tag already exists: {tag!r} in scope 'store' — a "
                    "publication cannot reuse it."
                )
            if branch in set(self._branches()):
                raise WorkspaceError(
                    f"Publication branch already exists: {branch!r} — an "
                    "earlier publish left it behind. Remove it with "
                    f"store.unpublish({name!r}, {chosen!r})."
                )
            published_from = Ref(session=ws.session, commit=head)
            commit_info: dict[str, Any] = {
                "tool": "publish",
                "name": name,
                "version": chosen,
                "published_from": str(published_from),
                **(info or {}),
            }
            commit = self._write_publication(
                ws, head, branch=branch, paths=paths, tag=tag, commit_info=commit_info
            )
            versions[chosen] = {
                "tag": tag,
                "ref": str(Ref(session=branch, commit=commit)),
                "published_from": str(published_from),
                "created": time.time(),
                "root": ws.root,
            }
            registry[name] = {"versions": versions, "current": chosen}
            return self._publication(name, registry)

        return self._update_registry(land)

    def publications(self) -> dict[str, Publication]:
        """Every publication on the store, by name."""
        registry = self._registry_read()
        return {name: self._publication(name, registry) for name in sorted(registry)}

    def publication(self, name: str) -> Publication | None:
        """One publication by name, or ``None`` if nothing of that name
        was ever published."""
        registry = self._registry_read()
        if name not in registry:
            return None
        return self._publication(name, registry)

    def set_current(self, name: str, version: str) -> Publication:
        """Point a publication at one of its versions.

        The registry is the mutable half of a publication: the versions
        themselves never move (they are tags), and this is what says
        which one is served. Rolling back moves as easily as rolling
        forward — for **code**. Data a version's handlers wrote lives
        outside the workspace and does not roll back with it.
        """

        def point(registry: dict[str, Any]) -> Publication:
            record = registry.get(name)
            if record is None:
                raise WorkspaceError(f"No such publication: {name!r}")
            versions = record.get("versions") or {}
            if version not in versions:
                raise WorkspaceError(
                    f"No such version of {name!r}: {version!r} — have "
                    f"{', '.join(sorted(versions))}"
                )
            record["current"] = version
            registry[name] = record
            return self._publication(name, registry)

        return self._update_registry(point)

    def unpublish(self, name: str, version: str, *, min_age: float = 3600) -> None:
        """Remove one published version: its tag, its branch, its record.

        The current version is refused while others remain — something
        is being served off it, and there is no obvious successor to
        pick. Move the pointer with :meth:`set_current` first. The last
        version of a publication may be removed as it stands, and takes
        the publication's record with it.

        ``min_age`` is the orphan sweep's grace period in seconds, as
        for :meth:`delete`.
        """
        self._require_own_layout("unpublish")
        self._require_kvgit("unpublish")

        def drop(registry: dict[str, Any]) -> None:
            record = registry.get(name)
            if record is None:
                raise WorkspaceError(f"No such publication: {name!r}")
            versions = dict(record.get("versions") or {})
            if version not in versions:
                raise WorkspaceError(
                    f"No such version of {name!r}: {version!r} — have "
                    f"{', '.join(sorted(versions))}"
                )
            if record.get("current") == version and len(versions) > 1:
                raise WorkspaceError(
                    f"{name}/{version} is the current version and is not the "
                    f"last one. Point {name!r} at another version with "
                    "store.set_current(name, version) first, or remove the "
                    "others."
                )
            from .providers.kvgit import KvgitProvider

            tag = versions[version].get("tag") or f"{name}/{version}"
            if self._raw_tag_info(tag) is not None:
                # The tag first: removing it is what makes the commit
                # collectable, so the branch deletion's own sweep
                # finishes the job in one pass.
                self._delete_store_tag(tag)
            branch = f"{_PUB_BRANCH_PREFIX}{name}/{version}"
            KvgitProvider.delete(self._kvgit_path(), {branch}, min_age=min_age)
            versions.pop(version)
            if not versions:
                registry.pop(name)
            else:
                record["versions"] = versions
                if record.get("current") == version:
                    record["current"] = sorted(versions)[0]
                registry[name] = record

        self._update_registry(drop)

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

    def _frozen_workspace(
        self, provider: WorkspaceProvider, *, root: str | None = None
    ) -> "Workspace":
        from .workspace import Workspace

        ws = Workspace(provider) if root is None else Workspace(provider, root=root)
        ws._store = self
        return ws

    # -- the publication registry ----------------------------------------
    #
    # The immutable half of a publication is a tag and a branch; this is
    # the mutable half — which versions exist and which one is current.
    # A plain JSON file beside the sessions, because it is small, it is
    # read far more often than written, and an embedder inspecting a
    # store by hand should be able to read it.

    def _registry_path(self) -> Path | None:
        """Where the registry file lives, or ``None`` for a store with
        no layout of its own."""
        if self._provider_factory is not None:
            return None
        return self._path / _REGISTRY_FILE

    def _registry_read(self) -> dict[str, Any]:
        path = self._registry_path()
        if path is None:
            return json.loads(json.dumps(self._memory_registry))
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError) as e:
            raise WorkspaceError(
                f"Publication registry is unreadable: {path} ({e}). The "
                "publications themselves are tags and branches on the store "
                "and are still there; this file is the index over them."
            ) from e
        return data if isinstance(data, dict) else {}

    def _registry_write(self, data: dict[str, Any]) -> None:
        path = self._registry_path()
        if path is None:
            self._memory_registry = data
            return
        # Write-then-rename: a reader either sees the old registry or
        # the new one, never half of either. The temp name carries a
        # uuid as well as the pid, so two threads writing at once cannot
        # land on one another's file even if the lock above is somehow
        # bypassed.
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        try:
            tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)

    @contextmanager
    def _registry_locked(self) -> "Iterator[None]":
        """Exclusive access to the registry for a read-modify-write.

        Two writers that each read the same registry and then replace it
        would silently drop one another's publication, though its branch
        and tag exist — so a mutation reads, decides and writes inside
        this, never around it.

        Two locks, because one process's threads and two processes are
        different problems: a ``threading.Lock`` keyed by the registry
        path (``flock`` is per open file description, so it does not
        exclude two threads of one process), and an ``flock`` on
        ``publications.lock`` beside the registry for everyone else.
        """
        path = self._registry_path()
        if path is None:
            with self._memory_registry_lock:
                yield
            return
        with _registry_lock_for(path):
            try:
                import fcntl
            except ImportError:
                # No flock here (Windows): the process-level lock above
                # is the whole guarantee, so two processes publishing to
                # one store can still lose a record. Best effort, said
                # out loud rather than pretended away.
                yield
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path.with_name(_REGISTRY_LOCK_FILE), "a+") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle, fcntl.LOCK_UN)

    def _update_registry(self, change: "Callable[[dict[str, Any]], Any]") -> Any:
        """Read the registry, apply ``change`` to it, write it back.

        The one path every mutation takes. ``change`` mutates the
        registry it is handed and returns whatever the caller wants
        back; the read happens INSIDE the lock, so a decision made from
        it (which version number is next, whether a name is free) is
        still true when the write lands. Nothing is written if
        ``change`` raises.
        """
        with self._registry_locked():
            registry = self._registry_read()
            result = change(registry)
            self._registry_write(registry)
            return result

    def _publication(self, name: str, registry: dict[str, Any]) -> Publication:
        record = registry[name]
        rows = record.get("versions") or {}
        versions = tuple(
            Version(
                name=name,
                version=v,
                tag=row.get("tag") or f"{name}/{v}",
                ref=Ref.parse(row["ref"]),
                published_from=(
                    Ref.parse(row["published_from"])
                    if row.get("published_from")
                    else None
                ),
                created=float(row.get("created") or 0.0),
            )
            for v, row in sorted(rows.items(), key=lambda kv: _version_order(kv[0]))
        )
        current = record.get("current") or (versions[-1].version if versions else "")
        return Publication(name=name, versions=versions, current=current, store=self)

    def _open_publication(self, version: Version) -> "Workspace":
        """The frozen workspace behind :meth:`Publication.open`.

        The registry is re-read here, not trusted from the
        :class:`Publication` the caller is holding: that object is a
        snapshot of the registry when it was fetched, and a version it
        names may have been unpublished since. Opening one anyway would
        serve unpublished content for as long as the orphan sweep's
        grace period, which is the opposite of what unpublish means.
        """
        self._require_own_layout("Publication.open")
        self._require_kvgit("Publication.open")
        registry = self._registry_read()
        row = ((registry.get(version.name) or {}).get("versions") or {}).get(
            version.version
        )
        if row is None:
            raise WorkspaceError(
                f"No longer published: {version.name}/{version.version} — it "
                "was unpublished after this Publication was fetched. Re-read "
                f"it with store.publication({version.name!r})."
            )
        ref = Ref.parse(row["ref"])
        return self._frozen_workspace(
            self._provider_at_commit(ref.session, ref.commit),
            root=row.get("root"),
        )

    def _write_publication(
        self,
        ws: "Workspace",
        head: str,
        *,
        branch: str,
        paths: "Sequence[str]",
        tag: str,
        commit_info: dict[str, Any],
    ) -> str:
        """Build the derived commit on its own branch and tag it.

        The branch starts at the store's empty root commit — opening a
        kvgit branch by a name that does not exist creates it empty —
        so the publication's only ancestor is nothing at all. Into it go
        the selected file blobs and a filesystem table pruned to exactly
        those files (plus the directory rows above them), which is what
        makes a frozen open read the published tree and see nothing
        else.

        Blobs are copied rather than pointed at. That is fine at app
        sizes, and content addressing in the store below would make the
        copy free — the keys are identical, so the same bytes would land
        under the same hash.
        """
        import kvgit
        from monkeyfs import VirtualFS

        from .errors import CommitNotFoundError
        from .providers.kvgit import KvgitProvider

        src = ws._provider
        handle = src.staged.checkout(head)
        if handle is None:
            raise CommitNotFoundError(head)
        wanted = {
            key: path
            for key, path in src._file_keys(handle.keys()).items()
            if _under(path, paths, ws.root)
        }
        if not wanted:
            raise WorkspaceError(
                f"Nothing to publish from {ws.session!r} at {head}: no files "
                f"under {', '.join(paths)!r}. Publish paths that exist, or "
                "widen paths=."
            )
        rows = _pruned_rows(handle.get(VirtualFS.METADATA_KEY), set(wanted.values()))

        pub = kvgit.store(kind="disk", path=str(self._kvgit_path()), branch=branch)
        try:
            for key in wanted:
                pub[key] = handle.get(key)
            pub[VirtualFS.METADATA_KEY] = json.dumps(rows, sort_keys=True).encode()
            result = pub.commit(info=commit_info)
            if not result.merged:
                raise WorkspaceError(
                    f"publish failed: could not commit the derived tree on "
                    f"{branch!r}: {result}"
                )
            commit = pub.current_commit
            KvgitProvider(pub, session=branch).tag(
                tag, at=commit, info=commit_info, scope="store"
            )
            return commit
        finally:
            self._close_backend(getattr(pub.versioned, "store", None))

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
            # commit's tree, since kvgit has no public accessor.
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
        from .errors import CommitNotFoundError

        stored = self._scoped_store_tag(name)
        if self._raw_tag_info(name) is None:
            raise CommitNotFoundError(f"No such tag: {name!r} in scope 'store'")
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
        store tag borrows an existing branch. Which one does not
        matter: a checkout is by commit or tag, both of which are
        store-wide.

        Sessions first, then a publication's own branch. That fallback
        is what makes a store of publications with no sessions left
        readable: a publication is exactly the thing a store tag is
        meant to outlive its session for, and it brings an anchor of
        its own.
        """
        names = self.sessions()
        if names:
            return names[0]
        reserved = sorted(
            b for b in self._branches() if b.startswith(_PUB_BRANCH_PREFIX)
        )
        if reserved:
            return reserved[0]
        raise WorkspaceError(
            f"{op} needs a branch on the store to read through, and this "
            "store has none. Open a session first."
        )

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
        from .errors import CommitNotFoundError
        from .providers.kvgit import KvgitProvider

        self._require_branch(session, f"read at {session}@{commit}")
        provider = KvgitProvider.open(self._kvgit_path(), session=session)
        handle = provider._staged.checkout(commit)
        if handle is None:
            provider.close()
            raise CommitNotFoundError(commit)
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


def _validate_publication_name(name: str) -> str:
    """A publication name is session-id shaped.

    It becomes a tag prefix (``<name>/<version>``) and a branch segment
    (``@store/pub/<name>/<version>``), so the characters that would make
    either ambiguous — a slash, a ``%``, a leading dot — are the ones a
    session id already forbids.
    """
    if not isinstance(name, str) or not SESSION_ID_RE.match(name):
        raise ValueError(
            f"Invalid publication name {name!r}: must match "
            f"{SESSION_ID_RE.pattern} (no slashes, no leading dot)"
        )
    return name


def _validate_version_name(version: str) -> str:
    """Same rules as a publication name: it is the other half of the
    tag and the branch."""
    if not isinstance(version, str) or not SESSION_ID_RE.match(version):
        raise ValueError(
            f"Invalid version name {version!r}: must match "
            f"{SESSION_ID_RE.pattern} (no slashes, no leading dot)"
        )
    return version


def _next_version(versions: Mapping[str, Any]) -> str:
    """``v<N>``, one past the highest v-number this lineage holds.

    Versions a caller named itself are skipped rather than parsed: the
    default sequence is nontainer's, and a lineage may hold both.
    """
    numbers = [
        int(m.group(1))
        for m in (_VERSION_NUMBER_RE.match(v) for v in versions)
        if m is not None
    ]
    return f"v{max(numbers) + 1 if numbers else 1}"


def _version_order(version: str) -> tuple[int, int, str]:
    """Sort key: the v-numbered versions in numeric order, then anything
    a caller named itself, alphabetically."""
    m = _VERSION_NUMBER_RE.match(version)
    return (0, int(m.group(1)), "") if m else (1, 0, version)


def _under(path: str, paths: "Sequence[str]", root: str) -> bool:
    """Whether one absolute workspace path falls under any of ``paths``.

    A relative entry is taken under ``root`` (``"app/"`` means
    ``<root>/app``), an absolute one as given, and a trailing slash is
    optional either way: a directory and the file it holds are both
    legitimate things to publish, and the caller should not have to
    know which spelling this wants.
    """
    for entry in paths:
        candidate = entry if entry.startswith("/") else f"{root.rstrip('/')}/{entry}"
        prefix = "/" + candidate.strip("/")
        if path == prefix or path.startswith(f"{prefix}/"):
            return True
    return False


def _pruned_rows(raw: Any, published: set[str]) -> dict[str, Any]:
    """The filesystem's metadata table, cut down to the published files.

    A published tree that carried no rows would read as empty: the rows
    are what a filesystem over the commit lists and stats. Rows are
    stored root-relative, so the leading slash comes off; directory rows
    above a published file come along, and every other row — every file
    outside ``paths`` — is left behind with its blob.
    """
    table: Any = {}
    if raw is not None:
        try:
            table = json.loads(raw)
        except (ValueError, TypeError):
            table = {}
    if not isinstance(table, dict):
        table = {}
    rows = {p.lstrip("/") for p in published}
    out: dict[str, Any] = {}
    for row, entry in table.items():
        if row in rows:
            out[row] = entry
        elif isinstance(entry, dict) and entry.get("is_dir"):
            if any(f.startswith(f"{row}/") for f in rows):
                out[row] = entry
    return out


def store(
    path: str | Path | None = None,
    *,
    backend: Backend = "kvgit",
    provider_factory: "Callable[[str], WorkspaceProvider] | None" = None,
) -> Store:
    """Build a :class:`Store` (sugar, matching ``nontainer.workspace``)."""
    return Store(path, backend=backend, provider_factory=provider_factory)
