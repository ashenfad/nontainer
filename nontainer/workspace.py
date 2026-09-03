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
  session serializes safely (each call atomic + checkpointed) instead
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
  command) and persists via the ``__cwd__`` framework key in kv, so
  on versioned providers rollback also restores *where you were*.
- **Execution is a seam.** How code runs — the python sandbox, the
  shell, worker lifecycle — lives behind :class:`Executor` (see
  executor.py); the default :class:`LocalExecutor` is the in-process
  sandtrap + termish wiring. The workspace keeps what execution must
  not own: the lock, the checkpoint flow, cwd, the cache key rules.
"""

from __future__ import annotations

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
from .errors import CheckpointNotFoundError, NotSupportedError, WorkspaceError
from .protocol import (
    Capabilities,
    CheckpointInfo,
    TagInfo,
    WorkspaceDiff,
    WorkspaceProvider,
)

if TYPE_CHECKING:
    from .editing import EditOutcome
    from .executor import Executor, ViewSpec

Isolation = Literal["none", "process", "kernel"]

_CWD_KEY = "__cwd__"
RESERVED_COMMANDS = frozenset({"python", "python3"})


@dataclass(frozen=True)
class Mount:
    """A real directory exposed inside the workspace tree (a "volume").

    Mounts are a *workspace* concern, not a python-sandbox concern:
    both tools see them — ``terminal("ls /data")`` and sandboxed
    ``open("/data/x.csv")`` agree. Composed via monkeyfs ``MountFS``
    (+ ``IsolatedFS``, + ``ReadOnlyFS`` when ``readonly``).

    Mounted paths are live views of the real directory: they are NOT
    versioned and NOT captured by checkpoints, so a rollback or restore
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

    checkpoint: str | None = None
    """Id of the commit this call's autocheckpoint created — pins the
    workspace state after the call (``ws.restore(result.checkpoint)``).
    ``None`` when nothing was committed: read-only call, autocheckpoint
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

    checkpoint: str | None = None
    """Id of the commit this call's autocheckpoint created (``None``
    when nothing was committed) — see ``TerminalResult.checkpoint``."""

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
    """Outcome of ``write_file`` / ``put``."""

    path: str
    """Workspace path written."""

    size: int
    """Bytes written."""

    created: bool
    """True for a new file, False for an overwrite."""

    checkpoint: str | None = None
    """Commit created by this call's autocheckpoint (``None`` when
    nothing was committed) — see ``TerminalResult.checkpoint``."""

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
    # The same sandbox checkpoint enforces timeout, cancel, and ticks,
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
    """``ws.fs`` wrapper: host-side writes mark the executor stale.

    ``ws.fs`` is the documented host-side escape hatch (seeding inputs,
    harvesting artifacts) and it writes straight into the provider —
    behind a remote executor's back. Without this, the guest tree never
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
      (``enable_apps`` injects ``curl`` that way).
    - ``autocheckpoint`` — has a public setter, and the documented
      turn-granularity path uses it (``WorkspaceTools(ws,
      checkpoint="turn")`` sets ``ws.autocheckpoint = False``). Replaying
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
        autocheckpoint: bool = True,
        max_observation: int = 32_000,
        executor: "Executor | None" = None,
        executor_factory: "Callable[[], Executor] | None" = None,
        root: str = "/workspace",
    ) -> None:
        self._provider = provider
        self._python_config = python or PythonConfig()
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
        # (each atomic + checkpointed) instead of interleaving writes
        # into the provider's staged buffer. Invariants: the lock is
        # taken ONLY in mutating public method bodies — never in
        # exec_python / build_sandbox / _maybe_checkpoint (the
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

        # autocheckpoint is meaningless (and forced off) when the
        # provider can't checkpoint.
        # Set while an operation writes on a call's behalf, so nested
        # write_file checkpoints fold into that call's single commit.
        self._defer_checkpoints = False
        self._autocheckpoint = (
            autocheckpoint and provider.caps.versioned and not self._frozen
        )
        # Construction owns the workspace root + initial cwd, but it
        # must never absorb caller-owned staged work into an automatic
        # init baseline. Embedders may deliberately seed provider.fs /
        # provider.kv before wrapping it, so remember rather than reject
        # that state: initialization joins their staged view and remains
        # explicitly theirs to checkpoint or discard.
        was_dirty = provider.caps.staging and provider.dirty

        # -- filesystem: provider fs, optionally wrapped with mounts.
        # Normalized once, here: the resolved mapping is what a fork
        # replays, so parent and fork can't resolve to different
        # directories (see _Settings).
        normalized_mounts = self._normalize_mounts(mounts)
        self._fs = self._build_fs(provider.fs, normalized_mounts)
        # Host-side writes move provider state behind a remote
        # executor's back; the public ``fs`` hands out a wrapper that
        # flags it, and the next execution syncs (see _SyncingFS).
        self._executor_stale = False
        self._public_fs = _SyncingFS(self._fs, self._mark_executor_stale)

        # -- terminal commands: user injections + the python bridge --
        user_commands = dict(commands or {})
        reserved = RESERVED_COMMANDS.intersection(user_commands)
        if reserved:
            raise ValueError(
                f"Reserved terminal command name(s): {sorted(reserved)}. "
                "'python' is nontainer's bridge into run_python."
            )
        user_commands["python"] = self._python_command
        user_commands["python3"] = self._python_command  # the reflex spelling
        self._commands = user_commands

        # -- execution: bound behind the Executor seam (executor.py).
        # Default is the in-process sandtrap+termish LocalExecutor.
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
        from .executor import ExecutionContext, LocalExecutor

        self._executor_factory = executor_factory
        if executor is not None:
            self._executor = executor
        elif executor_factory is not None:
            self._executor = executor_factory()
        else:
            self._executor = LocalExecutor()

        # -- what a fork replays. Captured from the NORMALIZED values,
        # not the raw arguments, so a fork starts from the state this
        # workspace resolved to: ``root`` collapsed by segment, mount
        # points validated and their sources resolved. Mutable-after-
        # construction settings are deliberately NOT here (see
        # :class:`_Settings`); fork() reads those live.
        self._settings = _Settings(
            python=self._python_config,
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
        # them under {"tool": "init"} regardless of autocheckpoint mode.
        # Reopening is read-only: the guards leave the provider clean,
        # so no second init commit is made.
        initialized = False
        if self._root != "/" and not self._fs.isdir(self._root):
            self._fs.makedirs(self._root, exist_ok=True)
            initialized = True

        # -- stateful cwd: restore from framework key if present, else
        # start at the workspace root. Guarded so a no-op restore
        # doesn't dirty staging providers (which would turn read-only
        # tool calls into commits).
        stored_cwd = provider.kv.get(_CWD_KEY) or self._root
        if stored_cwd != "/":
            try:
                if self._fs.getcwd() != stored_cwd:
                    self._fs.chdir(stored_cwd)
                    initialized = True
            except Exception:
                pass  # path may no longer exist; start at the fs root
        initialized = self._save_cwd() or initialized
        # A frozen workspace commits nothing, init baseline included: the
        # tagged checkpoint already holds a root and a cwd, and a
        # snapshot that wrote a commit of its own would not be one.
        if provider.caps.versioned and initialized and not was_dirty:
            if not self._frozen:
                provider.checkpoint(info={"tool": "init"})

        # open() LAST: it may fork a persistent isolation worker (see
        # LocalExecutor.open), and opening after everything else means
        # no later __init__ failure can orphan it (PR #10 review).
        # If open itself fails, the valid init baseline stays committed:
        # root/cwd are provider state, independent of executor health.
        self._executor.open(
            ExecutionContext(
                fs=_frozen_fs(self._fs, self._frozen_at) if self._frozen else self._fs,
                kv=self._kv_view,
                commands=self._commands,
                python_config=self._python_config,
                cache_enabled=self._cache_enabled,
                max_observation=self._max_observation,
                head=_state_identity(provider),
                root=self._root,
                frozen=self._frozen,
            )
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
    def supports_commands(self) -> bool:
        """Whether injected terminal commands reach the shell.

        An executor capability (``Executor.supports_commands``): true
        for the in-process termish shell, false for one running real
        bash in a guest. Tool descriptions gate on it — apps' ``curl``
        is only worth teaching where it exists.

        Defaults to true for executors predating the flag: that's the
        historical behavior, so a third-party executor keeps whatever
        it had rather than silently losing the primer's curl section.
        """
        return getattr(self._executor, "supports_commands", True)

    @property
    def caps(self) -> Capabilities:
        return self._provider.caps

    @property
    def autocheckpoint(self) -> bool:
        """Whether each successful mutating tool call commits. Settable:
        flip to False for turn-granularity commit policies (the agex
        model — one commit per agent turn), where the embedder or an
        adapter hook calls :meth:`checkpoint` at turn boundaries.
        Tradeoff: kvgit's staged buffer is in-memory, so deferring
        commits means a crash can lose the current turn's work."""
        return self._autocheckpoint

    @autocheckpoint.setter
    def autocheckpoint(self, value: bool) -> None:
        self._autocheckpoint = (
            bool(value) and self._provider.caps.versioned and not self._frozen
        )

    @property
    def head(self) -> str | None:
        """Id of the current (latest) checkpoint — pins the state a
        read-only call observed, since reads never move it. ``None``
        for unversioned providers. Caveat: staged-but-uncommitted
        changes (turn mode, manual ``ws.fs`` writes) are NOT in the
        head — check :attr:`dirty`; the pin is exact iff clean."""
        if not self._provider.caps.versioned:
            return None
        return self._provider.head

    @property
    def dirty(self) -> bool:
        """Staged-but-uncommitted changes exist (always False without
        ``caps.staging``)."""
        return self._provider.dirty

    @property
    def frozen(self) -> bool:
        """This workspace is a snapshot at a tag (see :meth:`at_tag`).

        Reads work; nothing can be written or committed.
        ``autocheckpoint`` is forced off, the write tools refuse, and
        the executor sees a read-only filesystem — so a shell redirect
        or ``open(..., "w")`` from agent code fails where it happens
        instead of staging a change that could never land."""
        return self._frozen

    @property
    def head_tree(self) -> str | None:
        """Content hash of the current head — the identity of *what the
        files and cache are*, where :attr:`head` identifies the point in
        history. Equal trees mean identical content; the converse does
        not hold, since a rewrite is stamped with its own time (see
        :class:`~nontainer.protocol.CheckpointInfo`). ``None`` for an
        unversioned provider, an empty history, or a provider that keeps
        no such hash. Staged changes are not in it: check
        :attr:`dirty`."""
        if not self._provider.caps.versioned:
            return None
        for entry in self.history(limit=1):
            return entry.tree
        return None

    @property
    def cache_enabled(self) -> bool:
        return self._cache_enabled

    @property
    def python_config(self) -> PythonConfig:
        return self._python_config

    @property
    def lock(self) -> threading.RLock:
        """EXTENSION SURFACE: the workspace's single-writer lock.
        Mutating public methods hold it; hold it yourself for
        host-side or extension work that mutates the workspace
        (``ws.fs`` writes, ``ws.cache`` mutation, multi-step
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
            self._sync_executor_if_stale()
            was_dirty = self._provider.dirty
            try:
                result = self._executor.exec_shell(command)
            except PermissionError as e:
                return TerminalResult(
                    stdout="", exit_code=1, stderr=self._refused_frozen(e)
                )
            torn = self._absorb_or_unwind(was_dirty)
            self._save_cwd()
            if torn is not None:
                stderr = f"{result.stderr}\n{torn}" if result.stderr else torn
                return replace(result, exit_code=result.exit_code or 1, stderr=stderr)
            cp = self._maybe_checkpoint("terminal")
        return replace(result, checkpoint=cp) if cp else result

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
        checkpoints), stdlib ``open()`` etc. routed to the workspace
        fs, and imports from ``helpers/`` on the fs. Never raises for
        sandboxed-code failure — check ``error`` / truthiness.
        """
        with self._lock:
            self._check_open()
            was_dirty = self._provider.dirty
            try:
                result = self.exec_python(code, inputs=inputs)
            except PermissionError as e:
                return PythonResult(stdout="", error=self._refused_frozen(e))
            torn = self._absorb_or_unwind(was_dirty)
            self._save_cwd()
            if torn is not None:
                error = f"{result.error}\n\n{torn}" if result.error else torn
                return replace(result, error=error)
            result = self._materialize_ui(result)
            cp = self._maybe_checkpoint("run_python")
        return replace(result, checkpoint=cp) if cp else result

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

        Runs before the checkpoint so the artifacts belong to the call
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
            # Artifact writes go through the public write_file, which
            # checkpoints for itself. Suppressed: they are part of THIS
            # call and ride its commit.
            with self._one_checkpoint():
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

    def exec_python(
        self,
        code: str,
        *,
        inputs: Mapping[str, Any] | None = None,
        stdin: str | None = None,
        argv: list[str] | None = None,
        echo: Literal["none", "last", "all"] | None = None,
        view: "ViewSpec | None" = None,
    ) -> PythonResult:
        """EXTENSION SURFACE: the raw execution path — no checkpoint,
        no lock. For embedders composing execution features on top of
        the workspace; most callers want :meth:`run_python`. Consumers:
        ``run_python`` itself, the terminal ``python`` builtin, and the
        apps dispatch (which passes a ``view`` for restricted handler
        execution — a read-only fs/cache view, a tighter budget,
        contract classes).

        ``view`` (see :class:`~nontainer.executor.ViewSpec`) requests a
        restricted, budgeted execution; it is executor-neutral (no
        sandbox object crosses the seam). ``echo`` overrides
        expression-echo for this call (``None`` = ``PythonConfig.echo``;
        script surfaces pass ``"none"``); ``stdin``/``argv`` expose the
        synthetic ``sys`` (the terminal ``python`` builtin wires the
        pipeline in). Safe to call concurrently — a ``view`` mints a
        fresh sandbox per call (frozen app serving relies on this);
        callers whose work mutates the workspace serialize via
        :attr:`lock`.

        Delegates to the executor (``LocalExecutor.exec_python`` —
        where the namespace assembly and rendering live)."""
        # The chokepoint for python execution — run_python, the
        # terminal ``python`` builtin, and apps dispatch all land here,
        # so a host-side write is visible to every one of them.
        self._sync_executor_if_stale()
        return self._executor.exec_python(
            code,
            inputs=inputs,
            stdin=stdin,
            argv=argv,
            echo=echo,
            view=view,
        )

    def _python_command(self, ctx: Any) -> Any:
        """The reserved ``python`` terminal builtin: a thin bridge over
        ``exec_python`` with script semantics — stdout flows to the
        pipeline, errors become exit code 1 + stderr, and the result
        namespace is deliberately DROPPED (pipelines are text;
        namespace-out belongs to the direct ``run_python`` surface).

        Forms: ``python -c 'code'`` | ``python file.py`` | piped stdin.
        Piped input reaches the code as ``sys.stdin`` (real-shell
        idiom: ``cat data | python script.py``), and ``sys.argv`` is
        populated — via sandtrap's synthetic ``sys``.
        """
        from termish import CommandResult

        args = list(ctx.args)
        # argv is always set so `sys`/argv are available in every form.
        if args and args[0] == "-c":
            if len(args) < 2:
                return CommandResult(exit_code=2, stderr="python: -c needs code")
            code = args[1]
            argv = ["-c", *args[2:]]
            stdin = ctx.stdin.read()  # piped data (empty when no pipe)
        elif args and args[0] == "-":
            # explicit "read program from stdin"; trailing args → argv
            code = ctx.stdin.read()
            if not code.strip():
                return CommandResult(exit_code=2, stderr="python: no code on stdin")
            argv = ["-", *args[1:]]
            stdin = ""  # program consumed stdin
        elif args and args[0].startswith("-"):
            return CommandResult(
                exit_code=2,
                stderr=f"python: unsupported option {args[0]!r} "
                "(only -c and - are supported)",
            )
        elif args:
            path = args[0]
            try:
                code = self._fs.read(path).decode("utf-8")
            except Exception as e:
                return CommandResult(exit_code=1, stderr=f"python: {path}: {e}")
            argv = [path, *args[1:]]
            stdin = ctx.stdin.read()
        else:
            code = ctx.stdin.read()  # stdin IS the code here (consumed)
            if not code.strip():
                return CommandResult(
                    exit_code=2, stderr="python: no code (use -c, a file, or stdin)"
                )
            argv = [""]
            stdin = ""

        # echo="none": script semantics by contract — a bare trailing
        # expression must not inject repr lines into pipelines
        result = self.exec_python(code, stdin=stdin, argv=argv, echo="none")
        ctx.stdout.write(result.stdout)
        if result.error is not None:
            return CommandResult(exit_code=1, stderr=result.error)
        if result.stderr:
            return CommandResult(exit_code=0, stderr=result.stderr)
        return None

    def register_command(self, name: str, fn: Callable[..., Any]) -> None:
        """Add a terminal command after construction (termish
        ``CommandFunc`` signature). Used by extras (e.g. apps' `curl`);
        also public for embedders. Reserved names and collisions with
        existing injections are rejected."""
        if name in RESERVED_COMMANDS:
            raise ValueError(f"Reserved terminal command name: {name!r}")
        if name in self._commands:
            raise ValueError(f"Terminal command already registered: {name!r}")
        self._commands[name] = fn

    # ------------------------------------------------------------------
    # direct (host-side) access
    # ------------------------------------------------------------------

    @property
    def fs(self) -> Any:
        """The termish-protocol filesystem, for host-side reads/writes
        (seeding inputs, harvesting artifacts) without the sandbox.
        Bypasses the workspace's single-writer lock — a host thread
        writing here while agent calls run holds :attr:`lock`.

        Writes through this handle mark a remote executor's view stale;
        the next execution re-syncs it, so a host-written file is
        visible to the guest's very next ``cat`` (see
        :class:`_SyncingFS`). Reads pass through untouched."""
        return self._public_fs

    def _mark_executor_stale(self) -> None:
        """Provider state moved without the executor seeing it; the
        next execution must refresh the guest first."""
        self._executor_stale = True

    def _sync_executor_if_stale(self) -> None:
        """Bring a remote executor's view current, once, right before
        it is used. No-op for ``LocalExecutor``, whose writes are
        already write-through.

        Cleared BEFORE the push, then RESTORED if the push raises.
        Clearing first is what keeps a write that lands mid-sync
        marked — it re-flags and earns its own sync — but a sync that
        fails leaves the guest exactly as stale as it was, so the flag
        has to come back or the retry would run against the old tree
        believing itself current (PR #23 review). The executor may
        still be recoverable: ``DudExecutor.sync`` handles a lost
        session itself, and what propagates here is the harder class
        (tree read, archive, push) where a caller retry is the point.
        """
        if not self._executor_stale:
            return
        self._executor_stale = False
        try:
            self._executor.sync()
        except BaseException:
            self._executor_stale = True
            raise

    def write_file(self, path: str, content: str | bytes) -> WriteOutcome:
        """Write a file (parents created, overwrites). The quoting-free
        alternative to shell redirects for multiline content; exposed
        by adapters as the ``file_write`` tool. Checkpointed."""
        data = content.encode() if isinstance(content, str) else content
        with self._lock:
            self._check_open()
            self._check_writable("file_write")
            created = not self._fs.exists(path)
            # PurePosixPath: workspace paths are POSIX regardless of host OS
            parent = str(PurePosixPath(path).parent)
            if parent not in (".", "/", ""):
                self._fs.makedirs(parent, exist_ok=True)
            self._fs.write(path, data)
            # host-side write behind the executor's back: flag it, and
            # the next execution syncs (no-op for LocalExecutor)
            self._mark_executor_stale()
            return WriteOutcome(
                path=path,
                size=len(data),
                created=created,
                checkpoint=self._maybe_checkpoint("file_write"),
            )

    def edit_file(
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
        lines?" snippet) otherwise. Checkpointed when it changes the
        file."""
        from .editing import EditError, apply_edit

        with self._lock:
            self._check_open()
            self._check_writable("file_edit")
            try:
                text = self._fs.read(path).decode("utf-8")
            except Exception as e:
                raise WorkspaceError(f"cannot read {path!r}: {e}") from e
            try:
                outcome = apply_edit(
                    text, old_string, new_string, replace_all=replace_all, path=path
                )
            except EditError as e:
                raise WorkspaceError(str(e)) from e
            if outcome.count:
                self._fs.write(path, outcome.content.encode())
                self._mark_executor_stale()  # see write_file()
                cp = self._maybe_checkpoint("file_edit")
                if cp:
                    outcome = replace(outcome, checkpoint=cp)
            return outcome

    def put(self, src: str | Path, dest: str | None = None) -> WriteOutcome:
        """Copy a host file INTO the workspace ("upload").

        Sugar over ``ws.fs.write`` — whole-bytes, so sized for
        documents/datasets, not multi-GB blobs (use a :class:`Mount`
        for those). ``dest`` defaults to the source's basename at the
        workspace root; parent directories are created. Overwrites.
        """
        src_path = Path(src).expanduser()
        data = src_path.read_bytes()
        ws_path = dest or src_path.name
        with self._lock:
            self._check_open()
            self._check_writable("put")
            created = not self._fs.exists(ws_path)
            parent = str(PurePosixPath(ws_path).parent)
            if parent not in (".", "/", ""):
                self._fs.makedirs(parent, exist_ok=True)
            self._fs.write(ws_path, data)
            self._mark_executor_stale()  # see write_file()
            return WriteOutcome(
                path=ws_path,
                size=len(data),
                created=created,
                checkpoint=self._maybe_checkpoint("put"),
            )

    def get(self, src: str, dest: str | Path | None = None) -> bytes:
        """Copy a workspace file OUT ("download"). Returns the bytes;
        also writes them to ``dest`` on the host when given.

        Read-only against the workspace — never checkpoints.
        """
        self._check_open()
        data = self._fs.read(src)
        if dest is not None:
            out = Path(dest).expanduser()
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(data)
        return data

    def read_artifact(self, path: str) -> bytes | None:
        """An artifact's bytes, or ``None`` if it cannot be read.

        Shaped to be handed straight to the a2ui envelope, whose
        ``read_bytes`` parameter is exactly this signature::

            turn_to_a2ui(prose, artifacts, ws.read_artifact, file_url, ...)

        The ``None`` is the whole point. ``turn_to_a2ui`` owns no I/O
        policy on purpose and documents ``None`` as "unreadable, degrade
        gracefully" — but the obvious ``lambda p: ws.fs.read(p)`` raises
        ``FileNotFoundError`` for a missing artifact and breaks that
        never-raises guarantee mid-stream, in an egress path. Holding
        that contract here means each consumer does not have to restate
        it correctly.

        Returns bytes rather than a parsed payload deliberately. Every
        consumer in this stack parses for itself (a2ui degrades on
        malformed JSON rather than raising), and a typed loader would
        invite reading it as "give me my DataFrame back" — which a
        ``head(200)`` artifact cannot honour. ``ArtifactPath.kind``
        says how to interpret the bytes when you want to.

        Read-only; never checkpoints. Any path works, not only ``/ui``
        — artifact notes are the usual source, but nothing here needs
        to police that.
        """
        try:
            self._check_open()
            return self._fs.read(path)
        except Exception:  # noqa: BLE001 - unreadable IS the answer here
            return None

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

    def checkpoint(self, info: dict[str, Any] | None = None) -> str:
        with self._lock:
            self._check_writable("checkpoint")
            return self._provider.checkpoint(info)

    def restore(self, checkpoint_id: str) -> None:
        with self._lock:
            self._provider.restore(checkpoint_id)
            # provider state moved under the executor: flag its view,
            # and the next execution refreshes it (no-op for
            # LocalExecutor, which holds no copy)
            self._mark_executor_stale()

    def rollback(self, steps: int = 1) -> str:
        """Restore the Nth-previous checkpoint; returns its id.

        Sugar over ``history()`` + ``restore()``. The explicit
        ``{"tool": "init"}`` lifecycle checkpoint is the floor:
        rollback may target it, but never cross it into a provider's
        pre-workspace seed. Legacy histories without that exact marker
        retain their existing provider-history behavior.
        """
        if steps < 1:
            raise ValueError("steps must be >= 1")
        with self._lock:
            entries = list(self._provider.history(limit=steps + 1))
            if len(entries) <= steps:
                raise CheckpointNotFoundError(
                    f"Cannot roll back {steps} step(s): only "
                    f"{len(entries)} checkpoint(s) in history"
                )
            # Exact metadata equality is deliberate: an unrelated
            # checkpoint that merely includes tool="init" plus other
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
                raise CheckpointNotFoundError(
                    f"Cannot roll back {steps} step(s): the workspace "
                    "initialization checkpoint is the rollback floor"
                )
            target = entries[steps]
            self._provider.restore(target.id)
            self._mark_executor_stale()  # see restore()
            return target.id

    def history(self, *, limit: int | None = None) -> Iterable[CheckpointInfo]:
        return self._provider.history(limit=limit)

    def fork(self, name: str, *, at: str | None = None) -> "Workspace":
        """Independent session seeded from current state — or, with
        ``at``, from an earlier checkpoint of this session, leaving this
        session where it is. Inherits this workspace's construction
        settings (see :class:`_Settings`) — python config, mounts, root,
        executor factory — plus its terminal commands. Cost varies by
        backend (see ``caps.cheap_fork`` and the README tradeoffs).

        ``at`` is what "branch from where I published" wants: the
        child starts at that commit with everything the commit holds,
        and nothing here is rewound to get it there."""
        # Mutating despite appearances: providers may checkpoint pending
        # staged changes so the fork sees current state (kvgit does).
        with self._lock:
            # Providers written to the older fork(name) shape — a custom
            # one, say — must keep working for the call that has no
            # ``at``; only a fork from the past asks them for more.
            forked = (
                self._provider.fork(name)
                if at is None
                else self._provider.fork(name, at=at)
            )
        # Commands and autocheckpoint are replayed from the LIVE
        # attributes rather than from _settings, because both can change
        # after construction (see _Settings). register_command mutates
        # the command set; the reserved bridge names are stripped here
        # because __init__ re-adds them. autocheckpoint has a public
        # setter, and a fork of a turn-granularity session must stay in
        # turn granularity.
        # (A command closed over THIS workspace still points at it from
        # the fork — the fork-bleed tracked in
        # scratch/plan-http-unification.md, whose Phase 0 removes the
        # closure rather than rebinding it here.)
        user_commands = {
            k: v for k, v in self._commands.items() if k not in RESERVED_COMMANDS
        }
        return Workspace(
            forked,
            commands=user_commands,
            autocheckpoint=self._autocheckpoint,
            **self._settings.as_kwargs(),
        )

    def discard(self) -> None:
        """Drop writes since the last checkpoint (staging providers)."""
        with self._lock:
            self._provider.discard()
            self._mark_executor_stale()  # see restore()

    # ------------------------------------------------------------------
    # tags (gated by caps.tags)
    # ------------------------------------------------------------------

    def _require_tags(self, op: str) -> None:
        if not self._provider.caps.tags:
            raise NotSupportedError(
                f"{type(self._provider).__name__} has no tags: {op}() is not "
                "supported. Use the kvgit backend for named checkpoints."
            )

    def tag(
        self,
        name: str,
        *,
        info: dict[str, Any] | None = None,
        scope: str = "session",
    ) -> str:
        """Name the current state, immutably; returns the checkpoint id.

        Two scopes, and nontainer decides what each means rather than
        handing embedders a flat namespace to partition themselves:

        - ``scope="session"`` (default) — the name belongs to this
          session. :meth:`tags` lists only its own, another session's
          ``v1`` is a different tag, and deleting the session
          (:func:`delete_workspace`) deletes it. This is the checkpoint
          you want to be able to name later: "before the refactor".
        - ``scope="store"`` — the name belongs to no session. Every
          workspace on the store can list and read it, and it survives
          the deletion of the session that made it. This is a
          publication: the state an app serves, the snapshot a report
          links to, anything that must outlive the conversation.

        Tags never move: an existing name raises rather than being
        repointed (delete it and tag again, so the move is visible in
        the calling code). A tag also anchors garbage collection — the
        named checkpoint and its ancestry stay reachable for as long as
        the tag exists.

        Staged changes are committed first (``info={"tool": "tag"}``),
        the way :meth:`fork` does, so the name means what the caller saw
        rather than the last commit before it. Everything that can be
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
            if self._provider.dirty:
                self._provider.checkpoint(info={"tool": "tag", "name": name})
            return self._provider.tag(name, info=info, scope=scope)

    def tags(self, *, scope: str = "session") -> dict[str, str]:
        """Tag name → checkpoint id, for one scope (see :meth:`tag`)."""
        with self._lock:
            self._require_tags("tags")
            return self._provider.tags(scope=scope)

    def tag_info(self, name: str, *, scope: str = "session") -> TagInfo | None:
        """Describe one tag, or ``None`` if there is no such tag."""
        with self._lock:
            self._require_tags("tag_info")
            return self._provider.tag_info(name, scope=scope)

    def delete_tag(self, name: str, *, scope: str = "session") -> None:
        """Drop a tag. What it named survives only while something else
        still reaches it — a branch, or another tag."""
        with self._lock:
            self._check_open()
            self._require_tags("delete_tag")
            self._check_writable("delete_tag")
            self._provider.delete_tag(name, scope=scope)

    def at_tag(self, name: str, *, scope: str = "session") -> "Workspace":
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
        # Same replay as fork(): commands and autocheckpoint come from
        # the live attributes because both can change after construction
        # (see _Settings). autocheckpoint is forced off for a frozen
        # provider regardless; passing it keeps the two paths identical.
        user_commands = {
            k: v for k, v in self._commands.items() if k not in RESERVED_COMMANDS
        }
        return Workspace(
            frozen,
            commands=user_commands,
            autocheckpoint=self._autocheckpoint,
            **self._settings.as_kwargs(),
        )

    def diff(self, a: str, b: str) -> WorkspaceDiff:
        """File-level changes between two checkpoint ids: which
        workspace paths were added, removed and modified. Framework
        state — cache, cwd, the stored conversation — is not a file and
        never appears, and ``modified`` holds the paths whose BYTES
        differ: a file re-saved with the content it already had is not
        a change, though the store's own key diff counts the write."""
        with self._lock:
            self._require_tags("diff")
            return self._provider.diff(a, b)

    def changed_since(self, ref: str, *, scope: str = "session") -> WorkspaceDiff:
        """What the files look like now versus at ``ref``.

        ``ref`` is a tag name or a checkpoint id — the tag is tried
        first, in ``scope``, so the everyday spelling is
        ``ws.changed_since("v1")``. The comparison ends at the current
        head, so staged-but-uncommitted work is not in it (check
        :attr:`dirty`).
        """
        with self._lock:
            self._require_tags("changed_since")
            info = self._provider.tag_info(ref, scope=scope)
            return self._provider.diff(info.id if info else ref, self._provider.head)

    # ------------------------------------------------------------------
    # power modes / lifecycle
    # ------------------------------------------------------------------

    def mount(self) -> AbstractContextManager[Path]:
        """Expose the workspace at a real path (FUSE providers only)."""
        return self._provider.mount()

    def close(self) -> None:
        with self._lock:  # don't close the provider mid-call
            if not self._closed:
                self._closed = True
                # Settle a pending sync BEFORE close. A closing
                # executor may park its tree tagged with the provider
                # head for later affinity (DudExecutor.close); parking
                # a stale tree under a matching tag would let a future
                # session resume it and skip the push, silently losing
                # host-side writes. Best-effort: a failure here must
                # not block the provider close.
                try:
                    self._sync_executor_if_stale()
                except Exception:
                    pass
                # Executor.close is best-effort-must-not-raise by
                # contract, but executors are an extension surface —
                # a third-party one that breaks the contract must not
                # get to skip the provider close (a held kvgit store).
                # Warn rather than swallow: the violation is theirs to
                # fix (PR #19 review).
                try:
                    self._executor.close()
                except Exception:
                    import warnings

                    warnings.warn(
                        f"{type(self._executor).__name__}.close() raised — "
                        "Executor.close must not (best-effort by contract); "
                        "closing the provider anyway",
                        RuntimeWarning,
                        stacklevel=2,
                    )
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
        return getattr(self._executor, "_sandbox", None)

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

    def _save_cwd(self) -> bool:
        # Guarded: an unconditional write would dirty staging providers
        # on every call, turning read-only `ls` into a commit.
        try:
            cwd = self._fs.getcwd()
            if self._provider.kv.get(_CWD_KEY) != cwd:
                self._provider.kv[_CWD_KEY] = cwd
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
    def _one_checkpoint(self):
        """Suppress nested checkpoints for the duration of an operation.

        ``write_file`` checkpoints, by design — it is a tool in its own
        right. But when the *workspace* writes on a call's behalf (``ui``
        artifacts), those writes belong to that call, not to a
        ``file_write`` of their own. Without this, materializing two
        rich values committed twice under the wrong tool and left
        ``result.checkpoint`` None, which reads as "nothing was
        committed" while the head had in fact moved.
        """
        outer = self._defer_checkpoints
        self._defer_checkpoints = True
        try:
            yield
        finally:
            self._defer_checkpoints = outer

    def _maybe_checkpoint(self, tool: str) -> str | None:
        """Commit this call's staged changes. Returns the created
        commit's id, or None when nothing was committed (no changes,
        autocheckpoint off, unversioned provider, or a nested write
        inside an operation that will commit for itself)."""
        if self._defer_checkpoints:
            return None
        if self._autocheckpoint and self._provider.dirty:
            return self._provider.checkpoint(info={"tool": tool})
        return None

    def _absorb_executor_diff(self) -> str | None:
        """Land a remote executor's staged writes in the provider,
        BEFORE the checkpoint flow — so the normal atomic commit and
        ``result.checkpoint`` semantics apply unchanged whichever
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
        d = self._executor.diff()
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
        from .executor import _apply_diff

        _apply_diff(self._fs, d.writes, d.deletes)
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
        staging (autocheckpoint off, prior host writes) holds earlier
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
                "staged and would ride the next checkpoint"
            )


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
    autocheckpoint: bool = True,
    max_observation: int = 32_000,
    executor_factory: "Callable[[], Executor] | None" = None,
    root: str = "/workspace",
) -> Workspace:
    """Build a session's :class:`Workspace` (the one-liner entry point).

    Session resolution by backend:

    - ``"kvgit"``: one shared store at ``store`` (default
      ``~/.nontainer``); ``session`` is a branch. Forks share storage.
    - ``"dir"``: ``store/<session>/`` as a plain directory
      (``IsolatedFS``). No versioning; time-travel verbs raise.
    - ``"agentfs"``: ``store/<session>.db``, one AgentFS file per
      session (unversioned spike).

    ``provider`` overrides ``backend``/``store`` entirely (bring your
    own substrate). ``session`` is validated against ``SESSION_ID_RE``
    in all paths.

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

    if provider is None:
        validate_session_id(session)
        base = Path(store).expanduser() if store else Path.home() / ".nontainer"
        if backend == "dir":
            from .providers.dir import DirProvider

            provider = DirProvider(base / session, session=session)
        elif backend == "kvgit":
            from .providers.kvgit import KvgitProvider

            provider = KvgitProvider.open(base / "kvgit", session=session)
        elif backend == "agentfs":
            from .providers.agentfs import AgentFSProvider

            provider = AgentFSProvider(base / f"{session}.db", session=session)
        else:
            raise ValueError(f"Unknown backend: {backend!r}")

    return Workspace(
        provider,
        python=python,
        mounts=mounts,
        commands=commands,
        cache=cache,
        autocheckpoint=autocheckpoint,
        max_observation=max_observation,
        executor_factory=executor_factory,
        root=root,
    )


def delete_workspace(
    sessions: str | Iterable[str],
    *,
    store: str | Path | None = None,
    backend: Literal["kvgit", "dir", "agentfs"] = "kvgit",
) -> None:
    """Delete one or more sessions' entire stored state.

    The teardown counterpart to :func:`workspace`, and it dispatches by
    ``backend`` the same way — resolving the same ``store`` (default
    ``~/.nontainer``) to the same per-backend layout the factory built:

    - ``"kvgit"``: deletes the named branches from the shared store at
      ``store/kvgit``, and with each branch the session-scoped tags it
      owns (everything under ``<session>/``). Store-scoped tags are left
      alone: that scope exists so a publication outlives the session
      that made it, and its checkpoints stay reachable through the tag
      even once every branch that reached them is gone.
    - ``"dir"``: removes the ``store/<session>/`` directory trees.
    - ``"agentfs"``: unlinks the ``store/<session>.db`` files.

    Plural because a caller often owns more than one branch/dir/db per
    logical session (an app that publishes snapshot branches, a batch
    cleanup). ``sessions`` may be a single id or any iterable of ids.
    Deleting a name that doesn't exist is a no-op; so is deleting from
    a store that was never created — teardown is idempotent.

    This is a store-level operation, not a live-session one: close any
    open :class:`Workspace` on these sessions first (a kvgit store
    handle pins its branch). It does not touch anything a caller keeps
    *beside* the workspace store (an app's own db files, transcripts) —
    that bookkeeping is the caller's to clean up.
    """
    if isinstance(sessions, str):
        names: set[str] = {sessions}
    else:
        names = set(sessions)
    base = Path(store).expanduser() if store else Path.home() / ".nontainer"
    if backend == "dir":
        from .providers.dir import DirProvider

        DirProvider.delete(base, names)
    elif backend == "kvgit":
        from .providers.kvgit import KvgitProvider

        KvgitProvider.delete(base / "kvgit", names)
    elif backend == "agentfs":
        from .providers.agentfs import AgentFSProvider

        AgentFSProvider.delete(base, names)
    else:
        raise ValueError(f"Unknown backend: {backend!r}")
