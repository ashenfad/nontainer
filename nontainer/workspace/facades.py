"""``ws.files``, ``ws.index`` and ``ws.tags``: the workspace's surfaces."""

from __future__ import annotations

import posixpath
from collections.abc import Iterable
from contextlib import AbstractContextManager
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from ..errors import WorkspaceError
from ..protocol import CommitInfo, TagInfo, WorkspaceStatus
from .results import RemoveOutcome, WriteOutcome

if TYPE_CHECKING:
    from ..agentgit import AgentGit
    from ..editing import EditOutcome
    from ..store import Ref
    from .core import Workspace


class WorkspaceFiles:
    """``ws.files``: the file surface of one session.

    Writes here are the same operation the file tools perform: the
    workspace's single-writer lock is held for the call, and the commit
    flow runs when it lands — one commit per call under autocommit,
    whatever the agent has staged. Reads take no lock and never
    commit.

    One instance per workspace, reached as ``ws.files``; it holds the
    workspace and no state of its own.
    """

    __slots__ = ("_ws",)

    def __init__(self, ws: "Workspace") -> None:
        self._ws = ws

    def __repr__(self) -> str:
        return f"<files of session {self._ws.session!r}>"

    # -- reads ---------------------------------------------------------

    def read(self, path: str, offset: int = 0, size: int = -1) -> bytes:
        """The file's bytes, or the ``size`` of them starting at
        ``offset``. Raises for a missing path — use
        :meth:`read_artifact` where absence is an answer rather than an
        error.

        The range is the filesystem's, forwarded: ``offset`` counts
        from the start and must not be negative, a negative ``size``
        reads to the end, a read starting at or past the end returns
        ``b""``, and one running past the end is truncated to what is
        there. With both defaults this is the whole file, byte for
        byte. On a backend that can serve a range without fetching the
        rest — a directory on disk, a file behind an isolation
        boundary — asking for a parquet footer costs the footer.
        """
        self._ws._check_open()
        return self._ws._fs.read(path, offset, size)

    def exists(self, path: str) -> bool:
        """Whether anything (file or directory) is at ``path``."""
        self._ws._check_open()
        return self._ws._fs.exists(path)

    def list(self, path: str = ".", recursive: bool = False) -> list[str]:
        """Files and directories under ``path``, sorted.

        Entries come back spelled the way ``path`` was — absolute for
        an absolute directory, relative to the cwd otherwise — so a
        result can be handed straight back to :meth:`read`. One level
        down by default; ``recursive`` takes everything beneath.

        The walk is the filesystem's own (``fs.list(recursive=True)``),
        not one composed here out of ``isdir``: on a real directory a
        symlink to an ancestor is an ordinary thing to find, and a
        hand-rolled descent follows it forever. monkeyfs walks with
        ``rglob``, which does not descend into symlinked directories.
        """
        ws = self._ws
        ws._check_open()
        base = posixpath.normpath(path)
        return sorted(
            posixpath.normpath(posixpath.join(base, entry))
            for entry in ws._fs.list(base, recursive=recursive)
        )

    def get(self, src: str, dest: str | Path | None = None) -> bytes:
        """Copy a workspace file OUT ("download"). Returns the bytes;
        also writes them to ``dest`` on the host when given.

        Read-only against the workspace — never commits.
        """
        ws = self._ws
        ws._check_open()
        data = ws._fs.read(src)
        if dest is not None:
            out = Path(dest).expanduser()
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(data)
        return data

    def read_artifact(self, path: str) -> bytes | None:
        """An artifact's bytes, or ``None`` if it cannot be read.

        Shaped to be handed straight to the a2ui envelope, whose
        ``read_bytes`` parameter is exactly this signature::

            turn_to_a2ui(prose, artifacts, ws.files.read_artifact, file_url, ...)

        The ``None`` is the whole point. ``turn_to_a2ui`` owns no I/O
        policy on purpose and documents ``None`` as "unreadable, degrade
        gracefully" — but the obvious ``lambda p: ws.files.read(p)``
        raises ``FileNotFoundError`` for a missing artifact and breaks
        that never-raises guarantee mid-stream, in an egress path.
        Holding that contract here means each consumer does not have to
        restate it correctly.

        Returns bytes rather than a parsed payload deliberately. Every
        consumer in this stack parses for itself (a2ui degrades on
        malformed JSON rather than raising), and a typed loader would
        invite reading it as "give me my DataFrame back" — which a
        ``head(200)`` artifact cannot honour. ``ArtifactPath.kind``
        says how to interpret the bytes when you want to.

        Read-only; never commits. Any path works, not only ``/ui`` —
        artifact notes are the usual source, but nothing here needs to
        police that.
        """
        try:
            self._ws._check_open()
            return self._ws._fs.read(path)
        except Exception:  # noqa: BLE001 - unreadable IS the answer here
            return None

    # -- writes --------------------------------------------------------

    def write(self, path: str, content: str | bytes) -> WriteOutcome:
        """Write a file (parents created, overwrites). The quoting-free
        alternative to shell redirects for multiline content; exposed
        by adapters as the ``file_write`` tool. Committed."""
        ws = self._ws
        data = content.encode() if isinstance(content, str) else content
        with ws._lock:
            ws._check_open()
            ws._check_writable("file_write")
            created = not ws._fs.exists(path)
            # PurePosixPath: workspace paths are POSIX regardless of host OS
            parent = str(PurePosixPath(path).parent)
            if parent not in (".", "/", ""):
                ws._fs.makedirs(parent, exist_ok=True)
            ws._fs.write(path, data)
            # host-side write behind the executor's back: flag it, and
            # the next execution syncs (no-op for LocalExecutor)
            ws._mark_executor_stale()
            return WriteOutcome(
                path=path,
                size=len(data),
                created=created,
                commit=ws._maybe_commit("file_write"),
            )

    def edit(
        self,
        path: str,
        old_string: str,
        new_string: str,
        *,
        replace_all: bool = False,
    ) -> "EditOutcome":
        """Exact-string replacement with agent-tolerant fallbacks (the
        agex strategy set — see ``nontainer.editing``): exact match,
        then trailing-whitespace-flexible, then indent-flexible with a
        re-indented replacement; a search that fails but whose
        replacement is already present is an idempotent no-op
        (``count == 0``). Raises ``WorkspaceError`` with an
        agent-actionable message (including a "did you mean these
        lines?" snippet) otherwise. Committed when it changes the
        file."""
        from ..editing import EditError, apply_edit

        ws = self._ws
        with ws._lock:
            ws._check_open()
            ws._check_writable("file_edit")
            try:
                text = ws._fs.read(path).decode("utf-8")
            except Exception as e:
                raise WorkspaceError(f"cannot read {path!r}: {e}") from e
            try:
                outcome = apply_edit(
                    text, old_string, new_string, replace_all=replace_all, path=path
                )
            except EditError as e:
                raise WorkspaceError(str(e)) from e
            if outcome.count:
                ws._fs.write(path, outcome.content.encode())
                ws._mark_executor_stale()  # see write()
                cp = ws._maybe_commit("file_edit")
                if cp:
                    outcome = replace(outcome, commit=cp)
            return outcome

    def remove(self, path: str) -> RemoveOutcome:
        """Delete a file. Committed, like a write.

        The same operation ``write`` is, spelled the other way: the
        workspace's single-writer lock is held for the call, the view
        rule refuses a path this session cannot see, and the commit
        flow runs when it lands — so a deletion is a commit of its own
        and the outcome names it, rather than sitting in the tree until
        something else commits.

        One FILE. A path naming a directory is refused with
        ``IsADirectoryError``: a directory here is the shape of the
        files under it, so removing one means removing them, which is
        the shell's ``rm -r`` or a ``ws.checkout(ref, paths=)`` that
        mirrors the directory as the ref has it. A path holding
        nothing raises ``FileNotFoundError``, the way ``read`` does —
        absence is the caller's to check with ``exists``.
        """
        ws = self._ws
        with ws._lock:
            ws._check_open()
            ws._check_writable("file_remove")
            if ws._fs.isdir(path):
                raise IsADirectoryError(
                    f"{path!r} is a directory: files.remove takes one file. "
                    "A directory is the shape of the files under it, so "
                    "emptying one is 'rm -r' in the terminal, or "
                    "ws.checkout(ref, paths=[...]) to make it match a ref."
                )
            if not ws._fs.exists(path):
                raise FileNotFoundError(path)
            ws._refuse_hidden([path])
            size = len(ws._fs.read(path))
            ws._fs.remove(path)
            # host-side write behind the executor's back: flag it, and
            # the next execution syncs (no-op for LocalExecutor)
            ws._mark_executor_stale()
            return RemoveOutcome(
                path=path,
                size=size,
                commit=ws._maybe_commit("file_remove"),
            )

    def put(self, src: str | Path, dest: str | None = None) -> WriteOutcome:
        """Copy a host file INTO the workspace ("upload").

        Sugar over ``ws.files.fs.write`` — whole-bytes, so sized for
        documents/datasets, not multi-GB blobs (use a :class:`Mount`
        for those). ``dest`` defaults to the source's basename at the
        workspace root; parent directories are created. Overwrites.
        """
        ws = self._ws
        src_path = Path(src).expanduser()
        data = src_path.read_bytes()
        ws_path = dest or src_path.name
        with ws._lock:
            ws._check_open()
            ws._check_writable("put")
            created = not ws._fs.exists(ws_path)
            parent = str(PurePosixPath(ws_path).parent)
            if parent not in (".", "/", ""):
                ws._fs.makedirs(parent, exist_ok=True)
            ws._fs.write(ws_path, data)
            ws._mark_executor_stale()  # see write()
            return WriteOutcome(
                path=ws_path,
                size=len(data),
                created=created,
                commit=ws._maybe_commit("put"),
            )

    # -- the tree as a whole -------------------------------------------

    def attach(
        self,
        ref: "str | Ref",
        at: str,
        *,
        readonly: bool = True,
        root: str | None = None,
    ) -> str:
        """Mount another session's tree, frozen at a commit, inside
        this one at ``at``; returns the ref it was attached from.

        For reading someone else's work in place — a delegate's branch
        while deciding whether to merge it, an expert's files while
        answering a question — without copying it in. Explicit and
        never automatic: nothing appears in a session's tree that the
        host did not put there.

        Like a :class:`Mount`, and for the same reasons: NOT versioned,
        not captured by commits, not carried by a fork, and gone when
        the session closes. Unlike a mount it is a state in the store
        rather than a directory on the host, and it is frozen, so
        ``readonly=False`` has nothing to offer and is refused.

        ``ref`` is ``session@commit`` (see :class:`~nontainer.Ref`), a
        session name, which means that session's current commit, or a
        store tag, which means the commit it names — and a tag holds
        its commit, so one mounts for as long as the tag exists,
        whatever became of the session that reached it.
        What lands at ``at`` is the source's workspace ROOT, so a file
        the delegate calls ``auth.py`` reads as ``<at>/auth.py``.
        A lineage shares one root, so that root is THIS session's;
        ``root`` names a different one for a session from elsewhere.

        What lands is the source's whole branch, not what its own
        session can see: a delegate given a narrow view is exactly the
        one a caller attaches in order to look at everything it did.

        ``at`` is workspace-absolute or relative to the root. INSIDE
        the root it reaches every rung, a guest included; outside it,
        an executor that runs somewhere else never sees it — the same
        contract a :class:`Mount` outside the root has, and for the
        same reason (a guest is given the root's subtree and nothing
        else).

        One more thing it shares with a mount: while anything is
        attached the working directory belongs to the composition, so
        a session committed in that state reopens at its root rather
        than where its agent was standing.
        """
        ws = self._ws
        with ws._lock:
            ws._check_open()
            return ws._attach(ref, at, readonly=readonly, root=root)

    def detach(self, at: str) -> None:
        """Remove an attachment and release the state behind it."""
        ws = self._ws
        with ws._lock:
            ws._check_open()
            ws._detach(at)

    def attachments(self) -> dict[str, str]:
        """What is attached, as ``{mount point: ref}``."""
        return {at: held.ref for at, held in self._ws._attached.items()}

    def export(self) -> AbstractContextManager[Path]:
        """Expose the workspace at a real path for the duration of the
        block (FUSE providers only; others raise
        ``NotSupportedError``). For the tools that must see real files
        — a subprocess, a C extension — rather than the VFS."""
        return self._ws._provider.mount()

    @property
    def fs(self) -> Any:
        """EXTENSION SURFACE: the termish-protocol filesystem, for
        host-side reads/writes (seeding inputs, harvesting artifacts)
        without the sandbox and without the commit flow. Bypasses the
        workspace's single-writer lock — a host thread writing here
        while agent calls run holds ``ws.lock``.

        Writes through this handle mark a remote executor's view stale;
        the next execution re-syncs it, so a host-written file is
        visible to the guest's very next ``cat`` (see
        :class:`_SyncingFS`). Reads pass through untouched."""
        return self._ws._public_fs


