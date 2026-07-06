"""DirProvider: the plain-directory substrate.

The degenerate provider — a real directory via monkeyfs ``IsolatedFS``,
no versioning machinery at all. The agent still gets the full terminal
+ sandboxed python + cache experience, against a folder you can open
in Finder. Time-travel verbs raise ``NotSupportedError``.

Because the files are real, this is also where C-extension workloads
live happily (sqlite, mmap, subprocesses via future mounts) without
FUSE — the least-surprising backend precisely because it has the
fewest virtual behaviors.

Layout:

- ``<root>/``                  — the workspace tree the agent sees
- ``<root>/.nontainer/kv.pkl`` — the kv store (cache + framework keys)

The ``.nontainer`` directory is framework-internal but visible to the
agent (v1 keeps it simple: documented, not hidden). Cache key rules
still hold at the Cache layer regardless of what the agent does to
the file directly — worst case it corrupts its own cache, which is
its own workspace to break.
"""

from __future__ import annotations

import pickle
from collections.abc import Iterable, Iterator, MutableMapping
from pathlib import Path
from typing import Any

from monkeyfs import IsolatedFS

from ..errors import NotSupportedError
from ..protocol import Capabilities, CheckpointInfo, validate_session_id

_KV_DIR = ".nontainer"
_KV_FILE = "kv.pkl"

_DIR_CAPS = Capabilities(
    versioned=False,
    staging=False,
    cheap_fork=False,
    merge=False,
    sql_audit=False,
    fuse_mount=False,
)


class _FileKV(MutableMapping[str, Any]):
    """A dict persisted to a pickle file on every mutation.

    Adequate for cache-sized data on an unversioned provider. Not
    safe for concurrent writers — but neither is the provider (see
    protocol.py concurrency note).
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._data: dict[str, Any] = {}
        if path.exists():
            with open(path, "rb") as f:
                self._data = pickle.load(f)

    def _flush(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        with open(tmp, "wb") as f:
            pickle.dump(self._data, f, protocol=pickle.HIGHEST_PROTOCOL)
        tmp.replace(self._path)

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self._data[key] = value
        self._flush()

    def __delitem__(self, key: str) -> None:
        del self._data[key]
        self._flush()

    def __iter__(self) -> Iterator[str]:
        return iter(list(self._data))

    def __len__(self) -> int:
        return len(self._data)

    def __contains__(self, key: object) -> bool:
        return key in self._data


class DirProvider:
    """``WorkspaceProvider`` over a plain directory. See module docstring."""

    def __init__(self, root: str | Path, *, session: str) -> None:
        validate_session_id(session)
        self._session = session
        self._root = Path(root).expanduser().resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        self._fs = IsolatedFS(str(self._root))
        self._kv = _FileKV(self._root / _KV_DIR / _KV_FILE)
        self._closed = False

    # -- identity ------------------------------------------------------

    @property
    def session(self) -> str:
        return self._session

    @property
    def caps(self) -> Capabilities:
        return _DIR_CAPS

    @property
    def root(self) -> Path:
        """The real directory backing this workspace."""
        return self._root

    # -- surfaces ------------------------------------------------------

    @property
    def fs(self) -> Any:
        return self._fs

    @property
    def kv(self) -> MutableMapping[str, Any]:
        return self._kv

    @property
    def dirty(self) -> bool:
        return False  # no staging: writes are durable immediately

    # -- versioning: unsupported ---------------------------------------

    def _unsupported(self, op: str) -> NotSupportedError:
        return NotSupportedError(
            f"DirProvider is unversioned: {op}() is not supported. "
            "Use the kvgit backend for checkpoints, history, and forking."
        )

    @property
    def head(self) -> str:
        raise self._unsupported("head")

    def checkpoint(self, info: dict[str, Any] | None = None) -> str:
        raise self._unsupported("checkpoint")

    def restore(self, checkpoint_id: str) -> None:
        raise self._unsupported("restore")

    def history(self, *, limit: int | None = None) -> Iterable[CheckpointInfo]:
        raise self._unsupported("history")

    def fork(self, name: str) -> "DirProvider":
        raise self._unsupported("fork")

    def discard(self) -> None:
        raise self._unsupported("discard")

    # -- power modes / lifecycle ---------------------------------------

    def mount(self) -> Any:
        raise NotSupportedError(
            "DirProvider needs no mount(): the workspace is already a real "
            f"directory at {self._root}"
        )

    def close(self) -> None:
        self._closed = True
