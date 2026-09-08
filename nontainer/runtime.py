"""Runtime: the executor half of a session.

``Workspace`` holds a session's state — the provider, the single-writer
lock, the commit flow, cwd, the cache key rules. ``Runtime`` holds
everything about *running code* against that state: which
:class:`~nontainer.protocol.Executor` is bound, the terminal command
registry, the shell environment, the python config, and the freshness
of the executor's view of the provider.

The division is the same one the ``Executor`` seam already draws, made
visible in the API:

- **Runtime-side**: executor construction and lifecycle, the raw
  execution calls, command registration, shell variables, and the
  stale/sync bookkeeping a remote executor needs.
- **Workspace-side**: the lock, the commit flow, cwd persistence, and
  the absorb-or-unwind handling of an executor's write harvest.
  Executors never commit; a runtime returns results and the workspace
  decides what becomes a commit.

A ``Runtime`` is normally built by ``Workspace.__init__`` and reached
as ``ws.runtime``. It is also constructible directly over an existing
workspace — including a frozen one — which is what serving a published
snapshot needs: a second execution environment over the same state,
with its own executor and its own budget, while the session's own
runtime keeps running.
"""

from __future__ import annotations

import re
import warnings
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, Literal

from .protocol import ExecutionContext, StagedDiff, ViewSpec
from .workspace import (
    Mount,
    PythonConfig,
    PythonResult,
    TerminalResult,
    Workspace,
    _frozen_fs,
    _state_identity,
)

if TYPE_CHECKING:
    from .protocol import Executor

RESERVED_COMMANDS = frozenset({"python", "python3"})
"""Terminal command names nontainer owns. ``python``/``python3`` are
the bridge into :meth:`Runtime.exec_python`; a caller cannot claim
them, and a fork does not inherit them (the new runtime re-adds its
own, bound to itself)."""