class WorkspaceIndex:
    """``ws.index``: the agent's own git over this session.

    An index the agent fills across several edits, commits it names,
    and a log of just those commits (requires ``caps.index``; other
    providers raise ``NotSupportedError`` naming the verb). It is a
    fiction over the store's history — see ``nontainer/agentgit.py``
    for the model — and the terminal's ``ws-git`` verbs are the same
    implementation, so an agent and its host see one index and one
    graph, not two.

    The framework's own commits (``ws.commit()``, a turn hook, a
    session db, a skill install) never disturb it: everything here is
    measured against the agent's last commit, not the store's head.
    """

    __slots__ = ("_ws",)

    def __init__(self, ws: "Workspace") -> None:
        self._ws = ws

    def __repr__(self) -> str:
        return f"<index of session {self._ws.session!r}>"

    @property
    def _git(self) -> "AgentGit":
        from ..agentgit import AgentGit

        return AgentGit(self._ws)

    @property
    def head(self) -> str | None:
        """The commit the agent's last :meth:`commit` made, or ``None``
        before the first one."""
        return self._git.head

    def stage(self, paths: Iterable[str]) -> tuple[str, ...]:
        """Add workspace file paths to the index; returns what was new.

        Bookkeeping only: nothing commits, and nothing changes about
        what autocommit does. Staging is optional — :meth:`commit`
        with an empty index takes everything modified, the way
        ``git commit -a`` does.
        """
        ws = self._ws
        with ws._lock:
            return self._git.stage(paths)

    def unstage(self, paths: Iterable[str]) -> tuple[str, ...]:
        """Remove paths from the index; returns what was removed."""
        ws = self._ws
        with ws._lock:
            return self._git.unstage(paths)

    def commit(self, message: str | None = None, *, info: dict | None = None) -> str:
        """Record an agent commit; returns its id.

        Takes the staged paths that differ from the agent's last
        commit, or everything modified when nothing is staged. The
        commit's tree is exactly that: work in progress the agent left
        out stays in the working tree and out of the commit, rather
        than being absorbed into its baseline.
        """
        ws = self._ws
        with ws._lock:
            ws._check_open()
            return self._git.commit(message, info)[0]

    def discard(self) -> None:
        """Abandon the composition; the working tree keeps its writes."""
        ws = self._ws
        with ws._lock:
            self._git.discard()

    def status(self) -> WorkspaceStatus:
        """Staged vs unstaged paths plus live merge context. Pure read —
        ungated like ``diff``/``log``: reads are the point of snapshots."""
        return self._git.status()

    def log(self, limit: int | None = None) -> list[CommitInfo]:
        """The agent's own commits, newest first — the framework's are
        not in it."""
        return self._git.log(limit)

    def checkout(self, commit: str) -> str:
        """Restore the working tree to one of the agent's commits and
        move its head there; returns the commit id.

        The fiction rewinds; the store appends. The restore lands as a
        new commit, so nothing already committed leaves the session's
        history. Only the AGENT's commits are reachable here;
        ``ws.checkout`` is the host's verb, which takes any commit of
        the session — and appends in the same way.

        Refused while a merge of this session's is outstanding: the
        restore would write a different tree and clear the context
        that records where the markers are, so a tree still holding
        them would read clean. Resolve them and commit, or
        ``ws-git merge --abort``.
        """
        ws = self._ws
        with ws._lock:
            ws._check_open()
            ws._require_unmerged(self._git, "checkout")
            return self._git.checkout(commit)

    @property
    def tags(self) -> "WorkspaceIndexTags":
        """``ws.index.tags``: the agent's own bookmarks.

        The namespace, which is also callable: ``ws.index.tags()``
        answers with the mapping, the older spelling of
        ``ws.index.tags.list()``.
        """
        return WorkspaceIndexTags(self._ws)

    def tag(self, name: str, ref: str | None = None, *, force: bool = False) -> str:
        """The older spelling of ``ws.index.tags.add(name, at=ref)``.

        ``ref`` defaults to the agent's head and may be any ref the
        agent can type. A name already taken is refused unless
        ``force`` moves it, and a name that could be read as a commit
        id is refused outright.
        """
        return self.tags.add(name, at=ref, force=force)

    def delete_tag(self, name: str) -> str:
        """The older spelling of ``ws.index.tags.delete(name)``:
        returns the commit the bookmark named, which stays where it
        is."""
        return self.tags.delete(name)


