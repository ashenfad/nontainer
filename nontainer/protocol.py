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
  staging=True, cheap_fork=True, merge=True.
- ``DirProvider``     — a real directory via monkeyfs ``IsolatedFS``.
  versioned=False; the tools work, the time-travel verbs raise.
- ``AgentFSProvider`` (spike) — Turso AgentFS via its Python SDK.
  sql_audit=True, fuse=True (opt-in mount); fork by file copy
  (cheap_fork=False).

Concurrency note: providers are NOT thread-safe. One provider per
session per process; serialize access per session above this layer
(the serving extra does). This is not hypothetical for adapters:
agno's ``arun()`` executes sync tools CONCURRENTLY on separate
threads — including parallel tool calls from one model turn — so the
agno adapter must expose ``async def`` tool methods that hold a
per-session ``asyncio.Lock`` around an ``asyncio.to_thread`` of the
sync core. Do not hand raw sync methods to an async harness and let
it thread them.
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


@dataclass(frozen=True)
class CheckpointInfo:
    """One entry in ``history()``."""

    id: str
    """Provider-scoped opaque id (kvgit: commit hash)."""

    time: float
    """Unix epoch seconds."""

    info: dict[str, Any] = field(default_factory=dict)
    """Caller-supplied metadata (``{"tool": "run_python", ...}``)."""


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

    # -- versioning (gated by caps.versioned) --------------------------

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

    # -- power modes / lifecycle ---------------------------------------

    def mount(self) -> Any:
        """Context manager yielding a real ``Path`` (requires
        ``caps.fuse_mount``). See README for platform caveats."""
        ...

    def close(self) -> None:
        """Release resources (db handles, mounts). Idempotent."""
        ...
