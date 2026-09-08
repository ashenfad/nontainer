"""What a session's filesystem shows: sparse views and attachments.

Two filesystem wrappers, both in the family monkeyfs's ``MountFS`` and
``ReadOnlyFS`` belong to — a ``FileSystem`` over a ``FileSystem``,
composed once when the workspace is built:

- :class:`ViewFS` is the **sparse view** a fork can be given. The
  child's branch still holds everything its parent had; only what it
  lists and reads is narrowed. That is what makes "push this content
  to the delegate" cost no prune commit, so the merge back stays an
  ordinary three-way with no rule to special-case.
- :class:`AttachFS` is where :meth:`Workspace.files.attach` mounts
  another session's frozen tree. It is always in the chain, because
  the executor is handed the filesystem object once at open and an
  attachment made later has to be visible through the same object.

**The write rule.** A narrowed session may CREATE a new path anywhere
— notes and scratch are ordinary, and a new path merges as an
addition. Modifying or deleting a path that exists outside the view is
refused with ``PermissionError``: you cannot overwrite what you cannot
see. Created paths join the view, because a delegate that could not
read back its own note would be a worse deal than git's sparse
checkout, not a better one.

**Where the view is recorded.** :data:`VIEW_KEY`, a reserved store key
beside the ws-git blob, so it survives reopen and travels with a fork.
It merges ``MergeChoice.OURS``: a view describes the session reading
it, and a caller merging a narrowed delegate keeps its own (usually
absent, meaning the whole tree).

The record holds two lists, because two questions need them. The
**view** is what the session can see right now and is what the write
rule is checked against; the **seed** is what it was given and never
grows. A diff groups by the seed — the note a delegate wrote is in its
view because it made it, and it is exactly the collateral a caller
looking at the diff must not miss.
"""

from __future__ import annotations

import json
import posixpath
from typing import Any

#: Reserved store key holding this session's view, when it has one. Not
#: a file key (the VFS prefixes its own), so no path can collide with
#: it. Absent means the full tree — a session with the whole tree
#: records nothing.
VIEW_KEY = "__ws_view__"

#: Layout version of the record.
VIEW_VERSION = 1


def _record(raw: Any) -> dict | None:
    """A view record, parsed, or ``None`` where there is none to read.

    Tolerant of absence and of a layout it does not know: either means
    "this session sees everything", which is the answer that cannot
    hide a file from its owner by accident.
    """
    if isinstance(raw, dict):
        parsed: Any = raw
    elif raw is None:
        return None
    else:
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return None
    if not isinstance(parsed, dict) or parsed.get("version") != VIEW_VERSION:
        return None
    return parsed


