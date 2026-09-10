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
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal

from .errors import NotSupportedError, WorkspaceError
from .protocol import SESSION_ID_RE, TagInfo, WorkspaceProvider, validate_session_id

if TYPE_CHECKING:
    from .protocol import Executor
    from .workspace import Mount, PythonConfig, Workspace

Backend = Literal["kvgit", "dir", "agentfs"]

# kvgit's own reserved branch namespaces, plus the legacy placeholder
# branch older nontainer versions minted during teardown. None of them
# is a session, so none of them belongs in a session listing.
_RESERVED_BRANCHES = frozenset({"__void__"})
_RESERVED_BRANCH_PREFIXES = ("refs/tags/", "@")

# Where a publication's tree lives: one reserved branch per version,
# under the store's own "@" namespace so no session can spell it.
_PUB_BRANCH_PREFIX = "@store/pub/"
# The store's own branch, in the same namespace: what a store-scoped
# read anchors on when the store holds no session and no publication.
# It carries one commit and that commit is empty, so it names a place
# to read from and nothing else.
_ANCHOR_BRANCH = "@store/anchor"
# The publication registry: the mutable half (which versions exist,
# which one is current) beside the immutable half (the tags).
_REGISTRY_FILE = "publications.json"
_REGISTRY_LOCK_FILE = "publications.lock"
_VERSION_NUMBER_RE = re.compile(r"^v(\d+)$")
# Keys publish() writes into the commit itself. A caller's ``info`` may
# not spell them: the commit is immutable and is where provenance is
# read from, so a false ``published_from`` would outlive every chance to
# notice it — and ``published_from`` and ``paths`` together are what a
# retry compares to recognise its own interrupted attempt.
_RESERVED_INFO_KEYS = ("tool", "name", "version", "published_from", "paths")
# The same keys read the other way: all of them present, carrying the
# values a caller can name, is what marks a commit as one publish
# attempt's work. Nothing the store did not publish is ever removed on
# the strength of its name alone.
_PUBLISH_IDENTITY_KEYS = _RESERVED_INFO_KEYS

# The construction keywords a frozen open takes. Exactly
# :meth:`Store.open`'s, minus ``autocommit``: a frozen provider commits
# nothing, so the flag has nothing to switch. ``provider`` and
# ``executor`` are absent for the reason they are absent from
# ``Store.open`` — the store builds the provider, and an executor
# instance is bound to one session, so a caller hands over the
# ``executor_factory`` instead. ``tests/test_store.py`` asserts this
# tuple and ``Store.open``'s signature stay in step.
_FROZEN_SETTINGS = (
    "python",
    "mounts",
    "commands",
    "cache",
    "max_observation",
    "executor_factory",
    "root",
)

# What ``Publication.open`` takes: the same, without ``root``. A
# publication records the workspace root its files were published
# under, so the root is a fact about the version rather than a choice
# at the call, and reading the tree at another one finds it empty.
_PUBLICATION_SETTINGS = tuple(n for n in _FROZEN_SETTINGS if n != "root")


def _check_frozen_settings(
    caller: str,
    settings: Mapping[str, Any],
    *,
    accepts: "Sequence[str]" = _FROZEN_SETTINGS,
) -> None:
    """Refuse a keyword a frozen open does not take.

    The entry points collect settings as ``**kwargs``, which accepts
    anything; without this an ``autocommit=False`` would be forwarded to
    a ``Workspace`` that takes the name and ignores the value, and a
    typo would open a snapshot missing the very config it was passed.
    """
    for name in settings:
        if name not in accepts:
            raise TypeError(
                f"{caller} got an unexpected keyword argument {name!r} — a "
                f"frozen open takes {', '.join(accepts)}"
            )


