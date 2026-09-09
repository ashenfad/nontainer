"""Workspace: the top-level API. One instance == one session's world.

Design notes (see README "Design decisions"):

- **Script model.** ``run_python`` is a fresh sandboxed execution per
  call; persistence lives in ``cache`` (data), ``helpers/`` (code, via
  VFS imports), and files (artifacts). No resident interpreter state.
- **Sync core.** termish and kvgit are synchronous (sandtrap is NOT
  the constraint — it has ``aexec()``); async harnesses wrap calls in
  ``asyncio.to_thread`` (the adapters do this). A workspace is
  single-writer and enforces it: mutating calls hold an internal
  ``RLock``, so a harness that threads parallel tool calls onto one
  session serializes safely (each call atomic + committed) instead
  of corrupting staged state. Read-only accessors don't take the
  lock. Open question for v1.x: an ``arun_python`` passing through to
  sandtrap ``aexec`` would let *agent code* use top-level ``await``
  (parallel host-object calls) — but it would still not be
  host-loop-safe end-to-end, since sandboxed file I/O hits sync kvgit
  under monkeyfs; async harnesses off-loop the call regardless.
- **Observations are bounded.** Tool results are truncated to
  ``max_observation`` characters with an explicit ``truncated`` flag —
  agents handle "output was cut" far better than silent loss or a
  blown context window.
- **cwd is stateful** across calls (like any other mutating terminal
  command) and persists in the provider's kv under the filesystem's
  own cwd key, so on versioned providers a rollback also restores
  *where you were*.
- **Execution is a seam.** How code runs — the python sandbox, the
  shell, worker lifecycle — lives behind :class:`Executor` (see
  protocol.py); the default :class:`LocalExecutor` is the in-process
  sandtrap + termish wiring, and :class:`~nontainer.runtime.Runtime`
  (``ws.runtime``) is the workspace-side half that owns it: the
  executor's lifecycle, the terminal command registry, the shell
  environment, the raw execution calls. The workspace keeps what
  execution must not own: the lock, the commit flow, cwd, the
  cache key rules.
"""

from __future__ import annotations

import posixpath
import re
import threading
import traceback
from collections.abc import Callable, Iterable, Mapping, MutableMapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from types import ModuleType
from typing import TYPE_CHECKING, Any, Literal

from .cache import Cache
from .errors import CommitNotFoundError, NotSupportedError, WorkspaceError
from .planes import CONVERSATION_PREFIX
from .protocol import (
    Capabilities,
    CommitInfo,
    MergeOutcome,
    TagInfo,
    WorkspaceDiff,
    WorkspaceProvider,
    WorkspaceStatus,
)
from .views import (
    VIEW_KEY,
    AttachFS,
    ViewFS,
    encode_view,
    normalize_view,
    parse_seed,
    parse_view,
)

if TYPE_CHECKING:
    from .agentgit import AgentGit
    from .editing import EditOutcome
    from .protocol import Executor
    from .runtime import Runtime
    from .store import Ref

Isolation = Literal["none", "process", "kernel"]


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


def _owns_cwd(provider_fs: Any) -> bool:
    """Whether the provider's filesystem owns the cwd key itself.

    True for monkeyfs ``VirtualFS`` (the kvgit backend): cwd is a key
    in the provider's kv, written on ``chdir``, so it commits, forks
    and rolls back with the files — and nontainer must not write it a
    second time. It must not write it at all there, in fact: with
    mounts the workspace's cwd is the composed ``MountFS``'s, and that
    composition REQUIRES the filesystem underneath to stay at the root
    (it hands that one paths it has already resolved), so a composed
    cwd stored under this key would break every relative path. Mounted
    trees are unversioned live views by contract, and on that backend
    their cwd is now equally transient: it starts at the workspace root
    each time the session is opened.

    False for the filesystems that hold cwd in memory (``IsolatedFS``
    on the dir backend, the AgentFS adapter). There the workspace
    writes the key itself — inert to the filesystem, read back when the
    session is reopened, which is the persistence those backends would
    otherwise have no way to get.
    """
    from monkeyfs import VirtualFS

    return isinstance(provider_fs, VirtualFS)


_LEGACY_CWD_KEY = "__cwd__"
"""Where nontainer used to keep its own copy of the cwd. Stores written
before the two keys were folded into one still carry it; a workspace
opened over such a store adopts the value and drops the key, and the
change rides the next commit."""


@dataclass(frozen=True)
class Mount:
    """A real directory exposed inside the workspace tree (a "volume").

    Mounts are a *workspace* concern, not a python-sandbox concern:
    both tools see them — ``terminal("ls /data")`` and sandboxed
    ``open("/data/x.csv")`` agree. Composed via monkeyfs ``MountFS``
    (+ ``IsolatedFS``, + ``ReadOnlyFS`` when ``readonly``).

    Mounted paths are live views of the real directory: they are NOT
    versioned and NOT captured by commits, so a rollback or checkout
    leaves them exactly as they are.

    A fork **inherits the mount point** and does NOT copy the data
    behind it: parent and fork observe the same live directory, and
    neither can roll it back.

    Write-enabled mounts therefore punch through the time-travel
    story — prefer ``readonly=True`` (the default) and have the agent
    copy inputs into the workspace when it needs to own them.
    """

    path: str | Path
    """Real directory on the host filesystem."""

    readonly: bool = True


@dataclass(frozen=True)
class ModuleGrant:
    """A whitelisted module plus its passthrough grants.

    Plain ``ModuleType`` entries in ``PythonConfig.modules`` are sugar
    for ``ModuleGrant(module)`` — no network, no host fs.
    """

    module: ModuleType

    network: bool = False
    """Callables in this module may perform socket operations
    (sandtrap's per-registration network grant). Grant to the HTTP
    client you registered, not to the world."""

    host_fs: bool = False
    """This module's own code sees the real filesystem while it runs
    (sandtrap ``host_fs_access``). For libraries that manage internal
    state on disk — download caches (``~/.cache/...``), temp files,
    lock files — which a workspace ``Mount`` can't address (the
    library's paths are absolute host paths that don't belong in the
    agent's tree). The grant is scoped to the module's calls: agent
    code still resolves against the workspace VFS, and the agent only
    reaches the real fs indirectly through this module's (policy-
    controlled) API. Distinct from ``Mount``, which deliberately
    shares host data *with* the agent."""

    include: str | Sequence[str] = "*"
    """Member whitelist patterns (sandtrap ``include``)."""

    exclude: str | Sequence[str] = ("_*", "*._*")
    """Member blacklist patterns (sandtrap ``exclude``). Replaces the
    default, so custom lists should usually re-include ``_*`` /
    ``*._*``."""

    recursive: bool = False
    """Register submodules recursively (sandtrap ``recursive``) — for
    big libraries agents already know (pandas, matplotlib)."""

    name: str | None = None
    """Registration name override. Needed for submodules reached as
    attributes (``ModuleGrant(os.path, name="os.path")``)."""


@dataclass(frozen=True)
class TerminalResult:
    """Outcome of one ``terminal()`` call (a full pipeline/script)."""

    stdout: str
    """Stdout of the final pipeline stage (termish semantics)."""

    exit_code: int
    stderr: str = ""
    truncated: bool = False

    commit: str | None = None
    """Id of the commit this call's autocommit created — pins the
    workspace state after the call (``ws.checkout(result.commit)``).
    ``None`` when nothing was committed: read-only call, autocommit
    off (turn mode), or an unversioned provider. HOST-facing, like
    ``PythonResult.namespace`` — adapters must not render it into the
    model's observation."""

    def __bool__(self) -> bool:
        return self.exit_code == 0


@dataclass(frozen=True)
class PythonResult:
    """Outcome of one ``run_python()`` call."""

    stdout: str
    stderr: str = ""
    """``sys.stderr`` writes from sandboxed code and libraries —
    warnings land here. Distinct from ``error``: stderr chatter does
    not imply failure."""

    error: str | None = None
    """Rendered traceback on failure, ``None`` on success. Sandboxed
    code that raises is a *result*, not a host exception — hosts only
    see exceptions for nontainer's own failures (bad config, provider
    errors)."""

    ticks: int = 0
    duration: float = 0.0
    truncated: bool = False

    namespace: Mapping[str, Any] = field(default_factory=dict)
    """Top-level bindings after execution (sandtrap's result namespace)
    — for the HOST, not the model. Modules and ``_``-prefixed names
    are excluded; under process/kernel isolation, unpicklable values
    are dropped in transit (sandtrap ``filter_namespace``). Adapters
    must NOT render this into the text observation — not the values,
    and not a list of the names either: the agent wrote those bindings,
    so naming them back is inventory rather than information.
    Structured payloads reach the
    embedder as plain variables by convention — e.g. an A2UI adapter
    reads ``result.namespace.get("ui")`` — no bespoke emission channel,
    no schema imposed by core."""

    commit: str | None = None
    """Id of the commit this call's autocommit created (``None``
    when nothing was committed) — see ``TerminalResult.commit``."""

    ui_problems: tuple[str, ...] = ()
    """Why a ``ui`` value did not render as intended — today the 8 MB
    artifact cap, with the remediation. Actionable text meant to reach
    the agent: it reads this in the tool result and self-corrects, and
    the human sees it where the figure would have been. Carried on the
    result because materialization happens in ``run_python`` now, so an
    adapter rendering afterwards has no other way to learn of it."""

    def __bool__(self) -> bool:
        return self.error is None


@dataclass(frozen=True)
class WriteOutcome:
    """Outcome of ``files.write`` / ``files.put``."""

    path: str
    """Workspace path written."""

    size: int
    """Bytes written."""

    created: bool
    """True for a new file, False for an overwrite."""

    commit: str | None = None
    """Commit created by this call's autocommit (``None`` when
    nothing was committed) — see ``TerminalResult.commit``."""

    def __str__(self) -> str:  # f"wrote {outcome}" reads as the path
        return self.path


