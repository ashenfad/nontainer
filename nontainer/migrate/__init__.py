"""Moving a branch written before monkeyfs 0.1.10 onto the current layout.

Two keys are what is left of that layout:

- ``__vfs_metadata__`` — every file's size and timestamps in one JSON
  table. Today each file has a metadata row of its own beside its blob
  (``VirtualFS.META_PREFIX``).
- ``__cwd__`` — nontainer's own copy of the working directory. Today
  the filesystem's cwd slot (``VirtualFS.CWD_KEY``) is the only one.

Neither is live state any more. A writable workspace refuses a branch
head that carries either (:class:`~nontainer.errors.LegacyLayoutError`),
and so does a merge of one; this module is the one place that converts
them:

- :func:`migrate_provider` rewrites one head in one commit, and
  :meth:`Store.migrate_layout <nontainer.store.Store.migrate_layout>`
  runs it over a store's sessions. ``python -m nontainer.migrate`` is
  the same from a shell.
- :func:`current_layout` is the same conversion as a read-only view of
  a tree, for the verbs that bring an old commit's state into a live
  head (a restore, a revert, a cherry-pick): what lands is converted,
  and nothing is written to the old commit.

The conversion, which is the same wherever it runs:

- each table entry becomes the row monkeyfs writes for that path, unless
  the path already has a row, which wins;
- an entry for a file with no blob at its path is looked for under the
  tree's cwd, which is where monkeyfs 0.1.8 and earlier keyed a relative
  write; one that describes no file there either is dropped;
- ``__cwd__`` moves into the cwd slot when the slot is empty, or holds
  the filesystem root — which is where a composition parks the
  filesystem and never where a session was left;
- both keys are then deleted.

Frozen state is never rewritten. A tag names one commit forever, a
publication's branch is served as it is, and an old commit in a
session's history stays what it was. All of them still read right
frozen: monkeyfs consults the table for any path without a row, so a
tree in the old layout lists and stats its files as it always did.
"""

from __future__ import annotations

import json
import posixpath
import sys
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from ..errors import NotSupportedError, WorkspaceError

#: The single metadata table monkeyfs kept before per-file rows.
LEGACY_TABLE_KEY = "__vfs_metadata__"
#: nontainer's own cwd key, from before the filesystem's slot was the
#: only one.
LEGACY_CWD_KEY = "__cwd__"
LEGACY_KEYS = (LEGACY_TABLE_KEY, LEGACY_CWD_KEY)

#: ``info["tool"]`` on the commit a migration lands.
MIGRATE_TOOL = "migrate-layout"


def legacy_keys(state: Any) -> tuple[str, ...]:
    """The legacy keys a tree carries, in :data:`LEGACY_KEYS` order.

    Empty for a tree in the current layout. A mapping that cannot
    answer a membership test holds nothing to migrate.
    """
    if state is None:
        return ()
    try:
        return tuple(key for key in LEGACY_KEYS if key in state)
    except Exception:  # noqa: BLE001 - a kv that refuses has nothing to migrate
        return ()


@dataclass(frozen=True)
class _Plan:
    """What converting one tree writes: keys to set, beside the legacy
    keys it deletes, and the counts a report gives."""

    found: tuple[str, ...]
    writes: dict[str, Any] = field(default_factory=dict)
    rows: int = 0
    kept: int = 0
    dropped: int = 0
    cwd: str | None = None


