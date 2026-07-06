"""Workspace: the top-level API. One instance == one session's world.

Design notes (see README "Design decisions"):

- **Script model.** ``run_python`` is a fresh sandboxed execution per
  call; persistence lives in ``cache`` (data), ``helpers/`` (code, via
  VFS imports), and files (artifacts). No resident interpreter state.
- **Sync core.** termish and kvgit are synchronous (sandtrap is NOT
  the constraint — it has ``aexec()``); async harnesses wrap calls in
  ``asyncio.to_thread`` (the adapters do this). One workspace must not
  be driven from two threads concurrently. Open question for v1.x: an
  ``arun_python`` passing through to sandtrap ``aexec`` would let
  *agent code* use top-level ``await`` (parallel host-object calls) —
  but it would still not be host-loop-safe end-to-end, since sandboxed
  file I/O hits sync kvgit under monkeyfs; async harnesses off-loop
  the call regardless.
- **Observations are bounded.** Tool results are truncated to
  ``max_observation`` characters with an explicit ``truncated`` flag —
  agents handle "output was cut" far better than silent loss or a
  blown context window.
- **cwd is stateful** across calls (like any other mutating terminal
  command) and persists via the ``__cwd__`` framework key in kv, so
  on versioned providers rollback also restores *where you were*.
"""

from __future__ import annotations

import contextlib
import io
import pickle
import time
import traceback
import warnings
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

Isolation = Literal["none", "process", "kernel"]

_CWD_KEY = "__cwd__"
_UNSET = object()
RESERVED_COMMANDS = frozenset({"python"})


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
    tick_limit: int = 1_000_000
    memory_limit_mb: int | None = None
    policy: Any | None = None
    """A pre-built ``sandtrap.Policy``; overrides everything above
    except ``host_objects``."""


