"""Session-scoped cache: a persistent dict for the agent.

A prefix-scoped view over the provider's ``kv`` mapping, following the
agex conventions so agent mental models transfer verbatim:

- keys are plain strings; ``__``-prefixed keys (framework bookkeeping)
  and keys containing ``/`` (namespacing) are rejected at write time
- values must be picklable **data**; picklability is validated at
  write time so the agent gets an immediate ``CacheError`` instead of
  a poisoned value discovered on a later read
- the cache holds data, not code — sandbox-defined functions and
  classes are plain unpicklable objects; put reusable code under
  ``helpers/`` on the filesystem instead
"""

from __future__ import annotations

import pickle
from collections.abc import Iterator, MutableMapping
from typing import Any

PREFIX = "__cache__/"


class CacheError(ValueError):
    """Raised when a cache operation cannot complete (e.g. unpicklable value)."""


class Cache(MutableMapping[str, Any]):
    """A persistent dict for the agent, scoped to the session's kv store."""

    def __init__(self, kv: MutableMapping[str, Any]) -> None:
        self._kv = kv

    @staticmethod
    def _check_writable_key(key: Any) -> str:
        if not isinstance(key, str):
            raise TypeError(f"Cache keys must be strings, got {type(key).__name__}")
        if key.startswith("__"):
            raise ValueError(
                f"Cache keys may not start with '__' (reserved for framework): {key!r}"
            )
        if "/" in key:
            raise ValueError(
                f"Cache keys may not contain '/' (reserved for namespacing): {key!r}"
            )
        return PREFIX + key

    def __getitem__(self, key: str) -> Any:
        if not isinstance(key, str):
            raise TypeError(f"Cache keys must be strings, got {type(key).__name__}")
        return self._kv[PREFIX + key]

    def __setitem__(self, key: str, value: Any) -> None:
        qualified = self._check_writable_key(key)
        try:
            pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
        except Exception as exc:
            raise CacheError(
                f"Cannot cache key {key!r}: value is not picklable ({exc}). "
                f"The cache holds data; for reusable code use helpers/."
            ) from exc
        self._kv[qualified] = value

    def __delitem__(self, key: str) -> None:
        if not isinstance(key, str):
            raise TypeError(f"Cache keys must be strings, got {type(key).__name__}")
        del self._kv[PREFIX + key]

    def __iter__(self) -> Iterator[str]:
        plen = len(PREFIX)
        for k in list(self._kv.keys()):
            if k.startswith(PREFIX):
                yield k[plen:]

    def __len__(self) -> int:
        return sum(1 for k in self._kv.keys() if k.startswith(PREFIX))

    def __contains__(self, key: object) -> bool:
        if not isinstance(key, str):
            return False
        return PREFIX + key in self._kv

    def __repr__(self) -> str:
        try:
            keys = sorted(self)
        except Exception:
            return "Cache(<unreadable>)"
        return f"Cache({keys!r})"


class RemoteCache(MutableMapping[str, Any]):
    """Worker-side cache stub for process/kernel isolation.

    A bare ``RpcProxy`` can't serve mapping syntax — dunder lookup
    bypasses ``__getattr__`` — so the parent ships
    ``RpcProxyMarker(wrapper="nontainer.cache:RemoteCache")`` and the
    worker wraps the proxy in this MutableMapping. Method names match
    ``LocalExecutor._cache_rpc_handler``'s dispatch; the real ``Cache``
    (and its versioned kv) stays in the parent."""

    def __init__(self, proxy: Any) -> None:
        self._proxy = proxy

    def __getitem__(self, key: str) -> Any:
        return self._proxy._call("getitem", key)

    def __setitem__(self, key: str, value: Any) -> None:
        self._proxy._call("setitem", key, value)

    def __delitem__(self, key: str) -> None:
        self._proxy._call("delitem", key)

    def __iter__(self) -> Iterator[str]:
        return iter(self._proxy._call("iter"))

    def __len__(self) -> int:
        return self._proxy._call("len")

    def __contains__(self, key: object) -> bool:
        return self._proxy._call("contains", key)

    def __repr__(self) -> str:
        return "RemoteCache()"
