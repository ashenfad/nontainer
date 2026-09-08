"""The agent's git, as a fiction over the store's own history.

An agent asks for git: an index it fills across several edits, commits
it names, a log it can read back. The store underneath has its own
history — one commit per tool call, or per turn — made by the framework
for durability, at moments the agent never chose. Those two histories
are not the same history, and the first version of this tried to make
them one: an agent commit WAS a framework commit, and staging suspended
autocommit so unstaged work stayed out of the store until the agent
landed it. That collided with the framework's own durability points (a
turn hook, a session db, a skill installer) and with the plain rule
that nothing an agent writes should sit outside the store.

So the agent's git is metadata instead:

- **The index and the agent's commit graph live in a blob** at a
  reserved key (:data:`BLOB_KEY`): the agent's current head (a store
  commit hash), the staged paths, and the context of an outstanding
  merge. The blob is an ordinary key. Autocommit commits it like any
  other. Nothing is ever withheld from the store and nothing suspends
  autocommit.
- **status diffs the working tree against the agent's head**, not the
  store's. Framework commits between turns move the store's head and
  leave the agent's where it was, so a composition survives them by
  construction.
- **An agent commit's tree is exactly what the agent committed.** At
  commit time every modified path OUTSIDE the commit set is written
  back to its content at the agent's head, the keyed commit is made,
  and the live content is written back afterwards — so the commit
  holds the agent's intent and the working tree keeps the agent's work
  in progress. (The reference implementation, ``@agex-ts/git``, parents
  its keyed commit on the store head and skips that step, so a partial
  commit silently absorbs the unstaged edits into its own baseline: the
  tree reads clean afterwards and a later checkout brings the absorbed
  edits back as if they had been committed. That is the one deviation
  here.)
- **log / show / diff walk the agent's graph**, threaded through
  ``virtual_parents`` in commit info. The framework's per-call commits
  are plumbing the agent never sees.
- **Branches are real branches** (sessions), not virtual ones: making
  one, and merging, are the host's to invoke.

The substrate surface this needs is small and provider-shaped:
``caps``, ``kv`` (the blob, by ordinary key access), ``fs`` (working
tree writes), ``files_at`` / ``working_files`` (a commit's files and
the live ones), ``commit_keys`` (the one keyed-commit primitive),
``diff``, ``history``, ``head``, ``dirty``. ``caps.index`` gates the
whole fiction; providers without it refuse verb by verb.
"""

from __future__ import annotations

import json
import posixpath
import re
from collections.abc import Iterable, Mapping
from typing import Any

from .errors import CommitNotFoundError, NotSupportedError, WorkspaceError
from .protocol import CommitInfo, WorkspaceStatus

#: Reserved store key holding the agent's index and commit graph. Not a
#: file key (the VFS prefixes its own), so no path can collide with it.
BLOB_KEY = "__ws_git__"

#: Layout version of the blob. 1 was the staged set plus the suspension
#: flag of the old model; 2 is head + staged + merge context.
BLOB_VERSION = 2

#: ``info["tool"]`` on an agent commit — what tells one from a
#: framework commit, which carries a tool name of its own.
TOOL = "ws-git"

#: Framework commits this module makes on the agent's behalf: the
#: working-tree restore after a partial commit, and the tree a checkout
#: writes. Neither is an agent commit — they carry no message and are
#: never shown in ``ws-git log``.
RESTORE_TOOL = "ws-git.restore"
CHECKOUT_TOOL = "ws-git.checkout"

#: ``info["tool"]`` on a merge commit, as the provider stamps it.
MERGE_TOOL = "ws-git.merge"

_MARKER = b"<<<<<<< "

_HASH = re.compile(r"[0-9a-f]{7,}")


def is_agent_commit(info: Mapping[str, Any]) -> bool:
    """Whether a commit's ``info`` says an agent made it deliberately.

    The tool name is the mark: every commit made here carries one, and
    the framework's own (``terminal``, ``turn``, ``ws-git.restore``)
    are not this. The reference implementation keys on the presence of
    a message instead, because its framework commits carry no info at
    all; here a message is optional, and a commit without one is still
    a point in the agent's graph. Merge commits are the host's doing
    but are part of that graph, so they count too.
    """
    return info.get("tool") in (TOOL, MERGE_TOOL)


