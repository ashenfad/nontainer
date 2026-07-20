"""Executor: the execution seam.

nontainer separates two contracts that most sandboxes weld together:

1. ``WorkspaceProvider`` (protocol.py) — where state LIVES: fs + kv +
   versioning verbs.
2. ``Executor`` (here) — how code RUNS against that state: the python
   sandbox, the shell, worker lifecycle, result rendering.

Today there is one implementation. :class:`LocalExecutor` is the
in-process sandtrap + termish wiring that used to live inside
``Workspace`` — moved, not rewritten. The seam exists for a second
implementation: a real machine (subprocess, microVM) that materializes
the workspace tree, runs real bash / real python against it, and hands
back a diff of what changed. Because nontainer uses a script model —
no resident interpreter state; persistence lives in cache + files —
the machine is stateless between calls, so swapping it never touches
the versioning semantics (checkpoint per call, fork, rollback), which
were always properties of the state layer.

The split of responsibilities:

- **Executor-side**: sandbox/policy construction, worker lifecycle,
  namespace assembly (inputs, host-object bridging, cache injection),
  shell interpretation, and observation rendering (budget-aware prints,
  truncation) — rendering sits executor-side because a remote executor
  must render where the printed objects live.
- **Workspace-side**: the single-writer lock, the checkpoint flow, cwd
  persistence, the cache layer's key rules, apps dispatch. Executors
  never checkpoint; they produce results (and, remotely, diffs) for
  the workspace to commit.

Error taxonomy (stage-1 shape):

- Failure of the AGENT'S code is a result, never a host exception:
  ``PythonResult.error`` carries the rendered traceback — timeout /
  tick / memory kills included — and shell failure is an exit code.
- Failure of the EXECUTOR ITSELF (bad construction config, a crashed
  worker) raises. One informal family today (sandtrap/termish's own
  exceptions); a remote transport will formalize it — guest crash and
  lost connection must stay distinguishable from agent bugs.

Concurrency: executors inherit the workspace's single-writer model —
mutating calls arrive serialized under ``Workspace.lock``. The one
sanctioned concurrent path is ``exec_python(view=...)`` under frozen
app serving, which dispatches WITHOUT the workspace lock — an
executor must make that safe its own way: ``LocalExecutor`` mints a
fresh sandbox per view call; an executor multiplexing one transport
(``DudExecutor``'s single session channel) must serialize internally.
"""

from __future__ import annotations

import pickle
import time
import warnings
from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import dataclass
from types import ModuleType
from typing import Any, Literal, Protocol, runtime_checkable

from .cache import Cache

# Config/result types (and traceback rendering) stay in workspace.py:
# they are the public vocabulary both sides of the seam speak. The
# import is one-way — workspace.py only imports this module lazily.
from .workspace import (
    ModuleGrant,
    PythonConfig,
    PythonResult,
    TerminalResult,
    _render_error,
)


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


def _apply_diff(
    fs: Any, writes: Mapping[str, bytes], deletes: tuple[str, ...] | list[str]
) -> None:
    """Land a write/delete harvest on a workspace fs (parents created,
    missing deletes tolerated) — the single absorption loop shared by
    the workspace (``StagedDiff`` staging) and remote executors
    (mutating-view write-through), so the two paths can't drift."""
    from pathlib import PurePosixPath

    for rel, data in writes.items():
        path = "/" + rel.lstrip("/")
        # PurePosixPath: workspace paths are POSIX regardless of host OS
        parent = str(PurePosixPath(path).parent)
        if parent not in (".", "/", ""):
            fs.makedirs(parent, exist_ok=True)
        fs.write(path, data)
    for rel in deletes:
        try:
            fs.remove("/" + rel.lstrip("/"))
        except Exception:
            pass  # already gone (e.g. parent dir removed first)