@dataclass(frozen=True)
class PythonConfig:
    """What sandboxed code may touch. Frozen at workspace construction.

    Thin sugar over a sandtrap ``Policy``; pass ``policy=`` to bypass
    the sugar entirely.
    """

    modules: Sequence[
        ModuleType | ModuleGrant | Sequence[ModuleType | ModuleGrant]
    ] = ()
    """Whitelisted importable modules (``import pandas`` works iff
    pandas is listed — or covered by ``stdlib``). Bare modules get no
    passthroughs; wrap in :class:`ModuleGrant` to grant network /
    host-fs / member patterns per module. Nested sequences flatten one
    level, so preset grant lists splice in directly::

        PythonConfig(modules=[dataframes(), plotting(), my_module])

    Entries registered after the stdlib set — an explicit grant for a
    stdlib module overrides its stdlib-set registration. Note:
    monkeyfs's safe-path passthrough is always on — stdlib and
    site-packages stay readable so registered libraries can load their
    own resources."""

    stdlib: bool = True
    """Grant the curated safe-stdlib set (math, json, csv, datetime,
    re, os-over-VFS, pathlib, gzip/zipfile/tarfile, ...) — see
    ``nontainer.presets.STDLIB``. A plain computer's python can do
    arithmetic and read files; disable for a truly bare cell (minimal
    surface, policy audits)."""

    host_objects: Mapping[str, Any] = field(default_factory=dict)
    """Live host resources injected into the namespace by name — the
    in-process superpower (your model, your db pool). Distinct from
    ``run_python(inputs=...)`` on purpose: inputs are per-call
    *picklable data* (they cross isolation boundaries by value);
    host_objects are session-lifetime *live objects* that get
    attribute-level policy at construction and RPC-proxy bridging
    under process/kernel isolation (or a loud construction-time error
    if unbridgeable). Merging the two would make `isolation="none"` →
    `"process"` a silent breaking change; keeping them apart makes the
    contract checkable at the right moment."""

    network: bool = False
    """Global network toggle for sandboxed code itself (sandtrap
    ``allow_network``). Coarse; prefer per-module ``ModuleGrant``
    grants. Note the kernel-isolation interaction below."""

    isolation: Isolation = "none"
    """Escalation ladder, with one loud caveat inherited from
    sandtrap: kernel restrictions (seccomp / Landlock / Seatbelt) are
    applied once at worker start and are strictly monotonic. If ANY
    grant enables network or host-fs — ``network=True`` here or on any
    ``ModuleGrant`` — the corresponding kernel restriction is OFF for
    the entire worker; only Python-level gating remains for everything
    else. nontainer emits a ``RuntimeWarning`` when building a
    ``"kernel"`` sandbox whose policy degrades a kernel restriction,
    so the weakening is visible at construction, not discovered in an
    audit."""

    timeout: float = 30.0
    # The same sandbox commit enforces timeout, cancel, and ticks,
    # so `timeout` is the real runaway guard; the tick limit is a
    # determinism backstop and must be sized to never fire on honest
    # work — a legitimate cleaning loop over a few-hundred-k-row CSV
    # is tens of millions of ticks, not a runaway.
    tick_limit: int = 50_000_000
    memory_limit_mb: int | None = None

    echo: Literal["none", "last", "all"] = "last"
    """Notebook-style display of bare top-level expressions in
    ``run_python`` (sandtrap's ``sys.displayhook`` semantics: repr
    rendering, ``None`` suppressed, ``"last"`` = Jupyter's last-expr).
    Agents carry the notebook prior — a trailing ``df.head()`` that
    prints nothing costs a wasted retry-with-print. Script surfaces
    (the terminal ``python`` builtin, app handlers) always run
    ``echo="none"`` regardless: their stdout feeds pipelines and
    api.log, not a conversation."""

    warm_view_workers: int = 1
    """How many ``exec_python(view=...)`` workers to keep **warm**, per
    distinct view, under ``isolation="process"``/``"kernel"`` only.

    A cache size, not a limit. It does **not** cap how many workers can
    exist at once — nothing here does; see the peak/resident note
    below. It was called ``view_workers`` in 0.3.0, which read as a
    bound and misled accordingly.

    Only view calls are cached. ``run_python`` and a plain
    ``exec_python`` run in the session sandbox, whose worker is created
    once at construction and held for the workspace's life — already
    warm, and untouched by this setting. The view surface is apps'
    handler dispatch: the live preview, ``test_app``, and published-app
    requests.

    **This is a latency optimization, not a safety mechanism.** It was
    once both: a view sandbox was minted per call, so serving an app
    meant one ``fork()`` per request from a live ASGI server, and a fork
    from a multi-threaded process can inherit a lock held by a thread
    the child doesn't have and hang. sandtrap >= 0.3 creates workers
    from a forkserver broker instead, which removes that hazard at its
    source. What pooling buys now is the worker start — and forkserver
    made that *more* expensive, not less, because a worker re-imports
    the granted stack rather than inheriting it copy-on-write.

    So size it against latency, and know what a resident worker holds.
    With a heavyweight policy (pandas, numpy, plotly) a worker is
    ~235ms to start and ~113MB resident; with a stdlib policy, ~18ms
    and ~23MB. The default of **1** keeps the app-iteration loop warm —
    edit, ``test_app``, preview, repeat is essentially sequential —
    while holding a single worker.

    Raise it for **concurrent** serving: a preview page issuing parallel
    API calls, or a published app with real traffic. Past the cap,
    concurrency falls back to a per-call sandbox rather than queueing,
    so the failure mode of too-low is latency, not errors.

    Too-high is memory, because **residency only rises**. A burst of N
    concurrent calls leaves ``min(N, warm_view_workers)`` workers resident
    for the executor's life — the ones past the cap are transient and
    reaped when their call ends, but those within it are kept. The cap
    is therefore a floor you fill and keep paying for, per distinct
    view, per workspace; it is not a ceiling you retreat from. Nothing
    reaps an idle worker today (see the idle-TTL item in
    ``docs/design.md``).

    ``0`` gives every call a pristine worker. That is the only setting
    with clean process-state semantics: any pool >0 means ``sys.modules``,
    module globals, and anything a handler mutated through a granted
    module outlive the request that did it, shared between handlers of
    one app. The blast radius is one workspace."""

    preload_grants: bool = False
    """Import granted modules once into sandtrap's forkserver broker, so
    every worker inherits them copy-on-write instead of importing its
    own copy. ``isolation="process"``/``"kernel"`` only.

    This is the big lever on worker cost, and it moves both numbers at
    once. With ``modules=[dataframes(), plotting()]`` a worker costs
    ~235ms and ~113MB by default; preloaded, ~14ms and ~29MB — the
    stack is paid for once in the broker rather than per worker. It
    applies to **every** worker, including the session worker each
    workspace holds for its life, so in a host with many open
    workspaces it moves more memory than ``warm_view_workers`` does.

    Off by default because preloading runs your grants' **import-time
    code in the broker**. A module that starts a background thread on
    import leaves the broker multi-threaded, and a worker forked from
    it can inherit a lock held by that thread — the exact hang the
    forkserver default exists to prevent. Your grants are yours: turn
    this on when you know they start no threads on import.

    **pyarrow, specifically.** ``dataframes()`` grants pyarrow, and
    pandas 3 imports it regardless — so preloading puts arrow's
    allocator in the broker. Arrow's default mimalloc pool keeps
    per-thread heaps that historically don't survive fork. The preset
    already pins ``ARROW_DEFAULT_MEMORY_POOL=system`` before pandas
    can import pyarrow, and the broker inherits that environment, so
    the preset path is covered — see
    ``test_dataframes_preset_pins_a_fork_safe_arrow_allocator``, which
    exists to stop that pin being deleted as obsolete. If you grant
    pandas or pyarrow **without** the preset and enable this, set that
    variable yourself, before the first pandas import anywhere in your
    process.

    **It is process-wide, not per-workspace.** multiprocessing reads
    the preload list once, when the broker starts, so only the first
    workspace to start a worker in your process decides. Later
    workspaces asking for a preload the running broker lacks still
    work — their modules are imported per worker — and sandtrap emits
    a ``RuntimeWarning`` saying so. Set it uniformly across the
    workspaces you build, or accept that the first one wins."""

    policy: Any | None = None
    """A pre-built ``sandtrap.Policy``; overrides everything above
    except ``host_objects``."""


_HOST_PREFIX_RE = re.compile(r'(File ")/[^"]*/(?:site-packages|python\d+\.\d+)/')
_FRAME_RE = re.compile(r'\s+File "([^"]*)"')


def _refuse_agent_tool(info: Mapping[str, Any] | None) -> None:
    """Refuse a framework commit that would pass for one of the agent's.

    ``info["tool"]`` is the one thing that says whose commit this is
    (``agentgit.is_agent_commit``), and the host's durability verb is
    not the agent's. A host commit labelled ``ws-git`` would appear in
    ``ws-git log`` as something the agent wrote, and one labelled
    ``ws-git.<anything>`` would be read as the fiction's own
    bookkeeping and skipped. Both are forgeries, so the name is
    reserved rather than merely discouraged.
    """
    from .agentgit import TOOL

    tool = (info or {}).get("tool")
    if not isinstance(tool, str):
        return
    if tool == TOOL or tool.startswith(TOOL + "."):
        raise ValueError(
            f"info['tool'] = {tool!r} is reserved: {TOOL!r} and {TOOL + '.*'!r} "
            "name the agent's own commits and its bookkeeping, and a commit "
            "made here is the host's. The agent's commit verb is "
            "ws.index.commit(message)."
        )


def _render_error(exc: BaseException) -> str:
    """The full traceback, not just the message — line numbers are what
    an agent's repair loop aims at.

    Under process isolation the traceback object doesn't survive the
    pickle home, so sandtrap's worker renders it in situ and attaches
    the text (``_st_traceback_text``, sandtrap >= 0.2.10); prefer that,
    fall back to formatting whatever frames we hold (in-process runs,
    older sandtraps, host-made errors like StTimeout)."""
    text = getattr(exc, "_st_traceback_text", None)
    if not isinstance(text, str) or not text:
        text = "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        ).rstrip()
    return _trim_rendered_traceback(text)


def _machinery_dirs() -> tuple[str, ...]:
    """Package dirs whose frames are sandbox plumbing, not signal."""
    import monkeyfs
    import sandtrap

    return tuple(
        str(Path(m.__file__).parent) for m in (sandtrap, monkeyfs) if m.__file__
    )


def _trim_rendered_traceback(text: str) -> str:
    """De-noise a rendered traceback for agent-visible surfaces.

    Sandtrap/monkeyfs machinery frames go entirely — a gate raising
    through ``__st_import__`` is OUR plumbing, not the agent's bug
    (``strip_internal_frames`` can only strip LEADING frames; text is
    where trailing ones can go). Host install prefixes carry zero
    signal and leak paths, so surviving library frames read
    ``pandas/core/generic.py``, not the absolute venv path. And
    pathological depth gets middle-elided — the entry frames and the
    raise site are the ends worth keeping."""
    machinery = _machinery_dirs()
    lines: list[str] = []
    dropping = False
    for line in text.splitlines():
        m = _FRAME_RE.match(line)
        if m:
            dropping = m.group(1).startswith(machinery)
        elif not line.startswith(("    ", "\t")):
            dropping = False  # left column: header / exception line
        if dropping:
            continue
        lines.append(_HOST_PREFIX_RE.sub(r"\1", line))
    if len(lines) > 60:
        elided = len(lines) - 48
        lines = lines[:8] + [f"[... {elided} traceback lines elided ...]"] + lines[-40:]
    return "\n".join(lines)


def _state_identity(provider: Any) -> "Callable[[], str | None] | None":
    """``ExecutionContext.head``: names the commit the fs currently
    equals — the provider head, guarded to None while staging is dirty
    (a dirty view names no committed state, and a reusable-substrate
    executor must never tag a tree with a state it doesn't hold).

    Fully lazy and shape-agnostic: ``head``/``dirty`` may be properties
    that RAISE on unversioned providers (DirProvider), and ``head`` is a
    property on KvgitProvider but may be a method elsewhere — every
    access happens inside the closure, any failure means "no identity".
    """
    if not hasattr(type(provider), "head"):
        return None

    def _current() -> str | None:
        try:
            if provider.dirty:
                return None
            head = provider.head
            return head() if callable(head) else head
        except Exception:
            return None

    return _current


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