def virtual_parents(info: Mapping[str, Any]) -> list[str]:
    """The agent-graph parents recorded on a commit (may be empty)."""
    parents = info.get("virtual_parents")
    if not isinstance(parents, list):
        return []
    return [p for p in parents if isinstance(p, str)]


def parse_blob(raw: Any) -> dict[str, Any]:
    """Blob value → normalized state, tolerant of absence and layout.

    Accepts whatever a key read returns (bytes, str, an already-decoded
    dict) and whatever an older layout wrote. A blob from layout 1 held
    a key-level index and a suspension flag, neither of which means
    anything now: it migrates to a fresh state (no head, nothing
    staged) rather than being reinterpreted. The migrated state is
    recorded by the next write — a read never writes, so ``status`` on
    an old branch stays a pure read.
    """
    if isinstance(raw, dict):
        parsed: Any = raw
    elif raw is None:
        parsed = {}
    else:
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            parsed = {}
    if not isinstance(parsed, dict) or parsed.get("version") != BLOB_VERSION:
        parsed = {}
    head = parsed.get("head")
    source = parsed.get("merge_source")
    return {
        "head": head if isinstance(head, str) else None,
        "staged": sorted(_strings(parsed.get("staged"))),
        "merge_source": source if isinstance(source, str) else None,
        "unresolved": sorted(_strings(parsed.get("unresolved"))),
    }


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, str)]


def _encode_blob(state: Mapping[str, Any]) -> bytes:
    return json.dumps(
        {
            "version": BLOB_VERSION,
            "head": state.get("head"),
            "staged": sorted(_strings(list(state.get("staged") or []))),
            "merge_source": state.get("merge_source"),
            "unresolved": sorted(_strings(list(state.get("unresolved") or []))),
        },
        sort_keys=True,
    ).encode()


def _has_markers(value: Any) -> bool:
    return isinstance(value, bytes) and _MARKER in value


