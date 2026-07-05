"""AgentFSProvider: Turso AgentFS as the workspace substrate (spike).

One SQLite file per session — the workspace-as-artifact backend: copy
the ``.db`` to back it up, open it with any sqlite tool to audit it.
Requires the ``[agentfs]`` extra (``agentfs-sdk``).

What this spike provides:

- ``fs``: a sync adapter over the (async-only) AgentFS Python SDK,
  satisfying both the termish 16-method protocol and the monkeyfs
  patch surface (``open()``, ``getsize``, ...) — so terminal AND
  sandboxed python work unchanged.
- ``kv``: a ``MutableMapping`` over AgentFS's KV. AgentFS values are
  JSON-typed; non-JSON picklables are wrapped as
  ``{"__pickle_b64__": ...}`` transparently (the fidelity/
  inspectability tradeoff: plain values stay SQL-readable, exotic
  ones ride through opaquely).
- capabilities: ``versioned=False`` for the spike (AgentFS snapshots
  are whole-file copies; wiring them as checkpoint/restore is future
  work), ``sql_audit=True`` (the substrate is a queryable SQLite db).

Sync facade: the SDK is async-only, so each provider owns a
background event-loop thread; every operation round-trips through it
(``run_coroutine_threadsafe``). Fine at agent timescales.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import pickle
import posixpath
import threading
from collections.abc import Iterable, Iterator, MutableMapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..errors import NotSupportedError
from ..protocol import Capabilities, CheckpointInfo, validate_session_id

_AGENTFS_CAPS = Capabilities(
    versioned=False,
    staging=False,
    cheap_fork=False,
    merge=False,
    sql_audit=True,
    fuse_mount=False,
)

_PICKLE_KEY = "__pickle_b64__"


class _Loop:
    """A dedicated event-loop thread; sync facade over an async SDK."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def call(self, coro: Any) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    def close(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)


def _iso(epoch: int | float | None) -> str:
    if not epoch:
        return ""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