@dataclass(frozen=True)
class _Settings:
    """The construction arguments a fork replays onto its own provider —
    the ones that cannot change after ``__init__``.

    ``fork()`` used to re-list these by hand, which is how ``mounts``
    came to be silently dropped: it was added after the list was
    written, so a forked (or published) workspace lost every mount.
    Capturing them once means the next argument cannot fall out the
    same way — ``tests/test_mounts_and_cache.py`` asserts every
    pass-through parameter of ``Workspace.__init__`` lands here.

    Two constructor arguments are deliberately absent:

    - ``provider`` — the fork gets its own; that IS the fork.
    - ``executor`` — a ready instance is bound to one session and
      cannot be shared (see the executor block in ``__init__``); forks
      fall back to ``executor_factory``.

    Two more are absent because they are MUTABLE after construction, so
    a value captured here would go stale. ``fork()`` replays both from
    the live attribute instead:

    - ``commands`` — ``register_command`` mutates the built set
      (``enable_apps`` injects ``ws-curl``/``curl`` that way).
    - ``autocommit`` — has a public setter, and the documented
      turn-granularity path uses it (``WorkspaceTools(ws,
      commit="turn")`` sets ``ws.autocommit = False``). Replaying
      the construction-time value would silently put a forked session
      back on per-call commits.

    That distinction is load-bearing, so a test asserts no field here
    has a setter on ``Workspace``.

    ``mounts`` is stored NORMALIZED (points validated, sources resolved
    to absolute paths), not as the caller passed it. Re-resolving at
    fork time would let a relative source or a retargeted symlink give
    the fork a different directory than the parent — breaking the
    live-view contract in :class:`Mount`, which promises both observe
    the same one.
    """

    python: "PythonConfig"
    mounts: Mapping[str, Mount]
    cache: bool
    max_observation: int
    executor_factory: "Callable[[], Executor] | None"
    root: str

    def as_kwargs(self) -> dict[str, Any]:
        """Shallow field mapping for ``Workspace(**...)``. Deliberately
        not ``dataclasses.asdict``, which recurses — it would flatten
        ``PythonConfig`` and each ``Mount`` into plain dicts."""
        return dict(vars(self))


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

    def read(self, path: str) -> bytes:
        """The file's bytes. Raises for a missing path — use
        :meth:`read_artifact` where absence is an answer rather than an
        error."""
        self._ws._check_open()
        return self._ws._fs.read(path)

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
        from .editing import EditError, apply_edit

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

        ``ref`` is ``session@commit`` (see :class:`~nontainer.Ref`) or
        a session name, which means that session's current commit.
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
        return {at: str(snap.ref) for at, snap in self._ws._attached.items()}

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
        from .agentgit import AgentGit

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
        """
        ws = self._ws
        with ws._lock:
            ws._check_open()
            return self._git.checkout(commit)


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


class Workspace:
    """A fake little computer: files + shell + python + cache, versioned.

    Construct via :func:`workspace` (typical) or directly from any
    :class:`WorkspaceProvider` (embedding, tests, custom substrates).
    Context manager: ``with workspace(...) as ws: ...`` closes on exit.
    """

    def __init__(
        self,
        provider: WorkspaceProvider,
        *,
        python: PythonConfig | None = None,
        mounts: Mapping[str, Mount] | None = None,
        commands: Mapping[str, Callable[..., Any]] | None = None,
        cache: bool = True,
        autocommit: bool = True,
        max_observation: int = 32_000,
        executor: "Executor | None" = None,
        executor_factory: "Callable[[], Executor] | None" = None,
        root: str = "/workspace",
    ) -> None:
        self._provider = provider
        # Which Store opened this workspace, when one did. Stamped by
        # Store.open and carried across fork/at_tag, so a store-level
        # verb handed a workspace can tell whether it is one of its
        # own — tagging through a workspace from a different store
        # would write to that store and leave this one empty. None for
        # a workspace built straight from a provider: no store claims
        # it, and none may act on its behalf.
        self._store: Any = None
        python_config = python or PythonConfig()
        self._cache_enabled = cache
        self._max_observation = max_observation
        self._closed = False
        # The workspace root: where agent-visible files live in the VFS
        # — one absolute path contract shared by every executor (the
        # local sandbox resolves imports from it; a VM guest mounts its
        # workspace AT it, making agent absolute paths identical on
        # both). "/" selects the flat pre-0.2 layout (no VM-rung path
        # parity — a guest can't mount at the fs root).
        if not root.startswith("/"):
            raise ValueError(f"root must be an absolute path, got {root!r}")
        # Normalize by segment. Anything a guest kernel would collapse
        # (trailing, doubled, or leading-only slashes) has to collapse
        # here too, or the executors silently disagree about the root:
        # "//" left as-is rstrips to "", which reads falsy downstream —
        # the local side then composes "/skills" (flat layout) while a
        # guest falls back to dud's own /workspace default. That split
        # is the exact bug this root exists to prevent.
        parts = [p for p in root.split("/") if p]
        if any(p in (".", "..") for p in parts):
            # Rejected rather than resolved: a guest would normalize
            # these and the VFS wouldn't, reopening the same split.
            raise ValueError(f"root must not contain . or .. segments, got {root!r}")
        self._root = "/" + "/".join(parts) if parts else "/"

        # Single-writer enforcement: mutating public methods hold this
        # lock, so concurrent calls from a threading harness serialize
        # (each atomic + committed) instead of interleaving writes
        # into the provider's staged buffer. Invariants: the lock is
        # taken ONLY in mutating public method bodies — never in
        # exec_python / build_sandbox / _maybe_commit (the
        # extension paths the apps extra drives; extensions take
        # ws.lock themselves when their work mutates) — and read-only
        # accessors don't take it. RLock, not Lock: agent code can
        # call injected
        # host_objects, and a host object that calls back into this
        # workspace's public API must serialize, not deadlock.
        self._lock = threading.RLock()

        # A frozen provider is a snapshot at a tag (provider.at_tag):
        # reads work, nothing commits. A workspace over one refuses its
        # mutating surface up front and hands the executor a read-only
        # filesystem. Read through getattr: `frozen` is a kvgit-side
        # property, and a third-party provider without it is not frozen.
        self._frozen = bool(getattr(provider, "frozen", False))
        self._frozen_at = getattr(provider, "frozen_at", None)
        # What the executor's cache and the host-side ``cache`` build
        # on: the provider's kv, or a refusing view of it when frozen.
        self._kv_view: MutableMapping[str, Any] = (
            _FrozenKV(provider.kv, self._frozen_at) if self._frozen else provider.kv
        )

        # autocommit is meaningless (and forced off) when the
        # provider can't commit.
        # Set while an operation writes on a call's behalf, so nested
        # files.write commits fold into that call's single commit.
        self._defer_commits = False
        self._autocommit = autocommit and provider.caps.versioned and not self._frozen
        # Construction owns the workspace root + initial cwd, but it
        # must never absorb caller-owned staged work into an automatic
        # init baseline. Embedders may deliberately seed provider.fs /
        # provider.kv before wrapping it, so remember rather than reject
        # that state: initialization joins their staged view and remains
        # explicitly theirs to commit or discard.
        was_dirty = provider.caps.staging and provider.dirty

        # -- filesystem: provider fs, optionally wrapped with mounts.
        # Normalized once, here: the resolved mapping is what a fork
        # replays, so parent and fork can't resolve to different
        # directories (see _Settings).
        normalized_mounts = self._normalize_mounts(mounts)
        # The view this session was seeded with, if it was narrowed: a
        # sparse checkout recorded on the branch (see
        # ``nontainer/views.py``), so a delegate reopened later is
        # narrowed exactly as it was. It wraps the PROVIDER's
        # filesystem — which always holds the whole tree, which is what
        # keeps the merge back an ordinary three-way — and sits under
        # the mounts, which are unversioned live views and were never
        # the delegate's to be given.
        self._view = parse_view(provider.kv.get(VIEW_KEY))
        viewed = provider.fs
        self._view_fs: ViewFS | None = None
        if self._view is not None:
            self._view_fs = ViewFS(provider.fs, self._view, extend=self._record_view)
            viewed = self._view_fs
        # The attachment layer is always in the chain: the executor is
        # handed ONE filesystem object when it opens, so a tree
        # attached later has to reach it through that same object.
        self._attached: dict[str, Workspace] = {}
        self._fs = AttachFS(self._build_fs(viewed, normalized_mounts), self._root)
        # A ws-git verb's mid-call harvest can fail after the guest
        # baseline advanced (provider refused part of the harvest):
        # the handler stashes the message here and terminal() unwinds
        # the partial staging like a torn call instead of
        # committing it. Transient per-call state, never replayed.
        self._pending_sync_error: str | None = None
        # Host-side writes move provider state behind a remote
        # executor's back; the public ``fs`` hands out a wrapper that
        # flags the runtime, and the next execution syncs (see
        # _SyncingFS).
        self._public_fs = _SyncingFS(self._fs, self._mark_executor_stale)

        # -- execution: bound behind the Executor seam (protocol.py),
        # and owned by the Runtime this workspace builds below.
        #
        # Two injection shapes, because an executor is stateful and
        # bound to ONE session (it may own a subprocess / guest VM), so
        # a single instance can't be shared across forks:
        # - ``executor`` — a ready instance for THIS workspace only;
        #   forks fall back to the factory (or the default).
        # - ``executor_factory`` — a zero-arg builder used for this
        #   workspace when no instance is given, AND carried into
        #   ``fork()`` so a whole session lineage runs on the same
        #   executor kind (what studio's "fork = new universe" needs on
        #   a dud backend). A fresh executor per session, no sharing.
        # Resolved here rather than in the Runtime because the factory
        # is what a fork replays, and _Settings is the workspace's.
        self._executor_factory = executor_factory
        if executor is None and executor_factory is not None:
            executor = executor_factory()

        # -- what a fork replays. Captured from the NORMALIZED values,
        # not the raw arguments, so a fork starts from the state this
        # workspace resolved to: ``root`` collapsed by segment, mount
        # points validated and their sources resolved. Mutable-after-
        # construction settings are deliberately NOT here (see
        # :class:`_Settings`); fork() reads those live.
        self._settings = _Settings(
            python=python_config,
            mounts=normalized_mounts,
            cache=self._cache_enabled,
            max_observation=self._max_observation,
            executor_factory=self._executor_factory,
            root=self._root,
        )

        # -- versioned initialization baseline: root + cwd belong to
        # workspace state, not to the first tool call. They must exist
        # before the executor opens (a remote executor materializes its
        # guest tree from them), and a fresh versioned workspace commits
        # them under {"tool": "init"} regardless of autocommit mode.
        # Reopening is read-only: the guards leave the provider clean,
        # so no second init commit is made.
        initialized = False
        if self._root != "/" and not self._fs.isdir(self._root):
            self._fs.makedirs(self._root, exist_ok=True)
            initialized = True

        # -- stateful cwd: where the filesystem left it, or the
        # workspace root on a fresh session. Guarded so a no-op chdir
        # doesn't dirty staging providers (which would turn read-only
        # tool calls into commits).
        self._owns_cwd = _owns_cwd(provider.fs)
        # Unconditionally, before anything reads a cwd: the legacy key
        # is dead state and every session that opens takes it out,
        # mounted or not, whether or not its value is still wanted.
        # Leaving it on a branch that has no use for it is what would
        # keep it around to be merged.
        legacy_cwd = self._legacy_cwd()
        if normalized_mounts and self._owns_cwd:
            # The composition resolves paths before handing them down,
            # so the filesystem underneath has to sit at the root. Any
            # cwd under the key belongs to un-mounted sessions of this
            # store; honoring it here would break every relative path.
            try:
                if provider.fs.getcwd() != "/":
                    provider.fs.chdir("/")
            except Exception:  # noqa: BLE001 - a root that won't chdir is not ours to fix
                pass
            stored_cwd = self._root
        else:
            stored = provider.kv.get(_cwd_key())
            if stored == "/" and self._root != "/":
                # The filesystem root is not somewhere a session ever
                # was: it is what a composition left behind. Mounts and
                # attachments both park the filesystem underneath there
                # (they hand it paths already resolved) and monkeyfs
                # persists that park like any chdir, so a session that
                # had a tree attached when it was last committed would
                # otherwise reopen at "/" instead of its own root.
                stored = None
            stored_cwd = stored or legacy_cwd or self._root
        if stored_cwd != "/":
            try:
                if self._fs.getcwd() != stored_cwd:
                    self._fs.chdir(stored_cwd)
                    initialized = True
            except Exception:
                pass  # path may no longer exist; start at the fs root
        initialized = self._save_cwd() or initialized
        # A frozen workspace commits nothing, init baseline included: the
        # tagged commit already holds a root and a cwd, and a
        # snapshot that wrote a commit of its own would not be one.
        if provider.caps.versioned and initialized and not was_dirty:
            if not self._frozen:
                provider.commit(info={"tool": "init"})

        # The namespaces: plain views onto this workspace (they hold
        # it and no state), built before the runtime so every path
        # below can reach ws.files.
        self._files = WorkspaceFiles(self)
        self._index = WorkspaceIndex(self)
        self._tags_ns = WorkspaceTags(self)

        # The runtime LAST: building it opens the executor, which may
        # fork a persistent isolation worker (see LocalExecutor.open),
        # and doing it after everything else means no later __init__
        # failure can orphan one (PR #10 review). If the open itself
        # fails, the valid init baseline stays committed: root/cwd are
        # provider state, independent of executor health.
        from .runtime import Runtime

        self._runtime = Runtime(
            self,
            executor=executor,
            python=python_config,
            commands=commands,
            max_observation=max_observation,
        )

    # ------------------------------------------------------------------
    # construction helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_mounts(mounts: Mapping[str, Mount] | None) -> dict[str, Mount]:
        """Validate the mount points and resolve each source to an
        absolute path, ONCE. The resolved mapping is what ``fork()``
        replays: re-resolving there would let a relative source (or a
        symlink retargeted in between) hand the fork a different
        directory than the parent holds, which is exactly what the
        live-view contract in :class:`Mount` promises cannot happen."""
        out: dict[str, Mount] = {}
        for point, mount in (mounts or {}).items():
            if point == "/" or not point.startswith("/"):
                raise ValueError(
                    f"Mount points must be absolute and not '/': {point!r}"
                )
            real = Path(mount.path).expanduser().resolve()
            if not real.is_dir():
                raise ValueError(f"Mount source is not a directory: {real}")
            out[point] = Mount(real, readonly=mount.readonly)
        return out

    @staticmethod
    def _build_fs(base: Any, mounts: Mapping[str, Mount]) -> Any:
        """Compose the mounted views over ``base``. Takes mounts already
        through :meth:`_normalize_mounts` — paths here are absolute and
        checked."""
        if not mounts:
            return base
        from monkeyfs import IsolatedFS, MountFS, ReadOnlyFS

        mounted: dict[str, Any] = {}
        for point, mount in mounts.items():
            sub: Any = IsolatedFS(str(mount.path))
            if mount.readonly:
                sub = ReadOnlyFS(sub)
            mounted[point] = sub
        return MountFS(base, mounted)

    # ------------------------------------------------------------------
    # identity
    # ------------------------------------------------------------------

    @property
    def session(self) -> str:
        return self._provider.session

    @property
    def root(self) -> str:
        """The workspace root: the absolute VFS path agent-visible
        files live under (default ``/workspace``) — one path contract
        across executors. Extensions derive their trees from it
        (``<root>/app``, ``<root>/skills``); ``"/"`` is the flat
        legacy layout."""
        return self._root

    @property
    def caps(self) -> Capabilities:
        """What the provider under this session can do — versioning,
        staging, cheap forks, merge, tags, the index. Execution
        capabilities are the runtime's (``ws.runtime.supports_commands``,
        ``ws.runtime.supports_ws_verbs``, ``ws.runtime.cache_enabled``):
        they belong to the executor, not the substrate."""
        return self._provider.caps

    @property
    def files(self) -> WorkspaceFiles:
        """The file surface — see :class:`WorkspaceFiles`."""
        return self._files

    @property
    def index(self) -> WorkspaceIndex:
        """The agent's own git — see :class:`WorkspaceIndex`."""
        return self._index

    @property
    def tags(self) -> WorkspaceTags:
        """This session's tags — see :class:`WorkspaceTags`.
        Store-scoped names live on ``store.tags``."""
        return self._tags_ns

    @property
    def autocommit(self) -> bool:
        """Whether each successful mutating tool call commits. Settable:
        flip to False for turn-granularity commit policies (the agex
        model — one commit per agent turn), where the embedder or an
        adapter hook calls :meth:`commit` at turn boundaries.
        Tradeoff: kvgit's staged buffer is in-memory, so deferring
        commits means a crash can lose the current turn's work."""
        return self._autocommit

    @autocommit.setter
    def autocommit(self, value: bool) -> None:
        self._autocommit = (
            bool(value) and self._provider.caps.versioned and not self._frozen
        )

    @property
    def head(self) -> str | None:
        """Id of the current (latest) commit — pins the state a
        read-only call observed, since reads never move it. ``None``
        for unversioned providers. Caveat: staged-but-uncommitted
        changes (turn mode, manual ``ws.files.fs`` writes) are NOT in the
        head — check :attr:`dirty`; the pin is exact iff clean."""
        if not self._provider.caps.versioned:
            return None
        return self._provider.head

    @property
    def ref(self) -> "Ref":
        """This session at its current commit, as a :class:`~nontainer.Ref`.

        A commit id alone says what the state is but not where it came
        from; a session id alone names a moving head. The ref is the
        pair, and it is what a snapshot, a publication or a
        cross-session read quotes. Exact iff clean — staged changes are
        in no commit, so check :attr:`dirty` first."""
        from .store import Ref

        head = self.head
        if head is None:
            raise NotSupportedError(
                f"Session {self.session!r} has no ref: its provider is not "
                "versioned, so there is no commit to name."
            )
        return Ref(session=self.session, commit=head)

    @property
    def uncommitted(self) -> bool:
        """The store's buffer holds writes no commit has taken yet
        (always False without ``caps.staging``).

        The FRAMEWORK's question, and only it: whether ``ws.commit()``
        would land anything. It says nothing about what the agent has
        in flight — autocommit keeps this False while an agent
        composes, because every tool call commits. "Does the agent
        have uncommitted work" is ``ws.index.status()``, which
        measures against the agent's own last commit.
        """
        return self._provider.dirty

    @property
    def frozen(self) -> bool:
        """This workspace is a snapshot at a tag (see ``ws.tags.at``).

        Reads work; nothing can be written or committed.
        ``autocommit`` is forced off, the write tools refuse, and
        the executor sees a read-only filesystem — so a shell redirect
        or ``open(..., "w")`` from agent code fails where it happens
        instead of staging a change that could never land."""
        return self._frozen

    @property
    def runtime(self) -> "Runtime":
        """How code runs against this workspace: the bound executor,
        the terminal command registry, the shell environment, the raw
        execution calls. See :class:`~nontainer.runtime.Runtime`."""
        return self._runtime

    @property
    def lock(self) -> threading.RLock:
        """EXTENSION SURFACE: the workspace's single-writer lock.
        Mutating public methods hold it; hold it yourself for
        host-side or extension work that mutates the workspace
        (``ws.files.fs`` writes, ``ws.cache`` mutation, multi-step
        read-modify-write) and must serialize with tool calls. It is
        an ``RLock``, so taking it around a block that calls locked
        public methods is safe."""
        return self._lock

    # ------------------------------------------------------------------
    # the two tools
    # ------------------------------------------------------------------

    def terminal(self, command: str) -> TerminalResult:
        """Execute a shell script (pipes, redirects, ``;``) against the
        workspace filesystem. Never raises for command failure — check
        ``exit_code`` / truthiness."""
        with self._lock:
            # Inside the lock: close() also holds it, so a call that
            # wins the lock either sees the workspace open for its
            # whole execution or raises cleanly — no TOCTOU (PR #7).
            self._check_open()
            was_dirty = self._provider.dirty
            try:
                result = self._runtime.exec_shell(command)
            except PermissionError as e:
                return TerminalResult(
                    stdout="", exit_code=1, stderr=self._refused_frozen(e)
                )
            torn = self._absorb_or_unwind(was_dirty)
            pending = self._pending_sync_error
            self._pending_sync_error = None
            if pending is not None:
                # A ws-git verb's pre-verb harvest failed after the
                # guest baseline advanced, so staging may hold partial
                # writes the post-call harvest can no longer see. Same
                # contract as a torn call: entry-clean staging holds
                # only this call's effects, so discard restores exact
                # pre-call state; entry-dirty staging holds earlier
                # work that is not ours to drop — leave it, and say so.
                if not was_dirty:
                    try:
                        self._provider.discard()
                        pending += "; this call's staged changes were rolled back"
                    except Exception:
                        pass
                else:
                    pending += (
                        "; WARNING: this call's staged changes may remain "
                        "and would ride the next commit"
                    )
                torn = pending if torn is None else f"{pending}\n{torn}"
            self._save_cwd()
            if torn is not None:
                stderr = f"{result.stderr}\n{torn}" if result.stderr else torn
                return replace(result, exit_code=result.exit_code or 1, stderr=stderr)
            cp = self._maybe_commit("terminal")
        return replace(result, commit=cp) if cp else result

    def run_python(
        self, code: str, *, inputs: Mapping[str, Any] | None = None
    ) -> PythonResult:
        """Execute Python in the sandbox against the workspace.

        Namespace in, namespace out: ``inputs`` are bound as top-level
        names for this call and must be picklable data (the per-call
        counterpart to construction-time ``host_objects``, which are
        live resources — see ``PythonConfig``); ``result.namespace``
        carries the bindings left behind. Also in scope: whitelisted
        ``modules``, ``cache`` (the *versioned* persistent dict —
        unlike the namespace, cache contents are captured by
        commits), stdlib ``open()`` etc. routed to the workspace
        fs, and imports from ``helpers/`` on the fs. Never raises for
        sandboxed-code failure — check ``error`` / truthiness.
        """
        with self._lock:
            self._check_open()
            was_dirty = self._provider.dirty
            try:
                result = self._runtime.exec_python(code, inputs=inputs)
            except PermissionError as e:
                return PythonResult(stdout="", error=self._refused_frozen(e))
            torn = self._absorb_or_unwind(was_dirty)
            self._save_cwd()
            if torn is not None:
                error = f"{result.error}\n\n{torn}" if result.error else torn
                return replace(result, error=error)
            result = self._materialize_ui(result)
            cp = self._maybe_commit("run_python")
        return replace(result, commit=cp) if cp else result

    def _materialize_ui(self, result: PythonResult) -> PythonResult:
        """Turn live ``ui`` values into files, and the bindings into
        :class:`~nontainer.artifacts.ArtifactPath`.

        Here rather than in an adapter because it is not a presentation
        choice: on a VM rung the object cannot leave the guest, so it
        is *already* serialized during execution. Leaving the
        in-process case to whoever happened to be rendering meant a
        chart became a file on one executor and stayed a live object on
        the other — and on the second, only if you used one particular
        adapter. Same code, different outcome, for no reason a caller
        could see.

        Runs before the commit so the artifacts belong to the call
        that produced them, and after the unwind check so a torn call
        writes nothing.

        Idempotent by construction: an ``ArtifactPath`` is a path
        string, and the renderer's reference tier resolves an existing
        workspace path to itself. So a value the guest already
        materialized passes through unchanged rather than being
        written twice — which is also what makes a *re*-run of the same
        code re-claim its artifact instead of going unnoticed.
        """
        ui = result.namespace.get("ui")
        if not isinstance(ui, dict) or not ui:
            return result
        # A guest that materialized during execution left CLAIMS, since
        # it cannot know this namespace: the paths coincide on a VM rung
        # only because the workspace is mounted at the host root, and
        # diverge on a subprocess rung. Resolved here rather than in the
        # executor because only here is the harvest already absorbed,
        # so a claim can be checked against a filesystem that has the
        # file — `ui` is agent-authored, and an ordinary dict wearing
        # the tag must not become an ArtifactPath on one rung only.
        from .dud_outputs import _PROBLEM

        claimed, claim_problems = {}, []
        for key, value in ui.items():
            path = self._claimed(value)
            if path is None:
                continue
            claimed[key] = path
            # The guest's own diagnosis (the size cap, a serializer that
            # raised). Without carrying it the agent was told the rule
            # up front and then got silence when it broke it — on this
            # rung only, which is worse than either.
            note = value.get(_PROBLEM)
            if isinstance(note, str):
                claim_problems.append(note)
        if claimed:
            ui = {**ui, **claimed}
            result = replace(result, namespace={**result.namespace, "ui": ui})
        if claim_problems:
            result = replace(result, ui_problems=(*result.ui_problems, *claim_problems))
        # ONLY the values that cannot cross as data. Materializing the
        # rest would replace an agent's plain string or dict with a
        # path, which is a far larger change to `ui` than swapping a
        # live object nobody could have used anyway. Adapters still
        # render everything for display; that is a different question
        # from what the binding holds.
        from .artifacts import is_rich

        rich = {k: v for k, v in ui.items() if is_rich(v)}
        if not rich:
            return result
        # Lazy: adapters.render imports Workspace at module scope.
        from .adapters.render import materialize_ui

        claims: dict[Any, Any] = {}
        problems: list[str] = []
        try:
            # Artifact writes go through the public files.write, which
            # commits for itself. Suppressed: they are part of THIS
            # call and ride its commit.
            with self._one_commit():
                _, problems = materialize_ui(self, rich, claims=claims)
        except Exception:  # noqa: BLE001 - rendering agent data is never fatal
            return result
        if not claims and not problems:
            return result
        return replace(
            result,
            namespace={**result.namespace, "ui": {**ui, **claims}},
            ui_problems=tuple(problems),
        )

    # -- async host facades ---------------------------------------------
    #
    # These exist for event-loop embedders (FastAPI, etc.): they run the
    # SYNC execution in a thread so the caller's loop stays responsive.
    # They change nothing about the sandbox — agent code is still sync;
    # this is purely how the HOST invokes it. (sandtrap has an async
    # aexec, but it only yields at the agent code's await points, so it
    # would still block the loop on the common CPU-bound handler —
    # threading is the robust choice and keeps the agent surface uniform.)
    #
    # A workspace is single-writer, same as the sync API — but the
    # workspace enforces it: threading makes accidental concurrency
    # easy to reach, and these facades go through the locked public
    # methods, so concurrent awaits serialize safely (at the cost of a
    # blocked executor thread each while they wait).

    async def aterminal(self, command: str) -> TerminalResult:
        """Async facade over :meth:`terminal` — runs it in a thread so an
        event-loop host doesn't block. Same result, same semantics."""
        import asyncio

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.terminal, command)

    async def arun_python(
        self, code: str, *, inputs: Mapping[str, Any] | None = None
    ) -> PythonResult:
        """Async facade over :meth:`run_python` — see :meth:`aterminal`."""
        import asyncio
        from functools import partial

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, partial(self.run_python, code, inputs=inputs)
        )

    # ------------------------------------------------------------------
    # direct (host-side) access
    # ------------------------------------------------------------------

    def _mark_executor_stale(self) -> None:
        """Provider state moved without the executor seeing it; the
        next execution must refresh the guest first. Every workspace
        path that writes behind the executor's back goes through here
        (see :meth:`Runtime.mark_stale`)."""
        self._runtime.mark_stale()

    @property
    def cache(self) -> MutableMapping[str, Any]:
        """The agent's persistent dict, host-side view. Key rules: str
        keys, no ``__`` prefix, no ``/``. Writes bypass the workspace's
        single-writer lock (they hit the same staged buffer) — a host
        thread mutating it while agent calls run holds :attr:`lock`."""
        if not self._cache_enabled:
            raise NotSupportedError(
                "cache is disabled for this workspace (cache=False)"
            )
        # Frozen: the same refusing view the executor got, so a host
        # write raises ``PermissionError`` here exactly as an agent's
        # ``cache['x'] = 1`` does inside the sandbox.
        return Cache(self._kv_view)

    # ------------------------------------------------------------------
    # versioning (gated by caps; see protocol.py)
    # ------------------------------------------------------------------

    def commit(self, info: dict[str, Any] | None = None) -> str:
        """Commit everything uncommitted — files, cache and cwd — as one
        atomic commit; returns its id.

        Everything, always — this is the framework's durability verb,
        and the code around a workspace (a turn hook, a session db, a
        skill installer) can only rely on it if its scope never depends
        on what the agent happens to have staged. It is also invisible
        to the agent's own git: ``ws.index`` measures against the
        agent's last commit, so a commit made here leaves the staged
        set staged, the work in progress modified, and ``ws-git status``
        reading exactly as it did before.

        ``ws.index.commit()`` is the agent's verb: the staged set, and
        a commit in the agent's own graph.

        ``info`` is caller metadata recorded on the commit and must be
        JSON-serializable. It may not claim one of the agent's own
        ``tool`` names (see :func:`_refuse_agent_tool`).
        """
        _refuse_agent_tool(info)
        with self._lock:
            self._check_open()
            self._check_writable("commit")
            return self._provider.commit(info)

    def checkout(
        self, ref: "str | Ref", paths: "Iterable[str] | str | None" = None
    ) -> str:
        """Two forms, as ``git checkout <ref> [-- <paths>]`` has two.

        With ``paths``, TAKE: make those paths match any ref and leave
        everything else alone — git's ``restore --source=<ref> --
        <paths>``. A named directory is mirrored, so a file it holds
        here and the ref does not is removed. See :meth:`_take`.

        Without them, RESTORE the whole session. The rest of this
        docstring is that form.

        Make this session what it was at one of its own commits;
        returns the id of the commit that lands.

        The whole session, not the files alone: files, cache, cwd, the
        ws-git blob and anything else stored here become what they
        were at that commit, and uncommitted writes are replaced by
        them (``ws.discard()`` is the explicit spelling for a caller
        who wants only that). Within this session only — a session IS
        a branch, so switching to another one is ``ws.fork(name)`` or
        ``store.open(name)``, never a checkout, and a name that is not
        a commit here is refused rather than guessed at.

        It APPENDS. The restored state is written and committed, so
        the returned id is a NEW commit and everything committed since
        the target is still in ``ws.log()`` — an undo is redo-able
        (``ws.rollback(1)`` right after lands on the commit the
        checkout stepped off), and nothing a tag or a fork was made to
        protect is at risk. Nothing but store-level admin
        (``Store.delete``) ever moves a head backward.

        The agent's git rewinds with the tree, because its head and
        graph live in a key the checkout restores like any other:
        after a checkout to a commit made when ``ws.index.head`` was
        X, it is X again, and ``ws.index.log()`` reads as it did then.
        """
        with self._lock:
            self._check_writable("checkout")
            if paths is not None:
                return self._take(ref, paths)
            commit = str(ref)
            try:
                landed = self._provider.checkout(commit)
            except CommitNotFoundError as e:
                raise CommitNotFoundError(
                    f"{commit!r} is not a commit on session "
                    f"{self.session!r} ({e}). checkout moves within one "
                    "session's history; sessions are branches, so reaching "
                    'another one is ws.fork("name") or store.open("name"). '
                    "To bring only some of its files here, name them: "
                    "ws.checkout(ref, paths=[...])."
                ) from e
            # provider state moved under the executor: flag its view,
            # and the next execution refreshes it (no-op for
            # LocalExecutor, which holds no copy)
            self._mark_executor_stale()
            return landed

    def _take(self, ref: "str | Ref", paths: "Iterable[str] | str") -> str:
        """``ws.checkout(ref, paths=[...])``: make these paths match
        the ref; returns the commit that lands (or the current head
        when nothing was written or autocommit is off).

        A path that names a DIRECTORY in the ref mirrors that whole
        subtree: files the ref does not hold are removed from it, so
        taking a delegate's ``pkg/`` cannot leave behind the
        ``pkg/old.py`` the delegate deleted. A path that names a FILE
        moves that one file and removes nothing.

        Ordinary writes and removals, so the file tools, the view rule
        and the agent's own status all see them as work in the tree —
        and only file keys move, so the merge-policy question never
        arises. The provenance is a soft reference in the commit's info
        (``taken_from``), not a parent: taking three files from a
        delegate does not make its history yours. ``paths`` records
        what landed and ``removed`` what the mirror dropped, so the log
        accounts for every file the take touched.

        ``ref`` may be a ``session@commit``, a session name (that
        session's last agent commit, as a merge would read it), or a
        commit of this session.
        """
        wanted = normalize_view(paths, self._root)
        source, files = self._take_source(ref)
        taken: dict[str, Any] = {}
        mirrored: list[str] = []
        for path in wanted:
            under = {
                p: v for p, v in files.items() if p == path or p.startswith(path + "/")
            }
            if not under:
                raise CommitNotFoundError(
                    f"nothing at {path!r} in {source} — a take makes a path "
                    "match the ref, and that ref holds no such file or "
                    "directory."
                )
            taken.update(under)
            if path not in files:
                # the ref holds it as a directory, so its subtree is
                # what the take is about, not just the files in it
                mirrored.append(path)
        here = self._provider.working_files() if mirrored else {}
        removed = sorted(
            path
            for path in here
            if path not in taken
            and any(path.startswith(under + "/") for under in mirrored)
        )
        # One refusal for the whole take, writes and removals together:
        # a file the view hides may not be dropped any more than it may
        # be overwritten.
        self._refuse_hidden(sorted(taken) + removed)
        for path, value in sorted(taken.items()):
            data = value if isinstance(value, bytes) else bytes(value)
            parent = posixpath.dirname(path)
            if parent not in ("", "/"):
                self._fs.makedirs(parent, exist_ok=True)
            self._fs.write(path, data)
        for path in removed:
            self._fs.remove(path)
        self._mark_executor_stale()
        if not (self._autocommit and self._provider.caps.versioned):
            return self.head or ""
        if self._provider.caps.staging and not self._provider.dirty:
            return self.head or ""
        info: dict[str, Any] = {
            "tool": "checkout",
            "taken_from": source,
            "paths": sorted(taken),
        }
        if removed:
            info["removed"] = removed
        return self._provider.commit(info)

    def _refuse_hidden(self, paths: "Iterable[str]") -> None:
        """The view's write rule over a whole set, before any of it is
        written.

        A take is one operation: refusing halfway through would leave
        the tree holding part of a take the caller is being told did
        not happen. Same pre-pass, for the same reason, as the one an
        executor's write harvest gets (:meth:`_view_refusal`).
        """
        if self._view_fs is None:
            return
        for path in paths:
            reason = self._view_fs.refuse_reason(path)
            if reason is not None:
                raise PermissionError(reason)

    def _take_source(self, ref: "str | Ref") -> "tuple[str, Mapping[str, Any]]":
        """``(ref as recorded, that state's files)`` for a take.

        A ``session@commit`` names its own state. A bare name is a
        session, and what a session means is its last AGENT commit —
        the same reading :meth:`merge` takes, so "take one file from
        the delegate" and "merge the delegate" cannot disagree about
        what the delegate said. Anything else is a commit of this
        session.
        """
        from .agentgit import BLOB_KEY, parse_blob
        from .store import Ref

        text = str(ref)
        if isinstance(ref, Ref) or "@" in text:
            parsed = Ref.parse(text)
            return str(Ref(parsed.session, parsed.commit)), self._provider.files_at(
                parsed.commit
            )
        try:
            head = self._provider.branch_head(text)
        except (ValueError, NotSupportedError, AttributeError):
            return f"{self.session}@{text}", self._provider.files_at(text)
        virtual = parse_blob(self._provider.key_at(head, BLOB_KEY))["head"] or head
        return f"{text}@{virtual}", self._provider.files_at(virtual)

    def rollback(self, steps: int = 1) -> str:
        """Check out the Nth-previous commit; returns the id of the
        commit that lands.

        The relative spelling of :meth:`checkout`, and it appends the
        same way: ``steps`` counts back over ``ws.log()`` as it stands
        now, and the restore joins that log. So ``rollback(1)``
        immediately after a checkout is the REDO — the commit before
        the restore is the one the checkout stepped off — and counting
        twice in a row is not the same as counting two at once.

        The explicit ``{"tool": "init"}`` lifecycle commit is the
        floor: rollback may target it, but never cross it into a
        provider's pre-workspace seed. Legacy histories without that
        exact marker retain their existing provider-history behavior.
        """
        if steps < 1:
            raise ValueError("steps must be >= 1")
        with self._lock:
            entries = list(self._provider.history(limit=steps + 1))
            if len(entries) <= steps:
                raise CommitNotFoundError(
                    f"Cannot roll back {steps} step(s): only "
                    f"{len(entries)} commit(s) in history"
                )
            # Exact metadata equality is deliberate: an unrelated
            # commit that merely includes tool="init" plus other
            # caller metadata must not become a workspace lifecycle
            # boundary. Newest-first history means targets beyond the
            # marker have a larger index; targeting the marker itself
            # remains valid.
            init_index = next(
                (
                    i
                    for i, entry in enumerate(entries)
                    if entry.info == {"tool": "init"}
                ),
                None,
            )
            if init_index is not None and steps > init_index:
                raise CommitNotFoundError(
                    f"Cannot roll back {steps} step(s): the workspace "
                    "initialization commit is the rollback floor"
                )
            target = entries[steps]
            landed = self._provider.checkout(target.id)
            self._mark_executor_stale()  # see checkout()
            return landed

    def log(self, *, limit: int | None = None) -> Iterable[CommitInfo]:
        return self._provider.history(limit=limit)

    def fork(
        self,
        name: str,
        *,
        at: str | None = None,
        inherit: str = "full",
        paths: "Iterable[str] | str | None" = None,
    ) -> "Workspace":
        """Independent session seeded from current state — or, with
        ``at``, from an earlier commit of this session, leaving this
        session where it is. Inherits this workspace's construction
        settings (see :class:`_Settings`) — python config, mounts, root,
        executor factory — plus its terminal commands. Cost varies by
        backend (see ``caps.cheap_fork`` and the README tradeoffs).

        **A fork point is always a commit.** Uncommitted writes here
        are landed first, under ``{"tool": "fork", "child": name}``,
        and THAT commit is the child's base and the merge base for the
        way back. Forking a copy of the staging buffer would give the
        child a state that never existed in history and a merge base
        that predates this session's own uncommitted edits.

        ``at`` is what "branch from where I published" wants: the
        child starts at that commit with everything the commit holds,
        and nothing here is rewound to get it there. Nothing is
        committed for it either — the buffer belongs to this session's
        present, not to the past the fork branches from.

        ``inherit`` decides whether the stored conversation comes
        along, and nothing else: ``"full"`` (default) keeps it — the
        continue-where-I-am fork — and ``"fresh"`` drops it, for a
        delegate that starts a chat of its own over these files. It
        never touches a file. A brief, a summary, a distilled context
        is content the caller supplies when it seeds the child's first
        turn; nothing here can write one, since the conversation is
        stored and not interpreted.

        ``paths`` narrows the child's VIEW, not its tree: its branch
        holds everything this one had, and its filesystem lists and
        reads only what is named (directories or files, absolute or
        relative to the root). So the merge back stays an ordinary
        three-way with no rule to special-case, and this session sees
        the child's whole branch regardless — ``diff``,
        ``checkout(ref, paths=)``, ``files.attach``. The child may
        CREATE new paths anywhere; changing or deleting one it cannot
        see is refused (see ``nontainer/views.py``). ``None`` is the
        whole tree.
        """
        if inherit not in ("full", "fresh"):
            raise ValueError(
                f"inherit must be 'full' or 'fresh': {inherit!r}. A summary "
                "or a brief is content the caller supplies with the child's "
                "task, not a third inheritance mode."
            )
        seed = None if paths is None else normalize_view(paths, self._root)
        # Mutating despite appearances: a fork point is a commit, and
        # the child is seeded before its workspace is built.
        with self._lock:
            self._check_open()
            if at is None and self._provider.caps.versioned and self._provider.dirty:
                self._check_writable("fork")
                self._provider.commit(
                    {"tool": "fork", "child": name, "inherit": inherit}
                    | ({"paths": list(seed)} if seed else {})
                )
            # Providers written to the older fork(name) shape — a custom
            # one, say — must keep working for the call that has no
            # ``at``; only a fork from the past asks them for more.
            forked = (
                self._provider.fork(name)
                if at is None
                else self._provider.fork(name, at=at)
            )
            self._seed_fork(forked, inherit=inherit, seed=seed)
        # Commands and autocommit are replayed from the LIVE
        # attributes rather than from _settings, because both can change
        # after construction (see _Settings). register_command mutates
        # the command set; the reserved bridge names are stripped here
        # because __init__ re-adds them. autocommit has a public
        # setter, and a fork of a turn-granularity session must stay in
        # turn granularity.
        new_ws = Workspace(
            forked,
            commands=self._runtime.forkable_commands(),
            autocommit=self._autocommit,
            **self._settings.as_kwargs(),
        )
        new_ws._store = self._store
        new_ws.runtime.env.update(self._runtime.env)
        self._adopt_commands(new_ws)
        return new_ws

    def _seed_fork(
        self,
        forked: WorkspaceProvider,
        *,
        inherit: str,
        seed: "tuple[str, ...] | None",
    ) -> None:
        """Write the child's own state onto its fresh branch, before a
        workspace is built over it.

        Two keys and nothing else: the view record (written when the
        child is narrowed, REMOVED when it is not — a fork of a
        narrowed session that is given the whole tree must not inherit
        its parent's blinkers), and the conversation, dropped whole for
        ``inherit="fresh"``. Landed as one commit of the child's own,
        so its head is consistent from the start and a reopen finds it.
        """
        kv = forked.kv
        changed = False
        if seed is not None:
            kv[VIEW_KEY] = encode_view(seed)
            changed = True
        elif parse_view(kv.get(VIEW_KEY)) is not None:
            del kv[VIEW_KEY]
            changed = True
        if inherit == "fresh":
            for key in [
                k
                for k in list(kv.keys())
                if isinstance(k, str) and k.startswith(CONVERSATION_PREFIX)
            ]:
                del kv[key]
                changed = True
        if changed and forked.caps.versioned:
            forked.commit(
                {"tool": "fork", "parent": self.session, "inherit": inherit}
                | ({"paths": list(seed)} if seed else {})
            )

    def _record_view(self, paths: tuple[str, ...]) -> None:
        """Keep the branch's view record in step with what this session
        can see.

        A path this session CREATED joins its view: a delegate that
        could not read back its own note would be a worse deal than
        git's sparse checkout rather than a better one. The record is
        an ordinary key, so it rides the next commit and is there on
        reopen.
        """
        self._view = paths
        self._provider.kv[VIEW_KEY] = encode_view(
            paths, parse_seed(self._provider.kv.get(VIEW_KEY))
        )

    def _attach(
        self,
        ref: "str | Ref",
        at: str,
        *,
        readonly: bool = True,
        root: str | None = None,
    ) -> str:
        """``ws.files.attach``, under the lock. See it for the contract."""
        from monkeyfs import ReadOnlyFS

        from .views import SubtreeFS

        if not readonly:
            raise NotSupportedError(
                "an attachment is a frozen state and accepts no writes: "
                "attach(..., readonly=True), and take what you want to "
                "change with ws.checkout(ref, paths=[...])."
            )
        point = posixpath.normpath(at if at.startswith("/") else f"{self._root}/{at}")
        if point == "/" or point in self._attached:
            raise ValueError(
                f"cannot attach at {at!r}: "
                + ("the root is the session's own" if point == "/" else "already taken")
            )
        snapshot = self._resolve_snapshot(ref, root or self._root)
        try:
            # The PROVIDER's filesystem, not the snapshot workspace's:
            # a session forked with a narrow view composes one over its
            # provider, and attaching that would show the caller only
            # what the delegate could see — while the whole reason to
            # attach a delegate is to look at everything it did. The
            # provider always holds the whole branch.
            self._fs.attach(
                point, ReadOnlyFS(SubtreeFS(snapshot._provider.fs, snapshot.root))
            )
        except BaseException:
            snapshot.close()
            raise
        self._attached[point] = snapshot
        self._mark_executor_stale()
        return str(snapshot.ref)

    def _detach(self, at: str) -> None:
        point = posixpath.normpath(at if at.startswith("/") else f"{self._root}/{at}")
        snapshot = self._attached.pop(point, None)
        if snapshot is None:
            raise ValueError(f"nothing attached at {at!r}")
        self._fs.detach(point)
        snapshot.close()
        self._mark_executor_stale()

    def _resolve_snapshot(self, ref: "str | Ref", root: str) -> "Workspace":
        """A frozen workspace at what ``ref`` names — a
        ``session@commit``, or a session name meaning its head — read
        under ``root``.

        The root matters: a commit holds its files at whatever root the
        session that made them used, so resolving at the wrong one
        reads an empty tree. A lineage shares one, which is why the
        caller's own is the default.
        """
        from .store import Ref

        if self._store is None:
            raise WorkspaceError(
                "this workspace was built straight from a provider, so no "
                "store can resolve a ref for it; open it through "
                "Store.open(...) to attach another session's tree."
            )
        text = str(ref)
        if isinstance(ref, Ref) or "@" in text:
            return self._store.resolve(Ref.parse(text), root=root)
        head = self._provider.branch_head(text)
        return self._store.resolve(Ref(session=text, commit=head), root=root)

    def _adopt_commands(self, new_ws: Workspace) -> None:
        """Re-bind framework-owned commands onto a fork/snapshot.

        A command closure captures the workspace it was built for, so
        inheriting the mapping would dispatch the fork into its parent
        (the fork-bleed). Each recorded factory rebuilds its command
        bound to ``new_ws`` instead. A factory that raises closes the
        half-built workspace and propagates — never a fork whose verbs
        silently belong to someone else.
        """
        adopted = self._runtime.framework_commands
        new_ws.runtime.framework_commands.clear()
        new_ws.runtime.framework_commands.update(adopted)
        try:
            # Deduplicated: one initializer may own several commands
            # (registering itself as each one's rebind), and a second
            # invocation would collide with the first one's
            # registrations and fail the fork.
            for factory in dict.fromkeys(adopted.values()):
                factory(new_ws)
        except BaseException:
            new_ws.close()
            raise

    def merge(self, source: str) -> MergeOutcome:
        """Merge another session's committed state into this one.

        A merge takes only what has been COMMITTED, on both sides, and
        refuses a source that has more. With ``caps.index``, committed
        means committed by the agent:

        - This session must have nothing modified relative to its own
          last ws-git commit. Autocommit keeps the store's buffer clean
          while an agent is composing, so the buffer is not the
          question; work in flight would otherwise be folded into the
          merge commit and attributed to the merge.
        - ``source`` is merged at ITS last agent commit, whose tree is
          exactly what that agent committed — and it is refused while
          that agent has anything newer, rather than merged at a state
          the delegate has already moved past. A session that never
          used ws-git has no agent commit to differ from and is merged
          at its store head, as before.

        File conflicts land as conflict markers IN the merge commit and
        are reported in the outcome rather than blocking it — resolve
        them with ordinary edits and commit. Requires ``caps.merge``.
        """
        if not self._provider.caps.merge:
            raise NotSupportedError(
                f"{type(self._provider).__name__} cannot merge: merge() is "
                "not supported. Use the kvgit backend for merges."
            )
        with self._lock:
            self._check_open()
            self._check_writable("merge")
            if self._provider.dirty:
                raise WorkspaceError(
                    "uncommitted changes on this session; a merge needs a "
                    "clean tree to land against — ws.commit() them, or "
                    "ws.discard() them, then merge"
                )
            at = None
            parents: list[str] = []
            if self._provider.caps.index:
                from .agentgit import AgentGit

                git = AgentGit(self)
                self._require_agent_clean(git)
                head = git.head
                # The merge commit joins the agent's graph, so its own
                # log walks through it instead of stopping at it.
                parents = [head] if head is not None else []
                self._require_source_clean(git, source)
                at = git.source_commit(source)
            outcome = self._provider.merge(
                source, at=at, info={"virtual_parents": parents}
            )
            if outcome.merged and self._provider.caps.index:
                from .agentgit import AgentGit

                AgentGit(self).record_merge(source, outcome.commit, outcome.conflicts)
            # provider state moved under the executor: flag its view.
            self._mark_executor_stale()
            return outcome

    @staticmethod
    def _require_agent_clean(git: "AgentGit") -> None:
        """Refuse a merge over work the agent has not committed.

        The store's buffer is clean by then — autocommit saw to that —
        so the only honest reading of "uncommitted" here is the agent's
        own: anything modified against its last ws-git commit. Merging
        over it would put work the agent never committed into the merge
        commit and move its head past it, with the merge's name on it.

        A session that has never made a ws-git commit has no such
        commit to differ from — every file it holds reads as modified —
        so only an open index refuses there. Same fallback as
        :meth:`AgentGit.source_commit` makes from the other side.
        """
        status = git.status()
        head = git.head
        if not status.staged and (head is None or not status.unstaged):
            return
        drop = f"ws-git checkout {head[:7]}" if head else "ws-git reset"
        raise WorkspaceError(
            "uncommitted ws-git work on this session: "
            f"{len(status.staged) + len(status.unstaged)} path(s) differ from "
            "your last ws-git commit, and a merge takes only what has been "
            f"committed. Land it (ws-git commit -m ...) or drop it ({drop}), "
            "then merge."
        )

    @staticmethod
    def _require_source_clean(git: "AgentGit", source: str) -> None:
        """Refuse a merge of work the SOURCE agent has not committed.

        Symmetric with :meth:`_require_agent_clean`. The source is
        merged at its last agent commit, so a delegate that wrote
        after it would have that work silently left behind — the merge
        would land, report success, and bring back a state the
        delegate has moved past. Say so instead, and name the two
        fixes in the source's own terms.
        """
        pending = git.source_uncommitted(source)
        if not pending:
            return
        raise WorkspaceError(
            f"uncommitted ws-git work on {source!r}: {len(pending)} path(s) "
            "differ from that session's last ws-git commit, and a merge "
            "takes only what has been committed. Land it there "
            "(ws-git commit -m ... in that session) or drop it "
            "(ws-git checkout <its last commit>), then merge."
        )

    def discard(self) -> None:
        """Drop writes since the last commit (staging providers)."""
        with self._lock:
            self._provider.discard()
            self._mark_executor_stale()  # see checkout()

    # ------------------------------------------------------------------
    # tags (gated by caps.tags)
    # ------------------------------------------------------------------

    def _require_tags(self, op: str) -> None:
        if not self._provider.caps.tags:
            raise NotSupportedError(
                f"{type(self._provider).__name__} has no tags: {op}() is not "
                "supported. Use the kvgit backend for named commits."
            )

    def _tag(
        self,
        name: str,
        *,
        at: str | None = None,
        info: dict[str, Any] | None = None,
        scope: str = "session",
    ) -> str:
        """Name a commit, immutably; returns the commit id.

        Two scopes, and nontainer decides what each means rather than
        handing embedders a flat namespace to partition themselves:

        - ``scope="session"`` (default) — the name belongs to this
          session. ``ws.tags.list()`` lists only its own, another session's
          ``v1`` is a different tag, and deleting the session
          (``Store.delete``) deletes it. This is the commit
          you want to be able to name later: "before the refactor".
        - ``scope="store"`` — the name belongs to no session. Every
          workspace on the store can list and read it, and it survives
          the deletion of the session that made it. This is a
          publication: the state an app serves, the snapshot a report
          links to, anything that must outlive the conversation.

        Tags never move: an existing name raises rather than being
        repointed (delete it and tag again, so the move is visible in
        the calling code). A tag also anchors garbage collection — the
        named commit and its ancestry stay reachable for as long as
        the tag exists.

        ``at`` names an earlier commit instead of the current state;
        without it, staged changes are committed first (``info={"tool":
        "tag"}``), the way :meth:`fork` does, so the name means what the
        caller saw rather than the last commit before it. Everything that can be
        checked is checked BEFORE that commit — the name and scope
        rules, and whether the name is taken — because a refusal after
        it would leave the history permanently advanced by a call that
        failed, which in turn-granularity mode is the whole turn the
        caller believed had not happened. The provider's own
        compare-and-set still decides a race between two taggers.
        """
        with self._lock:
            self._check_open()
            self._require_tags("tag")
            self._check_writable("tag")
            self._provider.check_tag(name, scope=scope)
            if self._provider.tag_info(name, scope=scope) is not None:
                raise WorkspaceError(
                    f"Tag already exists: {name!r} in scope {scope!r} — tags "
                    "never move; delete it first if you mean to repoint it"
                )
            if at is None and self._provider.dirty:
                # Through the public verb: naming the current state
                # means naming all of it, index included, by the same
                # rule ``commit`` applies.
                self.commit(info={"tool": "tag", "name": name})
            return self._provider.tag(name, at=at, info=info, scope=scope)

    def _tags(self, *, scope: str = "session") -> dict[str, str]:
        """Tag name → commit id, for one scope (see :meth:`_tag`)."""
        with self._lock:
            self._require_tags("tags")
            return self._provider.tags(scope=scope)

    def _tag_info(self, name: str, *, scope: str = "session") -> TagInfo | None:
        """Describe one tag, or ``None`` if there is no such tag."""
        with self._lock:
            self._require_tags("tag_info")
            return self._provider.tag_info(name, scope=scope)

    def _delete_tag(self, name: str, *, scope: str = "session") -> None:
        """Drop a tag. What it named survives only while something else
        still reaches it — a branch, or another tag."""
        with self._lock:
            self._check_open()
            self._require_tags("delete_tag")
            self._check_writable("delete_tag")
            self._provider.delete_tag(name, scope=scope)

    def _at_tag(self, name: str, *, scope: str = "session") -> "Workspace":
        """A frozen workspace over the tagged state.

        Reads see the tagged files, cache and cwd; nothing can be
        written or committed (see :attr:`frozen`). It inherits this
        workspace's construction settings the way :meth:`fork` does —
        python config and its live host objects, mounts, root, executor
        factory, terminal commands — so an app served from a snapshot
        still reaches the session's live db, which is the point: the
        *files* are frozen, the host's world is not.

        Close it when done; it holds an executor of its own.
        """
        with self._lock:
            self._require_tags("at_tag")
            frozen = self._provider.at_tag(name, scope=scope)
        # Same replay as fork(): commands and autocommit come from
        # the live attributes because both can change after construction
        # (see _Settings) — including the framework re-binding, so a
        # snapshot's verbs read the snapshot, not the live parent.
        # autocommit is forced off for a frozen provider regardless;
        # passing it keeps the two paths identical.
        new_ws = Workspace(
            frozen,
            commands=self._runtime.forkable_commands(),
            autocommit=self._autocommit,
            **self._settings.as_kwargs(),
        )
        new_ws._store = self._store
        new_ws.runtime.env.update(self._runtime.env)
        self._adopt_commands(new_ws)
        return new_ws

    def diff(self, a: str, b: str) -> WorkspaceDiff:
        """File-level changes between two commit ids: which
        workspace paths were added, removed and modified. Framework
        state — cache, cwd, the stored conversation — is not a file and
        never appears, and ``modified`` holds the paths whose BYTES
        differ: a file re-saved with the content it already had is not
        a change, though the store's own key diff counts the write."""
        with self._lock:
            self._require_tags("diff")
            return self._provider.diff(a, b)

    def changed_since(self, ref: "str | Any") -> WorkspaceDiff:
        """What the files look like now versus at ``ref``.

        ``ref`` is a tag name — this session's own is tried first, then
        the store's, so ``ws.changed_since("v1")`` is the everyday
        spelling and a published store tag needs no extra argument — or
        a commit id, or a :class:`~nontainer.store.Ref`, whose commit is
        used. The comparison ends at the current head, so
        staged-but-uncommitted work is not in it (check :attr:`dirty`).
        """
        with self._lock:
            self._require_tags("changed_since")
            name = getattr(ref, "commit", ref)
            info = self._provider.tag_info(name, scope="session") or (
                self._provider.tag_info(name, scope="store")
            )
            return self._provider.diff(info.id if info else name, self._provider.head)

    # ------------------------------------------------------------------
    # power modes / lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        with self._lock:  # don't close the provider mid-call
            if not self._closed:
                self._closed = True
                # The runtime first: it settles a pending sync and
                # releases the executor, and it is contracted not to
                # raise however badly a third-party executor behaves —
                # so the provider (a held kvgit store) always closes.
                self._runtime.close()
                # The attached states are workspaces of their own, held
                # only by this one: nothing else will close them.
                for snapshot in self._attached.values():
                    try:
                        snapshot.close()
                    except Exception:  # noqa: BLE001 - closing the session wins
                        pass
                self._attached.clear()
                self._provider.close()

    def __enter__(self) -> "Workspace":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    @property
    def _sandbox(self) -> Any:
        """Debug/test peephole into the LocalExecutor's default sandbox
        (the process-isolation tests kill its worker to exercise crash
        recovery). ``None`` for executors without one."""
        return getattr(self._runtime.executor, "_sandbox", None)

    def _check_open(self) -> None:
        if self._closed:
            raise WorkspaceError("Workspace is closed")

    def _refused_frozen(self, exc: PermissionError) -> str:
        """Turn a write refused by the executor itself into this call's
        result text — the shape ``terminal`` and ``run_python`` report
        every other failure in, since a refused tool call is a result
        and not a host error. An executor may write back on its own
        after the code ran (a guest's cache service is the case in
        hand), so the refusal can arrive as an exception rather than as
        something the sandbox caught. The executor's substrate is left
        marked stale, so the next call starts from the frozen tree
        again. Only frozen workspaces refuse this way; anything else is
        a real error and re-raises.
        """
        if not self._frozen:
            raise exc
        self._mark_executor_stale()
        return str(exc)

    def _check_writable(self, op: str) -> None:
        """Refuse a host-side write on a frozen workspace, up front.

        The tools that write by name — ``file_write``, ``file_edit``,
        ``put`` — say so before touching anything, because there is
        nothing partial to attempt. Writes that only *might* happen
        inside a shell command or python call are refused by the
        read-only filesystem instead, at the moment they occur."""
        if self._frozen:
            where = f" at tag {self._frozen_at!r}" if self._frozen_at else ""
            raise NotSupportedError(
                f"frozen: this workspace is a snapshot{where}; it accepts no "
                f"writes, so {op} is not supported"
            )

    def _legacy_cwd(self) -> str | None:
        """Drop the cwd key of the two-key layout, and return what it
        held in case this session still needs it.

        The removal is unconditional: a store written under that layout
        carries both keys, the filesystem's is the one that resolves
        paths (so this value is only ever the fallback), and a dead key
        left on a branch is one more thing for a merge to contest. It
        is staged like any other write and rides the next commit.
        """
        kv = self._provider.kv
        try:
            legacy = kv.get(_LEGACY_CWD_KEY)
            if legacy is None:
                return None
            del kv[_LEGACY_CWD_KEY]
            return legacy if isinstance(legacy, str) else None
        except Exception:  # noqa: BLE001 - a kv that refuses has nothing to migrate
            return None

    def _save_cwd(self) -> bool:
        """Persist the cwd where the filesystem keeps it.

        Nothing to do where the filesystem owns the key (see
        :func:`_fs_owns_cwd`) — it has already written it. Elsewhere
        this is what gives a session its cwd back on reopen, under the
        same key. Guarded, because an unconditional write would dirty
        staging providers on every call, turning a read-only ``ls``
        into a commit.
        """
        if self._owns_cwd:
            return False
        try:
            cwd = self._fs.getcwd()
            if self._provider.kv.get(_cwd_key()) != cwd:
                self._provider.kv[_cwd_key()] = cwd
                return True
        except Exception:
            pass
        return False

    def _claimed(self, value: Any) -> Any:
        """An artifact claim from the guest, resolved and verified.

        The envelope must be *only* the tag, its value a workspace
        relative path, and the file must be there. Anything else is the
        agent's own data: ``ui = {"cfg": {"__nt_artifact__": "nope"}}``
        stays a dict, on every rung.
        """
        from .artifacts import ArtifactPath
        from .dud_outputs import _CLAIM, _PROBLEM

        if not (
            isinstance(value, dict)
            and _CLAIM in value
            and set(value) <= {_CLAIM, _PROBLEM}
        ):
            return None
        rel = value[_CLAIM]
        if not isinstance(rel, str) or rel.startswith("/") or ".." in rel:
            return None
        base = "" if self.root == "/" else self.root.rstrip("/")
        path = f"{base}/{rel}"
        try:
            if not self._fs.exists(path):
                return None
        except Exception:  # noqa: BLE001 - a VFS miss just means "not a claim"
            return None
        return ArtifactPath(path)

    @contextmanager
    def _one_commit(self):
        """Suppress nested commits for the duration of an operation.

        ``files.write`` commits, by design — it is a tool in its own
        right. But when the *workspace* writes on a call's behalf (``ui``
        artifacts), those writes belong to that call, not to a
        ``file_write`` of their own. Without this, materializing two
        rich values committed twice under the wrong tool and left
        ``result.commit`` None, which reads as "nothing was
        committed" while the head had in fact moved.
        """
        outer = self._defer_commits
        self._defer_commits = True
        try:
            yield
        finally:
            self._defer_commits = outer

    def _maybe_commit(self, tool: str) -> str | None:
        """Commit this call's staged changes. Returns the created
        commit's id, or None when nothing was committed (no changes,
        autocommit off, unversioned provider, or a nested write
        inside an operation that will commit for itself)."""
        if self._defer_commits:
            return None
        if self._autocommit and self._provider.dirty:
            return self._provider.commit(info={"tool": tool})
        return None

    def _absorb_executor_diff(self) -> str | None:
        """Land a remote executor's staged writes in the provider,
        BEFORE the commit flow — so the normal atomic commit and
        ``result.commit`` semantics apply unchanged whichever
        executor produced the writes. ``None`` (LocalExecutor always;
        a remote executor after a read-only call) costs nothing and
        dirties nothing. Callers hold the lock.

        This is also where a frozen workspace's refusal has to hold for
        executors that run against their own substrate: a guest writes
        to its own tree and reports the harvest afterwards, so nothing
        earlier in the call could have stopped it. A non-empty harvest
        is DROPPED, the executor is marked stale so the next execution
        re-pushes the frozen tree over what the guest did, and the
        message comes back for the caller to put on the result — the
        same shape a torn call uses, and the same refusal the local
        read-only filesystem raises in-process. Returns that message,
        or None when there was nothing to refuse.
        """
        d = self._runtime.diff()
        if d is None:
            return None
        if self._frozen:
            if not (d.writes or d.deletes):
                return None  # a read-only call on a snapshot: nothing to do
            self._mark_executor_stale()
            return (
                f"{_frozen_message(self._frozen_at)}; this call's writes were "
                "made in the executor's own tree and have been discarded"
            )
        refused = self._view_refusal(d)
        if refused is not None:
            # Decided before anything is applied: a guest harvests a
            # whole call at once, and half-applying it would leave work
            # behind from a call that is about to read as refused.
            self._mark_executor_stale()
            return refused
        from .executor import _apply_diff

        _apply_diff(self._fs, d.writes, d.deletes)
        return None

    def _view_refusal(self, d: Any) -> str | None:
        """The view's write rule against an executor's harvest.

        The local rung enforces it where the write happens, in the
        filesystem the sandbox holds. A guest rung writes into a tree
        of its own and reports afterwards, so the same rule is applied
        here to the same effect — including the case a guest can even
        reach it in, which is recreating by name a path its narrowed
        tree was never given.
        """
        if self._view_fs is None:
            return None
        for rel in (*d.writes, *d.deletes):
            reason = self._view_fs.refuse_reason("/" + rel.lstrip("/"))
            if reason is not None:
                return (
                    f"{reason} This call's writes were made in the "
                    "executor's own tree and have been discarded"
                )
        return None

    def _absorb_or_unwind(self, was_dirty: bool) -> str | None:
        """:meth:`_absorb_executor_diff`, honoring the torn-call
        contract — and passing through its frozen refusal, which
        reaches the caller by the same route (a message on an errored
        result) because it is the same kind of news: the call did not
        land.

        The torn-call contract: ``HarvestLost`` means the guest died between a
        successful exec and its write harvest — the call's fs writes
        are unrecoverable while its cache write-backs may already sit
        in provider staging. Returns an error message for the result
        (the call must not read as a success), after unwinding what can
        be unwound: staging that was clean at call entry holds only
        this call's effects, so ``discard`` restores exact pre-call
        state (the call observably happened zero times). Dirty-at-entry
        staging (autocommit off, prior host writes) holds earlier
        work that is not ours to drop — leave it, and say so."""
        from .executor import HarvestLost

        try:
            return self._absorb_executor_diff()
        except HarvestLost as e:
            if not was_dirty:
                try:
                    self._provider.discard()
                    # No executor.sync(): entry-clean staging held only
                    # cache write-backs (fs writes never arrived), and
                    # cache rides the live kv plane, not the pushed
                    # tree — the recovered guest is already consistent.
                    return f"{e}; this call's staged changes were rolled back"
                except Exception:
                    pass
            return (
                f"{e}; WARNING: this call's cache write-backs may remain "
                "staged and would ride the next commit"
            )

    def _absorb_before_verb(self, verb: str) -> dict | None:
        """Harvest + absorb guest writes before a hostcall-dispatched
        verb (ws-git, ws-curl): the guest tree may hold writes this
        script made before invoking the verb, and the provider doesn't
        see them until harvest (which runs after exec returns).

        Returns an error triple to dispatch instead, or ``None`` to
        proceed. A failure absorbs AFTER the guest baseline advanced
        (rebase rides the harvest), so the triple alone would let the
        outer call commit partials as a routine success: the guest
        is flagged for rematerialization and the message is stashed
        for :meth:`terminal`, which unwinds like a torn call instead.
        """
        try:
            torn = self._absorb_or_unwind(self._provider.dirty)
        except Exception as e:  # noqa: BLE001 — honest triple, below
            self._mark_executor_stale()
            self._pending_sync_error = f"mid-call sync failed: {e}"
            return {
                "stdout": "",
                "stderr": f"{verb}: mid-call sync failed: {e}",
                "exit_code": 1,
            }
        if torn is not None:
            return {"stdout": "", "stderr": f"{verb}: {torn}", "exit_code": 1}
        return None


