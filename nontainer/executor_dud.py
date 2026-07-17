"""DudExecutor: real execution over a dud guest (the second Executor).

Where :class:`~nontainer.executor.LocalExecutor` emulates a computer
against workspace state (termish shell, monkeyfs VFS, sandtrap
gates), ``DudExecutor`` runs a *real* one: a dud ``Session`` — real
bash, real python, a real scratch filesystem — materialized from the
provider's tree, harvested back as a diff. The versioning semantics
(checkpoint per call, fork, rollback) are untouched: they were always
the provider's, and the workspace stages the harvest through its
normal checkpoint flow.

Requires the ``dud`` extra (``pip install nontainer[dud]``); dud's
rung-1 subprocess backend has ZERO containment — agent code runs as
the host user with open egress (own-agent-own-laptop posture only;
see dud's DESIGN.md "Backend ladder").

Intended deltas vs LocalExecutor (pinned by tests/test_dud_executor.py):

- **Policy narrows to reality.** ``PythonConfig.modules`` grants and
  ``stdlib``/``network``/``isolation`` knobs are meaningless here —
  what's importable is what's installed in the guest environment, and
  the rung-1 guest IS the host env. ``host_objects`` survive: live
  objects become hostcall proxies (dud's allowlist boundary), plain
  data is injected per call as inputs.
- **stderr merges into stdout.** A real terminal produces one
  transcript; ``TerminalResult.stderr``/``PythonResult.stderr`` stay
  empty (timeout notices excepted).
- **namespace narrows to codec values.** Outputs cross as dud's Value
  codec (json / bytes / file refs) — live Python objects don't leave
  the guest (DESIGN "Outputs: emits, not namespaces").
- **cache is opaque bytes host-side.** The guest pickles/unpickles;
  the host stores bytes and never unpickles guest bytes (the pickle
  rule). ``ws.cache`` reads of guest-written keys return ``bytes``.
- **ticks are gone**; ``PythonResult.ticks`` is always 0 (wall-clock
  timeout is the enforced budget). Injected terminal ``commands``
  (including apps' curl) don't exist in real bash.
"""

from __future__ import annotations

import io
import pickle
import shlex
import tarfile
import time
from collections.abc import Iterator, Mapping, MutableMapping
from typing import Any, Literal

from .cache import PREFIX
from .errors import NotSupportedError
from .executor import ExecutionContext, StagedDiff, _is_plain_data, _truncate
from .workspace import Isolation, PythonResult, TerminalResult


class _KvBytesCache(MutableMapping[str, bytes]):
    """dict[str, bytes] view over the provider kv for dud's cache plane.

    Guest keys map into nontainer's cache namespace (``__cache__/``
    prefix — see cache.py), so the guest's ``cache`` and the host's
    ``ws.cache`` are the same keyspace. Directionality of the pickle
    rule: host-written rich values are pickled here INTO the guest
    (serializing toward the untrusted side is safe); guest-written
    values arrive as opaque pickle bytes and are stored as-is — the
    host never unpickles them. nontainer's key rules (no ``__``
    prefix, no ``/``) are enforced guest-visibly only by the host
    ``Cache`` view for now; the dud runner's cache view is rule-blind
    (stage-3: move the rules guest-side)."""

    def __init__(self, kv: MutableMapping[str, Any]) -> None:
        self._kv = kv

    def __getitem__(self, key: str) -> bytes:
        value = self._kv[PREFIX + key]
        if isinstance(value, bytes):
            return value
        return pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)

    def __setitem__(self, key: str, value: bytes) -> None:
        self._kv[PREFIX + key] = value

    def __delitem__(self, key: str) -> None:
        del self._kv[PREFIX + key]

    def __iter__(self) -> Iterator[str]:
        plen = len(PREFIX)
        for k in list(self._kv.keys()):
            if k.startswith(PREFIX):
                yield k[plen:]

    def __len__(self) -> int:
        return sum(1 for k in self._kv.keys() if k.startswith(PREFIX))


