"""The filesystem a workspace hands out: where cwd lives, and the
wrappers that keep host writes in step and refuse a frozen one's."""

from __future__ import annotations

from collections.abc import Callable, Mapping, MutableMapping
from typing import Any


def _cwd_key() -> str:
    """The one key the agent's working directory persists under.

    monkeyfs's ``VirtualFS`` resolves every relative path against this
    key, and writes it on ``chdir`` — so on the versioned backend the
    filesystem already owns cwd, and a commit captures it because the
    key is in the same mapping as the files. nontainer used to keep a
    second key of its own beside it: two keys for one fact, two merge
    functions, and nothing to say which won a disagreement.
    """
    from monkeyfs import VirtualFS

    return VirtualFS.CWD_KEY


def _quiet_isdir(fs: Any, path: str) -> bool:
    try:
        return bool(fs.isdir(path))
    except Exception:
        return False


def _owns_cwd(provider_fs: Any) -> bool:
    """Whether the provider's filesystem owns the cwd key itself.

    True for monkeyfs ``VirtualFS`` (the kvgit backend): cwd is a key
    in the provider's kv, written on ``chdir``, so it commits, forks
    and checks out with the files — and nontainer must not write it a
    second time. It must not write it at all there, in fact: with
    mounts the workspace's cwd is the composed ``MountFS``'s, and that
    composition REQUIRES the filesystem underneath to stay at the root
    (it hands that one paths it has already resolved), so a composed
    cwd stored under this key would break every relative path. Mounted
    trees are unversioned live views by contract, and on that backend
    their cwd is now equally transient: it starts at the workspace root
    each time the session is opened.

    False for the filesystems that hold cwd in memory (a plain
    ``IsolatedFS``, the AgentFS adapter). There the workspace
    writes the key itself — inert to the filesystem, read back when the
    session is reopened, which is the persistence those backends would
    otherwise have no way to get.
    """
    from monkeyfs import VirtualFS

    return isinstance(provider_fs, VirtualFS)


_MUTATING_FS_METHODS = frozenset(
    {"write", "mkdir", "makedirs", "remove", "rmdir", "rename", "chdir"}
)


class _SyncingFS:
    """``ws.files.fs`` wrapper: host-side writes mark the executor stale.

    ``ws.files.fs`` is the documented host-side escape hatch (seeding
    inputs, harvesting artifacts) and it writes straight into the
    provider — behind a remote executor's back. Without this, the guest tree never
    learned: a host write landed in the provider and the guest kept
    serving its stale baseline until some *other* path happened to call
    ``sync()``. That made the failure nondeterministic, which is the
    worst way for it to present — the apps runtime's ``api.log`` was
    invisible to ``cat`` from the terminal unless an unrelated write
    intervened, so the agent's documented repair loop read as broken.

    Marking is LAZY on purpose. ``DudExecutor.sync()`` re-pushes the
    whole tree (tar + ``push_tree``), so syncing per write would turn
    an N-file seeding loop into N wholesale pushes; the workspace
    instead syncs once, before the next execution needs the guest to be
    current. The executor itself gets the RAW fs via
    ``ExecutionContext`` — its own writes are already guest-side and
    must not mark anything.

    Reads delegate untouched, so this stays a pure write-side concern.
    """

    __slots__ = ("_fs", "_mark")

    def __init__(self, fs: Any, mark: Callable[[], None]) -> None:
        self._fs = fs
        self._mark = mark

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._fs, name)
        if name not in _MUTATING_FS_METHODS or not callable(attr):
            return attr

        def _marking(*args: Any, **kwargs: Any) -> Any:
            result = attr(*args, **kwargs)
            self._mark()
            return result

        return _marking

    def __repr__(self) -> str:
        return f"<syncing {self._fs!r}>"


def _point_over(path: str, points: Mapping[str, Any]) -> str | None:
    """The MOST SPECIFIC mount point ``path`` lies at or under, or None.

    Mount points nest — a writable directory inside a read-only one,
    an attachment inside either — and the composition routes a path to
    the deepest point that covers it. Anything that reasons about
    where a path lands has to pick the same one.
    """
    best: str | None = None
    for point in points:
        if path == point or path.startswith(point + "/"):
            if best is None or len(point) > len(best):
                best = point
    return best


def _frozen_message(tag: str | None) -> str:
    """What a frozen workspace tells whoever tried to write to it."""
    where = f" at tag {tag!r}" if tag else ""
    return f"this workspace is a frozen snapshot{where}; it accepts no writes"


class _FrozenKV(MutableMapping):
    """Read-only view of the provider's kv for a frozen workspace.

    The executor builds the agent-facing ``cache`` on whatever kv the
    context carries, so a snapshot has to hand it a mapping that refuses
    writes — otherwise ``cache['x'] = 1`` succeeds against a checkout
    that can never commit it, in-process and in a guest's cache service
    alike. A ``MutableMapping`` so the derived mutators (``pop``,
    ``clear``, ``update``, ``setdefault``) route through the two that
    raise rather than reaching the underlying store.
    """

    def __init__(self, kv: MutableMapping[str, Any], tag: str | None) -> None:
        self._kv = kv
        self._message = _frozen_message(tag)

    def __getitem__(self, key: str) -> Any:
        return self._kv[key]

    def __iter__(self) -> Any:
        return iter(self._kv)

    def __len__(self) -> int:
        return len(self._kv)

    def __contains__(self, key: object) -> bool:
        return key in self._kv

    def get(self, key: str, default: Any = None) -> Any:
        return self._kv.get(key, default)

    def __setitem__(self, key: str, value: Any) -> None:
        raise PermissionError(self._message)

    def __delitem__(self, key: str) -> None:
        raise PermissionError(self._message)


def _frozen_fs(fs: Any, tag: str | None) -> Any:
    """A read-only view over a frozen workspace's filesystem.

    The executor gets this instead of the live fs, so a write attempted
    by agent code — a shell redirect, ``open(..., "w")``, a python
    ``os.remove`` — is refused where it happens, with a message the
    agent can act on rather than a silent write that could never be
    committed. Reads pass straight through.
    """
    from monkeyfs import ReadOnlyFS

    message = _frozen_message(tag)

    class FrozenFS(ReadOnlyFS):
        # Two refusal paths to override, because the wrapper has two:
        # mode-sensitive operations call ``_deny`` directly, and a
        # mutating method is served by a stand-in built by
        # ``_refuse_write``. Both say the same thing here.
        def _deny(self) -> None:  # type: ignore[override]
            raise PermissionError(message)

        def _refuse_write(self, name: str) -> Callable[..., Any]:  # type: ignore[override]
            def denied(*args: Any, **kwargs: Any) -> Any:
                raise PermissionError(f"{message} ({name}() would change them)")

            denied.__name__ = name
            return denied

        def touch(self, path: str) -> None:
            self._deny()

    return FrozenFS(fs)