def _plan(state: Any) -> _Plan:
    """Work out the conversion of one tree without writing anything."""
    from monkeyfs import VirtualFS

    found = legacy_keys(state)
    if not found:
        return _Plan(found=())
    writes: dict[str, Any] = {}
    moved_cwd: str | None = None
    if LEGACY_CWD_KEY in found:
        legacy = state.get(LEGACY_CWD_KEY)
        slot = state.get(VirtualFS.CWD_KEY)
        if (
            isinstance(legacy, str)
            and legacy.startswith("/")
            and legacy != "/"
            and slot in (None, "", "/")
        ):
            writes[VirtualFS.CWD_KEY] = legacy
            moved_cwd = legacy

    rows = kept = dropped = 0
    if LEGACY_TABLE_KEY in found:
        vfs = VirtualFS(state)
        # monkeyfs's own parse of the table: the entries it would read,
        # with an unparseable body or entry skipped exactly as a read
        # skips it.
        table = vfs._legacy()
        cwd = writes.get(VirtualFS.CWD_KEY) or state.get(VirtualFS.CWD_KEY) or "/"
        for path in sorted(table):
            meta = table[path]
            target = path
            if not meta.is_dir and vfs._encode_path("/" + path) not in state:
                resolved = vfs._normalize_path(posixpath.join(cwd, path))
                if (
                    resolved != path
                    and resolved not in table
                    and vfs._encode_path("/" + resolved) in state
                ):
                    target = resolved
                else:
                    dropped += 1
                    continue
            row = vfs.metadata_key("/" + target)
            if row in writes or row in state:
                kept += 1
                continue
            # The row body monkeyfs writes for a path, built by its own
            # field list.
            writes[row] = json.dumps(VirtualFS._row_fields(meta)).encode()
            rows += 1
    return _Plan(
        found=found,
        writes=writes,
        rows=rows,
        kept=kept,
        dropped=dropped,
        cwd=moved_cwd,
    )


class _Converted(Mapping):
    """A tree in the old layout, read as the current layout.

    Read-only and lazy: the legacy keys are absent, and the rows and the
    cwd a migration would write are present, without anything being
    written to the tree underneath.
    """

    def __init__(self, state: Any, plan: _Plan) -> None:
        self._state = state
        self._plan = plan

    @property
    def writes(self) -> Mapping[str, Any]:
        """The keys the conversion adds or replaces."""
        return self._plan.writes

    def __getitem__(self, key: str) -> Any:
        if key in self._plan.writes:
            return self._plan.writes[key]
        if key in LEGACY_KEYS:
            raise KeyError(key)
        value = self._state.get(key)
        if value is None and key not in self._state:
            raise KeyError(key)
        return value

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default

    def __contains__(self, key: object) -> bool:
        if key in self._plan.writes:
            return True
        return key not in LEGACY_KEYS and key in self._state

    def __iter__(self) -> Iterator[str]:
        writes = self._plan.writes
        for key in self._state.keys():
            if key not in LEGACY_KEYS and key not in writes:
                yield key
        yield from writes

    def __len__(self) -> int:
        return sum(1 for _ in self)


def current_layout(state: Any) -> Any:
    """``state`` read in the current layout.

    The tree itself when it already is — the common case, which costs
    two membership tests — and otherwise a read-only view of the
    conversion (see the module docstring). ``None`` stays ``None``: it
    is the empty tree.
    """
    if state is None:
        return None
    plan = _plan(state)
    if not plan.found:
        return state
    return _Converted(state, plan)


def converted_keys(view: Any) -> set[str]:
    """The keys a :func:`current_layout` view adds or replaces; empty
    for a tree that needed no conversion."""
    return set(view.writes) if isinstance(view, _Converted) else set()


@dataclass(frozen=True)
class LayoutMigration:
    """What migrating one session's head did — or, on a dry run, would do.

    ``found`` lists the legacy keys the head carried, and is empty for a
    head already in the current layout, which a migration leaves exactly
    as it is. ``rows`` counts table entries written as rows, ``kept``
    entries a row already existed for (the row wins), ``dropped``
    entries that describe no file. ``cwd`` is the working directory
    moved into the filesystem's slot, or ``None`` where the slot kept
    its own. ``commit`` is the commit the migration landed: ``None`` on
    a dry run, on a clean head, and on a backend that keeps no commits.
    """

    session: str
    found: tuple[str, ...] = ()
    rows: int = 0
    kept: int = 0
    dropped: int = 0
    cwd: str | None = None
    commit: str | None = None
    dry_run: bool = False

    @property
    def clean(self) -> bool:
        """Whether the head was already in the current layout."""
        return not self.found


