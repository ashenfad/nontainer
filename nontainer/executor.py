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
sanctioned concurrent path is ``exec_python`` with caller-supplied
sandboxes (frozen app serving), where every call brings its own
sandbox instance.
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
    Isolation,
    ModuleGrant,
    PythonConfig,
    PythonResult,
    TerminalResult,
    _render_error,
)


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
    """Workspace-relative path (no leading slash) -> full new content.
    Whole-file payloads, not patches — the wire-format decision (dud
    PLAN #1): one encoding both scan-diff and overlay harvest emit."""

    deletes: tuple[str, ...] = ()
    """Workspace-relative paths removed since the last harvest."""


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

    def build_sandbox(
        self,
        *,
        timeout: float | None = None,
        tick_limit: int | None = None,
        extra_classes: tuple[type, ...] = (),
        filesystem: Any | None = None,
        isolation: Isolation | None = None,
        cache_object: Any | None = None,
    ) -> Any:
        """A per-purpose execution environment sharing this executor's
        frozen ``PythonConfig`` (the apps extra is the consumer:
        tighter budgets, a read-only fs view, contract classes).

        Local-flavored slot: the return value is a sandtrap sandbox
        that only ``exec_python(sandbox=...)`` on the same executor
        understands. An executor without sandbox objects raises
        ``NotSupportedError``; the remote design re-cuts this into
        per-call budget/view overrides (dud PLAN, stage 2)."""
        ...

    def exec_python(
        self,
        code: str,
        *,
        inputs: Mapping[str, Any] | None = None,
        sandbox: Any | None = None,
        cache: Mapping[str, Any] | None = None,
        stdin: str | None = None,
        argv: list[str] | None = None,
        echo: Literal["none", "last", "all"] | None = None,
    ) -> PythonResult:
        """One scripted python execution against workspace state.

        ``inputs`` are picklable per-call data bound as top-level
        names; ``PythonConfig.host_objects`` and ``cache`` are
        injected per the frozen config; ``stdin``/``argv`` feed the
        synthetic ``sys``; ``echo`` overrides expression echo for this
        call. Agent-code failure is a result (``PythonResult.error``),
        never an exception. ``sandbox``/``cache`` overrides are
        products of :meth:`build_sandbox` — same local-flavored caveat.

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
        self._sandbox = self.build_sandbox()
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

    def build_sandbox(
        self,
        *,
        timeout: float | None = None,
        tick_limit: int | None = None,
        extra_classes: tuple[type, ...] = (),
        filesystem: Any | None = None,
        isolation: Isolation | None = None,
        cache_object: Any | None = None,
    ) -> Any:
        """See ``Workspace.build_sandbox`` (the documented extension
        surface, which delegates here) for the caller-facing contract.

        The built ``Policy`` is memoized per ``(timeout, tick_limit,
        extra_classes)`` — the registration loop is the expensive part,
        and the config is frozen, so the build is deterministic. That
        makes minting a fresh sandbox per request cheap (frozen app
        serving does), without sharing sandbox *instances* across
        concurrent executions. The memo is intentionally tolerant of
        races: a duplicate build is wasted work, not corruption."""
        from sandtrap import sandbox

        ctx = self._require_ctx()
        cfg = ctx.python_config
        # Coerce: extension-surface callers may pass a list, which
        # would be unhashable as part of the memo key (PR #7).
        extra_classes = tuple(extra_classes)
        key = (timeout, tick_limit, extra_classes)
        policy = self._policy_memo.get(key)
        if policy is None:
            policy = self._build_policy(
                timeout=timeout, tick_limit=tick_limit, extra_classes=extra_classes
            )
            self._policy_memo[key] = policy

        effective_isolation = isolation if isolation is not None else cfg.isolation
        rpc_handlers = None
        if effective_isolation != "none":
            rpc_handlers = {}
            if cache_object is not None:
                # a caller-supplied cache view (e.g. apps' read-only
                # wrapper) — the worker's `cache` proxy dispatches here
                rpc_handlers["cache"] = self._cache_rpc_handler(cache_object)
            elif ctx.cache_enabled:
                rpc_handlers["cache"] = self._cache_rpc_handler()
            for name, obj in cfg.host_objects.items():
                if not _is_plain_data(obj):
                    rpc_handlers[f"host:{name}"] = _host_object_rpc_handler(obj)

        return sandbox(
            policy,
            isolation=effective_isolation,
            mode="raw",
            filesystem=filesystem if filesystem is not None else ctx.fs,
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

        cfg = self._require_ctx().python_config
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
        sandbox: Any | None = None,
        cache: Mapping[str, Any] | None = None,
        stdin: str | None = None,
        argv: list[str] | None = None,
        echo: Literal["none", "last", "all"] | None = None,
    ) -> PythonResult:
        """See ``Workspace.exec_python`` (the documented extension
        surface, which delegates here) for the caller-facing contract."""
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

        sb = sandbox if sandbox is not None else self._sandbox
        # Process/kernel sandboxes expose an RPC-handler registry; live
        # objects cross as proxies, never by pickle. Duck-typed off the
        # SANDBOX (not the config): callers may pass an in-process
        # sandbox (apps do) while the executor default is isolated.
        bridged = hasattr(sb, "_rpc_handlers")

        for name, obj in ctx.python_config.host_objects.items():
            if bridged and not _is_plain_data(obj):
                from sandtrap import RpcProxyMarker

                # methods RPC to the parent's live object (attribute
                # READS don't cross — a documented limit of bridging)
                namespace[name] = RpcProxyMarker(target=f"host:{name}")
            else:
                namespace[name] = obj

        if cache is not None:
            if bridged and not _is_plain_data(cache):
                from sandtrap import RpcProxyMarker

                # contract: the caller built this sandbox with
                # cache_object=<this cache> so the handler is registered
                namespace["cache"] = RpcProxyMarker(
                    target="cache", wrapper="nontainer.cache:RemoteCache"
                )
            else:
                namespace["cache"] = cache
        elif ctx.cache_enabled:
            if not bridged:
                namespace["cache"] = Cache(ctx.kv)
            else:
                from sandtrap import RpcProxyMarker

                # wrapper (imported in the worker) restores mapping
                # syntax — a bare RpcProxy can't serve cache[k]
                namespace["cache"] = RpcProxyMarker(
                    target="cache", wrapper="nontainer.cache:RemoteCache"
                )
        start = time.monotonic()
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
