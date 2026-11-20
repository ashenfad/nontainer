"""Publications: immutable versions of part of a session's tree, which
an app is served from, and the registry that says which is current."""

from __future__ import annotations

import json
import logging
import os
import random
import re
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, NamedTuple

from ..errors import CommitNotFoundError, NotSupportedError, WorkspaceError
from ..protocol import (
    SESSION_ID_RE,
)
from .layout import (
    _FROZEN_SETTINGS,
    _PUB_BRANCH_PREFIX,
    _check_frozen_settings,
)
from .refs import Ref

if TYPE_CHECKING:
    from ..workspace import Workspace
    from .core import Store


_logger = logging.getLogger("nontainer.store")


# The publication registry: the mutable half (which versions exist,
# which one is current) beside the immutable half (the tags). A kvgit
# store keeps it in one key of its own backend, beside the branches and
# tags it indexes, so every process and every host sharing that backend
# reads and writes the same registry. The key is outside kvgit's own
# key families, which kvgit's sweep never leaves, and its leading
# ``__`` marks it as a value that changes under a fixed key, which a
# caching tier (``kvgit.kv.composite``) always reads from the
# authoritative store.
_REGISTRY_KEY = "__nontainer_publications__"


# Where releases before the shared registry kept it: a JSON file in the
# store directory. A store that still has one imports it on first use
# and renames it with ``_IMPORTED_SUFFIX``, so it stays readable by hand
# and is not imported again.
_LEGACY_REGISTRY_FILE = "publications.json"


_IMPORTED_SUFFIX = ".imported"


# How many times one registry change is read, applied and written
# before giving up, when other writers keep changing the registry
# between its read and its write.
_REGISTRY_ATTEMPTS = 64


_VERSION_NUMBER_RE = re.compile(r"^v(\d+)$")


# Keys publish() writes into the commit itself. A caller's ``info`` may
# not spell them: the commit is immutable and is where provenance is
# read from, so a false ``published_from`` would outlive every chance to
# notice it — and ``published_from``, ``paths`` and ``exclude`` together
# are what a retry compares to recognise its own interrupted attempt.
_RESERVED_INFO_KEYS = (
    "tool",
    "name",
    "version",
    "published_from",
    "paths",
    "exclude",
)


# The same keys read the other way: all of them present, carrying the
# values a caller can name, is what marks a commit as one publish
# attempt's work. Nothing the store did not publish is ever removed on
# the strength of its name alone. ``exclude`` is not required: a
# version published before publish recorded it carries none, and is a
# publication all the same.
_PUBLISH_IDENTITY_KEYS = tuple(k for k in _RESERVED_INFO_KEYS if k != "exclude")


# What ``Publication.open`` takes: the same, without ``root``. A
# publication records the workspace root its files were published
# under, so the root is a fact about the version rather than a choice
# at the call, and reading the tree at another one finds it empty.
_PUBLICATION_SETTINGS = tuple(n for n in _FROZEN_SETTINGS if n != "root")


# Where an app keeps the code a URL reaches, under the workspace root.
# A publication whose tree holds nothing there is files only, and the
# workspace it opens as has no executor at all.
_HANDLER_DIR = "app/api"


# What a publish leaves out unless told otherwise: the files an app's
# authoring loop writes beside the app for the agent to read back —
# the handler log (tracebacks, prints, exception messages that can
# carry secrets) and test_app's page captures. They are the author's
# working notes, not part of what a visitor is served, and a version
# carrying them would hand them to whoever holds its URL or its export.
# The apps layer owns these names; a test pins that the two agree.
_DEFAULT_PUBLISH_EXCLUDE = ("app/logs/", "app/screenshots/")


# The default workspace root, for a version published before the root
# was recorded with it: that is the root it was published under.
_DEFAULT_ROOT = "/workspace"


def _encode_registry(registry: Mapping[str, Any]) -> bytes:
    """The registry as it is stored: compact JSON, keys sorted, so equal
    registries are equal bytes."""
    return json.dumps(registry, sort_keys=True, separators=(",", ":")).encode()


def _decode_registry(raw: bytes | None, where: str) -> dict[str, Any]:
    """The registry from its stored bytes; empty when nothing is stored.

    Bytes that are not JSON raise rather than read as an empty registry:
    a store whose registry reads empty looks like a store with nothing
    published, and the next publish would write over every record.
    """
    if raw is None:
        return {}
    try:
        data = json.loads(raw)
    except ValueError as e:
        raise WorkspaceError(
            f"Publication registry is unreadable: {where} ({e}). The "
            "publications themselves are tags and branches on the store "
            "and are still there; the registry is the index over them."
        ) from e
    return data if isinstance(data, dict) else {}


def _holds_nothing(commit: Any) -> bool:
    """Whether a kvgit commit is a branch's untouched start: an empty
    tree and no info, which is what a branch is created at."""
    from kvgit.hamt import EMPTY_HASH

    return commit.root == EMPTY_HASH and not commit.info


def _live(record: Any) -> bool:
    """Whether a registry record is a publication: it holds at least one
    recorded version. A record holding only reservations is a lineage
    still being opened or being removed, and is published nowhere."""
    return isinstance(record, Mapping) and bool(record.get("versions"))


def _same_attempt(entry: Mapping[str, Any], attempt: Mapping[str, Any]) -> bool:
    """Whether a pending reservation is one publish attempt of
    ``attempt``: the same source commit, ``paths`` and ``exclude``.
    Publishing that twice writes the same tree, so two such attempts may
    share a reservation, a branch and a commit."""
    return entry.get("op") == "publish" and all(
        entry.get(key) == value for key, value in attempt.items()
    )


def _busy(name: str, version: str, entry: Mapping[str, Any]) -> str:
    """Why ``name/version`` is not free: another call holds its
    reservation, and what to do if that call died."""
    age = max(0.0, time.time() - float(entry.get("since") or 0.0))
    branch = f"{_PUB_BRANCH_PREFIX}{name}/{version}"
    if entry.get("op") == "publish":
        return (
            f"{name}/{version} is being published by another call, started "
            f"{age:.0f}s ago (branch {branch!r}). Publish another version, or "
            "wait for that one to finish. If it died, "
            f"store.unpublish({name!r}, {version!r}) clears what it left once "
            "it is older than min_age (min_age=0 clears it now)."
        )
    return (
        f"{name}/{version} is being unpublished by another call, started "
        f"{age:.0f}s ago (branch {branch!r}). Publish another version, or "
        "wait for that one to finish. If it died, "
        f"store.unpublish({name!r}, {version!r}) finishes it once it is "
        "older than min_age (min_age=0 finishes it now)."
    )


class _Claim(NamedTuple):
    """What :meth:`_Publishing.unpublish` claimed: the live row it took
    out of the registry (``None`` when there was none), whether another
    call's reservation was taken over, and the live versions left, for
    a message naming what the publication does hold."""

    row: dict[str, Any] | None
    took_over: bool
    live: tuple[str, ...]


@dataclass(frozen=True)
class Version:
    """One published version: an immutable state with a name.

    ``tag`` is the store-scoped tag naming the commit (``<name>/<version>``),
    ``ref`` points at it on the publication's own branch, and
    ``published_from`` is the session ref it was derived from — a soft
    reference recorded in the commit's info, not a parent pointer, so
    the version pins none of that session's history. A version
    published from a store tag's snapshot records the tag's ref
    (``published_from.tag``); one published that way before this was
    recorded names the branch the read borrowed, which
    :meth:`Store.resolve` still reads while the tag names that commit.

    ``info`` is the caller's own metadata from ``publish(info=...)``,
    recorded on the registry row as well as in the commit, so listing
    published apps with their display titles and owners is one registry
    read and no backend open. It holds what the caller passed and
    nothing else: what publish writes itself is already spelled out by
    ``name``, ``version``, ``published_from``, ``paths`` and
    ``exclude``. A row written before the field existed reads as an
    empty mapping.

    ``exclude`` is what the publish left out from under ``paths``; a
    version published before publish recorded it reads as empty.

    ``info`` reads as a read-only view all the way down: nested
    mappings are read-only too, and a JSON array reads as a tuple. A
    version is immutable, and a record that let a caller edit three
    keys in would say otherwise.

    ``info`` counts for equality but is left out of the hash, so a
    version goes in a set or a dict key like any other frozen record.
    It holds whatever JSON the caller passed — a nested dict — and that
    is not hashable, while everything that identifies the version
    (``tag``, ``ref``) is.
    """

    name: str
    version: str
    tag: str
    ref: Ref
    published_from: Ref | None
    created: float
    info: Mapping[str, Any] = field(
        default_factory=lambda: MappingProxyType({}), hash=False
    )
    paths: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()