class WorkspaceIndexTags:
    """``ws.index.tags``: the names the agent gives its own commits.

    Not ``ws.tags``, which are the store's. A ws-git tag is a name in
    the agent's own blob: it pins nothing against collection (the
    session's history is append-only and reaches every commit the agent
    made), it belongs to this session and dies with it, a fork starts
    with none, and a merge brings none over.

    The four verbs and the ``at=`` keyword are ``ws.tags``' and
    ``store.tags``', so the vocabulary is learned once and the object
    reached through says which scope it is about. There is no ``at()``
    here: a bookmark names a commit this session's own history already
    holds, and standing on one is ``ws.index.checkout(commit)`` rather
    than a frozen workspace over somebody's snapshot.

    Calling the namespace — ``ws.index.tags()`` — is the older spelling
    of :meth:`list`.
    """

    __slots__ = ("_ws",)

    def __init__(self, ws: "Workspace") -> None:
        self._ws = ws

    def __repr__(self) -> str:
        return f"<index tags of session {self._ws.session!r}>"

    def __call__(self) -> dict[str, str]:
        """The older spelling of :meth:`list`."""
        return self.list()

    @property
    def _git(self) -> "AgentGit":
        from ..agentgit import AgentGit

        return AgentGit(self._ws)

    def add(self, name: str, *, at: str | None = None, force: bool = False) -> str:
        """Bookmark one of the agent's commits by name; returns the id.

        ``at`` defaults to the agent's head and may be any ref the
        agent can type — a commit of its own, a short id, another
        bookmark. A name already taken is refused unless ``force``
        moves it, and a name that could be read as a commit id is
        refused outright.
        """
        ws = self._ws
        with ws._lock:
            ws._check_open()
            return self._git.tag(name, at, force=force)

    def list(self) -> dict[str, str]:
        """Tag name → commit id, for the agent's own bookmarks.

        A record of what the tags are, not a live view — changing the
        mapping changes no tag.
        """
        return self._git.tags()

    def info(self, name: str) -> TagInfo | None:
        """Describe one bookmark, or ``None`` if there is no such name.

        The scope reads ``"index"``, since the name lives in the
        agent's blob rather than in the store, and ``time``, ``tree``
        and ``info`` are the named COMMIT's: a bookmark carries no
        metadata of its own and no record of when it was made.
        ``dangling`` says the commit it names is no longer one the
        agent's own history reaches.
        """
        commit = self._git.tags().get(name)
        if commit is None:
            return None
        entry = self._git.entry(commit)
        return TagInfo(
            name=name,
            scope="index",
            id=commit,
            tree=entry.tree if entry else None,
            time=entry.time if entry else None,
            info=dict(entry.info) if entry else None,
            dangling=entry is None,
        )

    def delete(self, name: str) -> str:
        """Drop one bookmark; returns the commit it named. The commit
        stays exactly where it is — the session's own history already
        holds it, so removing the name takes nothing with it."""
        ws = self._ws
        with ws._lock:
            ws._check_open()
            return self._git.delete_tag(name)


