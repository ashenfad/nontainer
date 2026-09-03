"""WorkspaceProvider: the pluggable substrate seam.

A provider supplies three things:

1. ``fs``  — a filesystem satisfying termish's ``FileSystem`` protocol
   (16 methods). termish executes shell commands against it; monkeyfs
   routes sandboxed ``open()`` / ``os.*`` through it.
2. ``kv``  — a ``MutableMapping[str, Any]`` for small values. nontainer
   builds the agent-facing ``cache`` on top (prefix-scoped, key rules,
   picklability checks).
3. Versioning verbs — gated by ``Capabilities`` rather than pretended
   equivalence. A provider that can't fork says so; the toolkit layer
   degrades honestly instead of emulating badly.

Providers are session-scoped: one provider instance == one session's
world. Session resolution (e.g. "kvgit branch per session id") happens
in the factory that builds the provider, not here.

Planned implementations:

- ``KvgitProvider``   (default) — kvgit ``Staged`` per session branch.
  staging=True, cheap_fork=True, merge=True, tags=True.
- ``DirProvider``     — a real directory via monkeyfs ``IsolatedFS``.
  versioned=False; the tools work, the time-travel verbs raise.
- ``AgentFSProvider`` (spike) — Turso AgentFS via its Python SDK.
  sql_audit=True, fuse=True (opt-in mount); fork by file copy
  (cheap_fork=False).

Concurrency note: providers are NOT thread-safe, and don't need to
be — ``Workspace`` owns the single-writer invariant: its mutating
public methods hold an internal ``RLock``, so a harness that threads
parallel tool calls onto one session (not hypothetical: agno's
``arun()`` executes sync tools concurrently, including parallel calls
from one model turn) serializes safely instead of corrupting staged
state. Embedders driving a *provider* directly (bypassing Workspace)
take on serialization themselves.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, MutableMapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

# Session ids become branch names / storage paths / db filenames.
# Same rule as agex's Local host: no leading dot, no separators.
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9_.-]*$")


def validate_session_id(session: str) -> str:
    """Return ``session`` unchanged or raise ``SessionIdError``."""
    from .errors import SessionIdError

    if not isinstance(session, str) or not SESSION_ID_RE.match(session):
        raise SessionIdError(
            f"Invalid session id {session!r}: must match "
            f"{SESSION_ID_RE.pattern} (no leading dot, no path separators)"
        )
    return session


@dataclass(frozen=True)
class Capabilities:
    """What a provider can actually do. Flags, not promises.

    ``versioned`` is the master switch: when False, ``checkpoint`` /
    ``restore`` / ``history`` / ``fork`` all raise ``NotSupportedError``
    and the remaining flags are meaningless.
    """

    versioned: bool = True
    staging: bool = False
    """Writes accumulate invisibly-to-other-sessions until
    ``checkpoint()``; ``discard()`` drops them. When False, writes are
    durable immediately and ``discard()`` raises."""

    cheap_fork: bool = False
    """Fork is O(1) with shared storage (kvgit branch). When False but
    ``versioned``, fork may still work — just expensively (file copy)."""

    merge: bool = False
    """Concurrent sessions over one lineage can reconcile (CAS +
    key-level three-way merge). Reserved for the merge-preset roadmap;
    nothing in the v1 toolkit calls it."""

    sql_audit: bool = False
    """Operation-level audit log queryable with SQL (AgentFS)."""

    fuse_mount: bool = False
    """``mount()`` can expose the workspace at a real path for
    subprocesses / C extensions."""

    tags: bool = False
    """Checkpoints can be given names that outlive the call that made
    them: immutable references that also anchor garbage collection, so
    a named checkpoint (and everything it descends from) is kept for as
    long as the name exists. When False, ``tag`` / ``tags`` /
    ``tag_info`` / ``delete_tag`` / ``at_tag`` / ``diff`` raise
    ``NotSupportedError``."""


@dataclass(frozen=True)
class CheckpointInfo:
    """One entry in ``history()``."""

    id: str
    """Provider-scoped opaque id (kvgit: commit hash)."""

    time: float
    """Unix epoch seconds."""

    info: dict[str, Any] = field(default_factory=dict)
    """Caller-supplied metadata (``{"tool": "run_python", ...}``)."""

    tree: str | None = None
    """Hash of the checkpoint's content (kvgit: the keyset root) —
    the identity of *what the files and cache are*, as opposed to
    ``id``, the identity of *this point in history*. Equal trees mean
    identical content, whatever the metadata, ancestry or time around
    it; that implication runs one way only, because a store may stamp
    each write with when it happened (kvgit does), so rewriting a value
    with the same bytes still yields a different tree. ``None`` on
    providers with no such hash."""


@dataclass(frozen=True)
class TagInfo:
    """What a provider records about one tag."""

    name: str
    """The name as the caller gave it — no scope prefix."""

    scope: str
    """``"session"`` or ``"store"`` (see ``WorkspaceProvider.tag``)."""

    id: str
    """The checkpoint the tag names."""

    tree: str | None
    """The tagged checkpoint's content hash (see
    ``CheckpointInfo.tree``)."""

    time: float | None
    """When the tag was made, unix epoch seconds. ``None`` when the
    provider has no record of it."""

    info: dict[str, Any] | None
    """Caller metadata passed to ``tag()``, or ``None``."""

    dangling: bool
    """The named checkpoint is not in the store — damage rather than an
    ordinary state, and such a tag keeps nothing alive."""


@dataclass(frozen=True)
class WorkspaceDiff:
    """What changed between two checkpoints, as workspace file paths.

    Absolute VFS paths, the way agent code and ``ws.fs`` name files
    (``/workspace/data/in.csv``). Framework keys — the cache, cwd, the
    stored conversation, the filesystem's own bookkeeping — are not
    files and never appear here.

    ``modified`` holds the paths whose BYTES differ between the two
    checkpoints. A file re-saved with the content it already had is not
    a change here, even where the store's own key-level diff counts the
    write — the provider compares the content.
    """

    added: frozenset[str]
    removed: frozenset[str]
    modified: frozenset[str]


@runtime_checkable
class WorkspaceProvider(Protocol):
    """Substrate contract. See module docstring for the three surfaces."""

    # -- identity ------------------------------------------------------

    @property
    def session(self) -> str: ...

    @property
    def caps(self) -> Capabilities: ...

    # -- surfaces ------------------------------------------------------

    @property
    def fs(self) -> Any:
        """Filesystem satisfying termish's ``FileSystem`` protocol.

        Typed ``Any`` to avoid a hard import here; implementations
        return a termish-compatible object (monkeyfs ``VirtualFS`` /
        ``IsolatedFS``, or an AgentFS adapter).
        """
        ...

    @property
    def kv(self) -> MutableMapping[str, Any]:
        """Small-value store backing the agent cache.

        Values must round-trip pickle (kvgit) or the provider's own
        encoding (AgentFS: JSON — the Cache layer surfaces encoding
        failures at write time either way).
        """
        ...

    @property
    def dirty(self) -> bool:
        """Staged-but-uncommitted changes exist. Always False for
        providers without ``caps.staging``. Used by the apps extra to
        decide whether a failed handler can be rolled back atomically
        (``discard()``) without destroying unrelated pending work."""
        ...

    # -- versioning (gated by caps.versioned) --------------------------

    @property
    def head(self) -> str:
        """Id of the current (latest) checkpoint. Staged-but-uncommitted
        changes are NOT captured by it — check ``dirty``. Raises
        ``NotSupportedError`` for unversioned providers."""
        ...

    def checkpoint(self, info: dict[str, Any] | None = None) -> str:
        """Atomically capture fs + kv as one checkpoint; return its id.

        With ``caps.staging``, this is the moment staged writes become
        visible/durable. Without staging, it's a marker over already-
        durable state (AgentFS: snapshot).
        """
        ...

    def restore(self, checkpoint_id: str) -> None:
        """Reset fs + kv to a checkpoint. Staged changes are dropped."""
        ...

    def history(self, *, limit: int | None = None) -> Iterable[CheckpointInfo]:
        """Checkpoints, newest first."""
        ...

    def fork(self, name: str) -> "WorkspaceProvider":
        """New independent session seeded from current state.

        kvgit: O(1) branch. AgentFS: file copy. ``name`` is validated
        like a session id and must not already exist.
        """
        ...

    def discard(self) -> None:
        """Drop uncommitted staged writes (requires ``caps.staging``)."""
        ...

    # -- tags (gated by caps.tags) -------------------------------------

    def tag(
        self,
        name: str,
        *,
        at: str | None = None,
        info: dict[str, Any] | None = None,
        scope: str = "session",
    ) -> str:
        """Name a checkpoint immutably; return the checkpoint id.

        Two scopes, and nontainer picks which one applies rather than
        leaving the namespace to embedders:

        - ``"session"`` (default) — the name belongs to this session.
          ``tags()`` lists only its own, two sessions can both hold a
          ``v1``, and deleting the session deletes them.
        - ``"store"`` — the name belongs to no session. It is visible
          from every session on the store and survives the deletion of
          the session that made it: the scope for a publication that
          must outlive its author.

        Tags never move: an existing name raises rather than being
        repointed. ``at`` defaults to the current head; ``info`` must be
        JSON-serializable.
        """
        ...

    def check_tag(self, name: str, *, scope: str = "session") -> None:
        """Validate a name and scope without writing anything.

        The rules ``tag`` would apply, available before the commit a
        caller may have to make first: a workspace with staged work
        checkpoints before naming it, and a name rejected afterwards
        would leave that commit behind for nothing. Raises the same
        ``ValueError`` ``tag`` raises.
        """
        ...

    def tags(self, *, scope: str = "session") -> dict[str, str]:
        """Tag name (no scope prefix) → checkpoint id, in one scope."""
        ...

    def tag_info(self, name: str, *, scope: str = "session") -> TagInfo | None:
        """Describe one tag, or ``None`` if there is no such tag."""
        ...

    def delete_tag(self, name: str, *, scope: str = "session") -> None:
        """Drop a tag. The checkpoint it named survives only if
        something else still reaches it."""
        ...

    def at_tag(self, name: str, *, scope: str = "session") -> "WorkspaceProvider":
        """A FROZEN provider over the tagged checkpoint.

        Reads see the tagged state. Nothing can commit: ``checkpoint``,
        ``restore``, ``fork``, ``tag`` and ``delete_tag`` raise
        ``NotSupportedError``; writes may stage (so ``dirty`` can become
        True) but have nowhere to land, and ``discard`` drops them.

        Such a provider reports ``frozen`` True; a provider without the
        attribute reads as not frozen, which is what a workspace over a
        third-party provider assumes.
        """
        ...

    def diff(self, a: str, b: str) -> WorkspaceDiff:
        """File-level changes between two checkpoint ids."""
        ...

    # -- power modes / lifecycle ---------------------------------------

    def mount(self) -> Any:
        """Context manager yielding a real ``Path`` (requires
        ``caps.fuse_mount``). See README for platform caveats."""
        ...

    def close(self) -> None:
        """Release resources (db handles, mounts). Idempotent."""
        ...
