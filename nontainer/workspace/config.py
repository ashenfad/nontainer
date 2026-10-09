"""What a workspace is opened with: its python, mounts and profile."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType, ModuleType
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from ..protocol import Executor
    from .core import Workspace


Isolation = Literal["none", "process", "kernel"]


@dataclass(frozen=True)
class Mount:
    """A real directory exposed inside the workspace tree (a "volume").

    Mounts are a *workspace* concern, not a python-sandbox concern:
    both tools see them — ``terminal("ls /data")`` and sandboxed
    ``open("/data/x.csv")`` agree. Composed via monkeyfs ``MountFS``
    (+ ``IsolatedFS``, + ``ReadOnlyFS`` when ``readonly``).

    Mounted paths are live views of the real directory: they are NOT
    versioned and NOT captured by commits, so a checkout leaves them
    exactly as they are.

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

    host_objects: Mapping[str, Any] = field(default_factory=dict, hash=False)
    """Live host resources injected into the namespace by name — the
    in-process superpower (your model, your db pool). Distinct from
    ``run_python(inputs=...)`` on purpose: inputs are per-call
    *picklable data* (they cross isolation boundaries by value);
    host_objects are session-lifetime *live objects* that get
    attribute-level policy at construction and RPC-proxy bridging
    under process/kernel isolation (or a loud construction-time error
    if unbridgeable). Merging the two would make `isolation="none"` →
    `"process"` a silent breaking change; keeping them apart makes the
    contract checkable at the right moment.

    Read-only, like the config: each executor takes its set when it
    opens, so an object added to the mapping afterwards would reach
    in-process code and nowhere else. A different set is a different
    config, ``dataclasses.replace(python, host_objects={...})``, for a
    workspace opened (or forked) with it.

    An entry may be a :class:`HostObject`, which can say more about the
    object: ``HostObject(rows, type=list[Row])`` sends data of a declared
    type into the sandbox by value on every rung, and
    ``HostObject(db, stub=DbStub)`` puts a class that runs in the sandbox
    in front of a live object, whose calls are typed by its
    annotations."""

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
    many times slower to start and larger resident than with a stdlib
    policy. The default of **1** keeps the app-iteration loop warm —
    edit, ``test_app``, preview, repeat is essentially sequential —
    while holding a single worker.

    Raise it for **concurrent** serving: a preview page issuing parallel
    API calls, or a published app with real traffic. Past the cap,
    concurrency falls back to a per-call sandbox rather than queueing,
    so the failure mode of too-low is latency, not errors.

    Too-high is memory, because **residency only rises unless you reap
    it**. A burst of N concurrent calls leaves ``min(N, warm_view_workers)``
    workers resident — the ones past the cap are transient and reaped
    when their call ends, but those within it are kept. Left alone, the
    cap is therefore a floor you fill and keep paying for, per distinct
    view, per workspace, for the executor's life.
    ``ws.runtime.reap_idle(max_age)`` closes the ones idle for
    ``max_age`` seconds, and an embedder holding workspaces open calls
    it on a timer; nothing calls it on the embedder's behalf.

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
    once. With a heavy stack such as ``modules=[dataframes(), plotting()]``,
    a preloaded worker starts many times faster and holds a fraction of
    the memory — the stack is paid for once in the broker rather than
    per worker. It
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

    classes: Sequence[type] = field(default=(), hash=False)
    """Classes bound by name in every run, and importable from ``host``:
    the types agent code builds values of (a task's result type, say).
    In-process they are registered with the sandbox policy; under
    process isolation the worker imports them by their qualified name;
    on dud the guest imports them, or rebuilds their module from its
    source where it has no such module. A class defined inside a
    function, or in a script's ``__main__``, can't be named from
    another process, and is refused when a workspace opens with
    isolation or on dud."""

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "host_objects", MappingProxyType(dict(self.host_objects))
        )
        classes = tuple(self.classes)
        for klass in classes:
            if not isinstance(klass, type):
                raise TypeError(
                    f"PythonConfig.classes holds classes, not {type(klass).__name__}"
                )
        names = [klass.__name__ for klass in classes]
        twice = sorted({n for n in names if names.count(n) > 1})
        if twice:
            raise ValueError(
                f"PythonConfig.classes has two classes named {twice[0]!r}: "
                "code names them, so the names must differ"
            )
        clash = sorted(set(names) & {*self.host_objects, "host", "cache"})
        if clash:
            raise ValueError(
                f"PythonConfig.classes has a class named {clash[0]!r}, which is "
                "already a name in the sandbox (a host object, the host module "
                "or the cache)"
            )
        object.__setattr__(self, "classes", classes)