def _paths(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(sorted({p for p in value if isinstance(p, str) and p.startswith("/")}))


def parse_view(raw: Any) -> tuple[str, ...] | None:
    """What this session can see now, or ``None`` for the full tree."""
    parsed = _record(raw)
    if parsed is None:
        return None
    return _paths(parsed.get("paths")) or None


def parse_seed(raw: Any) -> tuple[str, ...]:
    """What this session was GIVEN — its view minus what it has made
    since. Empty for a session that sees the whole tree, and what a
    diff groups by: the paths a delegate created are its own, and they
    are exactly the collateral a caller must not miss."""
    parsed = _record(raw)
    if parsed is None:
        return ()
    return _paths(parsed.get("seed")) or _paths(parsed.get("paths"))


def encode_view(
    paths: "tuple[str, ...] | list[str]",
    seed: "tuple[str, ...] | list[str] | None" = None,
) -> bytes:
    return json.dumps(
        {
            "version": VIEW_VERSION,
            "paths": sorted(set(paths)),
            "seed": sorted(set(paths if seed is None else seed)),
        },
        sort_keys=True,
    ).encode()


def normalize_view(paths: "Any", root: str) -> tuple[str, ...]:
    """Caller-spelled seed paths → workspace-absolute, normalized.

    A seed is a directory or a file, spelled the way the caller thinks
    of it: absolute (``/workspace/auth``) or relative to the workspace
    root (``auth``), with or without a trailing slash. Empty is
    rejected rather than read as "see nothing": a session that can see
    nothing cannot even list its own root, and a caller who means the
    whole tree passes ``None``.
    """
    if isinstance(paths, str):
        paths = [paths]
    out: set[str] = set()
    for entry in paths:
        if not isinstance(entry, str) or not entry.strip():
            raise ValueError(f"view paths must be non-empty strings: {entry!r}")
        text = entry.strip().rstrip("/") or "/"
        if not text.startswith("/"):
            text = posixpath.join(root, text)
        out.add(posixpath.normpath(text))
    if not out:
        raise ValueError(
            "paths= must name at least one path; pass paths=None for the whole tree"
        )
    return tuple(sorted(out))


class ViewFS:
    """A filesystem narrowed to a subset of the tree beneath it.

    Everything outside the view reads as absent — ``exists`` is False,
    ``list`` does not name it, ``read`` and ``stat`` raise
    ``FileNotFoundError`` — while the tree underneath still holds it,
    which is what keeps a merge back ordinary. Writes follow the rule
    in this module's docstring: new paths anywhere, no modification or
    deletion of what the view hides.

    Ancestors of a view entry stay visible as directories, or the
    session could not list its own root to reach its seed.
    """

    __slots__ = ("_fs", "_paths", "_extend")

    def __init__(
        self,
        fs: Any,
        paths: "tuple[str, ...]",
        *,
        extend: "Any" = None,
    ) -> None:
        self._fs = fs
        self._paths = tuple(sorted(set(paths)))
        # Called with the new full view whenever a created path widens
        # it, so the record on the branch keeps up with what the
        # session can see. ``None`` in tests and other read-only uses.
        self._extend = extend

    # -- the view -------------------------------------------------------

    @property
    def view(self) -> tuple[str, ...]:
        return self._paths

    def __repr__(self) -> str:
        return f"<view of {type(self._fs).__name__}: {list(self._paths)}>"

    def _abs(self, path: str) -> str:
        if path.startswith("/"):
            return posixpath.normpath(path)
        return posixpath.normpath(posixpath.join(self._fs.getcwd(), path))

    def _visible_abs(self, target: str) -> bool:
        for entry in self._paths:
            if target == entry or target.startswith(entry + "/"):
                return True  # in the view
            if entry.startswith(target + "/") or target == "/":
                return True  # a directory on the way to the view
        return False

    def _visible(self, path: str) -> bool:
        return self._visible_abs(self._abs(path))

    def _hidden(self, path: str) -> str:
        return (
            f"{path!r} is outside this session's view "
            f"({', '.join(self._paths)}) — it holds a file this session "
            "was not given, and a path it cannot see is one it may not "
            "change. Write somewhere new instead."
        )

    def _refuse_missing(self, path: str, op: str) -> None:
        """A hidden path answers reads the way an absent one does."""
        raise FileNotFoundError(f"No such file or directory: {path!r} ({op})")

    def _guard_write(self, path: str) -> None:
        """The write rule, in one place: a new path anywhere, never a
        change to one the view hides."""
        target = self._abs(path)
        if self._visible_abs(target):
            return
        if self._fs.exists(path):
            raise PermissionError(self._hidden(path))
        self._widen(target)

    def sees(self, path: str) -> bool:
        """Whether this session's view shows ``path`` at all.

        The visibility question on its own, for code that reaches the
        tree through the provider rather than through this filesystem —
        the agent's index, whose ``working_files`` is the whole branch
        and would otherwise let an agent stage a file it cannot see.
        """
        return self._visible(path)

    def refuse_reason(self, path: str) -> str | None:
        """Why a write to ``path`` would be refused, or ``None``.

        The write rule asked rather than enforced, for a caller that
        has to decide before it starts: a guest rung harvests a whole
        call's writes at once, and applying half of them and then
        refusing would leave the session holding work the call is about
        to be told did not happen.
        """
        if self._visible(path) or not self._fs.exists(path):
            return None
        return self._hidden(path)

    def _widen(self, target: str) -> None:
        self._paths = tuple(sorted({*self._paths, target}))
        if self._extend is not None:
            self._extend(self._paths)

    # -- reads ----------------------------------------------------------
    #
    # Every forward takes ``*args, **kwargs``: the guard is about the
    # path, and the rest of a filesystem method's signature belongs to
    # the filesystem underneath — mirroring it here is how a wrapper
    # silently drops an argument a backend grew.

    def read(self, path: str, *args: Any, **kwargs: Any) -> Any:
        if not self._visible(path):
            self._refuse_missing(path, "read")
        return self._fs.read(path, *args, **kwargs)

    def exists(self, path: str) -> bool:
        return self._visible(path) and self._fs.exists(path)

    def lexists(self, path: str) -> bool:
        return self._visible(path) and self._fs.lexists(path)

    def isfile(self, path: str) -> bool:
        return self._visible(path) and self._fs.isfile(path)

    def isdir(self, path: str) -> bool:
        return self._visible(path) and self._fs.isdir(path)

    def islink(self, path: str) -> bool:
        return self._visible(path) and self._fs.islink(path)

    def stat(self, path: str, *args: Any, **kwargs: Any) -> Any:
        if not self._visible(path):
            self._refuse_missing(path, "stat")
        return self._fs.stat(path, *args, **kwargs)

    def getsize(self, path: str) -> int:
        if not self._visible(path):
            self._refuse_missing(path, "getsize")
        return self._fs.getsize(path)

    def readlink(self, path: str) -> str:
        if not self._visible(path):
            self._refuse_missing(path, "readlink")
        return self._fs.readlink(path)

    def access(self, path: str, mode: int) -> bool:
        return self._visible(path) and self._fs.access(path, mode)

    def samefile(self, path1: str, path2: str) -> bool:
        for path in (path1, path2):
            if not self._visible(path):
                self._refuse_missing(path, "samefile")
        return self._fs.samefile(path1, path2)

    def list(self, path: str = ".", recursive: bool = False) -> list[str]:
        if not self._visible(path):
            self._refuse_missing(path, "list")
        base = self._abs(path)
        return [
            entry
            for entry in self._fs.list(path, recursive=recursive)
            if self._visible_abs(posixpath.normpath(posixpath.join(base, entry)))
        ]

    def list_detailed(self, path: str = ".", recursive: bool = False) -> list[Any]:
        if not self._visible(path):
            self._refuse_missing(path, "list_detailed")
        base = self._abs(path)
        return [
            info
            for info in self._fs.list_detailed(path, recursive=recursive)
            if self._visible_abs(
                posixpath.normpath(posixpath.join(base, getattr(info, "name", "")))
            )
        ]

    def glob(self, pattern: str) -> list[str]:
        return [hit for hit in self._fs.glob(pattern) if self._visible(hit)]

    def get_metadata_snapshot(self) -> dict[str, Any]:
        # Root-relative keys, so the leading slash goes back on before
        # the view is asked about them.
        return {
            path: meta
            for path, meta in self._fs.get_metadata_snapshot().items()
            if self._visible_abs(posixpath.normpath("/" + path.lstrip("/")))
        }

    # -- writes ---------------------------------------------------------

    def write(self, path: str, *args: Any, **kwargs: Any) -> None:
        self._guard_write(path)
        self._fs.write(path, *args, **kwargs)

    def write_many(self, files: "dict[str, Any]", *args: Any, **kwargs: Any) -> None:
        for path in files:
            self._guard_write(path)
        self._fs.write_many(files, *args, **kwargs)

    def open(self, path: str, mode: str = "r", **kwargs: Any) -> Any:
        if any(c in mode for c in "wax+"):
            self._guard_write(path)
        elif not self._visible(path):
            self._refuse_missing(path, "open")
        return self._fs.open(path, mode, **kwargs)

    def remove(self, path: str, *args: Any, **kwargs: Any) -> None:
        self._guard_change(path, "remove")
        self._fs.remove(path, *args, **kwargs)

    def remove_many(self, paths: "list[str]", *args: Any, **kwargs: Any) -> None:
        for path in paths:
            self._guard_change(path, "remove")
        self._fs.remove_many(paths, *args, **kwargs)

    def rmdir(self, path: str, *args: Any, **kwargs: Any) -> None:
        self._guard_change(path, "rmdir")
        self._fs.rmdir(path, *args, **kwargs)

    def truncate(self, path: str, *args: Any, **kwargs: Any) -> None:
        self._guard_change(path, "truncate")
        self._fs.truncate(path, *args, **kwargs)

    def chmod(self, path: str, *args: Any, **kwargs: Any) -> None:
        self._guard_change(path, "chmod")
        self._fs.chmod(path, *args, **kwargs)

    def chown(self, path: str, *args: Any, **kwargs: Any) -> None:
        self._guard_change(path, "chown")
        self._fs.chown(path, *args, **kwargs)

    def utime(self, path: str, *args: Any, **kwargs: Any) -> None:
        self._guard_change(path, "utime")
        self._fs.utime(path, *args, **kwargs)

    def rename(self, src: str, dst: str, *args: Any, **kwargs: Any) -> None:
        self._guard_change(src, "rename")
        self._guard_write(dst)
        self._fs.rename(src, dst, *args, **kwargs)

    def replace(self, src: str, dst: str, *args: Any, **kwargs: Any) -> None:
        self._guard_change(src, "replace")
        self._guard_write(dst)
        self._fs.replace(src, dst, *args, **kwargs)

    def link(self, src: str, dst: str, *args: Any, **kwargs: Any) -> None:
        self._guard_change(src, "link")
        self._guard_write(dst)
        self._fs.link(src, dst, *args, **kwargs)

    def symlink(self, src: str, dst: str, *args: Any, **kwargs: Any) -> None:
        self._guard_write(dst)
        self._fs.symlink(src, dst, *args, **kwargs)

    def _guard_change(self, path: str, op: str) -> None:
        """Changing or dropping an EXISTING path: only inside the view.

        Outside it the answer depends on what is really there — a path
        the view hides is refused by name, and one that is simply not
        there gets the filesystem's own ``FileNotFoundError``.
        """
        if self._visible(path):
            return
        if self._fs.exists(path):
            raise PermissionError(self._hidden(path))
        self._refuse_missing(path, op)

    # -- directories and position ---------------------------------------

    def mkdir(self, path: str, *args: Any, **kwargs: Any) -> None:
        # A directory is a container, never content: making one (or
        # finding it already there) changes no file the view hides, and
        # refusing it would break the parent-creating write that is the
        # ordinary way to put a new file under an unseen directory.
        self._fs.mkdir(path, *args, **kwargs)

    def makedirs(self, path: str, *args: Any, **kwargs: Any) -> None:
        self._fs.makedirs(path, *args, **kwargs)

    def chdir(self, path: str) -> None:
        if not self._visible(path):
            self._refuse_missing(path, "chdir")
        self._fs.chdir(path)

    def getcwd(self) -> str:
        return self._fs.getcwd()

    def realpath(self, path: str) -> str:
        return self._fs.realpath(path)

    def resolve_path(self, path: str) -> str:
        return self._fs.resolve_path(path)

    def invalidate(self) -> None:
        invalidate = getattr(self._fs, "invalidate", None)
        if invalidate is not None:
            invalidate()

    def mount(self, prefix: str, fs: Any) -> None:
        raise PermissionError(
            "a narrowed session cannot mount: composition is the "
            "workspace's, and the view sits under it"
        )


class AttachFS:
    """The session's filesystem, plus whatever is attached to it.

    Always in the chain, and transparent while nothing is attached: the
    executor is handed one filesystem object when it opens, so an
    attachment made later has to arrive through that same object rather
    than by replacing it.

    An attachment is a ``MountFS`` mount like a workspace ``Mount`` —
    unversioned, explicit, and gone when the session closes. Composing
    one moves the working directory into the composition, which needs
    the filesystem underneath at the root (it hands that one paths it
    has already resolved), so an attached session's cwd stops
    persisting for as long as anything is attached — the same trade a
    configured mount already makes.
    """

    __slots__ = ("_base", "_attached", "_active")

    def __init__(self, base: Any) -> None:
        self._base = base
        self._attached: dict[str, Any] = {}
        self._active = base

    def __repr__(self) -> str:
        return f"<{type(self._base).__name__} + {sorted(self._attached)}>"

    def __getattr__(self, name: str) -> Any:
        # Private and dunder names are never forwarded: the interpreter
        # probes them on arbitrary objects and expects AttributeError,
        # and this also stops the recursion an instance built without
        # __init__ would hit looking up its own slots.
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._active, name)

    @property
    def attachments(self) -> dict[str, Any]:
        return dict(self._attached)

    def attach(self, at: str, fs: Any) -> None:
        if at in self._attached:
            raise ValueError(f"already attached at {at!r}")
        self._attached[at] = fs
        self._compose()

    def detach(self, at: str) -> None:
        if at not in self._attached:
            raise ValueError(f"nothing attached at {at!r}")
        del self._attached[at]
        self._compose()

    def _compose(self) -> None:
        cwd = self._current_cwd()
        if self._attached:
            from monkeyfs import MountFS

            self._move(self._base, "/")
            active: Any = MountFS(self._base, dict(self._attached))
        else:
            active = self._base
        self._active = active
        self._move(active, cwd)

    def _current_cwd(self) -> str:
        try:
            return self._active.getcwd()
        except Exception:  # noqa: BLE001 - a filesystem with no cwd starts at /
            return "/"

    @staticmethod
    def _move(fs: Any, where: str) -> None:
        try:
            if fs.getcwd() != where:
                fs.chdir(where)
        except Exception:  # noqa: BLE001 - a cwd that will not move is not fatal
            pass