_SHELL_VAR_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class Runtime:
    """How code runs against one workspace's state.

    Args:
        ws: The workspace whose state executions see. Its filesystem,
            kv, root and frozen flag are what the executor binds to.
        executor: A ready :class:`~nontainer.protocol.Executor` for
            THIS runtime. An executor is stateful and bound to one
            session (it may own a subprocess or a guest VM), so an
            instance is never shared. Default: an in-process
            ``LocalExecutor``.
        python: Sandbox policy. ``None`` inherits the workspace's
            current config when it already has a runtime (serving a
            snapshot under the session's own policy), else the
            defaults.
        mounts: Real directories exposed to *this runtime's*
            executions only. A workspace composes its own mounts into
            the filesystem it hands over — so ``ws.files.fs`` and execution
            agree — and passes none here; a standalone runtime uses
            this to add mounts of its own.
        commands: Terminal commands available to shell executions
            (termish ``CommandFunc`` signature). Reserved names and the
            framework's ``ws-`` prefix are refused.
        max_observation: Observation budget in characters for rendered
            stdout/stderr.
    """

    def __init__(
        self,
        ws: Workspace,
        *,
        executor: "Executor | None" = None,
        python: PythonConfig | None = None,
        mounts: Mapping[str, Mount] | None = None,
        commands: Mapping[str, Callable[..., Any]] | None = None,
        max_observation: int = 32_000,
    ) -> None:
        from .executor import LocalExecutor

        self._ws = ws
        self._closed = False
        if python is not None:
            self._python_config = python
        else:
            existing = getattr(ws, "_runtime", None)
            self._python_config = (
                existing.python_config if existing is not None else PythonConfig()
            )
        self._max_observation = max_observation
        self._cache_enabled = ws._cache_enabled

        # The filesystem executions see. The workspace has already
        # composed its own mounts into the one it hands over; extra
        # mounts here are this runtime's alone and are deliberately NOT
        # visible through ``ws.files.fs``.
        self._fs = ws._fs
        if mounts:
            self._fs = Workspace._build_fs(
                self._fs, Workspace._normalize_mounts(mounts)
            )

        # -- terminal commands: user injections + the python bridge --
        user_commands = dict(commands or {})
        reserved = RESERVED_COMMANDS.intersection(user_commands)
        if reserved:
            raise ValueError(
                f"Reserved terminal command name(s): {sorted(reserved)}. "
                "'python' is nontainer's bridge into run_python."
            )
        # Same ws-* reservation as register_command: constructor
        # injections are equally public, and without this a
        # user-claimed ws-* name would collide halfway through a later
        # framework registration, leaving it partially configured.
        prefixed = sorted(k for k in user_commands if k.startswith("ws-"))
        if prefixed:
            raise ValueError(
                f"Reserved terminal command prefix: {prefixed} — the 'ws-' "
                "prefix names framework verbs (ws-git, ws-curl); "
                "rename yours."
            )
        user_commands["python"] = self._python_command
        user_commands["python3"] = self._python_command  # the reflex spelling
        self._commands = user_commands
        # Framework-owned commands (ws-git, ws-curl) and how to re-bind
        # them: a command closure captures the workspace it was built
        # for, so a fork/snapshot that merely copied the mapping would
        # dispatch into its parent — the fork-bleed. fork()/at_tag()
        # drop these names from the copy and rebuild them bound to the
        # new workspace instead. Populated via
        # register_command(rebind=...), never replayed as construction
        # arguments: like commands themselves, factories arrive after
        # construction.
        self._framework_commands: dict[str, Callable[[Workspace], None]] = {}
        # Shell environment for script executions: `$VAR` expansion on
        # the termish rung, exported into the guest on dud rungs.
        # Runtime-owned (not ExecutionContext) so post-construction
        # features can contribute — `enable_apps` publishes
        # `$APP_ORIGIN` here. Executors snapshot it per call;
        # fork()/at_tag() replay it like commands. Embedders may add
        # their own; the shell never writes back.
        self._shell_env: dict[str, str] = {}

        # Host-side writes move provider state behind a remote
        # executor's back; the workspace flags it and the next
        # execution syncs.
        self._executor_stale = False

        self._executor = executor if executor is not None else LocalExecutor()
        # open() LAST: it may fork a persistent isolation worker (see
        # LocalExecutor.open), so nothing after it can fail and orphan
        # one.
        self._executor.open(
            ExecutionContext(
                fs=_frozen_fs(self._fs, ws._frozen_at) if ws.frozen else self._fs,
                kv=ws._kv_view,
                commands=self._commands,
                shell_env=self._shell_env,
                python_config=self._python_config,
                cache_enabled=self._cache_enabled,
                max_observation=self._max_observation,
                head=_state_identity(ws._provider),
                root=ws.root,
                frozen=ws.frozen,
                workspace=ws,
            )
        )

    # ------------------------------------------------------------------
    # identity / capabilities
    # ------------------------------------------------------------------

    @property
    def workspace(self) -> Workspace:
        """The workspace whose state these executions see."""
        return self._ws

    @property
    def executor(self) -> "Executor":
        """The bound executor. EXTENSION SURFACE: read it to probe
        capabilities or to reach an implementation-specific handle;
        the runtime owns its lifecycle."""
        return self._executor

    @property
    def python_config(self) -> PythonConfig:
        """The sandbox policy these executions run under."""
        return self._python_config

    @property
    def cache_enabled(self) -> bool:
        """Whether the agent-facing ``cache`` exists for these
        executions. A workspace-construction choice (``cache=False``),
        read here because execution is what sees it: the sandbox binds
        no ``cache`` name, and ``ws.cache`` raises."""
        return self._cache_enabled

    @property
    def max_observation(self) -> int:
        """Observation budget in characters for rendered output."""
        return self._max_observation

    @property
    def supports_commands(self) -> bool:
        """Whether injected terminal commands reach the shell.

        An executor capability (``Executor.supports_commands``): true
        for the in-process termish shell, false for one running real
        bash in a guest. Tool descriptions gate on it — apps' fetch
        verb ``ws-curl`` is only worth teaching where it exists.

        Defaults to true for executors predating the flag: that's the
        historical behavior, so a third-party executor keeps whatever
        it had rather than silently losing the primer's fetch section.
        """
        return getattr(self._executor, "supports_commands", True)

    @property
    def supports_ws_verbs(self) -> bool:
        """Whether ``ws-*`` verbs ferry into the shell.

        True for guest-bridging executors (the dud rung ferries ws-git
        and ws-curl over the hostcall channel) even though
        ``supports_commands`` is false there — real bash has no command
        registry. Tool descriptions offer the portable verbs where
        either flag holds. Same probe ``register_wsgit`` gates on, in
        one place.
        """
        return hasattr(self._executor, "_guest_to_host")

    # ------------------------------------------------------------------
    # execution
    # ------------------------------------------------------------------

    def exec_shell(self, script: str) -> TerminalResult:
        """EXTENSION SURFACE: run a shell script — no lock, no
        commit. ``Workspace.terminal`` wraps this with the
        single-writer lock and the commit flow; most callers want that.

        The executor's view is brought current first, so a host-side
        write is visible to the guest's very next ``cat``."""
        self.sync_if_stale()
        return self._executor.exec_shell(script)

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
        """EXTENSION SURFACE: the raw execution path — no commit,
        no lock. For embedders composing execution features on top of
        the workspace; most callers want ``Workspace.run_python``.
        Consumers: ``run_python`` itself, the terminal ``python``
        builtin, and the apps dispatch (which passes a ``view`` for
        restricted handler execution — a read-only fs/cache view, a
        tighter budget, contract classes).

        ``view`` (see :class:`~nontainer.protocol.ViewSpec`) requests a
        restricted, budgeted execution; it is executor-neutral (no
        sandbox object crosses the seam). ``echo`` overrides
        expression-echo for this call (``None`` = ``PythonConfig.echo``;
        script surfaces pass ``"none"``); ``stdin``/``argv`` expose the
        synthetic ``sys`` (the terminal ``python`` builtin wires the
        pipeline in). Safe to call concurrently — a ``view`` mints a
        fresh sandbox per call (frozen app serving relies on this);
        callers whose work mutates the workspace serialize via
        ``Workspace.lock``.

        Delegates to the executor (``LocalExecutor.exec_python`` —
        where the namespace assembly and rendering live)."""
        # The chokepoint for python execution — run_python, the
        # terminal ``python`` builtin, and apps dispatch all land here,
        # so a host-side write is visible to every one of them.
        self.sync_if_stale()
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

    # ------------------------------------------------------------------
    # the command registry / shell environment
    # ------------------------------------------------------------------

    @property
    def commands(self) -> dict[str, Callable[..., Any]]:
        """The live terminal command mapping the executor dispatches
        through. Mutated by :meth:`register_command`; bound by
        reference into ``ExecutionContext``, so a registration after
        construction reaches the shell."""
        return self._commands

    @property
    def framework_commands(self) -> dict[str, Callable[[Workspace], None]]:
        """Framework-owned command names mapped to the factory that
        rebuilds each one bound to a different workspace. What a
        fork/snapshot replays instead of inheriting parent-bound
        closures."""
        return self._framework_commands

    def register_command(
        self,
        name: str,
        fn: Callable[..., Any],
        *,
        rebind: "Callable[[Workspace], None] | None" = None,
    ) -> None:
        """Add a terminal command after construction (termish
        ``CommandFunc`` signature). Used by extras (e.g. apps' `curl`);
        also public for embedders. Reserved names and collisions with
        existing injections are rejected.

        ``rebind`` marks a framework-owned command: a factory taking
        the new workspace (usually the same function that called this)
        that rebuilds the command bound to a fork/snapshot.
        Without it the fork would inherit the parent-bound closure and
        dispatch into the parent — the fork-bleed. Embedder commands
        omit it and copy across as-is.

        The ``ws-`` prefix is reserved the same way: user-injected
        commands cannot claim it, so framework verbs never fight an
        agent's own command and no rename is ever needed. Framework
        registrations pass ``rebind`` and are exempt.
        """
        if name in RESERVED_COMMANDS:
            raise ValueError(f"Reserved terminal command name: {name!r}")
        if name.startswith("ws-") and rebind is None:
            raise ValueError(
                f"Reserved terminal command prefix: {name!r} — the 'ws-' "
                "prefix names framework verbs (ws-git, ws-curl); "
                "rename yours."
            )
        if name in self._commands:
            raise ValueError(f"Terminal command already registered: {name!r}")
        self._commands[name] = fn
        if rebind is not None:
            self._framework_commands[name] = rebind

    def shell_env(
        self, name: str | None = None, value: str | None = None
    ) -> "str | dict[str, str] | None":
        """The shell environment for script executions (``$VAR``
        expansion on the termish rung, exported into the guest on dud
        rungs).

        One verb, three forms: with a name and a value it publishes a
        variable, with a name alone it returns that variable's value
        (``None`` when unset), and with neither it returns the live
        mapping — mutable, which is how a fork replays a whole
        environment at once.

        Publishing is what post-construction features do
        (``enable_apps`` publishes ``$APP_ORIGIN``), and embedders may
        add their own. Forks and snapshots inherit a copy. The shell
        never writes back: this is configuration, not state.
        """
        if name is None:
            return self._shell_env
        if value is None:
            return self._shell_env.get(name)
        if not _SHELL_VAR_RE.fullmatch(name):
            raise ValueError(f"Invalid shell variable name: {name!r}")
        self._shell_env[name] = value
        return None

    def forkable_commands(self) -> dict[str, Callable[..., Any]]:
        """User commands safe to inherit: everything except the
        reserved bridge names (a new runtime re-adds them) and
        framework-owned commands, which the fork rebuilds bound to
        itself instead of inheriting parent-bound."""
        skip = RESERVED_COMMANDS | self._framework_commands.keys()
        return {k: v for k, v in self._commands.items() if k not in skip}

    # ------------------------------------------------------------------
    # the executor's view of provider state
    # ------------------------------------------------------------------

    @property
    def stale(self) -> bool:
        """Provider state has moved without the executor seeing it; the
        next execution syncs before it runs."""
        return self._executor_stale

    def mark_stale(self) -> None:
        """Flag the executor's view as out of date. Every path where
        provider state moves behind its back calls this — checkout /
        rollback / discard, the host-side write helpers, direct
        ``ws.files.fs`` writes."""
        self._executor_stale = True

    def sync_if_stale(self) -> None:
        """Bring a remote executor's view current, once, right before
        it is used. No-op for ``LocalExecutor``, whose writes are
        already write-through.

        Cleared BEFORE the push, then RESTORED if the push raises.
        Clearing first is what keeps a write that lands mid-sync
        marked — it re-flags and earns its own sync — but a sync that
        fails leaves the guest exactly as stale as it was, so the flag
        has to come back or the retry would run against the old tree
        believing itself current. The executor may still be
        recoverable: ``DudExecutor.sync`` handles a lost session
        itself, and what propagates here is the harder class (tree
        read, archive, push) where a caller retry is the point.
        """
        if not self._executor_stale:
            return
        self._executor_stale = False
        try:
            self._executor.sync()
        except BaseException:
            self._executor_stale = True
            raise

    def diff(self) -> "StagedDiff | None":
        """Harvest the executor's staged writes since the last harvest.
        ``None`` from an executor that writes through to the provider.
        The workspace absorbs the result before its commit flow."""
        return self._executor.diff()

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Release execution resources. Idempotent. Settles a pending
        sync first: a closing executor may park its tree tagged with
        the provider head for later affinity (``DudExecutor.close``),
        and parking a stale tree under a matching tag would let a
        future session resume it and skip the push, silently losing
        host-side writes. Best-effort — a failure there must not block
        the close."""
        if self._closed:
            return
        self._closed = True
        try:
            self.sync_if_stale()
        except Exception:
            pass
        # Executor.close is best-effort-must-not-raise by contract, but
        # executors are an extension surface — a third-party one that
        # breaks the contract must not get to stop the workspace from
        # closing its provider (a held kvgit store). Warn rather than
        # swallow: the violation is theirs to fix.
        try:
            self._executor.close()
        except Exception:
            warnings.warn(
                f"{type(self._executor).__name__}.close() raised — "
                "Executor.close must not (best-effort by contract); "
                "closing the provider anyway",
                RuntimeWarning,
                stacklevel=3,
            )