def _render_prints(prints: list[tuple[Any, ...]], budget: int) -> str:
    """Budget-aware stdout reconstruction from snapshotted print args.

    Each print gets an even share of the budget (floored so early
    prints stay legible when there are many); every value renders via
    reprobate with a hard per-value budget. Approximation caveat:
    print's sep/end kwargs aren't snapshotted — args join with a
    space, prints with newlines (the overwhelmingly common case).
    """
    import reprobate

    per_print = max(200, budget // max(1, len(prints)))
    lines: list[str] = []
    used = 0
    for i, args in enumerate(prints):
        if used + per_print > budget:
            lines.append(f"[...{len(prints) - i} more print(s) elided]")
            break
        per_arg = max(40, per_print // max(1, len(args)))
        rendered = " ".join(reprobate.render(a, budget=per_arg) for a in args)
        lines.append(rendered)
        used += len(rendered) + 1
    return "\n".join(lines)


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def _render_error(exc: BaseException) -> str:
    return "".join(
        traceback.format_exception(type(exc), exc, exc.__traceback__)
    ).rstrip()


def _is_plain_data(obj: Any) -> bool:
    """Builtin-typed values need no policy registration."""
    return type(obj).__module__ == "builtins" and not isinstance(obj, ModuleType)


def _flatten_grants(cfg: "PythonConfig") -> list[ModuleGrant]:
    """Normalize ``cfg.modules`` to a flat ModuleGrant list: the stdlib
    set first (when enabled), then user entries — nested sequences
    (preset grant lists) flatten one level, bare modules wrap. A later
    registration of the same module name wins in sandtrap, so explicit
    user grants override the stdlib set's."""
    entries: list[ModuleType | ModuleGrant] = []
    if cfg.stdlib:
        from .presets import STDLIB

        entries.extend(STDLIB)
    for entry in cfg.modules:
        if isinstance(entry, (ModuleType, ModuleGrant)):
            entries.append(entry)
        elif isinstance(entry, Sequence) and not isinstance(entry, (str, bytes)):
            entries.extend(entry)
        else:
            raise TypeError(
                f"PythonConfig.modules entry {entry!r} is not a module, "
                "ModuleGrant, or sequence of them"
            )
    return [
        e if isinstance(e, ModuleGrant) else ModuleGrant(e) for e in entries
    ]


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
    ) -> None:
        self._provider = provider
        self._python_config = python or PythonConfig()
        self._cache_enabled = cache
        self._max_observation = max_observation
        self._closed = False

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
        self._commands = user_commands

        # -- python sandbox: policy + sandbox built once (frozen) --
        self._sandbox = self._build_sandbox()

        # -- stateful cwd: restore from framework key if present.
        # Guarded so a no-op restore doesn't dirty staging providers
        # (which would turn read-only tool calls into commits).
        stored_cwd = provider.kv.get(_CWD_KEY)
        if stored_cwd:
            try:
                if self._fs.getcwd() != stored_cwd:
                    self._fs.chdir(stored_cwd)
            except Exception:
                pass  # path may no longer exist; start at root

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

    def _build_sandbox(
        self,
        *,
        timeout: float | None = None,
        tick_limit: int | None = None,
        extra_classes: tuple[type, ...] = (),
        filesystem: Any | None = None,
    ) -> Any:
        """Build a sandbox from the frozen PythonConfig. The keyword
        overrides exist for the apps extra: handler sandboxes share the
        registration config but carry tighter budgets, contract classes
        (Request/Response/HttpError), and a read-only fs view for GET."""
        from sandtrap import Policy, sandbox

        cfg = self._python_config
        grants = _flatten_grants(cfg)
        if cfg.policy is not None:
            policy = cfg.policy
        else:
            policy = Policy(
                timeout=timeout if timeout is not None else cfg.timeout,
                tick_limit=tick_limit if tick_limit is not None else cfg.tick_limit,
                memory_limit=cfg.memory_limit_mb,
                allow_network=cfg.network,
            )
            for grant in grants:
                policy.module(
                    grant.module,
                    name=grant.name,
                    include=grant.include,
                    exclude=grant.exclude,
                    recursive=grant.recursive,
                    network_access=grant.network,
                    host_fs_access=grant.host_fs,
                )

        for klass in extra_classes:
            policy.cls(klass)

        # Live (non-plain-data) host objects need attribute-level policy.
        for name, obj in cfg.host_objects.items():
            if not _is_plain_data(obj):
                policy.module(obj, name=name)

        # Loud construction-time warning when a kernel sandbox's policy
        # degrades a kernel restriction (seccomp/Landlock are monotonic).
        if cfg.isolation == "kernel":
            grants_network = cfg.network or any(g.network for g in grants)
            grants_host_fs = any(g.host_fs for g in grants)
            if grants_network or grants_host_fs:
                degraded = [
                    n
                    for n, on in (
                        ("network", grants_network),
                        ("host-fs", grants_host_fs),
                    )
                    if on
                ]
                warnings.warn(
                    f"isolation='kernel' with {'/'.join(degraded)} grant(s): "
                    "the corresponding kernel restriction is disabled for the "
                    "ENTIRE worker; only Python-level gating remains for it.",
                    RuntimeWarning,
                    stacklevel=3,
                )

        rpc_handlers = None
        if cfg.isolation != "none" and self._cache_enabled:
            rpc_handlers = {"cache": self._cache_rpc_handler()}

        return sandbox(
            policy,
            isolation=cfg.isolation,
            mode="raw",
            filesystem=filesystem if filesystem is not None else self._fs,
            rpc_handlers=rpc_handlers,
            # Snapshot print() ARGUMENTS (objects, not text) so oversized
            # stdout can be re-rendered budget-aware via reprobate — see
            # _render_prints. Structural elision beats a mid-token cut.
            snapshot_prints=True,
        )

    def _cache_rpc_handler(self) -> Callable[[str, tuple, dict], Any]:
        """RPC dispatch onto the parent-side live cache (the agex
        pattern) for process/kernel isolation."""
        cache = Cache(self._provider.kv)

        def handler(method: str, args: tuple, kwargs: dict) -> Any:
            match method:
                case "getitem":
                    return cache[args[0]]
                case "setitem":
                    cache[args[0]] = args[1]
                    return None
                case "delitem":
                    del cache[args[0]]
                    return None
                case "iter":
                    return list(cache)
                case "len":
                    return len(cache)
                case "contains":
                    return args[0] in cache
                case _:
                    raise AttributeError(method)

        return handler

    # ------------------------------------------------------------------
    # identity
    # ------------------------------------------------------------------

    @property
    def session(self) -> str:
        return self._provider.session

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

    # ------------------------------------------------------------------
    # the two tools
    # ------------------------------------------------------------------

    def terminal(self, command: str) -> TerminalResult:
        """Execute a shell script (pipes, redirects, ``;``) against the
        workspace filesystem. Never raises for command failure — check
        ``exit_code`` / truthiness."""
        from termish import TerminalError, execute
        from termish.parser import ParseError

        self._check_open()
        try:
            output = execute(command, self._fs, commands=self._commands)
            exit_code, stderr = 0, ""
        except ParseError as e:
            output, exit_code, stderr = "", 2, f"parse error: {e}"
        except TerminalError as e:
            # termish >= 0.1.7 preserves command exit codes (127 for
            # not-found, a CommandResult's own code — curl's 22 survives)
            output, exit_code, stderr = e.partial_output, e.exit_code, e.message

        self._save_cwd()
        stdout, trunc_out = _truncate(output, self._max_observation)
        stderr, trunc_err = _truncate(stderr, self._max_observation)
        return TerminalResult(
            stdout=stdout,
            exit_code=exit_code,
            stderr=stderr,
            truncated=trunc_out or trunc_err,
            checkpoint=self._maybe_checkpoint("terminal"),
        )

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
        self._check_open()
        result = self._exec_python(code, inputs=inputs)
        self._save_cwd()
        cp = self._maybe_checkpoint("run_python")
        return replace(result, checkpoint=cp) if cp else result

    def _exec_python(
        self,
        code: str,
        *,
        inputs: Mapping[str, Any] | None = None,
        sandbox: Any | None = None,
        cache_override: Any = _UNSET,
    ) -> PythonResult:
        """Shared execution path (no checkpoint) — used by
        ``run_python``, the terminal ``python`` builtin, and the apps
        dispatch (which passes its own sandbox and, for GET, a
        read-only cache view via ``cache_override``)."""
        namespace: dict[str, Any] = {}

        for name, value in (inputs or {}).items():
            try:
                pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
            except Exception as exc:
                raise TypeError(
                    f"inputs[{name!r}] is not picklable data ({exc}). "
                    "inputs carry per-call data; live resources belong in "
                    "PythonConfig.host_objects."
                ) from exc
            namespace[name] = value

        namespace.update(self._python_config.host_objects)

        if cache_override is not _UNSET:
            if cache_override is not None:
                namespace["cache"] = cache_override
        elif self._cache_enabled:
            if self._python_config.isolation == "none":
                namespace["cache"] = Cache(self._provider.kv)
            else:
                from sandtrap import RpcProxyMarker

                namespace["cache"] = RpcProxyMarker(target="cache")

        sb = sandbox if sandbox is not None else self._sandbox
        stderr_buf = io.StringIO()
        start = time.monotonic()
        with contextlib.redirect_stderr(stderr_buf):
            exec_result = sb.exec(code, namespace=namespace)
        duration = time.monotonic() - start

        error = (
            _render_error(exec_result.error)
            if exec_result.error is not None
            else None
        )

        # Filter the outgoing namespace: sandtrap already excludes
        # injected names; defensively drop modules and _-prefixed too.
        out_ns = {
            k: v
            for k, v in exec_result.namespace.items()
            if not k.startswith("_")
            and not isinstance(v, ModuleType)
            and k not in namespace
        }

        raw_stdout = exec_result.stdout
        if (
            len(raw_stdout) > self._max_observation
            and getattr(exec_result, "prints", None)
        ):
            # Oversized stdout + snapshotted print objects: rebuild the
            # view with reprobate's structural elision (hard budget,
            # "...N more" markers) instead of a mid-token head-cut.
            stdout = _render_prints(exec_result.prints, self._max_observation)
            trunc_out = True
        else:
            stdout, trunc_out = _truncate(raw_stdout, self._max_observation)
        stderr, trunc_err = _truncate(stderr_buf.getvalue(), self._max_observation)
        return PythonResult(
            stdout=stdout,
            stderr=stderr,
            error=error,
            ticks=exec_result.ticks,
            duration=duration,
            truncated=trunc_out or trunc_err,
            namespace=out_ns,
        )

    def _python_command(self, ctx: Any) -> Any:
        """The reserved ``python`` terminal builtin: a thin bridge over
        ``_exec_python`` with script semantics — stdout flows to the
        pipeline, errors become exit code 1 + stderr, and the result
        namespace is deliberately DROPPED (pipelines are text;
        namespace-out belongs to the direct ``run_python`` surface).

        Forms: ``python -c 'code'`` | ``python file.py`` | piped stdin.
        """
        from termish import CommandResult

        args = list(ctx.args)
        if args and args[0] == "-c":
            if len(args) < 2:
                return CommandResult(exit_code=2, stderr="python: -c needs code")
            code = args[1]
        elif args:
            path = args[0]
            try:
                code = self._fs.read(path).decode("utf-8")
            except Exception as e:
                return CommandResult(exit_code=1, stderr=f"python: {path}: {e}")
        else:
            code = ctx.stdin.read()
            if not code.strip():
                return CommandResult(
                    exit_code=2, stderr="python: no code (use -c, a file, or stdin)"
                )

        result = self._exec_python(code)
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
        (seeding inputs, harvesting artifacts) without the sandbox."""
        return self._fs

    def write_file(self, path: str, content: str | bytes) -> WriteOutcome:
        """Write a file (parents created, overwrites). The quoting-free
        alternative to shell redirects for multiline content; exposed
        by adapters as the ``file_write`` tool. Checkpointed."""
        self._check_open()
        data = content.encode() if isinstance(content, str) else content
        created = not self._fs.exists(path)
        # PurePosixPath: workspace paths are POSIX regardless of host OS
        parent = str(PurePosixPath(path).parent)
        if parent not in (".", "/", ""):
            self._fs.makedirs(parent, exist_ok=True)
        self._fs.write(path, data)
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
        self._check_open()
        src_path = Path(src).expanduser()
        data = src_path.read_bytes()
        ws_path = dest or src_path.name
        created = not self._fs.exists(ws_path)
        parent = str(PurePosixPath(ws_path).parent)
        if parent not in (".", "/", ""):
            self._fs.makedirs(parent, exist_ok=True)
        self._fs.write(ws_path, data)
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
        keys, no ``__`` prefix, no ``/``."""
        if not self._cache_enabled:
            raise NotSupportedError(
                "cache is disabled for this workspace (cache=False)"
            )
        return Cache(self._provider.kv)

    # ------------------------------------------------------------------
    # versioning (gated by caps; see protocol.py)
    # ------------------------------------------------------------------

    def checkpoint(self, info: dict[str, Any] | None = None) -> str:
        return self._provider.checkpoint(info)

    def restore(self, checkpoint_id: str) -> None:
        self._provider.restore(checkpoint_id)

    def rollback(self, steps: int = 1) -> str:
        """Restore the Nth-previous checkpoint; returns its id.
        Sugar over ``history()`` + ``restore()``."""
        if steps < 1:
            raise ValueError("steps must be >= 1")
        entries = list(self._provider.history(limit=steps + 1))
        if len(entries) <= steps:
            raise CheckpointNotFoundError(
                f"Cannot roll back {steps} step(s): only "
                f"{len(entries)} checkpoint(s) in history"
            )
        target = entries[steps]
        self._provider.restore(target.id)
        return target.id

    def history(self, *, limit: int | None = None) -> Iterable[CheckpointInfo]:
        return self._provider.history(limit=limit)

    def fork(self, name: str) -> "Workspace":
        """Independent session seeded from current state. Inherits this
        workspace's python config, commands, and settings. Cost varies
        by backend (see ``caps.cheap_fork`` and the README tradeoffs)."""
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
        )

    def discard(self) -> None:
        """Drop writes since the last checkpoint (staging providers)."""
        self._provider.discard()

    # ------------------------------------------------------------------
    # power modes / lifecycle
    # ------------------------------------------------------------------

    def mount(self) -> AbstractContextManager[Path]:
        """Expose the workspace at a real path (FUSE providers only)."""
        return self._provider.mount()

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._provider.close()

    def __enter__(self) -> "Workspace":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

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
) -> Workspace:
    """Build a session's :class:`Workspace` (the one-liner entry point).

    Session resolution by backend:

    - ``"kvgit"``: one shared store at ``store`` (default
      ``~/.nontainer``); ``session`` is a branch. Forks share storage.
      (Not yet implemented — landing in the next milestone.)
    - ``"dir"``: ``store/<session>/`` as a plain directory
      (``IsolatedFS``). No versioning; time-travel verbs raise.
    - ``"agentfs"``: ``store/<session>.db``, one AgentFS file per
      session. (Spike milestone.)

    ``provider`` overrides ``backend``/``store`` entirely (bring your
    own substrate). ``session`` is validated against ``SESSION_ID_RE``
    in all paths.
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

            provider = AgentFSProvider(
                base / f"{session}.db", session=session
            )
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
    )