def workspace(
    session: str,
    *,
    store: str | Path | None = None,
    backend: Literal["kvgit", "dir", "agentfs"] = "kvgit",
    provider: WorkspaceProvider | None = None,
    python: PythonConfig | None = None,
    mounts: Mapping[str, Mount] | None = None,
    commands: Mapping[str, Callable[..., Any]] | None = None,
    cache: bool = True,
    autocommit: bool = True,
    max_observation: int = 32_000,
    executor_factory: "Callable[[], Executor] | None" = None,
    root: str = "/workspace",
) -> Workspace:
    """Build a session's :class:`Workspace` (the one-liner entry point).

    Sugar for ``Store(store, backend=backend).open(session, ...)``, and
    the shortest way in when a caller has one session in mind. Reach
    for :class:`~nontainer.store.Store` directly when the store itself
    is the subject — listing sessions, deleting one, store-scoped tags.

    Session resolution by backend:

    - ``"kvgit"``: one shared store at ``store`` (default
      ``~/.nontainer``); ``session`` is a branch. Forks share storage.
    - ``"dir"``: ``store/<session>/`` as a plain directory
      (``IsolatedFS``). No versioning; time-travel verbs raise.
    - ``"agentfs"``: ``store/<session>.db``, one AgentFS file per
      session (unversioned spike).

    ``provider`` overrides ``backend``/``store`` entirely (bring your
    own substrate) — the same substitution
    ``Store(provider_factory=...)`` makes, for one session.
    ``session`` is validated against ``SESSION_ID_RE`` in all paths.

    ``executor_factory`` selects the execution backend for this session
    and every fork of it (default: the in-process ``LocalExecutor``).
    Pass ``lambda: DudExecutor()`` to run on a real machine — see
    ``nontainer.executor_dud`` and its ``[dud]`` extra.

    ``root`` is the workspace root — the absolute VFS path agent code
    sees its files under (default ``/workspace``; see
    :attr:`Workspace.root`). One value per session, inherited by
    forks.
    """
    from .protocol import validate_session_id
    from .store import Store

    if provider is None:
        return Store(store, backend=backend).open(
            session,
            python=python,
            mounts=mounts,
            commands=commands,
            cache=cache,
            autocommit=autocommit,
            max_observation=max_observation,
            executor_factory=executor_factory,
            root=root,
        )
    # A ready provider is one session's substrate, already built. It
    # goes in as the factory's answer for every id, and the id is
    # validated here rather than in Store.open, which leaves naming to
    # whatever the factory brings.
    validate_session_id(session)
    return Store(store, backend=backend, provider_factory=lambda _: provider).open(
        session,
        python=python,
        mounts=mounts,
        commands=commands,
        cache=cache,
        autocommit=autocommit,
        max_observation=max_observation,
        executor_factory=executor_factory,
        root=root,
    )
