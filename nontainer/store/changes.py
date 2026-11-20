"""What changed in a session's state between two exact refs: the change
``ws.land`` would apply, described for a host to judge before it lands."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from ..errors import ChangesTooLarge, NotSupportedError, WorkspaceError
from ..planes import CACHE_PREFIX
from .refs import Ref

if TYPE_CHECKING:
    from .core import Store


# The workspace root a commit is read under when the caller names none:
# the root a session opens at by default.
_DEFAULT_ROOT = "/workspace"


@dataclass(frozen=True)
class FileChange:
    """One file or directory that changed.

    ``change`` is ``"added"`` or ``"removed"`` (the path is new, or
    gone), ``"modified"`` (a file's bytes changed), ``"replaced"`` (a
    file became a directory, or a directory a file), or ``"metadata"``:
    only the path's metadata row changed, such as its timestamps, and a
    file's bytes are the same. ``is_dir`` says what the path is at
    ``theirs``, or for a removed path what it was. ``size`` is the
    file's size in bytes at ``theirs`` and ``base_size`` at ``base``, as
    its metadata row records it: ``None`` on a side where the path is
    not a file.
    """

    path: str
    change: Literal["added", "removed", "modified", "replaced", "metadata"]
    is_dir: bool = False
    size: int | None = None
    base_size: int | None = None


@dataclass(frozen=True)
class CacheChange:
    """One ``ws.cache`` key that changed: ``"set"`` (added or replaced,
    whatever the value, ``None`` included) or ``"deleted"``. ``size`` is
    the value's stored size in bytes, its encoding included; ``None``
    when deleted."""

    key: str
    change: Literal["set", "deleted"]
    size: int | None = None


@dataclass(frozen=True)
class StateChanges:
    """The change from ``base`` to ``theirs``, as ``ws.land`` reads it.

    ``files`` and ``cache`` are the state a land moves, in path and key
    order. ``cwd`` is ``(before, after)`` when a land would move the cwd
    to ``after``, ``before`` being what ``base`` stored (``None`` for
    none), and ``None`` when it would not. ``bookkeeping`` names the
    framework bookkeeping that also differs between the two, which a
    land leaves as it is: any of ``"ws-git"``, ``"view"``,
    ``"conversation"``, ``"compaction"`` and ``"other"``.

    ``entries`` and ``stored_bytes`` are the measures ``max_entries``
    and ``max_bytes`` bound: the changed files, directories and cache
    keys, and the cwd if it moves; and the bytes the change stores, the
    stored size of every file it adds or modifies and every cache value
    it sets, as kvgit records them, a few bytes of encoding included. A
    description is true for as long as both refs read: each names one
    exact commit, and neither moves.
    """

    base: Ref
    theirs: Ref
    files: tuple[FileChange, ...] = ()
    cache: tuple[CacheChange, ...] = ()
    cwd: tuple[str | None, str] | None = None
    bookkeeping: frozenset[str] = frozenset()
    stored_bytes: int = 0

    @property
    def entries(self) -> int:
        """Changed files, directories and cache keys, and the cwd if it
        moves."""
        return len(self.files) + len(self.cache) + (self.cwd is not None)

    def __bool__(self) -> bool:
        """Whether a land would move anything. Bookkeeping alone does not
        count: a land leaves it where it is."""
        return bool(self.files or self.cache or self.cwd)


class _Changes:
    """``Store.changes``: a part of ``Store``, not a class of its own.
    It runs on the store's repo and ref checks, which ``Store``
    defines."""

    def changes(
        self: "Store",
        base: "str | Ref",
        theirs: "str | Ref",
        *,
        root: str | None = None,
        max_entries: int | None = None,
        max_bytes: int | None = None,
    ) -> StateChanges:
        """What changed in a session's state from ``base`` to ``theirs``:
        the change :meth:`Workspace.land` would apply, described and not
        applied.

        Read-only: nothing is opened writable, committed or moved, so a
        host can apply its own policy to a result before it lands it.
        It describes what a land moves (files and directories, metadata
        changes among them, cache sets and deletes, the cwd) and names
        the bookkeeping that differs as well, which a land leaves alone,
        so a caller never reads storage keys. See :class:`StateChanges`.

        ``base`` and ``theirs`` are exact states, validated as
        :meth:`resolve` validates a ref: ``session@commit`` (whole or
        short), a :class:`Ref`, a store tag's ref, or a published
        version's ref. ``root`` is the workspace root the two were made
        under, for a lineage not at the default one: it decides, as it
        does for a land, whether a commit stored a cwd at all.

        The change is measured from the store's own records first:
        which keys differ, and the stored size kvgit keeps beside each
        value. No file or cache value is read for that, and nothing that
        did not change is read at all; only the two cwd slots are, to
        tell whether the cwd moves. ``max_entries`` and ``max_bytes``
        bound the two measures (:attr:`StateChanges.entries`,
        :attr:`StateChanges.stored_bytes`), and a change past either
        raises :class:`~nontainer.errors.ChangesTooLarge` before anything
        more is read. Within them, the only values read are the changed
        paths' metadata rows on each side, which are small: they tell a
        directory from a file, so a path that changed type reads as
        ``"replaced"``, and give a file's own size.

        Both commits must be in the current layout (monkeyfs 0.1.10 and
        later); an older one is refused. A commit written before kvgit's
        content-addressed layout can report a file whose bytes are equal
        as modified.
        """
        from kvgit.versioned.kv import commit_changes
        from monkeyfs import VirtualFS

        from ..migrate import legacy_keys
        from ..providers.kvgit import _row, bookkeeping_kind, state_kind

        self._require_own_layout("changes")
        if self._backend != "kvgit":
            raise NotSupportedError(
                f"The {self._backend!r} backend is not versioned, so it has no "
                "commits to compare. Use the kvgit backend."
            )
        for name, bound in (("max_entries", max_entries), ("max_bytes", max_bytes)):
            if bound is not None and (not isinstance(bound, int) or bound < 0):
                raise ValueError(f"{name} must be a whole number of 0 or more")
        root = root or _DEFAULT_ROOT
        base_ref = self._exact_ref(base, "describing changes from")
        theirs_ref = self._exact_ref(theirs, "describing changes to")
        repo = self._kvgit_repo(create=True)
        sides = {
            which: repo.snapshot(commit=ref.commit)
            for which, ref in (("base", base_ref), ("theirs", theirs_ref))
        }
        for which, ref in (("base", base_ref), ("theirs", theirs_ref)):
            if legacy_keys(sides[which]):
                raise WorkspaceError(
                    f"{which} {str(ref)!r} predates monkeyfs 0.1.10's per-file "
                    "layout, which changes() does not describe. ws.land still "
                    "reads it."
                )

        raw = commit_changes(repo.store, base_ref.commit, theirs_ref.commit)
        bookkeeping = frozenset(
            bookkeeping_kind(key) for key in raw if state_kind(key) is None
        )

        # Group what changed by the path or key it is about, from the
        # keys and the sizes kvgit records beside them: nothing read yet.
        blobs: dict[str, tuple[str, Any]] = {}
        rows: dict[str, tuple[str, Any]] = {}
        cache: list[CacheChange] = []
        cwd_changed = False
        stored = 0
        for key, change in raw.items():
            kind = state_kind(key)
            if kind == "file":
                path = "/" + VirtualFS._decode_path(key).lstrip("/")
                blobs[path] = (key, change)
                stored += _size(change.new, sides["theirs"], key) or 0
            elif kind == "row":
                path = "/" + VirtualFS.path_for_metadata_key(key).lstrip("/")
                rows[path] = (key, change)
            elif kind == "cache":
                size = _size(change.new, sides["theirs"], key)
                stored += size or 0
                cache.append(
                    CacheChange(
                        key=key[len(CACHE_PREFIX) :],
                        change="deleted" if change.new is None else "set",
                        size=size,
                    )
                )
            elif kind == "cwd":
                cwd_changed = True

        cwd = None
        if cwd_changed:
            before, after = (
                sides[which].get(VirtualFS.CWD_KEY) for which in ("base", "theirs")
            )
            if _stored_cwd(after, root):
                cwd = (before if _stored_cwd(before, root) else None, after)

        paths = set(blobs) | set(rows)
        entries = len(paths) + len(cache) + (cwd is not None)
        if (max_entries is not None and entries > max_entries) or (
            max_bytes is not None and stored > max_bytes
        ):
            raise ChangesTooLarge(
                f"The change from {str(base_ref)!r} to {str(theirs_ref)!r} is "
                f"{entries} entries and stores {stored} bytes, past the bounds "
                f"given (max_entries={max_entries}, max_bytes={max_bytes}). "
                "Nothing of it was read.",
                entries=entries,
                stored_bytes=stored,
            )

        # Every changed path's metadata row, on both sides: the rows are
        # small, and they say a directory from a file (on each side, so
        # a path that changed type reads as the replacement it is) and
        # give a file's own size. A row sits beside its blob under the
        # same encoding.
        def row_key(path: str) -> str:
            if path in rows:
                return rows[path][0]
            return VirtualFS.META_PREFIX + blobs[path][0][len(VirtualFS.PREFIX) :]

        keys = [row_key(path) for path in paths]
        read = {
            which: sides[which].get_many(*keys) if keys else {}
            for which in ("base", "theirs")
        }

        files = []
        for path in sorted(paths):
            before = _parsed_row(_row, read["base"].get(row_key(path)))
            after = _parsed_row(_row, read["theirs"].get(row_key(path)))
            blob = blobs[path][1] if path in blobs else None
            # A path is a file where its bytes are, and otherwise what
            # its row says; it is there on a side where either is.
            file_before = blob is not None and blob.old is not None
            file_after = blob is not None and blob.new is not None
            dir_before = not file_before and bool((before or {}).get("is_dir"))
            dir_after = not file_after and bool((after or {}).get("is_dir"))
            there_before = file_before or before is not None
            there_after = file_after or after is not None
            if not there_before:
                kind, is_dir = "added", dir_after
            elif not there_after:
                kind, is_dir = "removed", dir_before
            elif dir_before != dir_after:
                kind, is_dir = "replaced", dir_after
            elif blob is not None:
                kind, is_dir = "modified", False
            else:
                kind, is_dir = "metadata", dir_after
            files.append(
                FileChange(
                    path=path,
                    change=kind,
                    is_dir=is_dir,
                    size=(
                        _file_size(after, blobs, path, "new")
                        if there_after and not dir_after
                        else None
                    ),
                    base_size=(
                        _file_size(before, blobs, path, "old")
                        if there_before and not dir_before
                        else None
                    ),
                )
            )

        return StateChanges(
            base=base_ref,
            theirs=theirs_ref,
            files=tuple(files),
            cache=tuple(sorted(cache, key=lambda c: c.key)),
            cwd=cwd,
            bookkeeping=bookkeeping,
            stored_bytes=stored,
        )


def _size(entry: Any, side: Any, key: str) -> int | None:
    """A value's stored size, as kvgit recorded it beside the pointer.
    ``None`` where there is no value. A value written before kvgit
    recorded sizes is read once to measure it."""
    if entry is None:
        return None
    size = getattr(entry.meta, "size", None)
    if size is None:
        stored = side.raw.get(key)
        size = None if stored is None else len(stored)
    return size


def _file_size(
    row: dict[str, Any] | None, blobs: dict[str, Any], path: str, side: str
) -> int | None:
    """A file's size on one side: its metadata row's, or where the row
    is missing or says nothing, the stored size of its bytes."""
    size = (row or {}).get("size")
    if isinstance(size, int):
        return size
    if path in blobs:
        entry = getattr(blobs[path][1], side)
        if entry is not None:
            return getattr(entry.meta, "size", None)
    return None


def _parsed_row(parse: Any, value: Any) -> dict[str, Any] | None:
    """A metadata row as a dict; ``None`` for no row, or one that does
    not parse, which then describes nothing."""
    try:
        return parse(value)
    except Exception:  # noqa: BLE001 - an unreadable row describes nothing
        return None


def _stored_cwd(value: Any, root: str) -> bool:
    """Whether a commit's cwd slot holds a cwd of its own, by the rule a
    land and an open read it with: under a root other than ``/``, the
    root itself is what a mount composition committed before 0.11.12 in
    place of one."""
    return isinstance(value, str) and (value != "/" or root == "/")