def _check_frozen_mounts(mounts: "Mapping[str, Mount] | None") -> None:
    """Refuse a writable mount on a frozen open.

    A frozen workspace accepts no writes from anyone, and a mount is
    the one part of its filesystem that is a real host directory rather
    than a state in the store: ``ws.files.fs`` hands out the composed
    filesystem for host-side reads, so a mount left writable carries a
    write through to that directory, permanently and outside the
    versioning plane. The flag is a caller's mistake to surface rather
    than to quietly override — coercing it would hand back a workspace
    whose mounts do not do what the caller asked for, silently.
    """
    for point, mount in (mounts or {}).items():
        if not mount.readonly:
            raise ValueError(
                f"cannot mount {point!r} with readonly=False on a frozen "
                "workspace: a frozen workspace takes read-only mounts only, "
                "and a writable one would carry a ws.files.fs write into the "
                "host directory. Pass Mount(..., readonly=True), or open the "
                "session itself to write there."
            )


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

    ``info`` is the caller's own metadata from ``publish(info=...)``,
    recorded on the registry row as well as in the commit, so listing
    published apps with their display titles and owners is one registry
    read and no backend open. It holds what the caller passed and
    nothing else: what publish writes itself is already spelled out by
    ``name``, ``version``, ``published_from`` and ``paths``. A row
    written before the field existed reads as an empty mapping.

    ``info`` counts for equality but is left out of the hash, so a
    version goes in a set or a dict key like any other frozen record.
    It holds whatever JSON the caller passed — a list, a nested dict —
    and none of that is hashable, while everything that identifies the
    version (``tag``, ``ref``) is.
    """

    name: str
    version: str
    tag: str
    ref: Ref
    published_from: Ref | None
    created: float
    info: Mapping[str, Any] = field(
        default_factory=lambda: MappingProxyType({}), hash=False
    )
    paths: tuple[str, ...] = ()


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

    def open(self, version: str | None = None, **settings: Any) -> "Workspace":
        """A frozen workspace over a published version (default: the
        current one).

        Reads see the published files and nothing else; nothing can be
        written or committed. Close it when done — it holds an executor
        and a store handle of its own. ``ws.session`` names the
        publication's own branch, because that is where the state
        lives: a publication belongs to no session.

        ``settings`` are :meth:`Store.open`'s construction keywords —
        ``python``, ``mounts``, ``commands``, ``cache``,
        ``max_observation``, ``executor_factory`` — applied to the
        workspace this returns. A publication carries the tree and
        nothing else, so an embedder that serves it supplies the
        execution settings here: ``pub.open(python=PythonConfig(
        host_objects={"db": db}))`` is how a published handler reaches
        a live database. Host objects are the embedder's own live
        objects, which is exactly why they are not in the commit.

        ``root`` is not among them. A publication records the
        workspace root its files were published under, and reading
        them at another one finds an empty tree.
        """
        if "root" in settings:
            raise TypeError(
                "Publication.open() takes no 'root': a publication records "
                "the workspace root its files were published under, and "
                "reading them at another one finds an empty tree"
            )
        _check_frozen_settings(
            "Publication.open()", settings, accepts=_PUBLICATION_SETTINGS
        )
        target = self.current_version if version is None else self.version(version)
        if target is None:
            raise ValueError(
                f"No such version of {self.name!r}: {version!r} — have "
                f"{', '.join(v.version for v in self.versions)}"
            )
        return self.store._open_publication(target, **settings)


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

    def list_info(self) -> dict[str, TagInfo]:
        """Tag name → :class:`TagInfo`, for every store-scoped tag, on
        one backend open.

        The bulk read: describing N tags one at a time costs N opens,
        this costs one. :meth:`list` stays the cheaper answer when only
        the commit ids are wanted — it reads the tag table and nothing
        per tag.
        """
        return self._store._raw_tag_infos()

    def info(self, name: str) -> TagInfo | None:
        """Describe one store-scoped tag, or ``None`` if there is
        no such tag.

        Opens the backend for the read and closes it again: a store
        keeps no handle of its own, and holding a branch open by name
        would create that branch. Describing several tags at once goes
        through :meth:`list_info`, which pays that open once.
        """
        return self._store._raw_tag_info(name)

    def delete(self, name: str) -> None:
        """Drop a store-scoped tag, then sweep commits nothing else
        reaches. What it named survives only while something else
        still reaches it — a branch, or another tag."""
        self._store._delete_store_tag(name)

    def at(self, name: str, **settings: Any) -> "Workspace":
        """A frozen workspace over the tagged state.

        Reads see the tagged files, cache and cwd; nothing can be
        written or committed. Close it when done — it holds an executor
        and a store handle of its own.

        A store-scoped tag belongs to no session, so ``ws.session``
        names whichever branch the read was anchored on rather than an
        origin: the tag is the identity here, not the session. A store
        with sessions on it anchors on one of those, a store of
        publications on a publication's branch, and a store with
        neither on a reserved branch of its own — so a tag opens for as
        long as it exists, whatever became of the session that made it.

        ``settings`` are :meth:`Store.open`'s construction keywords —
        ``python``, ``mounts``, ``commands``, ``cache``,
        ``max_observation``, ``executor_factory``, ``root`` — applied
        to the workspace this returns. The store opens a tree, not a
        session, so it has no settings to inherit; an embedder that
        serves this snapshot supplies its own, and that is how a
        handler reaches a live host object (``python=PythonConfig(
        host_objects={"db": db})``). The commit holds files, never the
        objects.
        """
        _check_frozen_settings("StoreTags.at()", settings)
        return self._store._frozen_workspace(
            self._store._provider_at_store_tag(name), **settings
        )


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
        any open :class:`Workspace` on these sessions first (a kvgit
        store handle pins its branch). It does not touch anything a
        caller keeps *beside* the workspace store (an app's own db
        files, transcripts) — that bookkeeping is the caller's.
        """
        self._require_own_layout("delete")
        names = {sessions} if isinstance(sessions, str) else set(sessions)
        self._require_session_names(names)
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
            self._provider_at_commit(parsed.session, parsed.commit),
            root=root,
            **settings,
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
        current: bool = True,
        create_only: bool = False,
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

        The live ``cache`` does not travel. What lands is file blobs
        and the filesystem rows describing them, and a cache entry is
        neither — a frozen open starts with an empty one. Data an app
        needs precomputed belongs in a file under ``paths``: write it
        out, commit it, publish it, and the handler reads it back.

        Three things come out of one call: a reserved branch
        ``@store/pub/<name>/<version>`` holding the derived commit (the
        anchor that lets the version be opened without borrowing a live
        session), a store-scoped tag ``<name>/<version>`` naming it, and
        a record in the store's publication registry, which becomes the
        current version of ``name`` unless ``current=False`` says
        otherwise.

        :meth:`unpublish` refuses the version a publication points at
        while others remain, so undoing a publish that went wrong means
        moving the pointer back with :meth:`set_current` first — or
        publishing with ``current=False``, which never takes it.

        The record lands last, so a process that dies mid-publish
        leaves a reserved branch, and usually its tag, that no record
        names. Publishing the same thing again adopts it: a recordless
        branch whose head names the same source commit and the same
        ``paths`` was written by that attempt and no other, so its
        commit becomes this publish's and the record is written over
        it. A recordless branch of some other attempt is refused by
        name, and :meth:`unpublish` clears it.

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
                the highest ``v``-number this lineage holds — only
                names of that exact shape are counted, so a lineage
                holding ``v1``, ``v2`` and ``release-1`` gets ``v3``.
                The default is a series of its own; a version the
                caller named stands outside it and never moves it. An
                embedder that wants every version numbered passes
                ``version=`` itself. An explicit name must be unused:
                versions are immutable, so a name is never repointed.
            current: Whether this version becomes the one
                :meth:`Publication.open` serves. ``False`` records it
                and leaves what is served alone, so a caller can land
                the tree, check it at ``pub.open(version)`` and switch
                with :meth:`set_current` after — and can drop it with
                :meth:`unpublish` in between, which the current version
                refuses while others remain. The version that opens a
                lineage takes the pointer whatever this says, because a
                publication must point somewhere.
            create_only: Refuse the call if ``name`` already holds any
                version, rather than extending the lineage — for a
                caller that means to open one and would rather hear
                about a collision than silently publish a second
                version of somebody else's app. The check runs inside
                the lock that decides between creating and extending,
                so two publishers racing to open one lineage get one
                success and one ``ValueError``. It closes the
                check-then-publish race only for the caller that stops
                doing the check: the lock covers this call, not a read
                the caller made before it, and a name seen free by an
                earlier ``publication()`` can be taken before this call
                reaches the lock.
            info: Extra keys merged into the commit's info, beside the
                ``tool``/``name``/``version``/``published_from``/``paths``
                this writes itself. They are recorded on the registry
                row as well and come back as :attr:`Version.info`, so
                listing publications with their metadata costs one
                registry read and no backend open.

        Returns:
            The :class:`Publication`, with the new version current.

        Raises:
            ValueError: The call is wrong, and only the caller can fix
                it — an embedder mapping errors to HTTP answers 400. A
                publication or version name that is not session-id
                shaped; an ``info`` key publish writes itself; a
                version name this lineage already holds, since versions
                are immutable and a name is never repointed; a
                publication name that already holds a version, under
                ``create_only``; ``paths`` that match no file at
                ``ws``'s commit. Naming a version
                the registry does not hold to :meth:`set_current`,
                :meth:`unpublish` or :meth:`Publication.open` is the
                same mistake and raises the same class.
            WorkspaceError: The store is not in a state to publish, and
                the same call lands once it is — ``ws`` carries staged
                changes, ``ws`` belongs to another store, an earlier
                attempt of something else left a tag or a branch of
                this version's name behind, or the derived commit did
                not land.
            NotSupportedError: This store cannot publish at all: a
                backend without branches and tags, a
                ``provider_factory`` layout the store does not own, or
                a provider that keeps no commits.
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
            if create_only and versions:
                raise ValueError(
                    f"Publication already published: {name!r} — it holds "
                    f"{', '.join(sorted(versions))} and create_only= asked "
                    "for a new lineage. Publish under another name, or drop "
                    "create_only= to add a version to this one."
                )
            chosen = _next_version(versions) if version is None else version
            if chosen in versions:
                raise ValueError(
                    f"Version already published: {name}/{chosen} — versions "
                    "are immutable. Publish a new one, or unpublish that one "
                    "first."
                )
            tag = f"{name}/{chosen}"
            branch = f"{_PUB_BRANCH_PREFIX}{name}/{chosen}"
            published_from = Ref(session=ws.session, commit=head)
            commit_info: dict[str, Any] = {
                "tool": "publish",
                "name": name,
                "version": chosen,
                "published_from": str(published_from),
                "paths": sorted(paths),
                **(info or {}),
            }
            # No record holds this version (the check above says so), so
            # a branch of its name is an attempt that died before its
            # record landed. One that matches this call is that same
            # attempt and its commit is taken as this publish's; one
            # that does not is somebody else's leftover and is refused.
            stranded = branch in set(self._branches())
            resumed = (
                self._resume_publication(branch, tag, commit_info) if stranded else None
            )
            if resumed is None and stranded:
                raise WorkspaceError(
                    f"Publication branch already exists: {branch!r}, no "
                    "record names it, and its commit is not the one this "
                    "call would write — an earlier publish of something "
                    "else left it behind. Clear it with "
                    f"store.unpublish({name!r}, {chosen!r}), then publish "
                    "again."
                )
            if resumed is None and self._scoped_store_tag(tag) in self._raw_tags():
                raise WorkspaceError(
                    f"Tag already exists: {tag!r} in scope 'store' — a "
                    "publication cannot reuse it."
                )
            # The record describes the commit that is there, and an
            # adopted commit carries the info the attempt that wrote it
            # was given. A row built from this call's arguments would
            # say something the version does not serve.
            if resumed is not None:
                commit, landed = resumed
            else:
                landed = commit_info
                commit = self._write_publication(
                    ws,
                    head,
                    branch=branch,
                    paths=paths,
                    tag=tag,
                    commit_info=commit_info,
                )
            versions[chosen] = {
                "tag": tag,
                "ref": str(Ref(session=branch, commit=commit)),
                "published_from": str(published_from),
                "created": time.time(),
                "root": ws.root,
                "paths": list(landed.get("paths") or sorted(paths)),
                # The caller's keys only. What publish writes itself is
                # already spelled out by the fields around this one, and
                # the commit stays the place provenance is read from.
                "info": {
                    k: v for k, v in landed.items() if k not in _RESERVED_INFO_KEYS
                },
            }
            pointer = record.get("current")
            registry[name] = {
                "versions": versions,
                # The version that opens a lineage takes the pointer
                # whatever the caller asked: a publication must point
                # somewhere, and there is nothing else to point at.
                "current": chosen if current or pointer not in versions else pointer,
            }
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
                raise ValueError(f"No such publication: {name!r}")
            versions = record.get("versions") or {}
            if version not in versions:
                raise ValueError(
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
        pick. Move the pointer with :meth:`set_current` first, or
        publish the version with ``current=False`` so it never takes
        the pointer and can be dropped as it stands. The last version
        of a publication may be removed however it is pointed at, and
        takes the publication's record with it.

        A version with no record but a branch or a tag of its name —
        what a publish that died before writing its record leaves
        behind — is cleared here too: the branch and the tag go, the
        registry is untouched, and the name is free to publish again.
        Only what publish itself wrote is removed that way. A store tag
        may hold a slash, so a durable ``release/prod`` somebody tagged
        by hand answers to ``unpublish("release", "prod")`` by name
        alone; the tag's and the branch's own provenance is checked
        first, and one that does not carry it is left exactly as it is
        and the call raises ``ValueError``.

        ``min_age`` is the orphan sweep's grace period in seconds, as
        for :meth:`delete`.
        """
        self._require_own_layout("unpublish")
        self._require_kvgit("unpublish")

        def drop(registry: dict[str, Any]) -> None:
            record = registry.get(name)
            versions = dict((record or {}).get("versions") or {})
            if version not in versions:
                # No record, but a branch or a tag of that name: an
                # attempt that died before its record landed. Removing
                # it is the whole job, and it is what frees the name.
                if self._clear_stranded_attempt(name, version, min_age=min_age):
                    return
                if record is None:
                    raise ValueError(f"No such publication: {name!r}")
                raise ValueError(
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
            self._remove_version_state(
                versions[version].get("tag") or f"{name}/{version}",
                f"{_PUB_BRANCH_PREFIX}{name}/{version}",
                min_age=min_age,
            )
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
        the entry point the caller used. ``root`` is spelled as a named
        parameter so a caller can pass it either way and Python binds
        it here once — there is no second value to conflict with.

        A frozen workspace accepts no writes from anyone, so a mount
        with ``readonly=False`` is refused here rather than coerced:
        every frozen open funnels through this method, which is what
        makes the rule one rule instead of three. The mounted bytes
        still read, and reach the executor read-only like the rest of
        the tree.
        """
        from .workspace import Workspace

        if root is not None:
            settings["root"] = root
        _check_frozen_mounts(settings.get("mounts"))
        ws = Workspace(provider, **settings)
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
                info=MappingProxyType(dict(row.get("info") or {})),
                paths=tuple(row.get("paths") or ()),
            )
            for v, row in sorted(rows.items(), key=lambda kv: _version_order(kv[0]))
        )
        current = record.get("current") or (versions[-1].version if versions else "")
        return Publication(name=name, versions=versions, current=current, store=self)

    def _open_publication(self, version: Version, **settings: Any) -> "Workspace":
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
            **settings,
        )

    def _branch_head(self, branch: str) -> tuple[str | None, dict[str, Any] | None]:
        """The commit at a branch's head and the info it carries.

        ``(None, None)`` when the store has no such branch or the
        branch holds no commit. The existence check comes first
        because opening a kvgit branch by an unknown name creates it,
        and a reserved branch minted by a read is exactly what would
        then block republishing that version.
        """
        import kvgit

        if branch not in set(self._branches()):
            return None, None
        pub = kvgit.store(kind="disk", path=str(self._kvgit_path()), branch=branch)
        try:
            commit = pub.current_commit
            info = pub.versioned.commit_info(commit) if commit else None
            return commit, info
        finally:
            self._close_backend(getattr(pub.versioned, "store", None))

    def _clear_stranded_attempt(
        self, name: str, version: str, *, min_age: float
    ) -> bool:
        """Remove what a publish that died before its record left behind.

        True when something was cleared, ``False`` when the store holds
        nothing of that name. Whatever is there has to prove it came
        from a publish of this name and this version before any of it
        is touched: a store tag may hold a slash, so a durable
        ``release/prod`` somebody tagged by hand answers to
        ``unpublish("release", "prod")`` by name alone, and deleting it
        would drop a tag nobody published and make its commit
        collectable. A tag or a branch that does not carry publish's
        own provenance is left exactly as it is and the call raises.
        """
        expect = {"tool": "publish", "name": name, "version": version}
        tag = f"{name}/{version}"
        branch = f"{_PUB_BRANCH_PREFIX}{name}/{version}"
        found = self._raw_tag_info(tag)
        has_branch = branch in set(self._branches())
        if found is None and not has_branch:
            return False
        _, head_info = self._branch_head(branch) if has_branch else (None, None)
        tag_blocks = found is not None and not _is_publish_attempt(found.info, expect)
        branch_blocks = has_branch and not _is_publish_attempt(head_info, expect)
        if tag_blocks or branch_blocks:
            blocking = ", ".join(
                part
                for part, blocked in (
                    (f"store tag {tag!r}", tag_blocks),
                    (f"branch {branch!r}", branch_blocks),
                )
                if blocked
            )
            hint = (
                f" Delete it with store.tags.delete({tag!r}) if that is what you meant."
                if tag_blocks
                else ""
            )
            raise ValueError(
                f"No publication of {name!r} at version {version!r}. What "
                f"carries that name — {blocking} — was not written by "
                f"publish, so it is left where it is.{hint}"
            )
        self._remove_version_state(tag, branch, min_age=min_age)
        return True

    def _remove_version_state(self, tag: str, branch: str, *, min_age: float) -> bool:
        """Take out a published version's tag and its branch.

        True when either was there, which is what tells a caller
        holding no registry record that something was cleared. The tag
        goes first: removing it is what makes the commit collectable,
        so the branch deletion's own sweep finishes the job in one
        pass.
        """
        from .providers.kvgit import KvgitProvider

        found = self._raw_tag_info(tag) is not None
        if found:
            self._delete_store_tag(tag)
        if branch in set(self._branches()):
            found = True
        KvgitProvider.delete(self._kvgit_path(), {branch}, min_age=min_age)
        return found

    def _resume_publication(
        self, branch: str, tag: str, commit_info: Mapping[str, Any]
    ) -> "tuple[str, dict[str, Any]] | None":
        """Finish a publish that died before its registry record.

        The branch and the tag are written before the record, so a
        crash between them leaves a reserved branch no record names.
        The retry rebuilds the same commit info, and a branch whose
        head carries the same ``published_from`` commit and the same
        ``paths`` was written by that same attempt: its commit is
        returned to be recorded as this publish's, and the tag is
        minted if the crash came before it. ``None`` says the branch
        holds some other attempt's commit, which this publish may not
        adopt and may not overwrite.

        The commit and the info IT carries come back together, and that
        info is what the record and a late-minted tag describe. The
        adopted commit is immutable, so the resuming call's own ``info``
        landed nowhere: writing that into the record or the tag would
        describe the version as something it is not.

        The whole tree is not compared. What identifies the attempt is
        what it was told to do — one source commit, one set of paths —
        because publishing that twice writes the same files either way.
        """
        import kvgit

        from .providers.kvgit import KvgitProvider

        commit, found = self._branch_head(branch)
        if commit is None:
            return None
        if not _is_publish_attempt(
            found, {key: commit_info.get(key) for key in _PUBLISH_IDENTITY_KEYS}
        ):
            return None
        landed = dict(found or {})
        existing = self._raw_tag_info(tag)
        if existing is None:
            # Tagged through a handle on the branch that holds the
            # commit. A checkout at a commit is frozen, and a frozen
            # provider writes nothing — tags included.
            pub = kvgit.store(kind="disk", path=str(self._kvgit_path()), branch=branch)
            try:
                KvgitProvider(pub, session=branch).tag(
                    tag, at=commit, info=landed, scope="store"
                )
            finally:
                self._close_backend(getattr(pub.versioned, "store", None))
        elif existing.id != commit:
            return None
        return commit, landed

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
        the selected file blobs and, beside each one, the metadata row
        the source filesystem holds for that path (plus the rows of the
        directories above them), which is what makes a frozen open read
        the published tree and see nothing else. The rows are asked of a
        filesystem over the source commit rather than copied key by key,
        so a source still carrying the legacy ``__vfs_metadata__`` table
        publishes rows like any other — and the table itself, which
        describes files this publication does not hold, never travels.

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
            raise ValueError(
                f"Nothing to publish from {ws.session!r} at {head}: no files "
                f"under {', '.join(paths)!r}. Publish paths that exist, or "
                "widen paths=."
            )
        rows = _published_rows(
            VirtualFS(handle).get_metadata_snapshot(), set(wanted.values())
        )

        pub = kvgit.store(kind="disk", path=str(self._kvgit_path()), branch=branch)
        try:
            pub_fs = VirtualFS(pub)
            for key in wanted:
                pub[key] = handle.get(key)
            for row_path, fields in rows.items():
                pub[pub_fs.metadata_key("/" + row_path)] = json.dumps(
                    fields, sort_keys=True
                ).encode()
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

    @contextmanager
    def _raw_backend(self) -> "Iterator[Any | None]":
        """The kvgit store open for a handle-free read, or ``None``
        when nothing was ever written under this store.

        Opened as a backend rather than a branch handle: opening a
        branch by name would create that branch. Every admin read that
        must not do that goes through here, and a caller reading many
        things holds one of these across the lot.
        """
        p = self._kvgit_path()
        if not p.is_dir():
            yield None
            return
        from kvgit.kv.disk import Disk

        backend = Disk(str(p))
        try:
            yield backend
        finally:
            self._close_backend(backend)

    def _raw_tags(self, backend: Any = None) -> dict[str, str]:
        """Every tag in the kvgit store, stored (prefixed) names and
        all. Reads on ``backend`` when given one, else on an open of
        its own."""
        from kvgit.versioned.kv import tags as raw_tags

        if backend is not None:
            return dict(raw_tags(backend))
        with self._raw_backend() as opened:
            return {} if opened is None else dict(raw_tags(opened))

    def _raw_tag_info(self, name: str, backend: Any = None) -> TagInfo | None:
        """Describe one store-scoped tag. Reads on ``backend`` when
        given one, else on an open of its own."""
        if backend is None:
            with self._raw_backend() as opened:
                if opened is None:
                    return None
                return self._raw_tag_info(name, opened)
        from kvgit.encoding import safe_loads
        from kvgit.versioned.kv import tag_info as raw_tag_info

        stored = self._scoped_store_tag(name)
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

    def _raw_tag_infos(self) -> dict[str, TagInfo]:
        """Every store-scoped tag described, on one backend open."""
        prefix = self._store_tag_prefix()
        with self._raw_backend() as backend:
            if backend is None:
                return {}
            described = {}
            for stored in self._raw_tags(backend):
                if not stored.startswith(prefix):
                    continue
                name = stored[len(prefix) :]
                info = self._raw_tag_info(name, backend)
                if info is not None:
                    described[name] = info
            return described

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
        import kvgit

        if _ANCHOR_BRANCH in set(self._branches()):
            return _ANCHOR_BRANCH
        anchor = kvgit.store(
            kind="disk", path=str(self._kvgit_path()), branch=_ANCHOR_BRANCH
        )
        try:
            if anchor.current_commit is None:
                anchor.commit(info={"tool": "nontainer", "anchor": True})
        finally:
            self._close_backend(getattr(anchor.versioned, "store", None))
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