class _AgentFsFS:
    """Sync fs adapter: termish protocol + monkeyfs patch surface."""

    def __init__(self, afs: Any, loop: _Loop) -> None:
        self._fs = afs
        self._loop = loop
        self._cwd = "/"

    # -- path handling -------------------------------------------------

    def resolve_path(self, path: str) -> str:
        if not path or path == ".":
            path = self._cwd
        if not path.startswith("/"):
            path = posixpath.join(self._cwd, path)
        norm = posixpath.normpath(path)
        return norm if norm.startswith("/") else "/" + norm

    # -- cwd -------------------------------------------------------------

    def getcwd(self) -> str:
        return self._cwd

    def chdir(self, path: str) -> None:
        resolved = self.resolve_path(path)
        if resolved != "/" and not self.isdir(resolved):
            raise FileNotFoundError(f"No such directory: '{path}'")
        self._cwd = resolved

    # -- stat family -----------------------------------------------------

    def _stat_raw(self, path: str) -> Any | None:
        from agentfs_sdk import ErrnoException

        try:
            return self._loop.call(self._fs.stat(self.resolve_path(path)))
        except ErrnoException:
            return None

    def exists(self, path: str) -> bool:
        p = self.resolve_path(path)
        return p == "/" or self._stat_raw(p) is not None

    def isfile(self, path: str) -> bool:
        s = self._stat_raw(path)
        return bool(s and s.is_file())

    def isdir(self, path: str) -> bool:
        p = self.resolve_path(path)
        if p == "/":
            return True
        s = self._stat_raw(p)
        return bool(s and s.is_directory())

    def stat(self, path: str) -> Any:
        from monkeyfs.base import FileMetadata

        p = self.resolve_path(path)
        if p == "/":
            return FileMetadata(size=0, created_at="", modified_at="", is_dir=True)
        s = self._stat_raw(p)
        if s is None:
            raise FileNotFoundError(f"No such file or directory: '{path}'")
        return FileMetadata(
            size=s.size,
            created_at=_iso(getattr(s, "ctime", None)),
            modified_at=_iso(getattr(s, "mtime", None)),
            is_dir=s.is_directory(),
        )

    def getsize(self, path: str) -> int:
        return self.stat(path).size

    # -- read/write ------------------------------------------------------

    def read(self, path: str) -> bytes:
        from agentfs_sdk import ErrnoException

        try:
            data = self._loop.call(
                self._fs.read_file(self.resolve_path(path), encoding=None)
            )
        except ErrnoException as e:
            raise FileNotFoundError(str(e)) from e
        return data if isinstance(data, bytes) else data.encode()

    def write(self, path: str, content: bytes | str, mode: str = "w") -> None:
        data = content.encode() if isinstance(content, str) else content
        p = self.resolve_path(path)
        if mode.startswith("a") and self.exists(p) and self.isfile(p):
            data = self.read(p) + data
        # AgentFS write_file auto-creates parent directories.
        self._loop.call(self._fs.write_file(p, data))

    def touch(self, path: str) -> None:
        p = self.resolve_path(path)
        if not self.exists(p):
            self.write(p, b"")

    def open(self, path: str, mode: str = "r", **kwargs: Any) -> Any:
        """File-like objects for the monkeyfs open() patch."""
        binary = "b" in mode
        writing = any(c in mode for c in "wax")
        appending = "a" in mode
        p = self.resolve_path(path)

        if not writing and "r" in mode:
            data = self.read(p)  # raises FileNotFoundError if missing
            return io.BytesIO(data) if binary else io.StringIO(data.decode())

        adapter = self

        class _WriteBuffer(io.BytesIO if binary else io.StringIO):  # type: ignore[misc]
            def close(inner) -> None:  # noqa: N805
                content = inner.getvalue()
                adapter.write(p, content, mode="a" if appending else "w")
                super().close()

            def __exit__(inner, *exc: Any) -> None:  # noqa: N805
                inner.close()

        return _WriteBuffer()

    # -- directories -----------------------------------------------------

    def mkdir(self, path: str, parents: bool = False, exist_ok: bool = False) -> None:
        from agentfs_sdk import ErrnoException

        p = self.resolve_path(path)
        if parents:
            self.makedirs(p, exist_ok=exist_ok)
            return
        try:
            self._loop.call(self._fs.mkdir(p))
        except ErrnoException as e:
            if "EEXIST" in str(e):
                if not exist_ok:
                    raise FileExistsError(str(e)) from e
            else:
                raise

    def makedirs(self, path: str, exist_ok: bool = True) -> None:
        from agentfs_sdk import ErrnoException

        p = self.resolve_path(path)
        parts = [seg for seg in p.split("/") if seg]
        cur = ""
        for seg in parts:
            cur += "/" + seg
            try:
                self._loop.call(self._fs.mkdir(cur))
            except ErrnoException as e:
                if "EEXIST" not in str(e):
                    raise
        if not exist_ok and not parts:
            raise FileExistsError(path)

    def remove(self, path: str) -> None:
        from agentfs_sdk import ErrnoException

        try:
            self._loop.call(self._fs.unlink(self.resolve_path(path)))
        except ErrnoException as e:
            raise FileNotFoundError(str(e)) from e

    def rmdir(self, path: str) -> None:
        from agentfs_sdk import ErrnoException

        try:
            self._loop.call(self._fs.rmdir(self.resolve_path(path)))
        except ErrnoException as e:
            raise OSError(str(e)) from e

    def rename(self, src: str, dst: str) -> None:
        from agentfs_sdk import ErrnoException

        try:
            self._loop.call(
                self._fs.rename(self.resolve_path(src), self.resolve_path(dst))
            )
        except ErrnoException as e:
            raise OSError(str(e)) from e

    # -- listing ---------------------------------------------------------

    def list(self, path: str = ".", recursive: bool = False) -> list[str]:
        p = self.resolve_path(path)
        entries = self._loop.call(self._fs.readdir(p))
        if not recursive:
            return sorted(entries)
        out: list[str] = []
        for name in entries:
            child = posixpath.join(p, name)
            rel = name
            out.append(rel)
            if self.isdir(child):
                out.extend(
                    posixpath.join(rel, sub)
                    for sub in self.list(child, recursive=True)
                )
        return sorted(out)

    def list_detailed(self, path: str = ".", recursive: bool = False) -> list[Any]:
        from termish.fs import FileInfo

        p = self.resolve_path(path)
        out: list[Any] = []
        for rel in self.list(p, recursive=recursive):
            full = posixpath.join(p, rel)
            meta = self.stat(full)
            out.append(
                FileInfo(
                    name=posixpath.basename(rel),
                    path=rel,
                    size=meta.size,
                    created_at=meta.created_at,
                    modified_at=meta.modified_at,
                    is_dir=meta.is_dir,
                )
            )
        return out

    def glob(self, pattern: str) -> list[str]:
        import fnmatch

        if pattern.startswith("/"):
            base, rel_out = "/", False
        else:
            base, rel_out = self._cwd, True
        matches = []
        for rel in self.list(base, recursive=True):
            candidate = rel if rel_out else posixpath.join(base, rel)
            if fnmatch.fnmatch(candidate, pattern):
                matches.append(candidate)
        return sorted(matches)