@dataclass(frozen=True)
class Profile:
    """A session's profile: what the world its agent works in holds and
    where its code runs, as one value.

    :meth:`Store.open <nontainer.store.Store.open>` and
    :func:`workspace` take it as ``profile=`` in place of the six
    keywords it bundles (everything here but ``variables``),
    :meth:`Profile.of` reads it back off a workspace, and a fork
    inherits it, as forks have always inherited these settings. Read
    back, ``mounts`` are normalized (points validated, sources resolved)
    and ``commands`` and ``variables`` are the session's as they stand.

    Not part of it: ``cache``, ``autocommit`` and ``max_observation``.
    They say how a session behaves, not what its world holds.

    ``mounts``, ``commands`` and ``variables`` are copied into
    read-only mappings, so a ``Profile`` cannot change once built;
    derive a variant with :func:`dataclasses.replace`.
    """

    python: PythonConfig = field(default_factory=PythonConfig)
    """What sandboxed code may touch, host objects included."""

    mounts: Mapping[str, Mount] = field(default_factory=dict, hash=False)
    """Real directories exposed inside the tree (skills mounted with
    :func:`nontainer.skills.mounts` among them)."""

    commands: Mapping[str, Callable[..., Any]] = field(default_factory=dict, hash=False)
    """Custom terminal commands."""

    variables: Mapping[str, str] = field(default_factory=dict, hash=False)
    """Environment variables for script runs: ``$VAR`` expansion in the
    terminal, exported into the guest on dud rungs. Applied to
    ``ws.runtime.env`` when the session opens. Names a shell could not
    expand are refused here, and values are coerced to ``str``, as
    ``runtime.env`` does."""

    executor_factory: Callable[[], Executor] | None = None
    """Where code runs; ``None`` is the in-process ``LocalExecutor``."""

    root: str = "/workspace"
    """The workspace root agent code sees its files under."""

    ignore: tuple[str, ...] = ()
    """The embedder's ``.gitignore`` patterns."""

    def __post_init__(self) -> None:
        from ..runtime import _ShellEnv

        if isinstance(self.ignore, str):
            raise TypeError(
                "Profile.ignore takes a sequence of patterns, not one string: "
                f"use ({self.ignore!r},)"
            )
        variables = _ShellEnv()
        variables.update(self.variables)
        object.__setattr__(self, "mounts", MappingProxyType(dict(self.mounts)))
        object.__setattr__(self, "commands", MappingProxyType(dict(self.commands)))
        object.__setattr__(self, "variables", MappingProxyType(dict(variables)))
        object.__setattr__(self, "root", normalize_root(self.root))
        object.__setattr__(self, "ignore", tuple(self.ignore))

    @classmethod
    def of(cls, ws: Workspace) -> Profile:
        """``ws``'s profile: what a fork of it inherits, and what
        ``Store.open(..., profile=)`` takes to open another session in
        the same world. Commands are the session's as they stand,
        without the framework's own, which a session opened with this
        profile rebuilds bound to itself; variables are copied as a fork
        copies them."""
        settings = ws._settings
        return cls(
            python=settings.python,
            mounts=settings.mounts,
            commands=ws.runtime.forkable_commands(),
            variables=dict(ws.runtime.env),
            executor_factory=settings.executor_factory,
            root=settings.root,
            ignore=settings.ignore,
        )


def _profile_fields(
    profile: Profile | None,
    *,
    python: PythonConfig | None,
    mounts: Mapping[str, Mount] | None,
    commands: Mapping[str, Callable[..., Any]] | None,
    executor_factory: Callable[[], Executor] | None,
    root: str | None,
    ignore: Iterable[str] | None,
) -> dict[str, Any]:
    """The six profile keywords for ``Workspace(...)``: from ``profile``,
    or as given, never both. A keyword passed alongside ``profile`` would
    leave one of the two silently ignored, so it is refused."""
    if profile is None:
        return {
            "python": python,
            "mounts": mounts,
            "commands": commands,
            "executor_factory": executor_factory,
            "root": "/workspace" if root is None else root,
            "ignore": ignore,
        }
    given = [
        name
        for name, value in (
            ("python", python),
            ("mounts", mounts),
            ("commands", commands),
            ("executor_factory", executor_factory),
            ("root", root),
            ("ignore", ignore),
        )
        if value is not None
    ]
    if given:
        raise TypeError(
            "pass the profile as profile= or as its fields, not both: "
            f"{', '.join(given)} given with profile"
        )
    return {
        "python": profile.python,
        "mounts": dict(profile.mounts),
        "commands": dict(profile.commands),
        "executor_factory": profile.executor_factory,
        "root": profile.root,
        "ignore": profile.ignore,
    }


def normalize_root(root: str) -> str:
    """A workspace root as every executor reads it: absolute, normalized
    by segment, with no ``.`` or ``..`` in it.

    Anything a guest kernel would collapse (trailing, doubled, or
    leading-only slashes) has to collapse here too, or the executors
    silently disagree about the root: "//" left as-is rstrips to "",
    which reads falsy downstream — the local side then composes
    "/skills" (flat layout) while a guest falls back to dud's own
    /workspace default. That split is the exact bug this root exists
    to prevent. ``.`` and ``..`` are rejected rather than resolved: a
    guest would normalize them and the VFS wouldn't, reopening the same
    split. Anything that builds paths under a root an embedder passed
    (``skills.mounts``) normalizes it here, to land where the workspace
    looks.
    """
    if not root.startswith("/"):
        raise ValueError(f"root must be an absolute path, got {root!r}")
    parts = [p for p in root.split("/") if p]
    if any(p in (".", "..") for p in parts):
        raise ValueError(f"root must not contain . or .. segments, got {root!r}")
    return "/" + "/".join(parts) if parts else "/"


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
    ignore: tuple[str, ...]

    def as_kwargs(self) -> dict[str, Any]:
        """Shallow field mapping for ``Workspace(**...)``. Deliberately
        not ``dataclasses.asdict``, which recurses — it would flatten
        ``PythonConfig`` and each ``Mount`` into plain dicts."""
        return dict(vars(self))