class DudExecutor:
    """Executor over dud's subprocess backend (rung 1).

    Construct bare and pass as ``Workspace(..., executor=DudExecutor())``;
    the workspace binds state via :meth:`open`. ``root`` optionally
    pins the guest scratch directory (default: a temp dir the session
    cleans up)."""

    def __init__(self, *, root: str | None = None) -> None:
        self._root = root
        self._ctx: ExecutionContext | None = None
        self._session: Any | None = None
        self._work: str | None = None  # guest workspace dir (root/work)
        self._plain: dict[str, Any] = {}  # plain-data host_objects
        self._closed = False

    # -- lifecycle -------------------------------------------------------

    def open(self, context: ExecutionContext) -> None:
        from dud.backends.subprocess import Session

        self._ctx = context
        cfg = context.python_config
        # Live host objects cross as hostcall proxies (dud's own
        # method allowlist boundary; default = all public callables,
        # rung-1 cooperative posture). Plain data can't be proxied —
        # it rides into each exec as inputs instead (mirrors
        # LocalExecutor's direct namespace injection).
        live: dict[str, Any] = {}
        for name, obj in cfg.host_objects.items():
            if _is_plain_data(obj):
                self._plain[name] = obj
            else:
                live[name] = obj
        self._session = Session(
            root=self._root,
            host_objects=live,
            cache=_KvBytesCache(context.kv),
        )
        self._work = self._session.ping()["workspace"]
        self._push_tree()
        self._assert_guest_cwd()

    def close(self) -> None:
        if self._session is not None and not self._closed:
            self._closed = True
            try:
                self._session.close()
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
        raise NotSupportedError(
            "build_sandbox is a LocalExecutor surface (sandtrap sandbox "
            "objects); apps dispatch via dud is stage 3"
        )

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
        """Run code in a fresh guest runner (script model, literally:
        one interpreter per exec).

        ``sandbox``/``cache`` are LocalExecutor-flavored overrides;
        they cannot reach here through supported paths (apps is gated
        off by ``build_sandbox`` raising) and are ignored. ``echo`` is
        the guest runner's concern (it does last-expression echo);
        per-call override is not wired in v0. ``stdin``/``argv`` have
        no dud verb yet — and the terminal ``python`` builtin that
        used them doesn't exist here (real bash runs real python) —
        so they fail loud rather than silently dropping data."""
        ctx = self._require_ctx()
        if stdin is not None or argv is not None:
            raise NotSupportedError(
                "exec_python(stdin=/argv=) is not supported by DudExecutor "
                "(no wire support in dud v0; use terminal() — real bash "
                "pipes into real python)"
            )
        from dud.values import NotRepresentable

        merged = dict(self._plain)
        merged.update(inputs or {})
        start = time.monotonic()
        try:
            result = self._session.python(
                code,
                inputs=merged or None,
                timeout=ctx.python_config.timeout,
            )
        except NotRepresentable as exc:
            # Mirror LocalExecutor's contract for bad inputs (there:
            # unpicklable; here: outside the Value codec).
            raise TypeError(
                f"inputs value is not codec-representable ({exc}). "
                "DudExecutor inputs must be json-representable data or "
                "bytes; live resources belong in PythonConfig.host_objects."
            ) from exc
        duration = time.monotonic() - start

        error = None
        if result.error is not None:
            # The guest renders its own traceback (rendering happens
            # where the objects live); errors without one (Timeout,
            # RunnerCrash) read as "Type: message".
            tb = result.error.traceback.rstrip()
            error = tb or f"{result.error.etype}: {result.error.message}"

        stdout, trunc = _truncate(result.transcript, ctx.max_observation)
        return PythonResult(
            stdout=stdout,
            stderr="",  # merged into the transcript guest-side
            error=error,
            ticks=0,  # no tick machinery: wall-clock timeout is the budget
            duration=duration,
            truncated=trunc,
            namespace=dict(result.outputs),
        )

    # -- shell -----------------------------------------------------------

    def exec_shell(self, script: str) -> TerminalResult:
        """Real bash against the guest tree. Failure contract matches
        LocalExecutor's: never raises for command failure — where
        termish raises TerminalError and LocalExecutor folds it into
        (partial output, exit code, message), real bash's analog is a
        nonzero exit with everything in the merged transcript. Exit
        codes carry through untouched (127 for not-found, etc.)."""
        ctx = self._require_ctx()
        result = self._session.shell(script, timeout=ctx.python_config.timeout)
        self._mirror_cwd(result.cwd)
        stderr = ""
        if result.timed_out:
            stderr = f"timeout: script exceeded {ctx.python_config.timeout}s"
        stdout, trunc_out = _truncate(result.transcript, ctx.max_observation)
        stderr, trunc_err = _truncate(stderr, ctx.max_observation)
        return TerminalResult(
            stdout=stdout,
            exit_code=result.exit_code,
            stderr=stderr,
            truncated=trunc_out or trunc_err,
        )

    # -- staging -----------------------------------------------------------

    def diff(self) -> StagedDiff | None:
        """Harvest guest writes (rebase: the harvest becomes the new
        baseline, so each call yields only that call's changes).
        ``None`` for a clean harvest keeps read-only calls read-only
        (no provider dirtying, no phantom checkpoints)."""
        d = self._session.diff(rebase=True)
        if d.empty:
            return None
        return StagedDiff(writes=dict(d.writes), deletes=tuple(d.deletes))

    def sync(self) -> None:
        """Re-materialize the guest tree from the provider (wholesale:
        push_tree wipes and rebuilds — incremental shipping is a
        stage-3 economy), then re-assert the persisted cwd, which
        push_tree resets to the workspace root."""
        self._push_tree()
        self._assert_guest_cwd()

    # -- internals ---------------------------------------------------------

    def _require_ctx(self) -> ExecutionContext:
        if self._ctx is None or self._session is None:
            raise RuntimeError("DudExecutor is not open (Workspace calls open())")
        return self._ctx

    def _push_tree(self) -> None:
        """Tar the provider tree (via the termish-protocol fs, so
        staged-but-uncommitted state is included) and push it as the
        guest's new baseline."""
        fs = self._require_ctx().fs
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            for rel in fs.list("/", recursive=True):
                if fs.isfile("/" + rel):
                    data = fs.read("/" + rel)
                    info = tarfile.TarInfo(name=rel)
                    info.size = len(data)
                    tf.addfile(info, io.BytesIO(data))
        self._session.push_tree(buf.getvalue())

    def _assert_guest_cwd(self) -> None:
        """Point the guest shell at the host fs's cwd (best-effort:
        a cwd that doesn't exist guest-side stays at the root)."""
        try:
            cwd = self._require_ctx().fs.getcwd()
        except Exception:
            return
        rel = cwd.lstrip("/")
        if rel:
            self._session.shell(f"cd {shlex.quote(rel)}", timeout=10.0)

    def _mirror_cwd(self, guest_cwd: str) -> None:
        """The guest owns cwd within a session (real `cd`); mirror it
        onto the host fs after each shell call so Workspace._save_cwd
        persists it (and restore/fork land where the agent was).
        Best-effort: a directory born and entered in the same call
        isn't in the provider until its diff lands files there — the
        mirror catches up on the next shell call."""
        ctx = self._require_ctx()
        if not self._work or not guest_cwd.startswith(self._work):
            return
        rel = guest_cwd[len(self._work) :].lstrip("/")
        host = "/" + rel if rel else "/"
        try:
            if ctx.fs.getcwd() != host and ctx.fs.isdir(host):
                ctx.fs.chdir(host)
        except Exception:
            pass