def migrate_provider(provider: Any, *, dry_run: bool = False) -> LayoutMigration:
    """Rewrite one provider's head into the current layout.

    One commit on a versioned provider, holding every table entry as a
    row, the cwd moved, and both legacy keys deleted; a head that
    carries neither key is left alone, and no commit is made for it.
    Running it twice is running it once. ``dry_run`` computes the same
    report and writes nothing.

    Refused on a frozen provider (a snapshot takes no commits) and on
    one with uncommitted writes, which would ride into the migration
    commit as though they were part of it.
    """
    plan = _plan(provider.kv)
    report = LayoutMigration(
        session=provider.session,
        found=plan.found,
        rows=plan.rows,
        kept=plan.kept,
        dropped=plan.dropped,
        cwd=plan.cwd,
        dry_run=dry_run,
    )
    if not plan.found or dry_run:
        return report
    if getattr(provider, "frozen", False):
        raise NotSupportedError(
            f"cannot migrate {provider.session!r}: this handle is a frozen "
            "snapshot and takes no commits. Old commits are not rewritten; "
            "migrate the branch head instead."
        )
    caps = provider.caps
    if caps.staging and provider.dirty:
        raise WorkspaceError(
            f"cannot migrate {provider.session!r}: it has uncommitted writes, "
            "which would land in the migration commit. Commit or discard them "
            "first."
        )
    kv = provider.kv
    for key, value in plan.writes.items():
        kv[key] = value
    for key in plan.found:
        del kv[key]
    invalidate = getattr(provider, "_invalidate_fs", None)
    if callable(invalidate):
        invalidate()
    commit = None
    if caps.versioned:
        commit = provider.commit(
            info={
                "tool": MIGRATE_TOOL,
                "removed": list(plan.found),
                "rows": plan.rows,
                "kept": plan.kept,
                "dropped": plan.dropped,
            }
        )
    return replace(report, commit=commit)


def _describe(report: LayoutMigration) -> str:
    parts = [
        f"{report.rows} row{'s' if report.rows != 1 else ''} written",
        f"{report.kept} kept",
        f"{report.dropped} dropped",
    ]
    if report.cwd is not None:
        parts.append(f"cwd {report.cwd}")
    if report.commit is not None:
        parts.append(f"commit {report.commit[:7]}")
    verb = "would migrate" if report.dry_run else "migrated"
    return f"{report.session}: {verb} {', '.join(report.found)} ({'; '.join(parts)})"


def _build_parser() -> Any:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m nontainer.migrate",
        description=(
            "Rewrite session branches written before monkeyfs 0.1.10 into the "
            "current layout: the __vfs_metadata__ table becomes per-file rows "
            "and __cwd__ moves into the filesystem's cwd slot, one commit per "
            "branch. Publications, tags and old commits are never rewritten."
        ),
    )
    parser.add_argument(
        "--store", default=None, help="store directory (default ~/.nontainer)"
    )
    parser.add_argument(
        "--backend", default="kvgit", choices=["kvgit", "dir", "agentfs"]
    )
    parser.add_argument(
        "--session",
        action="append",
        default=[],
        metavar="NAME",
        help="migrate only this session (repeatable); default: every session",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="report what would change, write nothing"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    from ..store import Store

    args = _build_parser().parse_args(argv)
    store = Store(args.store, backend=args.backend)
    try:
        reports = store.migrate_layout(args.session or None, dry_run=args.dry_run)
    except (ValueError, WorkspaceError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    legacy = [r for r in reports.values() if not r.clean]
    for report in legacy:
        print(_describe(report))
    verb = "would migrate" if args.dry_run else "migrated"
    print(
        f"{store.path}: {verb} {len(legacy)} of {len(reports)} "
        f"session{'s' if len(reports) != 1 else ''}"
    )
    return 0