@dataclass(frozen=True)
class StagedDiff:
    """A remote executor's write harvest.

    A remote executor accumulates writes in its own staging area (an
    overlay upperdir, a scan-diff) rather than writing through to the
    provider; :meth:`Executor.diff` returns them as a ``StagedDiff``
    and the workspace stages them into the provider before its normal
    checkpoint flow — so atomic commit + ``result.checkpoint``
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

    root: str = "/workspace"
    """The workspace root: the absolute VFS path agent-visible files
    live under — the ONE path contract shared across executors.
    ``LocalExecutor`` points sandtrap's module imports here; a VM
    executor mounts its guest workspace at this exact path, so an
    absolute path in agent code means the same file everywhere.
    ``"/"`` selects the pre-0.2 layout (files at the fs root — a VM
    guest can't mount there, so absolute paths diverge on VM rungs)."""


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


class _ReadOnlyCache(MutableMapping):
    """Read-only cache view for GET handlers (structural REST).

    MutableMapping so derived mutators (pop, clear, update, setdefault)
    route through ``__setitem__``/``__delitem__`` and raise
    ``PermissionError`` rather than ``AttributeError``. (Moved here from
    apps.dispatch: the executor now owns read-only-view construction.)
    """

    def __init__(self, cache: Mapping) -> None:
        self._cache = cache

    def __getitem__(self, key: str) -> Any:
        return self._cache[key]

    def __iter__(self):
        return iter(self._cache)

    def __len__(self) -> int:
        return len(self._cache)

    def __contains__(self, key: object) -> bool:
        return key in self._cache

    def get(self, key: str, default: Any = None) -> Any:
        return self._cache.get(key, default)

    def __setitem__(self, key: str, value: Any) -> None:
        raise PermissionError("cache is read-only in GET handlers")

    def __delitem__(self, key: str) -> None:
        raise PermissionError("cache is read-only in GET handlers")


@runtime_checkable
class Executor(Protocol):
    """Execution contract. See module docstring for the seam's shape.

    ``diff``/``sync`` exist for executors whose writes don't land in
    the provider directly. The workspace calls ``diff`` after every
    mutating exec (absorbing any harvest into the provider before the
    checkpoint flow) and ``sync`` whenever it changes provider state
    behind the executor's back (restore/rollback/discard, host-side
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
    apps primer advertises ``curl`` only where it exists.
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

        The result's ``checkpoint`` is ``None``: executors never
        commit; the workspace stamps checkpoints."""
        ...

    # -- shell -----------------------------------------------------------

    def exec_shell(self, script: str) -> TerminalResult:
        """One shell script (pipes, redirects, ``;``) against the
        workspace fs, with the context's injected commands available.
        Never raises for command failure — exit codes are results.
        ``checkpoint`` is ``None`` here too (see ``exec_python``)."""
        ...

    # -- staging (remote executors) ---------------------------------------

    def diff(self) -> StagedDiff | None:
        """Harvest writes staged executor-side since the last harvest
        (or ``sync``). Called by the workspace after every mutating
        exec, before its checkpoint flow.

        ``LocalExecutor`` returns ``None`` — its writes land in the
        provider the moment they happen (monkeyfs/termish write
        through), so there is nothing to harvest; ``None`` also means
        "nothing staged" from a remote executor after a read-only
        call, so the workspace's dirty check stays accurate."""
        ...

    def sync(self) -> None:
        """Refresh the executor's view of workspace state from the
        provider. Called after restore/rollback/discard and after
        host-side writes (``write_file``/``edit_file``/``put``) —
        every path where provider state moves without the executor
        seeing it. No-op for ``LocalExecutor``: there is no second
        copy. (Direct ``ws.fs`` writes bypass this — same caveat as
        the single-writer lock.)"""
        ...


# ----------------------------------------------------------------------
# rendering / policy helpers (moved verbatim from workspace.py)
# ----------------------------------------------------------------------


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


def _host_object_rpc_handler(obj: Any):
    """Parent-side dispatch for one bridged host object: the worker's
    proxy calls methods by name; the live object never crosses. Public
    methods only — the proxy can't reach dunders or privates."""

    def handler(method: str, args: tuple, kwargs: dict) -> Any:
        if method.startswith("_"):
            raise AttributeError(method)
        attr = getattr(obj, method)
        if not callable(attr):
            raise AttributeError(
                f"{method!r} is not a method (attribute reads don't cross "
                "the process-isolation bridge)"
            )
        return attr(*args, **kwargs)

    return handler


def _is_plain_data(obj: Any) -> bool:
    """Builtin-typed values need no policy registration."""
    return type(obj).__module__ == "builtins" and not isinstance(obj, ModuleType)


def _flatten_grants(cfg: PythonConfig) -> list[ModuleGrant]:
    """Normalize ``cfg.modules`` to a flat ModuleGrant list: the stdlib
    set first (when enabled), then user entries — nested sequences
    (preset grant lists) flatten one level, bare modules wrap. A later
    registration of the same module name wins in sandtrap, so explicit
    user grants override the stdlib set's."""
    from collections.abc import Sequence

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
    return [e if isinstance(e, ModuleGrant) else ModuleGrant(e) for e in entries]


# ----------------------------------------------------------------------
# the default implementation
# ----------------------------------------------------------------------


class LocalExecutor:
    """The in-process executor: sandtrap for python, termish for the
    shell, both over the workspace's monkeyfs-protocol filesystem.

    Writes land directly in the provider (monkeyfs/termish write
    through), so :meth:`diff`/:meth:`sync` are no-ops — atomicity and
    versioning are entirely the provider's staging + the workspace's
    checkpoint flow. Lifecycle: under process/kernel isolation the
    sandbox forks a persistent worker at :meth:`open` and reaps it at
    :meth:`close`.
    """

    # termish takes the injected mapping directly (see exec_shell), so
    # apps' curl is a real command here.
    supports_commands = True

    def __init__(self) -> None:
        self._ctx: ExecutionContext | None = None
        # build_sandbox memoizes built policies per parameter set (the
        # registration loop is the expensive part); open() seeds the
        # memo with the default sandbox's build.
        self._policy_memo: dict[Any, Any] = {}
        self._sandbox: Any | None = None

    # -- lifecycle -------------------------------------------------------

    def open(self, context: ExecutionContext) -> None:
        self._ctx = context
        self._sandbox = self._build_sandbox()
        # Process/kernel sandboxes fork a persistent worker. Entering
        # at workspace construction — typically before an embedder's
        # server threads exist — is deliberate (forking a multithreaded
        # process can deadlock on macOS); the workspace calls open()
        # LAST so no later construction failure can orphan the worker
        # (PR #10 review).
        if context.python_config.isolation != "none":
            self._sandbox.__enter__()

    def close(self) -> None:
        shutdown = getattr(self._sandbox, "shutdown", None)
        if callable(shutdown):  # process/kernel worker
            try:
                shutdown()
            except Exception:
                pass  # best-effort by contract: the provider closes next

    # -- python ----------------------------------------------------------

    def _build_sandbox(self, view: ViewSpec | None = None) -> Any:
        """Build a sandtrap sandbox for the executor's standard
        environment (``view=None``, the long-lived default built at
        :meth:`open`) or for a per-call restricted view (apps handler
        dispatch). Handlers inherit the workspace isolation (the
        symmetry rule) — a view never overrides it.

        The built ``Policy`` is memoized per ``(timeout, tick_limit,
        extra_classes)`` — the registration loop is the expensive part,
        and the config is frozen, so a fresh sandbox per handler call is
        cheap (a COW worker fork over a memoized policy). The memo is
        race-tolerant: a duplicate build is wasted work, not corruption.
        Read-only fs/cache views are realized here — ``ReadOnlyFS`` and
        the ro cache rpc handler — so the returned sandbox refuses
        writes on its own."""
        from sandtrap import sandbox

        ctx = self._require_ctx()
        cfg = ctx.python_config
        timeout = view.timeout if view else None
        tick_limit = view.tick_limit if view else None
        extra_classes = tuple(view.extra_classes) if view else ()
        key = (timeout, tick_limit, extra_classes)
        policy = self._policy_memo.get(key)
        if policy is None:
            policy = self._build_policy(
                timeout=timeout, tick_limit=tick_limit, extra_classes=extra_classes
            )
            self._policy_memo[key] = policy

        fs = ctx.fs
        if view is not None and view.readonly_fs:
            from monkeyfs import ReadOnlyFS

            fs = ReadOnlyFS(ctx.fs)

        # A read-only cache view is bridged to the worker via the rpc
        # handler here (the in-process case injects it in exec_python) —
        # both paths must agree on read-only-ness, keyed off the view.
        ro_cache = bool(view and view.readonly_cache and ctx.cache_enabled)
        rpc_handlers = None
        if cfg.isolation != "none":
            rpc_handlers = {}
            if ctx.cache_enabled:
                cache_obj = _ReadOnlyCache(Cache(ctx.kv)) if ro_cache else None
                rpc_handlers["cache"] = self._cache_rpc_handler(cache_obj)
            for name, obj in cfg.host_objects.items():
                if not _is_plain_data(obj):
                    rpc_handlers[f"host:{name}"] = _host_object_rpc_handler(obj)

        return sandbox(
            policy,
            isolation=cfg.isolation,
            mode="raw",
            filesystem=fs,
            rpc_handlers=rpc_handlers,
            # Snapshot print() ARGUMENTS (objects, not text) so oversized
            # stdout can be re-rendered budget-aware via reprobate — see
            # _render_prints. Structural elision beats a mid-token cut.
            snapshot_prints=True,
            # the sandbox-level default; script surfaces (terminal
            # python, app handlers) override per-exec with echo="none"
            echo=cfg.echo,
        )

    def _build_policy(
        self,
        *,
        timeout: float | None,
        tick_limit: int | None,
        extra_classes: tuple[type, ...],
    ) -> Any:
        """One policy build: registration loop + extra classes + live
        host objects + the kernel-degradation warning."""
        from sandtrap import Policy

        ctx = self._require_ctx()
        cfg = ctx.python_config
        grants = _flatten_grants(cfg)
        if cfg.policy is not None:
            # An embedder-supplied policy is theirs wholesale — including
            # module_root, which they must align with the workspace root
            # if they move it.
            policy = cfg.policy
        else:
            policy = Policy(
                timeout=timeout if timeout is not None else cfg.timeout,
                tick_limit=tick_limit if tick_limit is not None else cfg.tick_limit,
                memory_limit=cfg.memory_limit_mb,
                allow_network=cfg.network,
                # VFS imports resolve from the workspace root, so
                # `from helpers import x` finds <root>/helpers.py — the
                # same place agent open() sees it, and the same place a
                # VM guest resolves it from (parity across executors).
                module_root=ctx.root,
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
                    stacklevel=4,
                )

        return policy

    def _cache_rpc_handler(
        self, cache: Any | None = None
    ) -> Callable[[str, tuple, dict], Any]:
        """RPC dispatch onto a parent-side live cache (the agex
        pattern) for process/kernel isolation. Default: the
        workspace's own cache; callers may hand in a view (apps pass
        their read-only wrapper) — its exceptions cross to the worker
        and re-raise at the call site."""
        if cache is None:
            cache = Cache(self._require_ctx().kv)

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
        """See ``Workspace.exec_python`` (the documented extension
        surface, which delegates here) for the caller-facing contract."""
        from contextlib import nullcontext

        ctx = self._require_ctx()
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

        # A view mints a fresh, restricted sandbox (policy memoized);
        # otherwise the long-lived default built at open(). A view
        # sandbox under process/kernel isolation is its own worker —
        # enter/exit it per call (COW fork, ~ms); the default worker is
        # entered once at open().
        if view is not None:
            sb = self._build_sandbox(view)
            own_worker = hasattr(sb, "shutdown")
        else:
            sb = self._sandbox
            own_worker = False
        bridged = hasattr(sb, "_rpc_handlers")

        for name, obj in ctx.python_config.host_objects.items():
            if bridged and not _is_plain_data(obj):
                from sandtrap import RpcProxyMarker

                # methods RPC to the parent's live object (attribute
                # READS don't cross — a documented limit of bridging)
                namespace[name] = RpcProxyMarker(target=f"host:{name}")
            else:
                namespace[name] = obj

        # Cache injection mirrors _build_sandbox's rpc-handler choice:
        # a read-only view gets the read-only wrapper (in-process) or
        # the marker onto the ro rpc handler (bridged).
        ro_cache = bool(view and view.readonly_cache and ctx.cache_enabled)
        if ctx.cache_enabled:
            if not bridged:
                base = Cache(ctx.kv)
                namespace["cache"] = _ReadOnlyCache(base) if ro_cache else base
            else:
                from sandtrap import RpcProxyMarker

                # wrapper (imported in the worker) restores mapping
                # syntax — a bare RpcProxy can't serve cache[k]
                namespace["cache"] = RpcProxyMarker(
                    target="cache", wrapper="nontainer.cache:RemoteCache"
                )
        start = time.monotonic()
        with sb if own_worker else nullcontext():
            exec_result = sb.exec(
                code, namespace=namespace, stdin=stdin, argv=argv, echo=echo
            )
        duration = time.monotonic() - start

        error = (
            _render_error(exec_result.error) if exec_result.error is not None else None
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
        if len(raw_stdout) > ctx.max_observation and getattr(
            exec_result, "prints", None
        ):
            # Oversized stdout + snapshotted print objects: rebuild the
            # view with reprobate's structural elision (hard budget,
            # "...N more" markers) instead of a mid-token head-cut.
            stdout = _render_prints(exec_result.prints, ctx.max_observation)
            trunc_out = True
        else:
            stdout, trunc_out = _truncate(raw_stdout, ctx.max_observation)
        stderr, trunc_err = _truncate(exec_result.stderr, ctx.max_observation)
        return PythonResult(
            stdout=stdout,
            stderr=stderr,
            error=error,
            ticks=exec_result.ticks,
            duration=duration,
            truncated=trunc_out or trunc_err,
            namespace=out_ns,
        )

    # -- shell -----------------------------------------------------------

    def exec_shell(self, script: str) -> TerminalResult:
        from termish import TerminalError, execute
        from termish.parser import ParseError

        ctx = self._require_ctx()
        try:
            output = execute(script, ctx.fs, commands=ctx.commands)
            exit_code, stderr = 0, ""
        except ParseError as e:
            output, exit_code, stderr = "", 2, f"parse error: {e}"
        except TerminalError as e:
            # termish >= 0.1.7 preserves command exit codes (127 for
            # not-found, a CommandResult's own code — curl's 22 survives)
            output, exit_code, stderr = e.partial_output, e.exit_code, e.message

        stdout, trunc_out = _truncate(output, ctx.max_observation)
        stderr, trunc_err = _truncate(stderr, ctx.max_observation)
        return TerminalResult(
            stdout=stdout,
            exit_code=exit_code,
            stderr=stderr,
            truncated=trunc_out or trunc_err,
        )

    # -- staging (no-ops: writes land in the provider directly) -----------

    def diff(self) -> StagedDiff | None:
        return None

    def sync(self) -> None:
        pass

    # -- internals ---------------------------------------------------------

    def _require_ctx(self) -> ExecutionContext:
        if self._ctx is None:
            raise RuntimeError("LocalExecutor is not open (Workspace calls open())")
        return self._ctx