class _AgentFsKV(MutableMapping[str, Any]):
    """MutableMapping over AgentFS KV with pickle-b64 fallback for
    non-JSON values (plain JSON values stay SQL-inspectable)."""

    def __init__(self, kv: Any, loop: _Loop) -> None:
        self._kv = kv
        self._loop = loop

    @staticmethod
    def _encode(value: Any) -> Any:
        # JSON pass-through only when the value ROUND-TRIPS identically
        # (json.dumps accepts tuples but returns them as lists — that
        # would silently change the value). Marker-shaped dicts are
        # force-pickled so decode can't misread user data.
        marker_shaped = isinstance(value, dict) and _PICKLE_KEY in value
        if not marker_shaped:
            try:
                # (equality is type-sensitive enough here: (1,2) != [1,2],
                # {1: 'a'} != {'1': 'a'} — non-round-tripping values fall
                # through to pickle)
                if json.loads(json.dumps(value)) == value:
                    return value
            except (TypeError, ValueError):
                pass
        raw = pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
        return {_PICKLE_KEY: base64.b64encode(raw).decode()}

    @staticmethod
    def _decode(value: Any) -> Any:
        if isinstance(value, dict) and set(value) == {_PICKLE_KEY}:
            return pickle.loads(base64.b64decode(value[_PICKLE_KEY]))
        return value

    def __getitem__(self, key: str) -> Any:
        sentinel = object()
        value = self._loop.call(self._kv.get(key, sentinel))
        if value is sentinel:
            raise KeyError(key)
        return self._decode(value)

    def __setitem__(self, key: str, value: Any) -> None:
        self._loop.call(self._kv.set(key, self._encode(value)))

    def __delitem__(self, key: str) -> None:
        if key not in self:
            raise KeyError(key)
        self._loop.call(self._kv.delete(key))

    def __iter__(self) -> Iterator[str]:
        for entry in self._loop.call(self._kv.list("")):
            yield entry["key"]

    def __len__(self) -> int:
        return len(list(iter(self)))

    def __contains__(self, key: object) -> bool:
        if not isinstance(key, str):
            return False
        sentinel = object()
        return self._loop.call(self._kv.get(key, sentinel)) is not sentinel

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default


class AgentFSProvider:
    """``WorkspaceProvider`` over a Turso AgentFS SQLite file."""

    def __init__(self, db_path: str | Path, *, session: str) -> None:
        try:
            from agentfs_sdk import AgentFS, AgentFSOptions
        except ImportError as e:
            raise ImportError(
                "AgentFSProvider requires the agentfs extra: "
                "pip install nontainer[agentfs]"
            ) from e

        validate_session_id(session)
        self._session = session
        self._db_path = Path(db_path).expanduser()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._loop = _Loop()
        self._afs = self._loop.call(
            AgentFS.open(AgentFSOptions(path=str(self._db_path)))
        )
        self._fs = _AgentFsFS(self._afs.fs, self._loop)
        self._kv = _AgentFsKV(self._afs.kv, self._loop)
        self._closed = False

    # -- identity ------------------------------------------------------

    @property
    def session(self) -> str:
        return self._session

    @property
    def caps(self) -> Capabilities:
        return _AGENTFS_CAPS

    @property
    def db_path(self) -> Path:
        """The SQLite file backing this workspace (the artifact)."""
        return self._db_path

    # -- surfaces ------------------------------------------------------

    @property
    def fs(self) -> Any:
        return self._fs

    @property
    def kv(self) -> MutableMapping[str, Any]:
        return self._kv

    # -- versioning: not in the spike ------------------------------------

    def _unsupported(self, op: str) -> NotSupportedError:
        return NotSupportedError(
            f"AgentFSProvider does not support {op}() (spike scope: AgentFS "
            "snapshots are whole-file copies; wiring them as checkpoints is "
            "future work). Use the kvgit backend for versioning."
        )

    def checkpoint(self, info: dict[str, Any] | None = None) -> str:
        raise self._unsupported("checkpoint")

    def restore(self, checkpoint_id: str) -> None:
        raise self._unsupported("restore")

    def history(self, *, limit: int | None = None) -> Iterable[CheckpointInfo]:
        raise self._unsupported("history")

    def fork(self, name: str) -> "AgentFSProvider":
        raise self._unsupported("fork")

    def discard(self) -> None:
        raise self._unsupported("discard")

    def mount(self) -> Any:
        raise NotSupportedError(
            "FUSE mounting is not exposed by the AgentFS Python SDK; "
            "use AgentFS's own tooling to mount the db file."
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._loop.call(self._afs.close())
        finally:
            self._loop.close()
