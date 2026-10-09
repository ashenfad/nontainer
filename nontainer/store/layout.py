"""Where a store keeps things: its backend, the branch names it reserves,
and the settings a frozen state keeps."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from ..workspace import Mount


Backend = Literal["kvgit", "agentfs"]


#: Environment variable naming the kvgit backend a :class:`Store` uses
#: when it is not handed one: a PostgreSQL URL (``postgresql://...``).
#: Unset, a store keeps its kvgit data on disk under ``<path>/kvgit``.
KV_ENV = "NONTAINER_KV"


#: The table a PostgreSQL backend named by URL keeps its store in
#: (default ``kvgit``). Several stores can share one database this way.
KV_TABLE_ENV = "NONTAINER_KV_TABLE"


def _backend_for(kv: Any) -> Any:
    """The kvgit ``KVStore`` a ``kv=`` argument or ``NONTAINER_KV``
    names: a ``KVStore`` as it is, or a PostgreSQL URL."""
    if not isinstance(kv, str):
        return kv
    if not kv.startswith(("postgresql://", "postgres://")):
        raise ValueError(
            f"kv={kv!r} is not a backend nontainer can open: pass a kvgit "
            "KVStore, or a PostgreSQL URL (postgresql://...)"
        )
    try:
        from kvgit.kv.postgres import Postgres
    except ImportError as e:
        raise ImportError(
            "A PostgreSQL store needs the postgres extra: "
            "pip install 'nontainer[postgres]'"
        ) from e
    return Postgres(kv, table=os.environ.get(KV_TABLE_ENV) or "kvgit")


def _disk_backend(path: Path, create: bool) -> Any:
    """The on-disk kvgit backend under ``path`` (the store's ``kvgit``
    directory), or ``None`` where nothing was ever written and nothing
    is to be: reading a store must not create one."""
    if not path.is_dir():
        if not create:
            return None
        path.mkdir(parents=True, exist_ok=True)
    from kvgit.kv.disk import Disk

    return Disk(str(path))


# kvgit's own reserved branch namespaces, plus the legacy placeholder
# branch older nontainer versions minted during teardown. None of them
# is a session, so none of them belongs in a session listing.
_RESERVED_BRANCHES = frozenset({"__void__"})


_RESERVED_BRANCH_PREFIXES = ("refs/tags/", "@")


# Where a publication's tree lives: one reserved branch per version,
# under the store's own "@" namespace so no session can spell it.
_PUB_BRANCH_PREFIX = "@store/pub/"


# The store's own branch, in the same namespace: what a store-scoped
# read is labelled with when the store holds no session and no
# publication, so the ref it quotes resolves. It sits at the empty root
# commit, so it names a place to read from and nothing else.
_ANCHOR_BRANCH = "@store/anchor"


# The session half a ref carries when it names a STORE TAG rather than
# a session, in the same "@" namespace: a store tag belongs to no
# session, so there is no session for the ref to name, and no session
# id can be spelled this way.
_TAG_REF_PREFIX = "@store/tag/"


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
    "ignore",
)


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