class WorkspaceTags:
    """``ws.tags``: names this session gives its own commits.

    Session-scoped, always. The name belongs to this session — another
    session's ``v1`` is a different tag, and deleting the session
    (``Store.delete``) deletes it — which is what makes it the right
    place for "before the refactor". A name that must outlive the
    session, or be read from another one, is store-scoped and lives on
    ``store.tags``.

    Tags never move: an existing name raises rather than being
    repointed. A tag also anchors garbage collection — the named commit
    and its ancestry stay reachable for as long as the name exists.
    Requires ``caps.tags``.
    """

    __slots__ = ("_ws",)

    def __init__(self, ws: "Workspace") -> None:
        self._ws = ws

    def __repr__(self) -> str:
        return f"<tags of session {self._ws.session!r}>"

    def add(
        self,
        name: str,
        *,
        at: str | None = None,
        info: dict[str, Any] | None = None,
    ) -> str:
        """Name a commit, immutably; returns the commit id.

        Names the current state by default, committing staged changes
        first so the name means what the caller saw; ``at`` names an
        earlier commit of this session instead. ``info`` is caller
        metadata and must be JSON-serializable.
        """
        return self._ws._tag(name, at=at, info=info, scope="session")

    def list(self) -> dict[str, str]:
        """Tag name → commit id, for this session's tags."""
        return self._ws._tags(scope="session")

    def info(self, name: str) -> TagInfo | None:
        """Describe one tag, or ``None`` if this session has no such tag."""
        return self._ws._tag_info(name, scope="session")

    def delete(self, name: str) -> None:
        """Drop a tag. What it named survives only while something else
        still reaches it — a branch, or another tag."""
        self._ws._delete_tag(name, scope="session")

    def at(self, name: str) -> "Workspace":
        """A frozen workspace over the tagged state.

        Reads see the tagged files, cache and cwd; nothing can be
        written or committed (see :attr:`Workspace.frozen`). Close it
        when done — it holds an executor of its own.
        """
        return self._ws._at_tag(name, scope="session")
