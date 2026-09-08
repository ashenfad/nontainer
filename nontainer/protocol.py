"""The seams: where workspace state lives, and how code runs on it.

``WorkspaceProvider`` (below) is the substrate seam; ``Executor``
(further down) is the execution seam. ``SessionRunner`` and
``HostObjectFactory`` are the loop seam — declared here, unused until
the sessions verbs land.

A provider supplies three things:

1. ``fs``  — a filesystem satisfying termish's ``FileSystem`` protocol
   (16 methods). termish executes shell commands against it; monkeyfs
   routes sandboxed ``open()`` / ``os.*`` through it.
2. ``kv``  — a ``MutableMapping[str, Any]`` for small values. nontainer
   builds the agent-facing ``cache`` on top (prefix-scoped, key rules,
   picklability checks).
3. Versioning verbs — gated by ``Capabilities`` rather than pretended
   equivalence. A provider that can't fork says so; the toolkit layer
   degrades honestly instead of emulating badly.

Providers are session-scoped: one provider instance == one session's
world. Session resolution (e.g. "kvgit branch per session id") happens
in the factory that builds the provider, not here.

Planned implementations:

- ``KvgitProvider``   (default) — kvgit ``Staged`` per session branch.
  staging=True, cheap_fork=True, merge=True, tags=True.
- ``DirProvider``     — a real directory via monkeyfs ``IsolatedFS``.
  versioned=False; the tools work, the time-travel verbs raise.
- ``AgentFSProvider`` (spike) — Turso AgentFS via its Python SDK.
  sql_audit=True, fuse=True (opt-in mount); fork by file copy
  (cheap_fork=False).

Concurrency note: providers are NOT thread-safe, and don't need to
be — ``Workspace`` owns the single-writer invariant: its mutating
public methods hold an internal ``RLock``, so a harness that threads
parallel tool calls onto one session (not hypothetical: agno's
``arun()`` executes sync tools concurrently, including parallel calls
from one model turn) serializes safely instead of corrupting staged
state. Embedders driving a *provider* directly (bypassing Workspace)
take on serialization themselves.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, MutableMapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from .workspace import PythonConfig, PythonResult, TerminalResult

# Session ids become branch names / storage paths / db filenames.
# Same rule as agex's Local host: no leading dot, no separators.
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9_.-]*$")


def validate_session_id(session: str) -> str:
    """Return ``session`` unchanged or raise ``SessionIdError``."""
    from .errors import SessionIdError

    if not isinstance(session, str) or not SESSION_ID_RE.match(session):
        raise SessionIdError(
            f"Invalid session id {session!r}: must match "
            f"{SESSION_ID_RE.pattern} (no leading dot, no path separators)"
        )
    return session


@dataclass(frozen=True)
class Capabilities:
    """What a provider can actually do. Flags, not promises.

    ``versioned`` is the master switch: when False, ``commit`` /
    ``checkout`` / ``history`` / ``fork`` all raise ``NotSupportedError``
    and the remaining flags are meaningless.
    """

    versioned: bool = True
    staging: bool = False
    """Writes accumulate invisibly-to-other-sessions until
    ``commit()``; ``discard()`` drops them. When False, writes are
    durable immediately and ``discard()`` raises."""

    cheap_fork: bool = False
    """Fork is O(1) with shared storage (kvgit branch). When False but
    ``versioned``, fork may still work — just expensively (file copy)."""

    merge: bool = False
    """This branch can merge another branch's HEAD (CAS + key-level
    three-way merge with conflict markers)."""

    sql_audit: bool = False
    """Operation-level audit log queryable with SQL (AgentFS)."""

    fuse_mount: bool = False
    """``mount()`` can expose the workspace at a real path for
    subprocesses / C extensions."""

    tags: bool = False
    """Commits can be given names that outlive the call that made
    them: immutable references that also anchor garbage collection, so
    a named commit (and everything it descends from) is kept for as
    long as the name exists. When False, ``tag`` / ``tags`` /
    ``tag_info`` / ``delete_tag`` / ``at_tag`` / ``diff`` raise
    ``NotSupportedError``."""

    index: bool = False
    """Keyed commits are available, so the agent-facing git fiction is:
    ``commit_keys`` can commit a subset of the tree, which is what lets
    an agent's own commit hold exactly what the agent staged while the
    framework goes on committing everything for durability (see
    ``nontainer/agentgit.py``). Appended last so earlier positional
    ``Capabilities(...)`` constructions keep their meaning."""


@dataclass(frozen=True)
class CommitInfo:
    """One entry in ``history()``."""

    id: str
    """Provider-scoped opaque id (kvgit: commit hash)."""

    time: float
    """Unix epoch seconds."""

    info: dict[str, Any] = field(default_factory=dict)
    """Caller-supplied metadata (``{"tool": "run_python", ...}``)."""

    tree: str | None = None
    """Hash of the commit's content (kvgit: the keyset root) —
    the identity of *what the files and cache are*, as opposed to
    ``id``, the identity of *this point in history*. Equal trees mean
    identical content, whatever the metadata, ancestry or time around
    it; that implication runs one way only, because a store may stamp
    each write with when it happened (kvgit does), so rewriting a value
    with the same bytes still yields a different tree. ``None`` on
    providers with no such hash."""


@dataclass(frozen=True)
class TagInfo:
    """What a provider records about one tag."""

    name: str
    """The name as the caller gave it — no scope prefix."""

    scope: str
    """``"session"`` or ``"store"`` (see ``WorkspaceProvider.tag``)."""

    id: str
    """The commit the tag names."""

    tree: str | None
    """The tagged commit's content hash (see
    ``CommitInfo.tree``)."""

    time: float | None
    """When the tag was made, unix epoch seconds. ``None`` when the
    provider has no record of it."""

    info: dict[str, Any] | None
    """Caller metadata passed to ``tag()``, or ``None``."""

    dangling: bool
    """The named commit is not in the store — damage rather than an
    ordinary state, and such a tag keeps nothing alive."""


@dataclass(frozen=True)
class WorkspaceDiff:
    """What changed between two commits, as workspace file paths.

    Absolute VFS paths, the way agent code and ``ws.files.fs`` name files
    (``/workspace/data/in.csv``). Framework keys — the cache, cwd, the
    stored conversation, the filesystem's own bookkeeping — are not
    files and never appear here.

    ``modified`` holds the paths whose BYTES differ between the two
    commits. A file re-saved with the content it already had is not
    a change here, even where the store's own key-level diff counts the
    write — the provider compares the content.
    """

    added: frozenset[str]
    removed: frozenset[str]
    modified: frozenset[str]


@dataclass(frozen=True)
class MergeOutcome:
    """What merging another branch produced, as workspace file paths.

    ``merged`` tells whether a merge commit was created. File conflicts
    materialize as conflict markers IN that commit (flagged here, never
    blocking): resolve with ordinary edits and commit. ``merged``
    False with ``commit`` None means nothing changed — non-file
    contested state, which no merge function can resolve, aborts
    untouched. ``conflicts`` names the paths needing resolution (raw
    store keys for non-files); ``auto_merged`` names what the merge
    took without judgment.
    """

    merged: bool
    commit: str | None
    conflicts: tuple[str, ...]
    auto_merged: tuple[str, ...]


@dataclass(frozen=True)
class WorkspaceStatus:
    """Staged vs unstaged file paths plus live merge context.

    Everything here is relative to the AGENT's last commit, not the
    store's head: the framework's own commits (a turn, a session db, a
    skill install) move the store and leave this untouched.
    """

    branch: str
    staged: tuple[str, ...]
    """Indexed paths that differ from the agent's last commit — what
    ``ws.index.commit()`` would take."""
    unstaged: tuple[str, ...]
    """Modified paths outside the index."""
    merge_source: str | None
    """Session the outstanding merge came from, or ``None``. Recorded
    by the merge and dropped once its markers are gone."""
    merge_unresolved: tuple[str, ...]
    """Paths the merge left marked that still carry markers (empty
    unless a merge is outstanding), so resolving them one at a time
    shows progress."""


@runtime_checkable
class WorkspaceProvider(Protocol):
    """Substrate contract. See module docstring for the three surfaces."""

    # -- identity ------------------------------------------------------

    @property
    def session(self) -> str: ...

    @property
    def caps(self) -> Capabilities: ...

    # -- surfaces ------------------------------------------------------

    @property
    def fs(self) -> Any:
        """Filesystem satisfying termish's ``FileSystem`` protocol.

        Typed ``Any`` to avoid a hard import here; implementations
        return a termish-compatible object (monkeyfs ``VirtualFS`` /
        ``IsolatedFS``, or an AgentFS adapter).
        """
        ...

    @property
    def kv(self) -> MutableMapping[str, Any]:
        """Small-value store backing the agent cache.

        Values must round-trip pickle (kvgit) or the provider's own
        encoding (AgentFS: JSON — the Cache layer surfaces encoding
        failures at write time either way).
        """
        ...

    @property
    def dirty(self) -> bool:
        """Staged-but-uncommitted changes exist. Always False for
        providers without ``caps.staging``. Used by the apps extra to
        decide whether a failed handler can be rolled back atomically
        (``discard()``) without destroying unrelated pending work."""
        ...

    # -- versioning (gated by caps.versioned) --------------------------

    @property
    def head(self) -> str:
        """Id of the current (latest) commit. Staged-but-uncommitted
        changes are NOT captured by it — check ``dirty``. Raises
        ``NotSupportedError`` for unversioned providers."""
        ...

    def commit(self, info: dict[str, Any] | None = None) -> str:
        """Atomically capture fs + kv as one commit; return its id.

        Everything uncommitted, always: this is the framework's
        durability verb, and code around a workspace can only rely on
        it if its scope never depends on what the agent has staged.
        ``commit_keys`` is the subset one.

        With ``caps.staging``, this is the moment staged writes become
        visible/durable. Without staging, it's a marker over already-
        durable state (AgentFS: snapshot).
        """
        ...

    def checkout(self, commit_id: str, *, info: dict[str, Any] | None = None) -> str:
        """Make fs + kv what they were at a commit; return the new id.

        APPENDS: the state is restored by writing it, so the result is
        a new commit whose keyset equals the target's — everything the
        session holds, not only its files — and every commit made
        since the target is still in ``history()``. Nothing here moves
        a branch head backward; only store-level admin does.

        It CONVERGES on the target: a write another handle lands on
        the session while this is in flight is superseded rather than
        carried into the result, and the caller finds it in
        ``history()`` like any other commit.

        Uncommitted writes are replaced by the restored state, since
        that is what "make the workspace what it was" means; a caller
        that wants them gone on their own terms calls ``discard()``
        first. When the working state already equals the target,
        nothing is committed and the current head comes back.

        ``info`` is merged into the commit's own metadata.
        """
        ...

    def history(self, *, limit: int | None = None) -> Iterable[CommitInfo]:
        """Commits, newest first."""
        ...

    def fork(self, name: str, *, at: str | None = None) -> "WorkspaceProvider":
        """New independent session seeded from current state.

        kvgit: O(1) branch. AgentFS: file copy. ``name`` is validated
        like a session id and must not already exist.
        """
        ...

    def discard(self) -> None:
        """Drop uncommitted staged writes (requires ``caps.staging``)."""
        ...

    # -- tags (gated by caps.tags) -------------------------------------

    def tag(
        self,
        name: str,
        *,
        at: str | None = None,
        info: dict[str, Any] | None = None,
        scope: str = "session",
    ) -> str:
        """Name a commit immutably; return the commit id.

        Two scopes, and nontainer picks which one applies rather than
        leaving the namespace to embedders:

        - ``"session"`` (default) — the name belongs to this session.
          ``tags()`` lists only its own, two sessions can both hold a
          ``v1``, and deleting the session deletes them.
        - ``"store"`` — the name belongs to no session. It is visible
          from every session on the store and survives the deletion of
          the session that made it: the scope for a publication that
          must outlive its author.

        Tags never move: an existing name raises rather than being
        repointed. ``at`` defaults to the current head; ``info`` must be
        JSON-serializable.
        """
        ...

    def check_tag(self, name: str, *, scope: str = "session") -> None:
        """Validate a name and scope without writing anything.

        The rules ``tag`` would apply, available before the commit a
        caller may have to make first: a workspace with staged work
        commits before naming it, and a name rejected afterwards
        would leave that commit behind for nothing. Raises the same
        ``ValueError`` ``tag`` raises.
        """
        ...

    def tags(self, *, scope: str = "session") -> dict[str, str]:
        """Tag name (no scope prefix) → commit id, in one scope."""
        ...

    def tag_info(self, name: str, *, scope: str = "session") -> TagInfo | None:
        """Describe one tag, or ``None`` if there is no such tag."""
        ...

    def delete_tag(self, name: str, *, scope: str = "session") -> None:
        """Drop a tag. The commit it named survives only if
        something else still reaches it."""
        ...

    def at_tag(self, name: str, *, scope: str = "session") -> "WorkspaceProvider":
        """A FROZEN provider over the tagged commit.

        Reads see the tagged state. Nothing can commit: ``commit``,
        ``checkout``, ``fork``, ``tag`` and ``delete_tag`` raise
        ``NotSupportedError``; writes may stage (so ``dirty`` can become
        True) but have nowhere to land, and ``discard`` drops them.

        Such a provider reports ``frozen`` True; a provider without the
        attribute reads as not frozen, which is what a workspace over a
        third-party provider assumes.
        """
        ...

    def diff(self, a: str, b: str) -> WorkspaceDiff:
        """File-level changes between two commit ids."""
        ...

    def merge(
        self,
        source: str,
        *,
        at: str | None = None,
        info: dict[str, Any] | None = None,
    ) -> MergeOutcome:
        """Merge another branch into this one (requires ``caps.merge``).

        ``source`` names the branch. ``at`` names the commit ON that
        branch whose state to merge; ``None`` means the branch's
        current head, and either way anything uncommitted on the source
        is not included. Both keywords are always passed, so a provider
        must accept them: ``at`` is how a caller merges a state the
        source has moved on from — the agent-facing git merges a
        session's last AGENT commit rather than whatever the framework
        committed for it since.

        Refuses with ``WorkspaceError`` on uncommitted changes here
        (commit or discard first) and with ``ValueError`` for unknown
        or self branches; an ``at`` the provider does not have raises
        ``CommitNotFoundError``. Providers without the capability raise
        ``NotSupportedError``. ``info`` is caller metadata recorded on
        the merge commit (and on any follow-up the merge needs), which
        is how the agent-facing git threads a merge into its own graph.
        """
        ...

    # -- keyed commits and read views (gated by caps.index) ------------

    def commit_keys(
        self, info: dict[str, Any] | None = None, *, keys: Iterable[str]
    ) -> str | None:
        """Commit exactly these keys (requires ``caps.index``). Returns
        the new commit hash, or ``None`` when none of them had anything
        pending.

        ``keys`` are provider keys as stored (``__agno__/session``) or
        absolute workspace paths, which the provider resolves to its
        own keys. Whatever bookkeeping a commit of those keys needs to
        be readable rides along.

        This is the one index-shaped primitive a substrate has to
        offer. The agent-facing git (``nontainer/agentgit.py``) is
        metadata over it: an agent commit is a keyed commit of exactly
        the tree the agent composed, made while the framework keeps
        committing everything for durability.
        """
        ...

    def files_at(self, commit: str) -> Mapping[str, Any]:
        """This session's files at one commit, by workspace path
        (requires ``caps.index``).

        A read view, not a copy: values are read on demand, so
        comparing two trees costs the blobs it actually reads. Unknown
        commits raise ``CommitNotFoundError``.
        """
        ...

    def working_files(self) -> Mapping[str, Any]:
        """The live working tree's files, by workspace path (requires
        ``caps.index``) — uncommitted writes included, uncommitted
        deletions excluded."""
        ...

    # -- power modes / lifecycle ---------------------------------------

    def mount(self) -> Any:
        """Context manager yielding a real ``Path`` (requires
        ``caps.fuse_mount``). See README for platform caveats."""
        ...

    def close(self) -> None:
        """Release resources (db handles, mounts). Idempotent."""
        ...


# ======================================================================
# The execution seam
# ======================================================================
#
# ``WorkspaceProvider`` says where state LIVES; ``Executor`` says how
# code RUNS against it. Both seams live here so an implementer of
# either reads one file, and so nothing has to import
# ``nontainer.executor`` (which pulls sandtrap and termish) just to
# type-check against the contract. ``executor.py`` re-exports these
# names, so existing imports keep working.
#
# The result and config types the contract speaks (``PythonResult``,
# ``TerminalResult``, ``PythonConfig``) stay in workspace.py: they are
# the public vocabulary, and importing them here at runtime would cycle
# (workspace.py imports this module for the provider protocol). Under
# ``from __future__ import annotations`` every annotation below is a
# string, so the TYPE_CHECKING import is enough.


class HarvestLost(RuntimeError):
    """A remote executor lost its guest between a successful exec and
    the write harvest. The call is TORN, not cleanly absent: dud
    applies cache write-backs inside the successful exec, but fs
    writes cross only via the follow-up ``diff()`` — so the cache half
    may already be staged provider-side while the fs half died,
    unrecoverable, with the guest. Raised by :meth:`Executor.diff`
    (after recovering a fresh session) so the workspace can surface an
    errored result and unwind the staged half — an empty diff here
    would report success for a call whose fs effects silently
    vanished."""


@dataclass(frozen=True)
class StagedDiff:
    """A remote executor's write harvest.

    A remote executor accumulates writes in its own staging area (an
    overlay upperdir, a scan-diff) rather than writing through to the
    provider; :meth:`Executor.diff` returns them as a ``StagedDiff``
    and the workspace stages them into the provider before its normal
    commit flow — so atomic commit + ``result.commit``
    semantics are identical across executors. ``LocalExecutor`` never
    constructs one: its writes land in the provider as they happen.
    """

    writes: Mapping[str, bytes]
    """Fs-root-relative path (no leading slash) -> full new content.
    Executors whose substrate roots the workspace elsewhere (dud's
    guest mount) translate to fs-root-relative before returning, so
    the workspace can stage without knowing the substrate layout.
    Whole-file payloads, not patches — the wire-format decision (dud
    PLAN #1): one encoding both scan-diff and overlay harvest emit."""

    deletes: tuple[str, ...] = ()
    """Fs-root-relative paths removed since the last harvest."""


@dataclass(frozen=True)
class ExecutionContext:
    """What :meth:`Executor.open` binds to: one session's workspace
    state, as executions see it.

    For ``LocalExecutor`` these are live references — execution works
    directly against the provider-backed objects, so writes land in
    the provider the moment they happen. A remote executor instead
    treats the context as the two ends of its transport: ``fs`` is
    what it materializes the guest tree from (and whose diff the
    workspace stages back), ``kv`` backs the cache service, and the
    rest is policy.
    """

    fs: Any
    """The workspace filesystem (termish protocol, mounts composed)."""

    kv: MutableMapping[str, Any]
    """The provider's kv store; the agent-facing cache builds on it."""

    commands: MutableMapping[str, Callable[..., Any]]
    """Injected terminal commands (termish ``CommandFunc``). A LIVE
    reference: the workspace mutates it after construction
    (``register_command`` — apps' curl) — bind the mapping, don't
    copy it."""

    python_config: PythonConfig

    cache_enabled: bool

    max_observation: int
    """Observation budget (chars) for rendered stdout/stderr. Applied
    executor-side because budget-aware rendering must happen where
    the printed objects live (see ``_render_prints``)."""

    head: "Callable[[], str | None] | None" = None
    """State-identity accessor: the commit id the workspace fs
    currently EQUALS — i.e. the provider head, but None whenever
    staging is dirty (the fs view then names no committed state) or the
    provider has no commit identity. Executors with reusable substrates
    (dud's VM pool) use it as a content-addressed state tag: a parked
    machine tagged with the same id can resume WITHOUT a tree push, so
    a tag must never name a state the tree doesn't exactly hold.
    Callable so it's read at tag time, not frozen at open."""

    frozen: bool = False
    """This workspace is a read-only snapshot at a tag.

    An executor that can refuse writes should: ``LocalExecutor`` needs
    no code for it, since the ``fs`` and ``kv`` bound here are already
    read-only views that raise where the write happens. An executor
    that runs against its own substrate (a guest tree) may write freely
    and report the harvest; the workspace refuses to absorb it and
    tells the caller the call was refused, so honouring the flag is an
    optimization, never the guarantee."""

    root: str = "/workspace"
    """The workspace root: the absolute VFS path agent-visible files
    live under — the ONE path contract shared across executors.
    ``LocalExecutor`` points sandtrap's module imports here; a VM
    executor mounts its guest workspace at this exact path, so an
    absolute path in agent code means the same file everywhere.
    ``"/"`` selects the pre-0.2 layout (files at the fs root — a VM
    guest can't mount there, so absolute paths diverge on VM rungs)."""

    shell_env: "MutableMapping[str, str] | None" = None
    """Shell variables for script executions: ``$VAR`` expansion on a
    termish rung, exported into the guest on a VM rung.

    A LIVE reference, like ``commands``, and for the same reason: the
    runtime publishes into it after construction (``enable_apps``
    publishes ``$APP_ORIGIN``), so an executor that copied it at open
    would run every later call without those variables. Executors
    snapshot it PER CALL instead — a command mutating the mapping
    termish is handed must not leak into the next call.

    It belongs to the runtime that bound this context, not to the
    workspace: one workspace can carry several runtimes (an app served
    from a snapshot runs on its own), and each has its own variables.
    ``None`` for a hand-built context: no variables.
    """

    workspace: Any = None
    """The live workspace, for framework callbacks that must act AS it.

    Executors that call back into workspace state (dud's ws-git verb
    handler invoking the registered terminal command) bind this, not a
    snapshot: provider, lock, and command mapping are read per call.
    Same behavior as the local rung by construction — including its
    known holes (the fork-bleed tracked in #63), which a snapshot
    would merely diverge from. ``None`` when constructed by hand
    (tests, embedders): framework callbacks are then unoffered."""


@dataclass(frozen=True)
class ViewSpec:
    """A per-call restricted, budgeted execution — the apps extra's
    handler dispatch is the only consumer.

    This is the executor-neutral replacement for the old
    ``build_sandbox`` + ``exec_python(sandbox=...)`` pair. The caller
    declares the *intent* — a read-only filesystem/cache view, a
    tighter timeout/tick budget, contract classes that must be in the
    handler's scope — and each executor realizes it its own way:
    ``LocalExecutor`` builds a sandtrap sandbox (policy memoized), a
    remote executor sets a read-only mount / rejects the write-diff and
    injects the contract classes by import. No sandbox object crosses
    the seam, so nothing sandtrap-shaped rides on the ``Executor``
    protocol.
    """

    readonly_fs: bool = False
    """A GET-handler view: writes to the workspace fs are refused.
    LocalExecutor wraps the fs in ``ReadOnlyFS`` (write-time
    ``PermissionError``); a remote executor rejects a non-empty
    write-diff after the call (rung-1 dud) or mounts read-only (VM
    rungs)."""

    readonly_cache: bool = False
    """The cache is read-only for this call (GET structural REST)."""

    timeout: float | None = None
    """Per-call wall-clock budget (else the config's)."""

    tick_limit: int | None = None
    """Per-call tick budget (LocalExecutor only; remote executors have
    no tick machinery and ignore it — wall-clock is the guard)."""

    extra_classes: tuple[type, ...] = ()
    """Classes the handler code must be able to name (apps' ``Request``
    / ``Response`` / ``HttpError``). LocalExecutor registers them in the
    sandbox policy; a remote executor imports them by qualified name."""


@runtime_checkable
class Executor(Protocol):
    """Execution contract. See module docstring for the seam's shape.

    ``diff``/``sync`` exist for executors whose writes don't land in
    the provider directly. The workspace calls ``diff`` after every
    mutating exec (absorbing any harvest into the provider before the
    commit flow) and ``sync`` whenever it changes provider state
    behind the executor's back (checkout/rollback/discard, host-side
    writes). Both are free no-ops for ``LocalExecutor``.
    """

    # -- capabilities ----------------------------------------------------

    supports_commands: bool
    """Whether ``ExecutionContext.commands`` reach the shell.

    A capability flag in the ``WorkspaceProvider`` spirit: declare the
    difference instead of pretending equivalence. ``LocalExecutor``
    runs termish and hands it the mapping, so injected builtins (apps'
    ``curl``) are real commands. An executor running actual bash in a
    guest has no such hook — the mapping isn't reachable from there.

    Tool descriptions are built against this. Teaching an agent a
    command that answers ``command not found`` costs it turns, so the
    apps primer advertises ``ws-curl`` only where it exists.
    """

    # -- lifecycle -------------------------------------------------------

    def open(self, context: ExecutionContext) -> None:
        """Bind to a workspace's state and start any resident machinery
        (LocalExecutor: build the default sandbox, fork the isolation
        worker; a VM executor: boot/resume and materialize the tree).

        Called once, by ``Workspace.__init__``, as its LAST step — so
        no construction failure after this point can orphan a worker.
        Not re-entrant."""
        ...

    def close(self) -> None:
        """Release execution resources (workers, VMs). Best-effort and
        idempotent — must not raise: the workspace closes its provider
        next regardless of what happens here."""
        ...

    # -- python ----------------------------------------------------------

    def exec_python(
        self,
        code: str,
        *,
        inputs: Mapping[str, Any] | None = None,
        stdin: str | None = None,
        argv: list[str] | None = None,
        echo: Literal["none", "last", "all"] | None = None,
        view: ViewSpec | None = None,
    ) -> PythonResult:
        """One scripted python execution against workspace state.

        ``inputs`` are picklable per-call data bound as top-level
        names; ``PythonConfig.host_objects`` and ``cache`` are injected
        per the frozen config; ``stdin``/``argv`` feed the synthetic
        ``sys``; ``echo`` overrides expression echo for this call.
        Agent-code failure is a result (``PythonResult.error``), never
        an exception.

        ``view`` (apps handler dispatch) requests a restricted,
        budgeted execution — a read-only fs/cache view, a tighter
        budget, contract classes in scope. It is executor-neutral: no
        sandbox object crosses the seam (see :class:`ViewSpec`). The
        default (``None``) is the executor's standard environment.

        The result's ``commit`` is ``None``: executors never
        commit; the workspace stamps commits."""
        ...

    # -- shell -----------------------------------------------------------

    def exec_shell(self, script: str) -> TerminalResult:
        """One shell script (pipes, redirects, ``;``) against the
        workspace fs, with the context's injected commands available.
        Never raises for command failure — exit codes are results.
        ``commit`` is ``None`` here too (see ``exec_python``)."""
        ...

    # -- staging (remote executors) ---------------------------------------

    def diff(self) -> StagedDiff | None:
        """Harvest writes staged executor-side since the last harvest
        (or ``sync``). Called by the workspace after every mutating
        exec, before its commit flow.

        ``LocalExecutor`` returns ``None`` — its writes land in the
        provider the moment they happen (monkeyfs/termish write
        through), so there is nothing to harvest; ``None`` also means
        "nothing staged" from a remote executor after a read-only
        call, so the workspace's dirty check stays accurate."""
        ...

    def sync(self) -> None:
        """Refresh the executor's view of workspace state from the
        provider. Every path where provider state moves without the
        executor seeing it marks the workspace stale — checkout /
        rollback / discard, the host-side write helpers
        (``files.write`` / ``files.edit`` / ``files.put``), and direct
        ``ws.files.fs`` writes — and the workspace calls this once, lazily,
        before the next execution. Lazy because a remote
        implementation may re-push the whole tree: N host writes cost
        one sync, not N. No-op for ``LocalExecutor``: there is no
        second copy."""
        ...


# ======================================================================
# The loop seam (declared, not yet used)
# ======================================================================
#
# Running a nested agent turn is neither provider-shaped nor
# executor-shaped: it needs a model, a tool loop and a budget, none of
# which nontainer owns. The two protocols below name that seam so the
# sessions verbs can be typed against a contract the embedder
# implements, rather than against whatever object happened to be
# injected. Nothing in nontainer calls them yet.


@runtime_checkable
class SessionRunner(Protocol):
    """Runs one agent turn against a session and returns its answer.

    The embedder implements this — it owns the model, the toolkit and
    the accounting. ``session`` names the workspace the turn runs
    against; ``task`` is the instruction; ``budget`` caps the work
    (interpretation is the runner's: turns, tokens, seconds).
    """

    def run(self, session: str, task: str, *, budget: Any = None) -> Any:
        """Run ``task`` against ``session`` and return the answer."""
        ...


@runtime_checkable
class HostObjectFactory(Protocol):
    """Decides which host objects a child session's code may reach.

    Host objects are live host resources (a db handle, an HTTP client),
    so a child session cannot simply inherit the parent's set by
    default in every embedding — a per-user db handle belongs to the
    user whose turn opened it. The factory is asked once per child,
    and the default implementation shares the parent's mapping.

    ``kind`` names why the child exists (a fork, a review, a
    sub-task); an embedder that scopes resources by purpose keys on it.
    """

    def __call__(
        self, parent_session: str, child_session: str, kind: str
    ) -> "Mapping[str, Any]":
        """The host objects for ``child_session``'s executions."""
        ...
