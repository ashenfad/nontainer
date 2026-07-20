"""DudExecutor: real execution over a dud guest (the second Executor).

Where :class:`~nontainer.executor.LocalExecutor` emulates a computer
against workspace state (termish shell, monkeyfs VFS, sandtrap
gates), ``DudExecutor`` runs a *real* one: a dud ``Session`` — real
bash, real python, a real scratch filesystem — materialized from the
provider's tree, harvested back as a diff. The versioning semantics
(checkpoint per call, fork, rollback) are untouched: they were always
the provider's, and the workspace stages the harvest through its
normal checkpoint flow.

Requires the ``dud`` extra (``pip install nontainer[dud]``). The
default backend is ``"vm"`` — a real microVM boundary, resolved per
platform. The opt-in ``"subprocess"`` backend has ZERO containment:
agent code runs as the host user with open egress (own-agent-own-laptop
posture only; see dud's DESIGN.md "Backend ladder"). It is the dev/CI
floor, not a step on the isolation ladder — ``LocalExecutor`` is the
gated in-process option.

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
import os
import pickle
import shlex
import tarfile
import threading
import time
from collections.abc import Iterator, Mapping, MutableMapping
from dataclasses import replace
from typing import Any, Literal

from .cache import PREFIX
from .errors import NotSupportedError
from .executor import (
    ExecutionContext,
    HarvestLost,
    StagedDiff,
    ViewSpec,
    _apply_diff,
    _is_plain_data,
    _truncate,
)
from .workspace import PythonResult, TerminalResult


def _lost_exc() -> Any:
    """dud's ``SessionLost``, or a never-matching sentinel (the empty
    tuple) when the installed dud predates the lifecycle contract —
    ``except _lost_exc():`` then catches nothing and calls run bare."""
    try:
        from dud.backends.base import SessionLost
    except ImportError:
        return ()
    return SessionLost


# Guest-side wrapper for app-handler (view) execution. PRELUDE
# reconstructs the pickled inputs (Request et al.) that rode in host→
# guest; EPILOGUE reduces any dataclass result (Response) to a tagged,
# json-crossable dict (bytes fields base64'd) since guest→host never
# pickles. All names are underscore-prefixed so the runner's harvest
# (which drops ``_*``) ignores the scaffolding.
_VIEW_PRELUDE = (
    "import pickle as __nt_pk, base64 as __nt_b64\n"
    "__nt_in = set()\n"
    "for __nt_k, __nt_v in __nt_pk.loads(__nt_b64.b64decode(__nt_blob)).items():\n"
    "    globals()[__nt_k] = __nt_v\n"
    "    __nt_in.add(__nt_k)\n"
)

# Runs BEFORE the prelude's unpickle: make the contract classes'
# modules importable in the guest. On the subprocess rung the plain
# import succeeds (guest shares the host venv). On a VM rung the guest
# has no nontainer install, so the module is synthesized from source
# shipped in ``__nt_boot`` (host→guest, the trusted direction — same as
# the pickle blob) and registered in ``sys.modules`` under its real
# dotted name, which is exactly where the unpickle will look for it.
_VIEW_BOOTSTRAP = (
    "import sys as __nt_sys, types as __nt_ty\n"
    "for __nt_mod in __nt_boot:\n"
    "    try:\n"
    "        __import__(__nt_mod)\n"
    "    except ImportError:\n"
    "        __nt_parts = __nt_mod.split('.')\n"
    "        for __nt_i in range(1, len(__nt_parts)):\n"
    "            __nt_pkg = '.'.join(__nt_parts[:__nt_i])\n"
    "            if __nt_pkg not in __nt_sys.modules:\n"
    "                __nt_pm = __nt_ty.ModuleType(__nt_pkg)\n"
    "                __nt_pm.__path__ = []\n"
    "                __nt_sys.modules[__nt_pkg] = __nt_pm\n"
    "        __nt_m = __nt_ty.ModuleType(__nt_mod)\n"
    "        __nt_sys.modules[__nt_mod] = __nt_m\n"  # register BEFORE exec
    "        exec(compile(__nt_boot[__nt_mod]['src'],\n"
    "                     '<contract:' + __nt_mod + '>', 'exec', 0, 1),\n"
    "             __nt_m.__dict__)\n"
    "        if '.' in __nt_mod:\n"
    "            __nt_par, __nt_leaf = __nt_mod.rsplit('.', 1)\n"
    "            setattr(__nt_sys.modules[__nt_par], __nt_leaf, __nt_m)\n"
    "    for __nt_n in __nt_boot[__nt_mod]['names']:\n"
    "        globals()[__nt_n] = getattr(__nt_sys.modules[__nt_mod], __nt_n)\n"
)

_VIEW_EPILOGUE = (
    "\n"
    # Input names never ride back: LocalExecutor drops injected names
    # from the outgoing namespace, so parity says the unpickled inputs
    # (nt__req et al.) vanish here too — before the harvest, so the
    # non-codec Request object never even reaches dud's Value encoder.
    "for __nt_k in list(__nt_in):\n"
    "    globals().pop(__nt_k, None)\n"
    "import dataclasses as __nt_dc, base64 as __nt_b64\n"
    "def __nt_marshal(__nt_o):\n"
    "    if __nt_dc.is_dataclass(__nt_o) and not isinstance(__nt_o, type):\n"
    "        __nt_f = {}\n"
    "        for __nt_fld in __nt_dc.fields(__nt_o):\n"
    "            __nt_val = getattr(__nt_o, __nt_fld.name)\n"
    "            if isinstance(__nt_val, (bytes, bytearray)):\n"
    "                __nt_val = {'__nt_b__': __nt_b64.b64encode(bytes(__nt_val)).decode()}\n"
    "            __nt_f[__nt_fld.name] = __nt_val\n"
    "        return {'__nt_dc__': type(__nt_o).__module__ + ':' + type(__nt_o).__qualname__,\n"
    "                'fields': __nt_f}\n"
    "    return __nt_o\n"
    "for __nt_name in [__nt_g for __nt_g in list(globals()) if not __nt_g.startswith('_')]:\n"
    "    globals()[__nt_name] = __nt_marshal(globals()[__nt_name])\n"
)


def _rebuild_dataclass(value: Any, contract: tuple[type, ...]) -> Any:
    """Reverse ``_VIEW_EPILOGUE``: turn a ``{'__nt_dc__': ...}`` tag back
    into an instance of the named contract class (base64 bytes fields
    decoded). Safe across a real boundary — a known class name from the
    view's own ``extra_classes`` plus primitive fields, never pickle. An
    untagged value (plain dict/list/str/bytes) passes through."""
    import base64

    if not (isinstance(value, dict) and "__nt_dc__" in value):
        return value
    ref = value["__nt_dc__"]
    fields = {}
    for k, fv in value.get("fields", {}).items():
        if isinstance(fv, dict) and "__nt_b__" in fv:
            fields[k] = base64.b64decode(fv["__nt_b__"])
        else:
            fields[k] = fv
    for c in contract:
        if f"{c.__module__}:{c.__qualname__}" == ref:
            try:
                return c(**fields)
            except Exception:
                return value  # can't rebuild — leave the tag for the caller
    return value


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
    """Executor over a dud backend — a host process or a real microVM.

    Construct bare and pass as ``Workspace(..., executor=DudExecutor())``;
    the workspace binds state via :meth:`open`. Everything above the
    transport is identical across rungs (dud's ``HostSession``), so this
    executor only chooses which one to open — via ``dud.session()``, so
    a new dud rung needs no edit here.

    - ``backend="vm"`` (default): whichever VM rung fits this host —
      vfkit on macOS, firecracker on Linux/KVM. Prefer it over naming
      one: config written against ``"vm"`` survives new rungs landing,
      and a host that can't provide one fails closed
      (``IsolationUnavailable``) rather than degrading silently.
    - ``backend="vfkit"`` / ``"firecracker"``: pin a specific rung.
    - ``backend="subprocess"``: the guest runtime as a host process.
      Real bash/python/files and **ZERO isolation** — agent code runs
      as you, with your network and your files. It buys fidelity, not
      a boundary, so it is opt-in: it's dud's dev/CI floor and the only
      rung that works with no hypervisor at all. ``root`` optionally
      pins the guest scratch dir. If you want containment without a VM,
      you want ``LocalExecutor``, not this.

    On the VM rungs ``vm`` is passed through to the dud session
    (``image``, ``kernel``, ``memory_mib``, ``cpus``, …); its defaults
    boot ``python:3.12-slim`` with the kernel resolved from
    ``$DUD_KERNEL``/``~/.dud``. ``vm={"pooled": False}`` opts out of
    warm-pool reuse. Requesting a rung the host can't provide fails
    closed (``IsolationUnavailable``).

    Concurrency: all wire-touching methods serialize on an internal
    lock. One session = one channel, and dud's request/response framing
    is not concurrency-safe — while the seam's sanctioned lock-free
    path (frozen app serving's ``exec_python(view=)``) stays *correct*
    here, it is serialized, not parallel. Parallel frozen serving needs
    per-request sessions (a dud golden-snapshot economy, not a v0
    concern)."""

    def __init__(
        self,
        *,
        root: str | None = None,
        backend: str = "vm",
        vm: Mapping[str, Any] | None = None,
    ) -> None:
        self._host_root = root  # rung-1 host dir for dud's Session(root=)
        self._backend = backend
        self._vm = dict(vm or {})
        self._ctx: ExecutionContext | None = None
        self._session: Any | None = None
        self._work: str | None = None  # guest workspace dir (from ping)
        # The workspace root inside the provider fs (ExecutionContext
        # .root), normalized to "" for "/" so f"{root}/{rel}" composes.
        # Host <root>/x <-> guest <work>/x is THE path mapping; on VM
        # rungs both sides read /workspace/x (dud mounts its workspace
        # at the same absolute path), so agent code and prompts see one
        # path contract across executors.
        self._ws_root = ""
        self._plain: dict[str, Any] = {}  # plain-data host_objects
        self._live: dict[str, Any] = {}  # live host_objects (for recovery)
        self._cache: Any | None = None  # kv-backed cache view (for recovery)
        self._closed = False
        # One session = one channel; dud's framing can't interleave
        # requests. RLock: recovery paths make wire calls while held.
        self._lock = threading.RLock()

    # -- lifecycle -------------------------------------------------------

    def _make_session(self, host_objects: dict[str, Any], cache: Any) -> Any:
        """Open the dud session for the configured rung (transport only).

        Everything goes through ``dud.session()`` — the façade exists so
        consumers don't hardcode the backend switch. That's what makes
        ``backend="vm"`` resolve per platform (vfkit on macOS,
        firecracker on Linux) and what lets a new dud rung land without
        an edit here.
        """
        import dud

        if self._backend == "subprocess":
            # No boot to skip, so no pooling; root pins the guest scratch dir.
            return dud.session(
                "subprocess",
                root=self._host_root,
                host_objects=host_objects,
                cache=cache,
            )

        vm = dict(self._vm)
        # The guest mounts its workspace AT the host-side root, so
        # absolute paths agree across the boundary. root="/" can't
        # be mounted in a guest — dud's default (/workspace) stays
        # and the generic prefix mapping covers the difference.
        if self._ws_root:
            vm.setdefault("workspace", self._ws_root)
        # Same image spec across sessions -> the VM is fungible; reuse a
        # warm one (guest reset + tree push) instead of booting. close()
        # parks it back in dud's shared pool. State affinity: a parked VM
        # tagged with our provider head already HOLDS this workspace tree
        # (see close()); on such a match `resumed` is set and open()
        # skips the push entirely.
        pooled = vm.pop("pooled", True)
        return dud.session(
            self._backend,
            pooled=pooled,
            state=self._head() if pooled else None,
            host_objects=host_objects,
            cache=cache,
            **vm,
        )

    def open(self, context: ExecutionContext) -> None:
        self._ctx = context
        self._ws_root = "" if context.root == "/" else context.root.rstrip("/")
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
        self._live = live
        self._cache = _KvBytesCache(context.kv)
        self._session = self._make_session(live, self._cache)
        self._bind_session()

    def _bind_session(self) -> None:
        """Ping + materialize the tree + re-assert cwd on a freshly made
        session — closing it on ANY failure. Without this, a boot race
        or a bad tree raising out of ``open()`` (the last step of
        ``Workspace.__init__``) orphans a live guest: nobody holds a
        workspace to close, and a leaked pooled session sits in the
        pool's bound set, which pool teardown doesn't reap."""
        try:
            self._work = self._session.ping()["workspace"]
            if not getattr(self._session, "resumed", False):
                self._push_tree()  # affinity hit: the tree is already ours
            self._assert_guest_cwd()
        except BaseException:
            try:
                self._session.close()
            except Exception:
                pass
            self._session = None
            raise

    def close(self) -> None:
        with self._lock:
            self._close_locked()

    def _close_locked(self) -> None:
        if self._session is not None and not self._closed:
            self._closed = True
            # Tag the parked tree with its provider commit so a session
            # resuming at this exact state skips the push (pool state
            # affinity). Every synced mutation path keeps guest == fs
            # view, and the head only names COMMITTED state — so a
            # divergent/dirty close simply yields a tag no future head
            # matches, degrading to the normal push. (The documented raw
            # ``ws.fs`` bypass is outside this guarantee, as ever.)
            try:
                self._session.park_state = self._head()
            except Exception:
                pass
            try:
                self._session.close()
            except Exception:
                pass  # best-effort by contract: the provider closes next

    def _head(self) -> str | None:
        """The provider's current commit id, if it exposes one."""
        if self._ctx is None or self._ctx.head is None:
            return None
        try:
            return self._ctx.head()
        except Exception:
            return None

    # -- death recovery ----------------------------------------------------

    def _with_recovery(self, fn: Any) -> Any:
        """Run one session call; on SessionLost, rebuild and retry ONCE.

        Any dud VM may vanish mid-conversation — a crash, or the pool
        reclaiming the least-recently-used session under a capacity cap
        (``DUD_VM_MAX_TOTAL``). The disposable thesis makes recovery
        uniform: reopen a session (warm-pool acquire when available),
        re-push the provider tree, retry the call. Workspace effects of
        the dead attempt never left the VM (diffs are pulled only after
        success) and cache writes land only on success, so the retry is
        at-most-once-observed for state; live-object ``hostcalls`` the
        dead attempt already made are the one thing that may repeat.
        """
        try:
            return fn()
        except _lost_exc():
            self._recover()
            return fn()

    def _recover(self) -> None:
        try:
            self._session.close()  # pool tears the corpse down, not parks
        except Exception:
            pass
        self._session = self._make_session(self._live, self._cache)
        self._bind_session()

    # -- python ----------------------------------------------------------

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
        """Run code in a fresh guest runner (script model, literally:
        one interpreter per exec).

        ``echo`` is the guest runner's concern (it does last-expression
        echo); per-call override is not wired in v0. ``stdin``/``argv``
        have no dud verb yet — and the terminal ``python`` builtin that
        used them doesn't exist here (real bash runs real python) — so
        they fail loud rather than silently dropping data. ``view``
        (apps handler dispatch) is implemented in stage 3c (read-only
        views + contract-class injection + per-request atomicity);
        until that lands it raises rather than silently running a
        handler with full read-write access."""
        ctx = self._require_ctx()
        if stdin is not None or argv is not None:
            raise NotSupportedError(
                "exec_python(stdin=/argv=) is not supported by DudExecutor "
                "(no wire support in dud v0; use terminal() — real bash "
                "pipes into real python)"
            )
        if view is not None:
            # The lock spans exec + harvest so a read-only view's diff
            # check can't see a concurrent call's writes.
            with self._lock:
                return self._exec_view(ctx, code, dict(inputs or {}), view)

        from dud.values import NotRepresentable

        merged = dict(self._plain)
        merged.update(inputs or {})
        start = time.monotonic()
        try:
            with self._lock:
                result = self._with_recovery(
                    lambda: self._session.python(
                        code,
                        inputs=merged or None,
                        timeout=ctx.python_config.timeout,
                    )
                )
        except NotRepresentable as exc:
            # Mirror LocalExecutor's contract for bad inputs (there:
            # unpicklable; here: outside the Value codec).
            raise TypeError(
                f"inputs value is not codec-representable ({exc}). "
                "DudExecutor inputs must be json-representable data or "
                "bytes; live resources belong in PythonConfig.host_objects."
            ) from exc
        return self._map_result(result, ctx, time.monotonic() - start)

    def _exec_view(
        self,
        ctx: ExecutionContext,
        code: str,
        inputs: dict[str, Any],
        view: ViewSpec,
    ) -> PythonResult:
        """Restricted app-handler execution (apps dispatch).

        Contract objects don't fit dud's json/bytes codec, so they
        cross by direction: ``Request`` (and any inputs) ride IN as a
        host→guest pickle — the safe direction, same as the cache write
        path — reconstructed guest-side. Returns cross OUT via a small
        marshaller (guest→host never pickles): dataclass results (e.g.
        ``Response``) reduce to a tagged dict with base64 for bytes
        fields, reconstructed host-side from the view's declared
        ``extra_classes``. Read-only is enforced (cache write raises
        guest-side; a GET that writes the fs is rejected here), and a
        mutating handler's writes are absorbed into the provider (so
        ``ws.fs`` reflects them, like LocalExecutor's write-through)."""
        import base64
        import inspect
        import pickle
        import sys

        merged = dict(self._plain)
        merged.update(inputs)
        blob = base64.b64encode(pickle.dumps(merged)).decode()
        # Contract classes cross by SOURCE, not by install: group them by
        # defining module and ship each module's source so the guest can
        # synthesize it when the import fails (VM rungs have no nontainer).
        # A module whose source can't be read falls back to a plain import
        # line — the pre-VM behavior, correct wherever the guest shares
        # the host venv.
        boot: dict[str, dict[str, Any]] = {}
        for c in view.extra_classes:
            mod, name = getattr(c, "__module__", None), getattr(c, "__name__", None)
            if not mod or not name:
                continue
            entry = boot.setdefault(mod, {"src": None, "names": []})
            if name not in entry["names"]:
                entry["names"].append(name)
        imports = ""
        for mod, entry in list(boot.items()):
            try:
                entry["src"] = inspect.getsource(sys.modules[mod])
            except Exception:
                imports += f"from {mod} import {', '.join(entry['names'])}\n"
                del boot[mod]
        full = imports + _VIEW_BOOTSTRAP + _VIEW_PRELUDE + code + _VIEW_EPILOGUE
        timeout = (
            view.timeout if view.timeout is not None else ctx.python_config.timeout
        )
        start = time.monotonic()
        result = self._with_recovery(
            lambda: self._session.python(
                full,
                inputs={"__nt_blob": blob, "__nt_boot": boot},
                timeout=timeout,
                cache_readonly=view.readonly_cache,
            )
        )
        duration = time.monotonic() - start

        if view.readonly_fs:
            # A session lost during this check recovers to a fresh guest
            # whose diff is empty — correct for a GET: nothing was
            # supposed to be written, nothing gets absorbed either way.
            d = self._with_recovery(lambda: self._session.diff(rebase=False))
            if not d.empty:
                # A GET that wrote the fs (rung-1 has no read-only mount,
                # so the write lands then is rejected here — DESIGN.md).
                self._with_recovery(lambda: self._session.reset())
                return PythonResult(
                    stdout="",
                    stderr="",
                    error="PermissionError: filesystem is read-only in GET handlers",
                    ticks=0,
                    duration=duration,
                    truncated=False,
                    namespace={},
                )
        else:
            # Mutating handler: land the writes in the provider staging,
            # matching LocalExecutor's write-through (dispatch's atomicity
            # then discards them via ws.discard() on error). A session
            # lost HERE is a torn call — the handler's cache write-backs
            # already landed but its fs writes died unharvested — so it
            # surfaces as an errored result, which routes dispatch into
            # its ws.discard() atomicity path; retrying the diff on the
            # recovered guest would instead report success minus writes.
            try:
                d = self._session.diff(rebase=True)
            except _lost_exc():
                self._recover()
                out = self._map_result(
                    result, ctx, duration, contract=view.extra_classes
                )
                return replace(
                    out,
                    error="HarvestLost: guest lost before the handler's "
                    "filesystem changes were harvested; its writes are gone "
                    "(a fresh guest was recovered)",
                )
            _apply_diff(
                ctx.fs,
                {self._fs_rel(k): v for k, v in d.writes.items()},
                tuple(self._fs_rel(k) for k in d.deletes),
            )

        return self._map_result(result, ctx, duration, contract=view.extra_classes)

    def _map_result(
        self,
        result: Any,
        ctx: ExecutionContext,
        duration: float,
        contract: tuple[type, ...] = (),
    ) -> PythonResult:
        error = None
        if result.error is not None:
            # The guest renders its own traceback (rendering happens
            # where the objects live); errors without one (Timeout,
            # RunnerCrash) read as "Type: message".
            tb = result.error.traceback.rstrip()
            error = tb or f"{result.error.etype}: {result.error.message}"

        namespace = dict(result.outputs)
        if contract:
            namespace = {
                k: _rebuild_dataclass(v, contract) for k, v in namespace.items()
            }

        stdout, trunc = _truncate(result.transcript, ctx.max_observation)
        return PythonResult(
            stdout=stdout,
            stderr="",  # merged into the transcript guest-side
            error=error,
            ticks=0,  # no tick machinery: wall-clock timeout is the budget
            duration=duration,
            truncated=trunc,
            namespace=namespace,
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
        with self._lock:
            result = self._with_recovery(
                lambda: self._session.shell(script, timeout=ctx.python_config.timeout)
            )
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
        (no provider dirtying, no phantom checkpoints). A session lost
        HERE is a torn call, not a clean miss: the exec already
        committed its cache write-backs (they land inside a successful
        ``session.python``) while the fs writes died unharvested in the
        guest. Recover a fresh session, then raise ``HarvestLost`` so
        the workspace surfaces an errored result and unwinds the staged
        half — retrying the diff on the recovered guest would report
        success for a call whose fs effects silently vanished."""
        with self._lock:
            try:
                d = self._session.diff(rebase=True)
            except _lost_exc() as exc:
                self._recover()
                raise HarvestLost(
                    "guest lost between execution and harvest: this call's "
                    "file writes died with it (a fresh guest was recovered "
                    "at the last synced state)"
                ) from exc
        if d.empty:
            return None
        return StagedDiff(
            writes={self._fs_rel(k): v for k, v in d.writes.items()},
            deletes=tuple(self._fs_rel(k) for k in d.deletes),
        )

    def _fs_rel(self, rel: str) -> str:
        """Guest-workspace-relative -> fs-root-relative (the StagedDiff
        contract): the guest harvests paths relative to its mount; the
        workspace stages them relative to the provider fs root."""
        base = self._ws_root.lstrip("/")
        return f"{base}/{rel}" if base else rel

    def sync(self) -> None:
        """Re-materialize the guest tree from the provider (wholesale:
        push_tree wipes and rebuilds — incremental shipping is a
        stage-3 economy), then re-assert the persisted cwd, which
        push_tree resets to the workspace root. A push that dies
        mid-flight routes through ``_recover()``, whose rebuild pushes
        (or affinity-resumes) the same tree itself — no retry on top,
        or the wholesale push would run twice."""
        with self._lock:
            try:
                self._push_tree()
            except _lost_exc():
                self._recover()  # rebuild pushes + re-asserts cwd itself
                return
            self._assert_guest_cwd()

    # -- internals ---------------------------------------------------------

    def _require_ctx(self) -> ExecutionContext:
        if self._ctx is None or self._session is None:
            raise RuntimeError("DudExecutor is not open (Workspace calls open())")
        return self._ctx

    def _push_tree(self) -> None:
        """Tar the workspace-root subtree of the provider (via the
        termish-protocol fs, so staged-but-uncommitted state is
        included) and push it as the guest's new baseline. Files the
        embedder parks OUTSIDE the root are host-only state by the
        contract — they never reach the guest."""
        fs = self._require_ctx().fs
        base = self._ws_root  # "" when the root is the fs root
        buf = io.BytesIO()
        # Plain tar: gzip dominated reactivation ~4:1 at scale and buys
        # nothing on a local socket (guest extract auto-detects either).
        with tarfile.open(fileobj=buf, mode="w") as tf:
            for rel in fs.list(base or "/", recursive=True):
                full = f"{base}/{rel}"
                if fs.isfile(full):
                    data = fs.read(full)
                    info = tarfile.TarInfo(name=rel)
                    info.size = len(data)
                    tf.addfile(info, io.BytesIO(data))
        self._session.push_tree(buf.getvalue())

    def _assert_guest_cwd(self) -> None:
        """Point the guest shell at the host fs's cwd (best-effort: a
        cwd that doesn't exist guest-side, or lives outside the
        workspace root, stays at the guest workspace root)."""
        try:
            cwd = self._require_ctx().fs.getcwd()
        except Exception:
            return
        base = self._ws_root
        if base:
            if cwd == base:
                rel = ""
            elif cwd.startswith(base + "/"):
                rel = cwd[len(base) + 1 :]
            else:
                return  # host cwd outside the root: no guest analog
        else:
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
        if not self._work:
            return
        work = self._work
        if not guest_cwd.startswith(work):
            # A real shell reports resolved paths; the configured
            # workspace may ride a symlink (macOS /var -> /private/var).
            work = os.path.realpath(work)
            if not guest_cwd.startswith(work):
                return
        rel = guest_cwd[len(work) :].lstrip("/")
        base = self._ws_root
        host = f"{base}/{rel}" if rel else (base or "/")
        try:
            if ctx.fs.getcwd() != host and ctx.fs.isdir(host):
                ctx.fs.chdir(host)
        except Exception:
            pass