class AgentGit:
    """The agent's git over one workspace.

    Both spellings of the fiction — ``ws.index`` on the host and the
    ``ws-git`` terminal verbs — are this class, so the agent and its
    host see one index and one graph rather than two.
    """

    __slots__ = ("_ws", "_provider")

    def __init__(self, ws: Any) -> None:
        self._ws = ws
        self._provider = ws._provider

    # -- gates ---------------------------------------------------------

    def _require(self, verb: str) -> None:
        """Refuse where the substrate cannot hold the fiction.

        ``caps.index`` means keyed commits, which is the one thing the
        fiction cannot emulate: without them an agent commit could not
        hold a subset of the tree.
        """
        caps = self._provider.caps
        if not (caps.versioned and caps.index):
            raise NotSupportedError(
                f"{type(self._provider).__name__} has no index: {verb} is not "
                "supported. Use the kvgit backend for ws-git."
            )

    def _writable(self, verb: str) -> None:
        self._require(verb)
        self._ws._check_writable(verb)

    # -- blob ----------------------------------------------------------

    def _read(self) -> dict[str, Any]:
        return parse_blob(self._provider.kv.get(BLOB_KEY))

    def _write(self, state: Mapping[str, Any], *, force: bool = False) -> None:
        """Stage the blob (never commits it — autocommit does that).

        Skips a write that would change nothing, so a verb that turns
        out to be a no-op leaves the tree as clean as it found it.
        ``force`` writes anyway: a commit needs the blob pending so its
        keyed commit has something of its own to carry.
        """
        value = _encode_blob(state)
        if not force and self._read() == parse_blob(value):
            return
        self._provider.kv[BLOB_KEY] = value

    @property
    def head(self) -> str | None:
        """The agent's own head: the commit its last ``ws-git commit``
        made, or ``None`` before the first one. Not the store's head,
        which every framework commit moves."""
        self._require("ws-git head")
        return self._read()["head"]

    # -- the working tree against the agent's head ----------------------

    def _modified(self, head: str | None) -> set[str]:
        """Paths whose content differs from the agent's head.

        With no head yet, every visible file counts as modified —
        there is no baseline to compare against, exactly as in a repo
        before its first commit. The comparison is a content one, so a
        framework commit that landed an edit does not hide it: the edit
        is still not in the agent's head.
        """
        live = self._provider.working_files()
        if head is None:
            return set(live)
        if head == self._provider.head and not self._provider.dirty:
            # The agent's head IS the store's, with nothing pending:
            # the common clean case pays no tree walk.
            return set()
        base = self._provider.files_at(head)
        live_paths, base_paths = set(live), set(base)
        modified = live_paths ^ base_paths
        modified |= {p for p in live_paths & base_paths if live[p] != base[p]}
        return modified

    def _merge_context(self, blob: Mapping[str, Any]) -> tuple[str | None, list[str]]:
        """The outstanding merge, if its markers are still in the tree.

        The merge records which paths it left marked; each read checks
        which of them still carry markers, so resolving them one at a
        time shows progress and the context ends when the last one
        goes — whichever commit, agent's or framework's, carried the
        resolution.
        """
        stored = blob["unresolved"]
        if not stored:
            return None, []
        live = self._provider.working_files()
        unresolved = [p for p in stored if _has_markers(live.get(p))]
        if not unresolved:
            return None, []
        return blob["merge_source"], unresolved

    def status(self) -> WorkspaceStatus:
        """Staged vs unstaged paths plus live merge context.

        Pure read: never writes, commits, or moves the index.
        """
        self._require("ws-git status")
        blob = self._read()
        modified = self._modified(blob["head"])
        staged = set(blob["staged"])
        source, unresolved = self._merge_context(blob)
        return WorkspaceStatus(
            branch=self._provider.session,
            staged=tuple(sorted(modified & staged)),
            unstaged=tuple(sorted(modified - staged)),
            merge_source=source,
            merge_unresolved=tuple(unresolved),
        )

    # -- index ---------------------------------------------------------

    def stage(self, paths: Iterable[str]) -> tuple[str, ...]:
        """Add workspace file paths to the index; returns what was new.

        Staging is bookkeeping and nothing else: no commit, no change
        to what autocommit does. A path must name a file in the working
        tree or at the agent's head — a pathspec that matches nothing
        is an error, as in git.
        """
        self._writable("ws-git stage")
        paths = _unique(paths)
        if not paths:
            return ()
        blob = self._read()
        self._check_paths(paths, blob)
        staged = set(blob["staged"])
        added = [p for p in paths if p not in staged]
        if not added:
            return ()
        blob["staged"] = sorted(staged | set(paths))
        self._write(blob)
        return tuple(added)

    def unstage(self, paths: Iterable[str]) -> tuple[str, ...]:
        """Remove paths from the index; returns what was removed.

        A known path that was not staged is silently ignored, the way
        git ignores ``restore --staged`` on an unstaged file.
        """
        self._writable("ws-git unstage")
        paths = _unique(paths)
        if not paths:
            return ()
        blob = self._read()
        self._check_paths(paths, blob)
        staged = set(blob["staged"])
        removed = [p for p in paths if p in staged]
        if not removed:
            return ()
        blob["staged"] = sorted(staged - set(removed))
        self._write(blob)
        return tuple(removed)

    def discard(self) -> None:
        """Abandon the composition: clear the index, keep the tree."""
        self._writable("ws-git reset")
        blob = self._read()
        if not blob["staged"]:
            return
        blob["staged"] = []
        self._write(blob)

    def _check_paths(self, paths: list[str], blob: Mapping[str, Any]) -> None:
        """Refuse a pathspec that names nothing indexable.

        Known means: a file in the working tree, or one at the agent's
        head (so a deletion can be staged after the file is gone).
        Anything else is a directory (git stages files) or a typo.
        """
        known = set(self._provider.working_files())
        head = blob["head"]
        if head is not None:
            known |= set(self._provider.files_at(head))
        fs = self._provider.fs
        for path in paths:
            if path in known:
                continue
            if fs.isdir(path):
                raise ValueError(f"cannot stage {path!r}: stage files, not directories")
            raise ValueError(
                f"unknown path {path!r}: no such file at HEAD or in the working tree"
            )

    # -- commit --------------------------------------------------------

    def commit(
        self, message: str | None = None, info: dict[str, Any] | None = None
    ) -> tuple[str, tuple[str, ...]]:
        """Record an agent commit; returns ``(commit id, files)``.

        The commit set is the staged paths that actually differ from
        the agent's head, or every modified path when nothing is staged
        (``stage`` is optional, as ``git add`` is optional with
        ``commit -a``). The commit's tree is exactly that: modified
        paths outside the set are written back to their content at the
        agent's head first, so the commit says what the agent said and
        nothing more.

        Two store commits come of it. The first is the agent's, keyed
        to the commit set plus the reverted paths plus the blob. The
        second is the framework's: the reverted paths are written back
        to the work in progress they held, and the workspace's ordinary
        autocommit lands that restore. Nothing is at risk in between —
        the work in progress is in the previous framework commit.
        """
        self._writable("ws-git commit")
        blob = self._read()
        head = blob["head"]
        modified = self._modified(head)
        if not modified:
            raise WorkspaceError("nothing to commit")
        if blob["staged"]:
            files = sorted(modified & set(blob["staged"]))
            if not files:
                raise WorkspaceError(
                    "nothing to commit (the staged paths match the last commit)"
                )
        else:
            files = sorted(modified)
        reverted = sorted(modified - set(files))

        provider = self._provider
        fs = provider.fs
        live = provider.working_files()
        base = provider.files_at(head) if head is not None else {}
        # What the working tree holds for the paths this commit leaves
        # out, so it can hold it again once the commit is made.
        pending = {path: live.get(path) for path in reverted}

        for path in reverted:
            if path in base:
                _put(fs, path, base[path])
            elif path in live:
                fs.remove(path)
        for path in files:
            if path in live:
                # Re-set so the keyed commit picks the value up even
                # when a framework commit already flushed it.
                _put(fs, path, live[path])

        landed = dict(blob)
        landed["staged"] = []
        if not [p for p in blob["unresolved"] if p not in files]:
            landed["merge_source"] = None
            landed["unresolved"] = []
        # The commit cannot carry its own hash, so the blob rides along
        # with the composition closed and the head it had; the new head
        # is written straight after and lands with the restore.
        self._write(landed, force=True)

        # Caller metadata first: the graph's own fields are the
        # fiction's to state, and an agent commit that could be
        # labelled something else would not be findable as one.
        commit_info = {
            **(info or {}),
            "tool": TOOL,
            "message": message,
            "files": list(files),
            "virtual_parents": [head] if head is not None else [],
        }
        commit = provider.commit_keys(commit_info, keys=[*files, *reverted, BLOB_KEY])
        if commit is None:  # pragma: no cover - the blob is always pending
            raise WorkspaceError("nothing to commit")

        # The buffer moved under the view: read the tree afresh.
        after = provider.working_files()
        for path, value in pending.items():
            if value is None:
                if path in after:
                    fs.remove(path)
            else:
                _put(fs, path, value)
        landed["head"] = commit
        self._write(landed, force=True)
        self._ws._maybe_commit(RESTORE_TOOL)
        return commit, tuple(files)

    # -- history -------------------------------------------------------

    def log(self, limit: int | None = None) -> list[CommitInfo]:
        """The agent's own commits, newest first.

        Walks ``virtual_parents`` from the agent's head, so the
        framework's per-call commits — the ones between the agent's —
        are never in it. The walk is first-parent, as ``git log`` is.
        """
        self._require("ws-git log")
        head = self._read()["head"]
        if head is None:
            return []
        out: list[CommitInfo] = []
        wanted: str | None = head
        for entry in self._provider.history():
            if wanted is None:
                break
            if entry.id != wanted:
                continue
            if is_agent_commit(entry.info):
                out.append(entry)
                if limit is not None and len(out) >= limit:
                    break
            parents = virtual_parents(entry.info)
            wanted = parents[0] if parents else None
        return out

    def history(self) -> Iterable[CommitInfo]:
        """The session's commits, newest first — the framework's
        included. ``log`` is the agent's own subset."""
        self._require("ws-git log")
        return self._provider.history()

    def entry(self, commit: str) -> CommitInfo | None:
        """One commit's record, or ``None`` when this session has no
        such commit."""
        self._require("ws-git show")
        for record in self._provider.history():
            if record.id == commit:
                return record
        return None

    def resolve(self, ref: str) -> str:
        """A ref an agent typed → a commit id on this session.

        ``HEAD`` is the agent's head; a hash (7 chars or more) names a
        commit, its own graph first and then the session's history, so
        the ids ``ws-git log`` prints resolve without the agent needing
        to know the framework's. Anything else is refused: a name is a
        session, and sessions are branches that do not switch.
        """
        self._require("ws-git checkout")
        blob = self._read()
        if ref == "HEAD":
            head = blob["head"]
            if head is None:
                raise ValueError("HEAD is unborn: this session has no ws-git commits")
            return head
        if not _HASH.fullmatch(ref):
            raise ValueError(
                f"{ref!r} is not a commit — sessions are branches, and a "
                "branch does not switch here. Name a commit from ws-git log."
            )
        for entry in self.log():
            if entry.id.startswith(ref):
                return entry.id
        for entry in self._provider.history():
            if entry.id.startswith(ref):
                return entry.id
        raise CommitNotFoundError(f"no commit {ref!r} on session {self._ws.session!r}")

    # -- checkout ------------------------------------------------------

    def checkout(self, commit: str) -> str:
        """Restore the working tree to a commit and move the agent's
        head there; returns the commit id.

        The fiction rewinds, the store does not: the restore is written
        into the working tree and lands as a new commit, so nothing
        that was committed leaves the session's history. The index is
        cleared, as ``git reset --hard`` clears it.
        """
        self._writable("ws-git checkout")
        provider = self._provider
        target = provider.files_at(commit)
        live = provider.working_files()
        fs = provider.fs
        for path in sorted(set(live) - set(target)):
            fs.remove(path)
        for path in sorted(target):
            if path not in live or live[path] != target[path]:
                _put(fs, path, target[path])
        blob = self._read()
        blob.update(head=commit, staged=[], merge_source=None, unresolved=[])
        self._write(blob, force=True)
        self._ws._maybe_commit(CHECKOUT_TOOL)
        # The tree moved under an executor that keeps its own copy of
        # it (a guest rung does): flag its view so the next call
        # re-materializes rather than harvesting the old files back.
        self._ws._mark_executor_stale()
        return commit

    # -- merge ---------------------------------------------------------

    def record_merge(self, source: str, commit: str) -> tuple[str, ...]:
        """Take a merge into the fiction: the merge commit becomes the
        agent's head, and the paths it left marked become the merge
        context ``status`` reports. Returns those paths.

        The markers are IN the merge commit, so the tree reads clean
        against the new head — the ``UU`` entries and ``diff --check``
        are what say the merge is unfinished, and they end when the
        markers do.
        """
        self._require("ws-git merge")
        marked = sorted(
            path
            for path, value in self._provider.working_files().items()
            if _has_markers(value)
        )
        blob = self._read()
        blob.update(
            head=commit,
            merge_source=source if marked else None,
            unresolved=marked,
        )
        self._write(blob)
        return tuple(marked)

    # -- content, for the verbs that render ----------------------------

    def files_at(self, commit: str) -> Mapping[str, Any]:
        """The files of one commit, by workspace path."""
        self._require("ws-git show")
        return self._provider.files_at(commit)

    def working_files(self) -> Mapping[str, Any]:
        """The live working tree's files, by workspace path."""
        self._require("ws-git diff")
        return self._provider.working_files()

    def head_files(self) -> Mapping[str, Any]:
        """The files at the agent's head — empty before its first
        commit, which is what makes everything read as added."""
        head = self._read()["head"]
        return self._provider.files_at(head) if head is not None else {}


def _unique(paths: Iterable[str]) -> list[str]:
    if isinstance(paths, str):
        paths = [paths]
    return list(dict.fromkeys(paths))


def _put(fs: Any, path: str, value: Any) -> None:
    """Write a stored file value back into the working tree.

    Through the filesystem, not the key: the file table the VFS keeps
    has to describe what the commit will hold, and only a real write
    keeps it in step.
    """
    if not isinstance(value, bytes):
        value = value.encode() if isinstance(value, str) else bytes(value)
    parent = posixpath.dirname(path)
    if parent:
        fs.makedirs(parent, exist_ok=True)
    fs.write(path, value)