class SubtreeFS:
    """One directory of a filesystem, shown as if it were the root.

    What an attachment mounts: the source session's workspace root, so
    a file its agent calls ``auth.py`` reads as ``<mount>/auth.py``
    rather than ``<mount>/workspace/auth.py``. Reads only — an
    attachment is a frozen state, and ``ReadOnlyFS`` wraps this to
    refuse everything else by name.
    """

    __slots__ = ("_fs", "_prefix")

    def __init__(self, fs: Any, prefix: str) -> None:
        self._fs = fs
        self._prefix = posixpath.normpath(prefix) if prefix else "/"

    def __repr__(self) -> str:
        return f"<subtree {self._prefix!r} of {type(self._fs).__name__}>"

    def _under(self, path: str) -> str:
        rel = path.lstrip("/")
        return posixpath.normpath(
            posixpath.join(self._prefix, rel) if rel else self._prefix
        )

    def _out(self, path: str) -> str:
        """A path from the source, spelled the way this view names it."""
        if path == self._prefix:
            return "/"
        if path.startswith(self._prefix + "/"):
            return path[len(self._prefix) :]
        return "/" + path.lstrip("/")

    # -- reads ----------------------------------------------------------

    def read(self, path: str, *args: Any, **kwargs: Any) -> Any:
        return self._fs.read(self._under(path), *args, **kwargs)

    def open(self, path: str, mode: str = "r", **kwargs: Any) -> Any:
        return self._fs.open(self._under(path), mode, **kwargs)

    def exists(self, path: str) -> bool:
        return self._fs.exists(self._under(path))

    def lexists(self, path: str) -> bool:
        return self._fs.lexists(self._under(path))

    def isfile(self, path: str) -> bool:
        return self._fs.isfile(self._under(path))

    def isdir(self, path: str) -> bool:
        return self._fs.isdir(self._under(path))

    def islink(self, path: str) -> bool:
        return self._fs.islink(self._under(path))

    def stat(self, path: str, *args: Any, **kwargs: Any) -> Any:
        return self._fs.stat(self._under(path), *args, **kwargs)

    def getsize(self, path: str) -> int:
        return self._fs.getsize(self._under(path))

    def readlink(self, path: str) -> str:
        return self._out(self._fs.readlink(self._under(path)))

    def realpath(self, path: str) -> str:
        return self._out(self._fs.realpath(self._under(path)))

    def resolve_path(self, path: str) -> str:
        return self._out(self._fs.resolve_path(self._under(path)))

    def access(self, path: str, mode: int) -> bool:
        return self._fs.access(self._under(path), mode)

    def samefile(self, path1: str, path2: str) -> bool:
        return self._fs.samefile(self._under(path1), self._under(path2))

    def list(self, path: str = ".", recursive: bool = False) -> list[str]:
        # Entries come back relative to the directory that was asked
        # for, so they need no translation.
        return self._fs.list(self._under(path), recursive=recursive)

    def list_detailed(self, path: str = ".", recursive: bool = False) -> list[Any]:
        return self._fs.list_detailed(self._under(path), recursive=recursive)

    def glob(self, pattern: str) -> list[str]:
        return [self._out(hit) for hit in self._fs.glob(self._under(pattern))]

    def get_metadata_snapshot(self) -> dict[str, Any]:
        prefix = self._prefix.lstrip("/")
        out: dict[str, Any] = {}
        for path, meta in self._fs.get_metadata_snapshot().items():
            plain = path.lstrip("/")
            if prefix and plain != prefix and not plain.startswith(prefix + "/"):
                continue
            out[plain[len(prefix) :].lstrip("/") if prefix else plain] = meta
        return out

    # -- position -------------------------------------------------------

    def getcwd(self) -> str:
        return "/"

    def chdir(self, path: str) -> None:
        if not self.isdir(path):
            raise FileNotFoundError(f"No such directory: {path!r}")

    def invalidate(self) -> None:
        invalidate = getattr(self._fs, "invalidate", None)
        if invalidate is not None:
            invalidate()