def _is_publish_attempt(
    info: "Mapping[str, Any] | None", expect: "Mapping[str, Any]"
) -> bool:
    """Whether a commit's info was written by a publish attempt.

    Every key in ``_PUBLISH_IDENTITY_KEYS`` must be there, and every
    key ``expect`` names must carry the value it names; a key ``expect``
    omits must merely be present, which is all a caller holding a
    publication name and a version can require. A store tag made by
    hand carries none of them — and a store tag may hold a slash, so
    ``release/prod`` is indistinguishable from version ``prod`` of
    publication ``release`` until this is asked.
    """
    if info is None:
        return False
    if any(key not in info for key in _PUBLISH_IDENTITY_KEYS):
        return False
    return all(info.get(key) == value for key, value in expect.items())


def _next_version(versions: Mapping[str, Any]) -> str:
    """``v<N>``, one past the highest v-number this lineage holds.

    Only names of that exact shape are counted, so ``v1, v2,
    release-1`` yields ``v3``. Versions a caller named itself are
    skipped rather than parsed: the default is a series of its own, and
    a lineage may hold both kinds.
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


def _published_rows(
    snapshot: Mapping[str, Any], published: set[str]
) -> dict[str, dict[str, Any]]:
    """The metadata rows a publication carries, by root-relative path.

    A published tree whose blobs had no rows would read as empty: a row
    is what a filesystem over the commit lists and stats. Paths are
    stored root-relative, so the leading slash comes off; the row of a
    directory above a published file comes along, and every other row —
    every file outside the published paths — is left behind with its
    blob.
    """
    rows = {p.lstrip("/") for p in published}
    out: dict[str, dict[str, Any]] = {}
    for path, meta in snapshot.items():
        fields = {
            "size": getattr(meta, "size", 0),
            "created_at": getattr(meta, "created_at", ""),
            "modified_at": getattr(meta, "modified_at", ""),
            "is_dir": getattr(meta, "is_dir", False),
        }
        if path in rows:
            out[path] = fields
        elif fields["is_dir"] and any(f.startswith(f"{path}/") for f in rows):
            out[path] = fields
    return out


def store(
    path: str | Path | None = None,
    *,
    backend: Backend = "kvgit",
    provider_factory: "Callable[[str], WorkspaceProvider] | None" = None,
) -> Store:
    """Build a :class:`Store` (sugar, matching ``nontainer.workspace``)."""
    return Store(path, backend=backend, provider_factory=provider_factory)