@dataclass(frozen=True)
class Publication:
    """A named lineage of published versions, and which one is current.

    Generic on purpose: it knows nothing about handlers, tokens, routes
    or databases. An embedder that serves a publication keeps those in
    its own table, keyed by name — see ``docs/apps.md``.

    ``meta`` is the publication's own metadata, written by
    :meth:`Store.set_meta` and replaced whole: the mutable overlay for
    what changes without a release, such as a display title. It is
    per-publication, where ``Version.info`` is per-version and written
    once at publish; a row with none reads as an empty mapping.

    This object is a record of the registry as it was read, not a live
    view of it: a snapshot taken before a ``set_meta`` keeps the
    ``meta`` it was fetched with, and re-reading it is what shows the
    new one. ``meta`` reads read-only all the way down for that
    reason — nested mappings are read-only too, and a JSON array reads
    as a tuple — so the way to change it is :meth:`Store.set_meta`,
    which is where the compare-and-swap and the validation are. It counts
    for equality but is left out of the hash, as ``Version.info``
    does: it holds whatever JSON the caller passed, and that is not
    hashable.
    """

    name: str
    versions: tuple[Version, ...]
    current: str
    store: "Store" = field(repr=False, compare=False)
    meta: Mapping[str, Any] = field(
        default_factory=lambda: MappingProxyType({}), hash=False
    )

    def version(self, name: str) -> Version | None:
        """One version by name, or ``None``."""
        for v in self.versions:
            if v.version == name:
                return v
        return None

    @property
    def current_version(self) -> Version:
        """The version :meth:`open` serves by default."""
        found = self.version(self.current)
        if found is None:  # pragma: no cover - registry invariant
            raise WorkspaceError(
                f"Publication {self.name!r} points at version "
                f"{self.current!r}, which its registry does not hold"
            )
        return found

    def open(self, version: str | None = None, **settings: Any) -> "Workspace":
        """A frozen workspace over a published version (default: the
        current one).

        Reads see the published files and nothing else; nothing can be
        written or committed. Close it when done — it holds an executor
        when it has something to run. It reads through this store's
        repository and does not hold it open, so closing it while a
        request is in flight is safe: the request finishes (a dud-backed
        one makes close() wait for it), and the store stays open for the
        rest. A closed one runs no new handlers, so take it out of
        routing before closing it. ``ws.session`` names the
        publication's own branch, because
        that is where the state lives: a publication belongs to no
        session.

        **A publication with no handlers opens static, and that is the
        default.** A published tree with no ``.py`` file directly under
        ``app/api/`` — ``_``-prefixed modules there are helpers no URL
        routes to — has nothing to execute, so it opens with no
        executor at all: no sandbox, no worker, nothing warmed, and the
        files serve as the bytes they are. ``ws.runtime.executes`` says
        which tier a handle is. On a static one every execution raises
        naming the publication: ``run_python``, ``terminal``, and a
        ``/api/`` request, which the router answers as the 404 it is.

        Execution settings are accepted on a static open and simply go
        unused — ``executor_factory`` is never called and ``python``
        never reaches a sandbox. An embedder serves every publication
        from one table and passes one set of settings for all of them;
        having to know a tree's tier before opening it would be a
        worse deal than settings that have nothing to apply to.

        ``settings`` are :meth:`Store.open`'s construction keywords —
        ``python``, ``mounts``, ``commands``, ``cache``,
        ``max_observation``, ``executor_factory`` — applied to the
        workspace this returns. A publication carries the tree and
        nothing else, so an embedder that serves it supplies the
        execution settings here: ``pub.open(python=PythonConfig(
        host_objects={"db": db}))`` is how a published handler reaches
        a live database. Host objects are the embedder's own live
        objects, which is exactly why they are not in the commit.

        ``root`` is not among them. A publication records the
        workspace root its files were published under, and reading
        them at another one finds an empty tree.
        """
        if "root" in settings:
            raise TypeError(
                "Publication.open() takes no 'root': a publication records "
                "the workspace root its files were published under, and "
                "reading them at another one finds an empty tree"
            )
        _check_frozen_settings(
            "Publication.open()", settings, accepts=_PUBLICATION_SETTINGS
        )
        target = self.current_version if version is None else self.version(version)
        if target is None:
            raise ValueError(
                f"No such version of {self.name!r}: {version!r} — have "
                f"{', '.join(v.version for v in self.versions)}"
            )
        return self.store._open_publication(target, **settings)


