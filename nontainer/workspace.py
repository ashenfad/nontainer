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
from contextlib import AbstractContextManager
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from types import ModuleType
from typing import TYPE_CHECKING, Any, Literal

from .cache import Cache
from .errors import CheckpointNotFoundError, NotSupportedError, WorkspaceError
from .protocol import Capabilities, CheckpointInfo, WorkspaceProvider

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
    versioned, NOT captured by checkpoints, and NOT copied by forks.
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
    must NOT inline this into the text observation (at most a
    ``[namespace: ui, df, n=3]`` note). Structured payloads reach the
    embedder as plain variables by convention — e.g. an A2UI adapter
    reads ``result.namespace.get("ui")`` — no bespoke emission channel,
    no schema imposed by core."""

    checkpoint: str | None = None
    """Id of the commit this call's autocheckpoint created (``None``
    when nothing was committed) — see ``TerminalResult.checkpoint``."""

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

        # autocheckpoint is meaningless (and forced off) when the
        # provider can't checkpoint.
        self._autocheckpoint = autocheckpoint and provider.caps.versioned

        # -- filesystem: provider fs, optionally wrapped with mounts --
        self._fs = self._build_fs(provider.fs, mounts or {})

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

        # -- workspace root dir: must exist before the executor opens
        # (a remote executor materializes its guest tree from it) and
        # before cwd lands there. Guarded like the cwd restore so
        # reopening an existing session stays read-only.
        if self._root != "/" and not self._fs.isdir(self._root):
            self._fs.makedirs(self._root, exist_ok=True)

        # -- stateful cwd: restore from framework key if present, else
        # start at the workspace root. Guarded so a no-op restore
        # doesn't dirty staging providers (which would turn read-only
        # tool calls into commits).
        stored_cwd = provider.kv.get(_CWD_KEY) or self._root
        if stored_cwd != "/":
            try:
                if self._fs.getcwd() != stored_cwd:
                    self._fs.chdir(stored_cwd)
            except Exception:
                pass  # path may no longer exist; start at the fs root

        # open() LAST: it may fork a persistent isolation worker (see
        # LocalExecutor.open), and opening after everything else means
        # no later __init__ failure can orphan it (PR #10 review).
        self._executor.open(
            ExecutionContext(
                fs=self._fs,
                kv=provider.kv,
                commands=self._commands,
                python_config=self._python_config,
                cache_enabled=self._cache_enabled,
                max_observation=self._max_observation,
                head=_state_identity(provider),
                root=self._root,
            )
        )

    # ------------------------------------------------------------------
    # construction helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_fs(base: Any, mounts: Mapping[str, Mount]) -> Any:
        if not mounts:
            return base
        from monkeyfs import IsolatedFS, MountFS, ReadOnlyFS

        mounted: dict[str, Any] = {}
        for point, mount in mounts.items():
            if point == "/" or not point.startswith("/"):
                raise ValueError(
                    f"Mount points must be absolute and not '/': {point!r}"
                )
            real = Path(mount.path).expanduser().resolve()
            if not real.is_dir():
                raise ValueError(f"Mount source is not a directory: {real}")
            sub: Any = IsolatedFS(str(real))
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
        self._autocheckpoint = bool(value) and self._provider.caps.versioned

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
            was_dirty = self._provider.dirty
            result = self._executor.exec_shell(command)
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
            result = self.exec_python(code, inputs=inputs)
            torn = self._absorb_or_unwind(was_dirty)
            self._save_cwd()
            if torn is not None:
                error = f"{result.error}\n\n{torn}" if result.error else torn
                return replace(result, error=error)
            cp = self._maybe_checkpoint("run_python")
        return replace(result, checkpoint=cp) if cp else result

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
        writing here while agent calls run holds :attr:`lock`."""
        return self._fs

    def write_file(self, path: str, content: str | bytes) -> WriteOutcome:
        """Write a file (parents created, overwrites). The quoting-free
        alternative to shell redirects for multiline content; exposed
        by adapters as the ``file_write`` tool. Checkpointed."""
        data = content.encode() if isinstance(content, str) else content
        with self._lock:
            self._check_open()
            created = not self._fs.exists(path)
            # PurePosixPath: workspace paths are POSIX regardless of host OS
            parent = str(PurePosixPath(path).parent)
            if parent not in (".", "/", ""):
                self._fs.makedirs(parent, exist_ok=True)
            self._fs.write(path, data)
            # host-side write behind the executor's back: refresh its
            # view (no-op for LocalExecutor)
            self._executor.sync()
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
                self._executor.sync()  # see write_file()
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
            created = not self._fs.exists(ws_path)
            parent = str(PurePosixPath(ws_path).parent)
            if parent not in (".", "/", ""):
                self._fs.makedirs(parent, exist_ok=True)
            self._fs.write(ws_path, data)
            self._executor.sync()  # see write_file()
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
        return Cache(self._provider.kv)

    # ------------------------------------------------------------------
    # versioning (gated by caps; see protocol.py)
    # ------------------------------------------------------------------

    def checkpoint(self, info: dict[str, Any] | None = None) -> str:
        with self._lock:
            return self._provider.checkpoint(info)

    def restore(self, checkpoint_id: str) -> None:
        with self._lock:
            self._provider.restore(checkpoint_id)
            # provider state moved under the executor: refresh its view
            # (no-op for LocalExecutor, which holds no copy)
            self._executor.sync()

    def rollback(self, steps: int = 1) -> str:
        """Restore the Nth-previous checkpoint; returns its id.
        Sugar over ``history()`` + ``restore()``."""
        if steps < 1:
            raise ValueError("steps must be >= 1")
        with self._lock:
            entries = list(self._provider.history(limit=steps + 1))
            if len(entries) <= steps:
                raise CheckpointNotFoundError(
                    f"Cannot roll back {steps} step(s): only "
                    f"{len(entries)} checkpoint(s) in history"
                )
            target = entries[steps]
            self._provider.restore(target.id)
            self._executor.sync()  # see restore()
            return target.id

    def history(self, *, limit: int | None = None) -> Iterable[CheckpointInfo]:
        return self._provider.history(limit=limit)

    def fork(self, name: str) -> "Workspace":
        """Independent session seeded from current state. Inherits this
        workspace's python config, commands, and settings. Cost varies
        by backend (see ``caps.cheap_fork`` and the README tradeoffs)."""
        # Mutating despite appearances: providers may checkpoint pending
        # staged changes so the fork sees current state (kvgit does).
        with self._lock:
            forked = self._provider.fork(name)
        user_commands = {
            k: v for k, v in self._commands.items() if k not in RESERVED_COMMANDS
        }
        return Workspace(
            forked,
            python=self._python_config,
            commands=user_commands,
            cache=self._cache_enabled,
            autocheckpoint=self._autocheckpoint,
            max_observation=self._max_observation,
            executor_factory=self._executor_factory,
            root=self._root,
        )

    def discard(self) -> None:
        """Drop writes since the last checkpoint (staging providers)."""
        with self._lock:
            self._provider.discard()
            self._executor.sync()  # see restore()

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

    def _save_cwd(self) -> None:
        # Guarded: an unconditional write would dirty staging providers
        # on every call, turning read-only `ls` into a commit.
        try:
            cwd = self._fs.getcwd()
            if self._provider.kv.get(_CWD_KEY) != cwd:
                self._provider.kv[_CWD_KEY] = cwd
        except Exception:
            pass

    def _maybe_checkpoint(self, tool: str) -> str | None:
        """Commit this call's staged changes. Returns the created
        commit's id, or None when nothing was committed (no changes,
        autocheckpoint off, unversioned provider)."""
        if self._autocheckpoint and self._provider.dirty:
            return self._provider.checkpoint(info={"tool": tool})
        return None

    def _absorb_executor_diff(self) -> None:
        """Land a remote executor's staged writes in the provider,
        BEFORE the checkpoint flow — so the normal atomic commit and
        ``result.checkpoint`` semantics apply unchanged whichever
        executor produced the writes. ``None`` (LocalExecutor always;
        a remote executor after a read-only call) costs nothing and
        dirties nothing. Callers hold the lock."""
        d = self._executor.diff()
        if d is None:
            return
        from .executor import _apply_diff

        _apply_diff(self._fs, d.writes, d.deletes)

    def _absorb_or_unwind(self, was_dirty: bool) -> str | None:
        """:meth:`_absorb_executor_diff`, honoring the torn-call
        contract: ``HarvestLost`` means the guest died between a
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
            self._absorb_executor_diff()
            return None
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