def _as_json_value(value: Any) -> Any:
    """What ``json.dumps`` should make of a value it does not know.

    The mappings a record hands back are ``MappingProxyType``, which
    ``json`` refuses; they serialize as the dicts they are, so the
    obvious edit — read a record's metadata, change one key, write it
    back — is not refused for the shape it came back in. Anything else
    is genuinely not JSON and raises.
    """
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _frozen_json(value: Any) -> Any:
    """A JSON value as a read-only view of itself, all the way down.

    Mappings become ``MappingProxyType`` and arrays become tuples, so a
    record's metadata cannot be edited through the record — at the top
    level or three keys in. A record describes what the store holds; a
    caller who wants a change builds a new mapping and writes it back,
    where the write is the thing that can be refused, locked and
    logged. Deep-frozen rather than deep-copied because a read is the
    common case and a copy per read would pay for a write that mostly
    does not come.
    """
    if isinstance(value, Mapping):
        return MappingProxyType({k: _frozen_json(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_frozen_json(v) for v in value)
    return value


def _validate_meta(meta: Any) -> dict[str, Any]:
    """A publication's metadata, normalized to plain JSON.

    The round trip is the serializability check and the copy at once:
    what comes back shares nothing with what the caller passed, so
    editing a nested list afterwards says nothing about the
    publication, and a value the registry could not be written with is
    refused before anything is read rather than halfway through the
    write.
    Both refusals are ``ValueError``: what was passed is the caller's
    to fix.
    """
    if not isinstance(meta, Mapping):
        raise ValueError(
            f"Publication meta must be a mapping, not {type(meta).__name__}: "
            f"{meta!r}. It is replaced whole, so pass what the publication "
            "should read as."
        )
    try:
        return json.loads(json.dumps(dict(meta), default=_as_json_value))
    except (TypeError, ValueError) as e:
        raise ValueError(
            f"Publication meta must be JSON-serializable, and this is not: "
            f"{e}. The registry is stored as JSON."
        ) from e


def _validate_publication_name(name: str) -> str:
    """A publication name is session-id shaped.

    It becomes a tag prefix (``<name>/<version>``) and a branch segment
    (``@store/pub/<name>/<version>``), so the characters that would make
    either ambiguous — a slash, a ``%``, a leading dot — are the ones a
    session id already forbids.
    """
    if not isinstance(name, str) or not SESSION_ID_RE.match(name):
        raise ValueError(
            f"Invalid publication name {name!r}: must match "
            f"{SESSION_ID_RE.pattern} (no slashes, no leading dot)"
        )
    return name


def _validate_version_name(version: str) -> str:
    """Same rules as a publication name: it is the other half of the
    tag and the branch."""
    if not isinstance(version, str) or not SESSION_ID_RE.match(version):
        raise ValueError(
            f"Invalid version name {version!r}: must match "
            f"{SESSION_ID_RE.pattern} (no slashes, no leading dot)"
        )
    return version


def _is_publish_attempt(
    info: "Mapping[str, Any] | None", expect: "Mapping[str, Any]"
) -> bool:
    """Whether a commit's info was written by a publish attempt.

    Every key in ``_PUBLISH_IDENTITY_KEYS`` must be there, and every
    key ``expect`` names must carry the value it names; a key ``expect``
    omits must merely be present, which is all a caller holding a
    publication name and a version can require. A store tag made by
    hand carries none of them — and a store tag may hold a slash, so
    ``release/prod`` is indistinguishable from version ``prod`` of
    publication ``release`` until this is asked.
    """
    if info is None:
        return False
    if any(key not in info for key in _PUBLISH_IDENTITY_KEYS):
        return False
    return all(info.get(key) == value for key, value in expect.items())


def _source_commit(published_from: Any) -> Any:
    """The commit half of a recorded ``published_from``, or the value
    as it came where it does not parse as a ref."""
    try:
        return Ref.parse(published_from).commit
    except ValueError:
        return published_from


def _next_version(versions: Mapping[str, Any]) -> str:
    """``v<N>``, one past the highest v-number this lineage holds.

    Only names of that exact shape are counted, so ``v1, v2,
    release-1`` yields ``v3``. Versions a caller named itself are
    skipped rather than parsed: the default is a series of its own, and
    a lineage may hold both kinds.
    """
    numbers = [
        int(m.group(1))
        for m in (_VERSION_NUMBER_RE.match(v) for v in versions)
        if m is not None
    ]
    return f"v{max(numbers) + 1 if numbers else 1}"


def _version_order(version: str) -> tuple[int, int, str]:
    """Sort key: the v-numbered versions in numeric order, then anything
    a caller named itself, alphabetically."""
    m = _VERSION_NUMBER_RE.match(version)
    return (0, int(m.group(1)), "") if m else (1, 0, version)


def _under(path: str, paths: "Sequence[str]", root: str) -> bool:
    """Whether one absolute workspace path falls under any of ``paths``.

    A relative entry is taken under ``root`` (``"app/"`` means
    ``<root>/app``), an absolute one as given, and a trailing slash is
    optional either way: a directory and the file it holds are both
    legitimate things to publish, and the caller should not have to
    know which spelling this wants.
    """
    for entry in paths:
        candidate = entry if entry.startswith("/") else f"{root.rstrip('/')}/{entry}"
        prefix = "/" + candidate.strip("/")
        if path == prefix or path.startswith(f"{prefix}/"):
            return True
    return False


def _has_handlers(fs: Any, root: str) -> bool:
    """Whether a published tree holds anything a request can run.

    The rule is the router's, read off the tree instead of a request:
    a ``.py`` file directly under ``<root>/app/api`` whose name does
    not begin with ``_``. An ``_``-prefixed module there is a helper
    the handlers import and no URL routes to, and a subdirectory is
    not routable either, whatever it is named — so a tree holding only
    those has nothing to execute, exactly as an empty ``app/api`` does.

    An unreadable directory reads as no handlers: the tier it decides
    is the one that runs no code, and a tree nothing can list is not a
    tree to run code from.
    """
    base = "" if root == "/" else root
    directory = f"{base}/{_HANDLER_DIR}"
    try:
        if not fs.isdir(directory):
            return False
        names = fs.list(directory)
    except OSError:
        return False
    return any(
        n.endswith(".py") and not n.startswith("_") and fs.isfile(f"{directory}/{n}")
        for n in names
    )


def _published_rows(
    snapshot: Mapping[str, Any], published: set[str]
) -> dict[str, dict[str, Any]]:
    """The metadata rows a publication carries, by root-relative path.

    A published tree whose blobs had no rows would read as empty: a row
    is what a filesystem over the commit lists and stats. Paths are
    stored root-relative, so the leading slash comes off; the row of a
    directory above a published file comes along, and every other row —
    every file outside the published paths — is left behind with its
    blob.
    """
    rows = {p.lstrip("/") for p in published}
    out: dict[str, dict[str, Any]] = {}
    for path, meta in snapshot.items():
        fields = {
            "size": getattr(meta, "size", 0),
            "created_at": getattr(meta, "created_at", ""),
            "modified_at": getattr(meta, "modified_at", ""),
            "is_dir": getattr(meta, "is_dir", False),
        }
        if path in rows:
            out[path] = fields
        elif fields["is_dir"] and any(f.startswith(f"{path}/") for f in rows):
            out[path] = fields
    return out


class _Publishing:
    """The publications a :class:`~nontainer.store.Store` serves apps
    from: ``publish``, ``unpublish`` and the registry of versions.

    A part of ``Store``, not a class of its own: it runs on the store's
    layout, repo and tags (``_repo``, ``_branches``, ``_raw_tag_info``
    and the ``_require_*`` checks), which ``Store`` defines."""

    # ------------------------------------------------------------------
    # publications
    # ------------------------------------------------------------------

    def publish(
        self,
        ws: "Workspace",
        name: str,
        *,
        paths: "Sequence[str]" = ("app/",),
        exclude: "Sequence[str] | str" = _DEFAULT_PUBLISH_EXCLUDE,
        version: str | None = None,
        current: bool = True,
        create_only: bool = False,
        info: dict[str, Any] | None = None,
    ) -> Publication:
        """Publish part of a session's tree as an immutable version.

        What lands is the **subtree**, not the session. The published
        commit holds the files under ``paths`` and the filesystem rows
        that describe them — no cache, no working directory, no ws-git
        bookkeeping, no conversation record, nothing else the session
        happens to carry. Provenance is a soft reference in the commit's
        info (``published_from``), not a parent pointer, so the version
        is self-contained: it reads alone, it exports alone, and it pins
        none of the session's history against the orphan sweep.

        The live ``cache`` does not travel. What lands is file blobs
        and the filesystem rows describing them, and a cache entry is
        neither — a frozen open starts with an empty one. Data an app
        needs precomputed belongs in a file under ``paths``: write it
        out, commit it, publish it, and the handler reads it back.

        Three things come out of one call: a reserved branch
        ``@store/pub/<name>/<version>`` holding the derived commit (the
        anchor that lets the version be opened without borrowing a live
        session), a store-scoped tag ``<name>/<version>`` naming it, and
        a record in the store's publication registry, which becomes the
        current version of ``name`` unless ``current=False`` says
        otherwise.

        :meth:`unpublish` refuses the version a publication points at
        while others remain, so undoing a publish that went wrong means
        moving the pointer back with :meth:`set_current` first — or
        publishing with ``current=False``, which never takes it.

        The registry is shared by every process and host on the store's
        backend, and a publish takes three steps through it. It first
        RESERVES the version in the registry, which is what decides
        between two publishers wanting one version number, on one host
        or two. It then writes the branch and the tag, and last it turns
        the reservation into the version's record. Until then the
        version is not published: it is listed nowhere and resolves
        nowhere, and no other call can claim its name or clear what it
        has written so far.

        A process that dies mid-publish leaves its reservation, a
        reserved branch and usually its tag. Publishing the same thing
        again adopts them: an attempt with the same source commit,
        ``paths`` and ``exclude`` writes the same tree, so its commit
        becomes this publish's and the record is written over it. A
        publish of anything else under that version is refused by name,
        and :meth:`unpublish` clears what the dead attempt left once it
        is older than ``min_age``.

        Args:
            ws: The session to publish from. It must be one this store
                opened, and it must be clean — publish names a commit,
                and staged changes are in no commit yet. Commit them
                (``ws.commit()``) or drop them (``ws.discard()``).
            name: The publication's name — the lineage every version of
                it belongs to. Session-id shaped (no ``/``, no ``%``).
            paths: What to publish, as workspace paths. A relative path
                is taken under ``ws.root``, an absolute one as given; a
                trailing slash is optional. The default publishes the
                app tree an ``[apps]`` handler serves.
            exclude: What to leave out from under ``paths``, spelled as
                ``paths`` is. The default leaves out what an app's
                authoring loop writes beside it for the agent alone —
                ``app/logs/`` (the handler log, which holds tracebacks
                and exception messages) and ``app/screenshots/`` — so a
                version never carries them. Pass ``()`` to publish
                everything under ``paths``; to leave out more, pass the
                default's entries along with your own.
            version: The version name. Defaults to ``v<N>``, one past
                the highest ``v``-number this lineage holds — only
                names of that exact shape are counted, so a lineage
                holding ``v1``, ``v2`` and ``release-1`` gets ``v3``.
                The default is a series of its own; a version the
                caller named stands outside it and never moves it. An
                embedder that wants every version numbered passes
                ``version=`` itself. An explicit name must be unused:
                versions are immutable, so a name is never repointed.
            current: Whether this version becomes the one
                :meth:`Publication.open` serves. ``False`` records it
                and leaves what is served alone, so a caller can land
                the tree, check it at ``pub.open(version)`` and switch
                with :meth:`set_current` after — and can drop it with
                :meth:`unpublish` in between, which the current version
                refuses while others remain. The version that opens a
                lineage takes the pointer whatever this says, because a
                publication must point somewhere.
            create_only: Refuse the call if ``name`` already holds any
                version, rather than extending the lineage — for a
                caller that means to open one and would rather hear
                about a collision than silently publish a second
                version of somebody else's app. The check is part of
                the reservation, which is one compare-and-swap on the
                registry, so two publishers racing to open one lineage,
                on one host or two, get one success and one
                ``ValueError``. It closes the check-then-publish race
                only for the caller that stops doing the check: the
                swap covers this call, not a read the caller made
                before it, and a name seen free by an earlier
                ``publication()`` can be taken before this call
                reserves it.
            info: Extra keys merged into the commit's info, beside the
                ``tool``/``name``/``version``/``published_from``/``paths``/
                ``exclude`` this writes itself. They are recorded on the
                registry row as well and come back as
                :attr:`Version.info`, so listing publications with their
                metadata costs one registry read and no backend open.

        Returns:
            The :class:`Publication`, with the new version current.

        Raises:
            ValueError: The call is wrong, and only the caller can fix
                it — an embedder mapping errors to HTTP answers 400. A
                publication or version name that is not session-id
                shaped; an ``info`` key publish writes itself; a
                version name this lineage already holds, since versions
                are immutable and a name is never repointed; a
                publication name that already holds a version, under
                ``create_only``; ``paths`` that match no file at
                ``ws``'s commit once ``exclude`` is left out. Naming a
                version the registry does not hold to :meth:`set_current`,
                :meth:`unpublish` or :meth:`Publication.open` is the
                same mistake and raises the same class.
            WorkspaceError: The store is not in a state to publish, and
                the same call lands once it is — ``ws`` carries staged
                changes, ``ws`` belongs to another store, another call
                holds this version's reservation, an earlier attempt of
                something else left a tag or a branch of this version's
                name behind, an unpublish cleared this call's
                reservation before its record landed, or the derived
                commit did not land.
            NotSupportedError: This store cannot publish at all: a
                backend without branches and tags, a
                ``provider_factory`` layout the store does not own, or
                a provider that keeps no commits.
        """
        self._require_own_layout("publish")
        self._require_kvgit("publish")
        self._require_own_workspace(ws, "publish")
        _validate_publication_name(name)
        reserved = sorted(set(info or ()) & set(_RESERVED_INFO_KEYS))
        if reserved:
            raise ValueError(
                f"info may not set {', '.join(repr(k) for k in reserved)}: "
                "publish writes those into the commit itself, and the commit "
                "is where a reader checks where a version came from. Refused "
                "rather than overridden, because a false provenance in an "
                "immutable commit outlives every chance to notice it."
            )
        if ws.uncommitted:
            raise WorkspaceError(
                f"Cannot publish {ws.session!r}: it has staged changes, and "
                "publish names a commit. Land them with ws.commit() or drop "
                "them with ws.discard(), then publish."
            )
        head = ws.head
        if head is None:
            raise NotSupportedError(
                f"Cannot publish {ws.session!r}: its provider is not "
                "versioned, so it has no commit to publish."
            )

        if version is not None:
            _validate_version_name(version)
        if isinstance(exclude, str):
            exclude = (exclude,)

        # A store tag's snapshot reads through a branch it only
        # borrows, and that branch's name is no record of where the
        # state came from: the tag's ref is, and it reads back.
        source_tag = getattr(ws, "_store_tag", None)
        published_from = (
            Ref.at_tag(source_tag, head)
            if source_tag is not None
            else Ref(session=ws.session, commit=head)
        )
        # What identifies this attempt to another one: two publishes of
        # the same source commit, paths and exclusions, from the same
        # workspace root, write the same tree, so they may share a
        # reservation and a commit. The root counts because relative
        # paths are resolved against it.
        attempt = {
            "source": head,
            "root": ws.root,
            "paths": sorted(paths),
            "exclude": sorted(exclude),
        }
        token = uuid.uuid4().hex
        chosen = self._reserve_version(
            name, version, attempt, token, create_only=create_only
        )
        try:
            tag = f"{name}/{chosen}"
            branch = f"{_PUB_BRANCH_PREFIX}{name}/{chosen}"
            commit_info: dict[str, Any] = {
                "tool": "publish",
                "name": name,
                "version": chosen,
                "published_from": str(published_from),
                "paths": sorted(paths),
                "exclude": sorted(exclude),
                **(info or {}),
            }
            commit, landed = self._land_version(
                ws,
                head,
                branch=branch,
                tag=tag,
                paths=paths,
                exclude=exclude,
                commit_info=commit_info,
            )
            # The record describes the commit that is there, and an
            # adopted commit carries the info the attempt that wrote it
            # was given. A row built from this call's arguments would
            # say something the version does not serve.
            row = {
                "tag": tag,
                "ref": str(Ref(session=branch, commit=commit)),
                # The commit's own spelling: an adopted attempt may have
                # recorded its source the way an earlier release did.
                "published_from": str(landed.get("published_from") or published_from),
                "created": time.time(),
                "root": ws.root,
                "paths": list(landed.get("paths") or sorted(paths)),
                "exclude": list(landed.get("exclude") or ()),
                # The caller's keys only. What publish writes itself is
                # already spelled out by the fields around this one, and
                # the commit stays the place provenance is read from.
                "info": {
                    k: v for k, v in landed.items() if k not in _RESERVED_INFO_KEYS
                },
            }
            return self._record_version(
                name, chosen, token, attempt, row, current=current
            )
        except BaseException:
            # The reservation is this call's, and a call that failed
            # gives it back, so the version is free for a retry of
            # anything. A process that dies here cannot, and its
            # reservation waits for a retry of the same publish or for
            # unpublish (see the docstring).
            self._release_quietly(name, chosen, token)
            raise

    def publications(self) -> dict[str, Publication]:
        """Every publication on the store, by name: one registry read."""
        registry = self._registry_read()
        return {
            name: self._publication(name, registry)
            for name in sorted(registry)
            if _live(registry[name])
        }

    def publication(self, name: str) -> Publication | None:
        """One publication by name, or ``None`` if nothing of that name
        is published."""
        registry = self._registry_read()
        if not _live(registry.get(name)):
            return None
        return self._publication(name, registry)

    def set_current(self, name: str, version: str) -> Publication:
        """Point a publication at one of its versions.

        The registry is the mutable half of a publication: the versions
        themselves never move (they are tags), and this is what says
        which one is served. Rolling back moves as easily as rolling
        forward — for **code**. Data a version's handlers wrote lives
        outside the workspace and does not roll back with it.
        """

        def point(registry: dict[str, Any]) -> Publication:
            record = registry.get(name)
            if not _live(record):
                raise ValueError(f"No such publication: {name!r}")
            versions = record.get("versions") or {}
            if version not in versions:
                raise ValueError(
                    f"No such version of {name!r}: {version!r} — have "
                    f"{', '.join(sorted(versions))}"
                )
            record["current"] = version
            registry[name] = record
            return self._publication(name, registry)

        return self._update_registry(point)

    def set_meta(self, name: str, meta: "Mapping[str, Any]") -> Publication:
        """Replace a publication's own metadata; returns the row as it
        now reads.

        The mutable overlay beside the versions: a display title, an
        owner, a blurb — what changes about a published app without a
        release. ``Version.info`` is the other half and the immutable
        one, written at publish and never edited, so a reader can tell
        what a version was shipped as apart from what its publication
        is called today.

        The mapping is replaced whole rather than merged, so dropping a
        key is spelled the same way as changing one: pass what the
        publication should read as. ``{}`` clears it. Values must be
        JSON-serializable, because the registry is stored as JSON, and
        what lands is a copy: editing a nested list afterwards says
        nothing about the publication. A mapping read back off a
        record is accepted as it comes, so ``set_meta(name, {**pub.meta,
        "title": "New"})`` is the way to change one key.

        The read, the replacement and the write are one compare-and-swap
        on the registry, so a concurrent publish or ``set_current`` on
        another publication, from this process or another host, is not
        lost.
        """
        replacement = _validate_meta(meta)

        def write(registry: dict[str, Any]) -> Publication:
            record = registry.get(name)
            if not _live(record):
                raise ValueError(f"No such publication: {name!r}")
            record["meta"] = replacement
            registry[name] = record
            return self._publication(name, registry)

        return self._update_registry(write)

    def unpublish(self, name: str, version: str, *, min_age: float = 3600) -> None:
        """Remove one published version: its tag, its branch, its record.

        The current version is refused while others remain — something
        is being served off it, and there is no obvious successor to
        pick. Move the pointer with :meth:`set_current` first, or
        publish the version with ``current=False`` so it never takes
        the pointer and can be dropped as it stands. The last version
        of a publication may be removed however it is pointed at, and
        takes the publication's record with it.

        The record goes first, as a reservation of the version that
        this call holds while it removes the tag and the branch, so no
        publish can claim the version or write its branch until they
        are gone. The version stops resolving the moment the record
        goes. If removing them fails, the record is put back and the
        call raises: a failed unpublish leaves the version published,
        and calling it again finishes the job.

        A version with no record but a branch or a tag of its name is
        what a publish that died before writing its record leaves
        behind, and it is cleared here too: the branch and the tag go,
        and the name is free to publish again. A publish or an unpublish
        still under way holds a reservation of the version, on this host
        or another, and it is left alone until the reservation is older
        than ``min_age``: the age is all that tells a call that is slow
        from one that died. Pass ``min_age=0`` to take over one known to
        be dead. Only what publish itself wrote is removed: a store
        tag may hold a slash, so a durable ``release/prod`` somebody
        tagged by hand answers to ``unpublish("release", "prod")`` by
        name alone; the tag's and the branch's own provenance is checked
        first, and one that does not carry it is left exactly as it is
        and the call raises ``ValueError``.

        ``min_age`` is also the orphan sweep's grace period in seconds,
        as for :meth:`delete`.
        """
        self._require_own_layout("unpublish")
        self._require_kvgit("unpublish")
        token = uuid.uuid4().hex
        claim = self._claim_for_unpublish(name, version, token, min_age=min_age)
        try:
            if claim.row is not None:
                self._remove_version_state(
                    claim.row.get("tag") or f"{name}/{version}",
                    f"{_PUB_BRANCH_PREFIX}{name}/{version}",
                    min_age=min_age,
                )
            elif (
                not self._clear_stranded_attempt(name, version, min_age=min_age)
                and not claim.took_over
            ):
                if not claim.live:
                    raise ValueError(f"No such publication: {name!r}")
                raise ValueError(
                    f"No such version of {name!r}: {version!r} — have "
                    f"{', '.join(claim.live)}"
                )
        except BaseException:
            if claim.row is not None:
                self._restore_quietly(name, version, token, claim.row)
            else:
                self._release_quietly(name, version, token)
            raise
        self._release(name, version, token)

    # -- the publication registry ----------------------------------------
    #
    # The immutable half of a publication is a tag and a branch; this is
    # the mutable half — which versions exist and which one is current —
    # held as one JSON value under ``_REGISTRY_KEY`` in the store's own
    # kvgit backend. Every change is a read, a decision and a
    # compare-and-swap expecting the bytes that were read, so it is
    # atomic across threads, processes and hosts, on disk and on
    # PostgreSQL alike, with no lock to hold or to lose.
    #
    # Beside a publication's live ``versions``, its record may hold
    # ``pending`` reservations: a version a publish has claimed and not
    # yet recorded, or one an unpublish is still removing. A reservation
    # is what keeps two calls off one version while either is writing
    # outside the registry, and it carries a token, so only the call
    # that made it (or one that took it over) can give it back.

    def _legacy_registry_path(self) -> Path | None:
        """The file a release before the shared registry kept it in, or
        ``None`` for a store that never had one: one with no directory
        of its own, or no kvgit backend to publish on."""
        if (
            self._provider_factory is not None
            or self._path is None
            or self._backend != "kvgit"
        ):
            return None
        return self._path / _LEGACY_REGISTRY_FILE

    def _registry_get(self) -> bytes | None:
        """The registry's stored bytes, or ``None`` when none are stored.

        A ``provider_factory`` store has no backend of its own to keep
        the registry in, so it keeps it on this object. It cannot
        publish (:meth:`publish` needs the store's own layout), so its
        registry holds only what is written to it directly.
        """
        if self._provider_factory is not None:
            return self._memory_registry
        if self._backend != "kvgit":
            return None
        repo = self._kvgit_repo()
        return None if repo is None else repo.store.get(_REGISTRY_KEY)

    def _registry_put(self, expected: bytes | None, registry: dict[str, Any]) -> bool:
        """Store ``registry`` if the stored bytes are still ``expected``
        (``None``: nothing stored); False, with nothing written, when
        another writer changed them first."""
        raw = _encode_registry(registry)
        if self._provider_factory is not None:
            with self._memory_registry_lock:
                if self._memory_registry != expected:
                    return False
                self._memory_registry = raw
                return True
        if self._backend != "kvgit":
            raise NotSupportedError(
                f"The {self._backend!r} backend has no publication registry: "
                "publishing needs the kvgit backend."
            )
        repo = self._kvgit_repo(create=True)
        return repo.store.cas_many({_REGISTRY_KEY: expected}, {_REGISTRY_KEY: raw})

    def _registry_read(self) -> dict[str, Any]:
        """The registry as it is now: one read of the backend."""
        self._import_legacy_registry()
        return _decode_registry(self._registry_get(), self._registry_where())

    def _registry_where(self) -> str:
        """Where the registry is, for a message about it."""
        return f"key {_REGISTRY_KEY!r} of {self!r}"

    def _update_registry(self, change: "Callable[[dict[str, Any]], Any]") -> Any:
        """Read the registry, apply ``change`` to it, write it back.

        The one path every mutation takes. ``change`` mutates the
        registry it is handed and returns whatever the caller wants
        back. The write expects the bytes the read returned, so a
        decision made from the read (which version number is next,
        whether a name is free) is still true when the write lands; when
        another writer got there first, the registry is read again and
        ``change`` applied again, so ``change`` must do nothing but
        decide. Nothing is written if ``change`` raises, or leaves the
        registry as it was.
        """
        self._import_legacy_registry()
        return self._change_registry(change)

    def _change_registry(self, change: "Callable[[dict[str, Any]], Any]") -> Any:
        """:meth:`_update_registry` without the legacy import, which is
        itself one of these changes."""
        where = self._registry_where()
        for attempt in range(_REGISTRY_ATTEMPTS):
            raw = self._registry_get()
            before = _decode_registry(raw, where)
            registry = _decode_registry(raw, where)
            result = change(registry)
            if registry == before or self._registry_put(raw, registry):
                return result
            # Another writer landed between the read and the write. Back
            # off a little, at random, so writers retrying together
            # spread out instead of colliding again.
            time.sleep(random.uniform(0, min(0.05, 0.001 * 2**attempt)))
        raise WorkspaceError(
            f"The publication registry kept changing under this call: "
            f"{_REGISTRY_ATTEMPTS} attempts, each overtaken by another "
            "writer. Nothing was written; the call lands once the store is "
            "less busy."
        )

    def _import_legacy_registry(self) -> None:
        """Fold a registry file an earlier release kept into the shared
        registry, once per store object.

        Each host sharing a backend kept a file of its own, so each one
        imports its own: the file's records are merged into whatever the
        shared registry already holds, never written over it. A version
        the shared registry already has, or holds a reservation for,
        keeps what the registry says. A version whose store tag does not
        name the commit its record names is not imported: the tag and
        the branch are shared, and a record they no longer back is one
        an unpublish on another host already removed, or one written
        over a version some other host published since. Those are
        logged rather than raised, since nothing can serve them.

        The file is then renamed ``publications.json.imported``, so it
        stays readable by hand and is not imported again. A store with
        no kvgit repository yet has nothing a record could name, and is
        left alone.
        """
        if self._registry_imported:
            return
        path = self._legacy_registry_path()
        if path is None or not path.is_file():
            self._registry_imported = True
            return
        if self._kvgit_repo() is None:
            return
        try:
            data = json.loads(path.read_text())
        except FileNotFoundError:
            # Another handle on this directory imported it first.
            self._registry_imported = True
            return
        except (OSError, ValueError) as e:
            raise WorkspaceError(
                f"Publication registry is unreadable: {path} ({e}). An "
                "earlier release kept the registry in this file, and its "
                "records are imported into the store once it reads. The "
                "publications themselves are tags and branches on the store "
                "and are still there."
            ) from e
        records = data if isinstance(data, dict) else {}
        backed, dropped = self._backed_versions(records)
        if dropped:
            _logger.warning(
                "Not importing %s from %s: the store's tags no longer name "
                "the commits those records name.",
                ", ".join(dropped),
                path,
            )

        def merge(registry: dict[str, Any]) -> None:
            for name, record in records.items():
                rows = {
                    v: row
                    for v, row in ((record or {}).get("versions") or {}).items()
                    if (name, v) in backed
                }
                if not rows:
                    continue
                shared = dict(registry.get(name) or {})
                versions = dict(shared.get("versions") or {})
                pending = shared.get("pending") or {}
                added = {
                    v: row
                    for v, row in rows.items()
                    if v not in versions and v not in pending
                }
                if not added:
                    continue
                versions.update(added)
                current = shared.get("current")
                if current not in versions:
                    current = record.get("current")
                if current not in versions:
                    current = sorted(versions, key=_version_order)[-1]
                shared.update(
                    versions=versions,
                    current=current,
                    meta=shared.get("meta") or record.get("meta") or {},
                )
                registry[name] = shared

        self._change_registry(merge)
        try:
            os.replace(path, path.with_name(path.name + _IMPORTED_SUFFIX))
        except FileNotFoundError:
            pass  # another handle on this directory imported it first
        self._registry_imported = True

    def _backed_versions(
        self, records: Mapping[str, Any]
    ) -> tuple[set[tuple[str, str]], list[str]]:
        """Which of a legacy file's versions the store still backs: their
        store tag names the commit their record names. Returns those as
        ``(name, version)`` pairs, and the rest spelled ``name/version``
        for a message."""
        backed: set[tuple[str, str]] = set()
        dropped: list[str] = []
        for name, record in records.items():
            if not isinstance(record, Mapping):
                continue
            for v, row in (record.get("versions") or {}).items():
                try:
                    commit = Ref.parse(row["ref"]).commit
                    tag = self._raw_tag_info(row.get("tag") or f"{name}/{v}")
                except (KeyError, TypeError, ValueError):
                    tag = None
                if tag is not None and not tag.dangling and tag.id == commit:
                    backed.add((name, v))
                else:
                    dropped.append(f"{name}/{v}")
        return backed, dropped

    def _reserve_version(
        self,
        name: str,
        version: str | None,
        attempt: Mapping[str, Any],
        token: str,
        *,
        create_only: bool,
    ) -> str:
        """Claim the version a publish will write; returns its name.

        One compare-and-swap decides it, so two publishers, on one host
        or two, never claim one version. ``version=None`` takes the next
        free ``v<N>``, past every version the lineage holds or another
        call has reserved, unless an earlier attempt of this same
        publish reserved a v-numbered version and died: that one is
        resumed. An explicit version must be unpublished and unreserved,
        or reserved by an attempt of this same publish.
        """

        def reserve(registry: dict[str, Any]) -> str:
            record = dict(registry.get(name) or {})
            versions = record.get("versions") or {}
            pending = dict(record.get("pending") or {})
            others = sorted(
                (
                    v
                    for v, entry in pending.items()
                    if not _same_attempt(entry, attempt)
                ),
                key=_version_order,
            )
            if create_only and versions:
                raise ValueError(
                    f"Publication already published: {name!r} — it holds "
                    f"{', '.join(sorted(versions))} and create_only= asked "
                    "for a new lineage. Publish under another name, or drop "
                    "create_only= to add a version to this one."
                )
            if create_only and others:
                raise ValueError(
                    f"Publication already being published: {name!r} — another "
                    f"call holds {', '.join(others)} and create_only= asked for "
                    "a new lineage. Publish under another name, or drop "
                    "create_only= to add a version to this one."
                )
            if version is None:
                resumed = sorted(
                    (
                        v
                        for v, entry in pending.items()
                        if _same_attempt(entry, attempt) and _VERSION_NUMBER_RE.match(v)
                    ),
                    key=_version_order,
                )
                chosen = (
                    resumed[0] if resumed else _next_version({**versions, **pending})
                )
            else:
                chosen = version
                if chosen in versions:
                    raise ValueError(
                        f"Version already published: {name}/{chosen} — versions "
                        "are immutable. Publish a new one, or unpublish that one "
                        "first."
                    )
                entry = pending.get(chosen)
                if entry is not None and not _same_attempt(entry, attempt):
                    raise WorkspaceError(_busy(name, chosen, entry))
            pending[chosen] = {
                "op": "publish",
                "token": token,
                "since": time.time(),
                **attempt,
            }
            record.setdefault("versions", {})
            record.setdefault("current", None)
            record.setdefault("meta", {})
            record["pending"] = pending
            registry[name] = record
            return chosen

        return self._update_registry(reserve)

    def _record_version(
        self,
        name: str,
        version: str,
        token: str,
        attempt: Mapping[str, Any],
        row: dict[str, Any],
        *,
        current: bool,
    ) -> Publication:
        """Turn a publish's reservation into the version's record.

        The reservation must still be this call's, or one an attempt of
        this same publish took over: the commit both wrote is the same.
        A version already recorded at this commit was recorded by such
        an attempt, and reads as published. Anything else means an
        unpublish took the reservation over first, and the version is
        refused rather than recorded over whatever that left.
        """
        commit = Ref.parse(row["ref"]).commit

        def record(registry: dict[str, Any]) -> Publication:
            held = dict(registry.get(name) or {})
            versions = dict(held.get("versions") or {})
            pending = dict(held.get("pending") or {})
            recorded = versions.get(version)
            if recorded is not None:
                if Ref.parse(recorded["ref"]).commit == commit:
                    return self._publication(name, registry)
                raise WorkspaceError(
                    f"{name}/{version} was recorded at another commit while this "
                    "publish held its reservation. Nothing was recorded for "
                    "this call."
                )
            entry = pending.get(version)
            if entry is None or not (
                entry.get("token") == token or _same_attempt(entry, attempt)
            ):
                raise WorkspaceError(
                    f"{name}/{version} was cleared before this publish could "
                    "record it: an unpublish took over its reservation. Nothing "
                    "was published; publish again."
                )
            pointer = held.get("current")
            # The version that opens a lineage takes the pointer
            # whatever the caller asked: a publication must point
            # somewhere, and there is nothing else to point at.
            held["current"] = version if current or pointer not in versions else pointer
            versions[version] = row
            held["versions"] = versions
            # Publishing says nothing about the publication's own
            # metadata: an existing one keeps what set_meta wrote, and a
            # new lineage starts with none.
            held["meta"] = dict(held.get("meta") or {})
            pending.pop(version)
            if pending:
                held["pending"] = pending
            else:
                held.pop("pending", None)
            registry[name] = held
            return self._publication(name, registry)

        return self._update_registry(record)

    def _claim_for_unpublish(
        self, name: str, version: str, token: str, *, min_age: float
    ) -> _Claim:
        """Take a version out of the registry and reserve it for the
        removal of its tag and branch.

        The reservation is taken even when the registry holds nothing
        of the version, before anything looks for a stranded branch or
        tag: looking first and claiming after would leave a moment in
        which a publish could reserve the version and write its branch,
        and the removal would then take a version that publish is still
        writing. Another call's reservation younger than ``min_age`` is
        refused, a publish's or an unpublish's alike: that call may still
        be writing or removing, here or on another host, and an
        unpublish that took over a live one's reservation would free the
        version while the first was still removing, so a publish could
        reclaim it and lose its new branch to the first remover. An
        older one is taken over.
        """

        def claim(registry: dict[str, Any]) -> _Claim:
            record = dict(registry.get(name) or {})
            versions = dict(record.get("versions") or {})
            pending = dict(record.get("pending") or {})
            row = versions.pop(version, None)
            took_over = False
            if row is not None:
                if record.get("current") == version and versions:
                    raise WorkspaceError(
                        f"{name}/{version} is the current version and is not "
                        f"the last one. Point {name!r} at another version with "
                        "store.set_current(name, version) first, or remove the "
                        "others."
                    )
                if record.get("current") == version:
                    record["current"] = None
            elif version in pending:
                # Another call's reservation, publish or unpublish: it
                # may still be writing or removing, here or on another
                # host, and only its age says it is not.
                entry = pending[version]
                age = time.time() - float(entry.get("since") or 0.0)
                if age < min_age:
                    raise WorkspaceError(_busy(name, version, entry))
                took_over = True
            pending[version] = {"op": "unpublish", "token": token, "since": time.time()}
            record["versions"] = versions
            record.setdefault("current", None)
            record.setdefault("meta", {})
            record["pending"] = pending
            registry[name] = record
            return _Claim(
                row=row,
                took_over=took_over,
                live=tuple(sorted(versions, key=_version_order)),
            )

        return self._update_registry(claim)

    def _release(self, name: str, version: str, token: str) -> None:
        """Give back a reservation this call holds. A publication left
        with no versions and no reservations goes, and its ``meta`` with
        it, so republishing the name starts empty. A reservation another
        call has taken over is that call's, and is left alone."""

        def release(registry: dict[str, Any]) -> None:
            record = registry.get(name)
            pending = dict((record or {}).get("pending") or {})
            if (pending.get(version) or {}).get("token") != token:
                return
            pending.pop(version)
            record = dict(record)
            if pending:
                record["pending"] = pending
            else:
                record.pop("pending", None)
            if record.get("versions") or pending:
                registry[name] = record
            else:
                registry.pop(name)

        self._update_registry(release)

    def _restore_quietly(
        self, name: str, version: str, token: str, row: dict[str, Any]
    ) -> None:
        """Put back the record an unpublish took out, from a call whose
        removal failed, and give back its reservation with it.

        Whatever the removal got through, the version is published again
        rather than dropped from every listing with its tag or branch
        still on the store, and the failed call's own error is the one
        it raises. A publication the removal left with no versions points
        at this one again. A reservation another call has taken over is
        that call's, and is left alone.
        """

        def restore(registry: dict[str, Any]) -> None:
            record = registry.get(name)
            pending = dict((record or {}).get("pending") or {})
            if (pending.get(version) or {}).get("token") != token:
                return
            pending.pop(version)
            record = dict(record)
            versions = dict(record.get("versions") or {})
            versions[version] = row
            record["versions"] = versions
            if record.get("current") not in versions:
                record["current"] = version
            if pending:
                record["pending"] = pending
            else:
                record.pop("pending", None)
            registry[name] = record

        try:
            self._update_registry(restore)
        except Exception:
            _logger.warning(
                "Could not put back the record of %s/%s",
                name,
                version,
                exc_info=True,
            )

    def _release_quietly(self, name: str, version: str, token: str) -> None:
        """:meth:`_release` from a call that is already failing: the
        failure it is handling is the one to raise, and a reservation
        left behind is cleared by a retry or by :meth:`unpublish`."""
        try:
            self._release(name, version, token)
        except Exception:
            _logger.warning(
                "Could not give back the reservation of %s/%s",
                name,
                version,
                exc_info=True,
            )

    def _publication(self, name: str, registry: dict[str, Any]) -> Publication:
        record = registry[name]
        rows = record.get("versions") or {}
        versions = tuple(
            Version(
                name=name,
                version=v,
                tag=row.get("tag") or f"{name}/{v}",
                ref=Ref.parse(row["ref"]),
                published_from=(
                    Ref.parse(row["published_from"])
                    if row.get("published_from")
                    else None
                ),
                created=float(row.get("created") or 0.0),
                info=_frozen_json(row.get("info") or {}),
                paths=tuple(row.get("paths") or ()),
                exclude=tuple(row.get("exclude") or ()),
            )
            for v, row in sorted(rows.items(), key=lambda kv: _version_order(kv[0]))
        )
        current = record.get("current") or (versions[-1].version if versions else "")
        return Publication(
            name=name,
            versions=versions,
            current=current,
            store=self,
            meta=_frozen_json(record.get("meta") or {}),
        )

    def _open_publication(self, version: Version, **settings: Any) -> "Workspace":
        """The frozen workspace behind :meth:`Publication.open`.

        The registry is re-read here, not trusted from the
        :class:`Publication` the caller is holding: that object is a
        snapshot of the registry when it was fetched, and a version it
        names may have been unpublished since. Opening one anyway would
        serve unpublished content for as long as the orphan sweep's
        grace period, which is the opposite of what unpublish means.

        The re-read and the open need no lock between them. The open is
        by commit and creates no branch, so an unpublish that lands in
        between leaves nothing behind it, and the version reads for as
        long as one opened just before that unpublish would.

        The tier is decided here, on the tree the open is about to
        serve: a publication with no handler under ``app/api/`` is
        served as files and is given no executor, which is why this is
        the one frozen open that decides it. Every other one
        (``store.resolve``, ``store.tags.at``) names an arbitrary
        state, and a snapshot of a session is a legitimate thing to
        run code against whatever its tree holds.
        """
        self._require_own_layout("Publication.open")
        self._require_kvgit("Publication.open")
        row = self._registry_row(version.name, version.version)
        gone = WorkspaceError(
            f"No longer published: {version.name}/{version.version} — "
            "it was unpublished after this Publication was fetched. "
            f"Re-read it with store.publication({version.name!r})."
        )
        if row is None:
            raise gone
        ref = Ref.parse(row["ref"])
        root = row.get("root")
        from kvgit import KvgitError

        try:
            provider = self._provider_at_commit(ref.session, ref.commit)
        except (WorkspaceError, KvgitError):
            # Its branch went after the row was read, before or while
            # the open walked it: an unpublish landed in between, and
            # says so better than the branch or the backend does.
            if self._registry_row(version.name, version.version) is None:
                raise gone from None
            raise
        if not _has_handlers(provider.fs, root or _DEFAULT_ROOT):
            from ..executor import NoExecutor

            settings["executor"] = NoExecutor(
                f"{version.name}/{version.version} opened static: its "
                f"published tree holds no handler under {_HANDLER_DIR}/, so "
                "it was opened with no executor and nothing here can run "
                "code. Serving its files needs none; to run code against "
                f"this tree, open its ref instead — store.resolve({str(ref)!r})."
            )
        return self._frozen_workspace(provider, root=root, **settings)

    def _registry_row(self, name: str, version: str) -> dict[str, Any] | None:
        """One version's live record, or ``None``: one registry read."""
        record = self._registry_read().get(name) or {}
        return (record.get("versions") or {}).get(version)

    def _land_version(
        self,
        ws: "Workspace",
        head: str,
        *,
        branch: str,
        tag: str,
        paths: "Sequence[str]",
        exclude: "Sequence[str]",
        commit_info: dict[str, Any],
    ) -> "tuple[str, dict[str, Any]]":
        """Write a reserved version's branch and tag, or adopt what an
        attempt of this same publish wrote; returns the commit and the
        info it carries.

        The reservation keeps every other publish of this version away,
        but not another attempt of this same publish, which may be
        writing the same tree at the same moment. So the commit is
        decided by the branch's own compare-and-swap: one attempt's
        commit lands, and an attempt that finds the branch moved under
        it adopts that commit instead of writing a second.

        A branch that already holds a commit no record names was left by
        an attempt that died. Its commit is adopted when it is this same
        publish's, and refused when it is not.
        """

        def holds(commit: str) -> bool:
            # The tree this call would write lies under its paths at its
            # own root, outside its exclusions. Relative paths are
            # resolved against the root, so a call from another root
            # publishes another tree from the same commit and paths.
            with self._repo() as repo:
                files = ws._provider._file_keys(repo.snapshot(commit=commit).keys())
            return bool(files) and all(
                _under(path, paths, ws.root) and not _under(path, exclude, ws.root)
                for path in files.values()
            )

        for _ in range(3):
            if self._branch_head(branch)[0] is not None:
                resumed = self._resume_publication(branch, tag, commit_info, holds)
                if resumed is None:
                    name, version = commit_info["name"], commit_info["version"]
                    raise WorkspaceError(
                        f"Publication branch already exists: {branch!r}, no "
                        "record names it, and its commit is not the one this "
                        "call would write — an earlier publish of something "
                        "else left it behind. Clear it with "
                        f"store.unpublish({name!r}, {version!r}), then publish "
                        "again."
                    )
                return resumed
            if self._scoped_store_tag(tag) in self._raw_tags():
                raise WorkspaceError(
                    f"Tag already exists: {tag!r} in scope 'store' — a "
                    "publication cannot reuse it."
                )
            commit = self._write_publication(
                ws,
                head,
                branch=branch,
                paths=paths,
                exclude=exclude,
                tag=tag,
                commit_info=commit_info,
            )
            if commit is not None:
                return commit, commit_info
        raise WorkspaceError(
            f"Could not write {branch!r}: it kept changing under this publish. "
            "Nothing was recorded; publish again."
        )

    def _branch_head(self, branch: str) -> tuple[str | None, dict[str, Any] | None]:
        """The commit at a branch's head and the info it carries.

        ``(None, None)`` when the store has no such branch, or when the
        branch holds nothing yet: a head with an empty tree and no info
        is a branch created and never written — what a publish that
        died between making its reserved branch and committing to it
        leaves behind. Such a branch holds nobody's state, so it neither
        blocks a retry nor needs anyone's provenance to be cleared.
        """
        with self._repo() as repo:
            if repo is None or branch not in repo.branches:
                return None, None
            commit = repo.get_commit(repo.branches[branch])
            if _holds_nothing(commit):
                return None, None
            return commit.hash, commit.info

    def _clear_stranded_attempt(
        self, name: str, version: str, *, min_age: float
    ) -> bool:
        """Remove what a publish that died before its record left behind.

        True when something was cleared, ``False`` when the store holds
        nothing of that name. Whatever is there has to prove it came
        from a publish of this name and this version before any of it
        is touched: a store tag may hold a slash, so a durable
        ``release/prod`` somebody tagged by hand answers to
        ``unpublish("release", "prod")`` by name alone, and deleting it
        would drop a tag nobody published and make its commit
        collectable. A tag or a branch that does not carry publish's
        own provenance is left exactly as it is and the call raises.
        """
        expect = {"tool": "publish", "name": name, "version": version}
        tag = f"{name}/{version}"
        branch = f"{_PUB_BRANCH_PREFIX}{name}/{version}"
        found = self._raw_tag_info(tag)
        has_branch = branch in set(self._branches())
        if found is None and not has_branch:
            return False
        head, head_info = self._branch_head(branch) if has_branch else (None, None)
        tag_blocks = found is not None and not _is_publish_attempt(found.info, expect)
        branch_blocks = head is not None and not _is_publish_attempt(head_info, expect)
        if tag_blocks or branch_blocks:
            blocking = ", ".join(
                part
                for part, blocked in (
                    (f"store tag {tag!r}", tag_blocks),
                    (f"branch {branch!r}", branch_blocks),
                )
                if blocked
            )
            hint = (
                f" Delete it with store.tags.delete({tag!r}) if that is what you meant."
                if tag_blocks
                else ""
            )
            raise ValueError(
                f"No publication of {name!r} at version {version!r}. What "
                f"carries that name — {blocking} — was not written by "
                f"publish, so it is left where it is.{hint}"
            )
        self._remove_version_state(tag, branch, min_age=min_age)
        return True

    def _remove_version_state(self, tag: str, branch: str, *, min_age: float) -> bool:
        """Take out a published version's tag and its branch.

        True when either was there, which is what tells a caller
        holding no registry record that something was cleared. The tag
        goes first: removing it is what makes the commit collectable,
        so the branch deletion's own sweep finishes the job in one
        pass. One already gone is no failure: an unpublish that took
        over another's reservation may be removing the same two things
        beside it.
        """
        from kvgit import UnknownTagError

        from ..providers.kvgit import KvgitProvider

        found = self._raw_tag_info(tag) is not None
        if found:
            try:
                self._delete_store_tag(tag)
            except (CommitNotFoundError, UnknownTagError):
                pass
        if branch in set(self._branches()):
            found = True
        repo = self._kvgit_repo()
        if repo is not None:
            KvgitProvider.delete_in(repo, {branch}, min_age=min_age)
        return found

    def _resume_publication(
        self,
        branch: str,
        tag: str,
        commit_info: Mapping[str, Any],
        holds: "Callable[[str], bool]",
    ) -> "tuple[str, dict[str, Any]] | None":
        """Adopt the commit an attempt of this same publish wrote.

        The branch and the tag are written before the record, so an
        attempt that died before its record, or one still writing
        beside this call, leaves a reserved branch no record names. The
        retry rebuilds the same commit info, and a branch whose head
        carries the same ``published_from`` commit, the same ``paths``
        and the same ``exclude`` was written by an attempt of this same
        publish: its commit is returned to be recorded as this
        publish's, and the tag is minted if it is not there yet.
        ``None`` says the branch holds some other attempt's commit, which
        this publish may not adopt and may not overwrite.

        The commit and the info IT carries come back together, and that
        info is what the record and a late-minted tag describe. The
        adopted commit is immutable, so the resuming call's own ``info``
        landed nowhere: writing that into the record or the tag would
        describe the version as something it is not.

        The whole tree is not compared. What identifies the attempt is
        what it was told to do — one source commit, one set of paths and
        exclusions — because publishing that twice writes the same files
        either way. The source is compared by its COMMIT: the ref's
        other half is how it was spelled, and a store tag's snapshot
        recorded the branch it borrowed before it recorded the tag. The
        commit does not record the workspace root its relative paths
        were resolved against, so ``holds`` asks of its tree instead:
        whether every file lies where this call would publish from,
        which a call from another root does not.
        """
        commit, found = self._branch_head(branch)
        if commit is None:
            return None
        expect = {
            key: commit_info.get(key)
            for key in _RESERVED_INFO_KEYS
            if key != "published_from"
        }
        if not _is_publish_attempt(found, expect) or _source_commit(
            (found or {}).get("published_from")
        ) != _source_commit(commit_info.get("published_from")):
            return None
        if not holds(commit):
            return None
        landed = dict(found or {})
        with self._repo() as repo:
            if not self._mint_tag(repo, branch, tag, commit, landed):
                return None
        return commit, landed

    def _mint_tag(
        self, repo: Any, branch: str, tag: str, commit: str, info: Mapping[str, Any]
    ) -> bool:
        """Name ``commit`` with a version's store tag; False when the tag
        already names some other commit.

        Two attempts of one publish may both reach here for the same
        commit, and only one creates the tag: the other finds it naming
        that commit, which is the outcome it wanted. The tag is created
        through a provider on the branch that holds the commit, so the
        store scope's name rules apply.
        """
        from ..providers.kvgit import KvgitProvider

        existing = self._raw_tag_info(tag, repo)
        if existing is None:
            try:
                KvgitProvider(repo, repo.worktree(branch), session=branch).tag(
                    tag, at=commit, info=dict(info), scope="store"
                )
                return True
            except WorkspaceError:
                existing = self._raw_tag_info(tag, repo)
                if existing is None:
                    raise
        return existing.id == commit

    def _write_publication(
        self,
        ws: "Workspace",
        head: str,
        *,
        branch: str,
        paths: "Sequence[str]",
        exclude: "Sequence[str]",
        tag: str,
        commit_info: dict[str, Any],
    ) -> str | None:
        """Build the derived commit on its own branch and tag it;
        returns the commit, or ``None`` when another attempt of this
        same publish committed to the branch first, for the caller to
        adopt.

        The branch is created at the store's empty root commit, so the
        publication's only ancestor is nothing at all. Into it go
        the selected file blobs and, beside each one, the metadata row
        the source filesystem holds for that path (plus the rows of the
        directories above them), which is what makes a frozen open read
        the published tree and see nothing else. The rows are asked of a
        filesystem over the source commit rather than copied key by key,
        so a source commit written before per-file rows publishes rows
        like any other (monkeyfs reads its ``__vfs_metadata__`` table for
        a path with no row), and the table itself never travels. A
        publication is therefore always in the current layout.

        The commit lands only on the empty root it was built over,
        decided by the branch's compare-and-swap, so a second attempt
        writing the same tree beside this one never stacks a second
        commit on the first.

        Blobs are copied rather than pointed at. That is fine at app
        sizes, and content addressing in the store below would make the
        copy free — the keys are identical, so the same bytes would land
        under the same hash.
        """
        from kvgit import BranchExistsError, BranchMovedError
        from monkeyfs import VirtualFS

        src = ws._provider
        handle = src._snapshot(head)
        wanted = {
            key: path
            for key, path in src._file_keys(handle.keys()).items()
            if _under(path, paths, ws.root) and not _under(path, exclude, ws.root)
        }
        if not wanted:
            left_out = f" once {', '.join(exclude)!r} is left out" if exclude else ""
            raise ValueError(
                f"Nothing to publish from {ws.session!r} at {head}: no files "
                f"under {', '.join(paths)!r}{left_out}. Publish paths that "
                "exist, or widen paths=."
            )
        rows = _published_rows(
            VirtualFS(handle).get_metadata_snapshot(), set(wanted.values())
        )

        with self._repo(create=True) as repo:
            if branch not in repo.branches:
                try:
                    repo.branches.create(branch)  # at the empty root
                except BranchExistsError:
                    pass  # another attempt made it; its head decides below
            pub = repo.worktree(branch)
            if not _holds_nothing(repo.get_commit(pub.head)):
                return None
            pub_fs = VirtualFS(pub)
            for key in wanted:
                pub[key] = handle.get(key)
            for row_path, fields in rows.items():
                pub[pub_fs.metadata_key("/" + row_path)] = json.dumps(
                    fields, sort_keys=True
                ).encode()
            try:
                result = pub.commit(info=commit_info, on_moved="raise")
            except BranchMovedError:
                return None
            if not result.merged:
                raise WorkspaceError(
                    f"publish failed: could not commit the derived tree on "
                    f"{branch!r}: {result}"
                )
            commit = pub.head
            if not self._mint_tag(repo, branch, tag, commit, commit_info):
                raise WorkspaceError(
                    f"Tag already exists: {tag!r} in scope 'store', naming "
                    "another commit — a publication cannot reuse it."
                )
            return commit
