"""Naming a frozen state: a :class:`Ref`, and the store-scoped tags."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..errors import WorkspaceError
from ..protocol import (
    TagInfo,
)
from .layout import (
    _TAG_REF_PREFIX,
    _check_frozen_settings,
)

if TYPE_CHECKING:
    from ..workspace import Workspace
    from .core import Store


@dataclass(frozen=True)
class Ref:
    """A pointer into a store: ``session@commit`` with an optional
    path, spelled ``session@commit:/path``.

    A commit id alone says *what* the state is but not *where* it came
    from, and a session id alone names a moving head. A ref names one
    exact state on one session, which is what a snapshot, a
    publication, or a cross-session read has to quote.

    ``path`` is carried but not yet interpreted: it is the spelling a
    later sparse read (``store.resolve("a@b:/app")``) will use.

    A ref may also name a STORE TAG instead of a session, and then
    :attr:`tag` is the name and the session half is the store's own
    reserved spelling for one. Such a ref is still one exact state —
    the commit the tag names — and it has no branch behind it, which
    is what lets it be read after the session that reached that commit
    is deleted.
    """

    session: str
    commit: str
    path: str | None = None

    @property
    def tag(self) -> str | None:
        """The store tag this ref names, or ``None`` when it names a
        session.

        A store tag belongs to no session, so the tag takes the place
        of the session half. Reads of such a ref are frozen at its
        commit, since there is no branch to follow."""
        if self.session.startswith(_TAG_REF_PREFIX):
            return self.session[len(_TAG_REF_PREFIX) :]
        return None

    @classmethod
    def at_tag(cls, name: str, commit: str) -> "Ref":
        """The ref for a store tag: the commit it names, and the tag in
        place of a session."""
        return cls(session=f"{_TAG_REF_PREFIX}{name}", commit=commit)

    @classmethod
    def parse(cls, text: "str | Ref") -> "Ref":
        """Parse ``session@commit`` or ``session@commit:/path``.

        A :class:`Ref` passes through, so callers can accept either
        spelling without branching.
        """
        if isinstance(text, Ref):
            return text
        if not isinstance(text, str) or "@" not in text:
            raise ValueError(
                f"Not a ref: {text!r} — expected 'session@commit' "
                "(optionally 'session@commit:/path')"
            )
        # Split at the LAST "@" of the pointer, not the first: a
        # reserved branch leads with one (``@store/pub/app/v1@abc``),
        # and a commit id never contains one. The path is cut first so
        # an "@" inside it cannot be mistaken for the separator.
        base, sep, path = text.partition(":")
        session, _, commit = base.rpartition("@")
        if not session or not commit:
            raise ValueError(
                f"Not a ref: {text!r} — both a session and a commit are required"
            )
        return cls(session=session, commit=commit, path=(path if sep else None) or None)

    def __str__(self) -> str:
        base = f"{self.session}@{self.commit}"
        return f"{base}:{self.path}" if self.path else base


class StoreTags:
    """Store-scoped tags: names that belong to no session.

    Every workspace on the store can read one, and it survives the
    deletion of the session that made it — that is the whole point of
    the scope. The state an app serves, the snapshot a report links
    to, a released version somebody wants a second name for.

    Session-scoped tags stay on the workspace (``ws.tags.add``,
    ``ws.tags.list``, ``ws.tags.at``): they belong to a session and go
    when it does.

    A store tag is also a REF — it is spelled where
    ``session@commit[:/path]`` is spelled — so a new name may hold
    neither ``@`` nor ``:``, the two delimiters that grammar owns; a
    name holding either could be stored and never addressed. Slashes
    are fine, which is what makes a publication's ``<name>/<version>``
    one of these. The rule is asked of a name being created: a tag
    already on a store keeps working by name whatever it is called.
    Session-scoped tags are reached by name alone and take no such
    rule.

    Reached as ``store.tags``; kvgit only, since it is the one backend
    with tags.
    """

    def __init__(self, store: "Store") -> None:
        self._store = store

    def add(
        self,
        source: "Workspace | Ref | str",
        name: str,
        *,
        info: dict[str, Any] | None = None,
    ) -> str:
        """Name a commit store-scoped; returns the commit it names.

        ``source`` is the workspace whose current state to name (its
        staged changes are committed first, so the name means what the
        caller saw), or a ref naming an exact commit. Any ref this
        store hands out is a ref this takes back: a session's, and a
        published version's own (``version.ref``), which names a
        reserved branch rather than a session. A published version is
        named on the terms the registry sets — a version unpublished
        since is refused, as it is for every other read of its ref.

        A workspace tags through its own provider, so it must be one
        this store opened — a workspace from somewhere else would write
        the tag to ITS store and leave this one's listing empty, which
        reads as a silent no-op. Such a call is refused instead. Use
        that store's own ``tags``, or name the commit by ref.

        Tags never move: an existing name raises rather than being
        repointed.
        """
        from ..workspace import Workspace

        if isinstance(source, Workspace):
            self._require_own(source)
            return source._tag(name, info=info, scope="store")
        ref = Ref.parse(source)
        with self._store._ref_provider(ref, "store.tags.add") as (provider, at):
            try:
                provider.check_tag(name, scope="store")
                if provider.tag_info(name, scope="store") is not None:
                    raise WorkspaceError(
                        f"Tag already exists: {name!r} in scope 'store' — tags "
                        "never move; delete it first if you mean to repoint it"
                    )
                return provider.tag(name, at=at.commit, info=info, scope="store")
            finally:
                provider.close()

    def _require_own(self, ws: "Workspace") -> None:
        """Refuse a workspace this store did not open."""
        self._store._require_own_workspace(ws, "tag")

    def list(self) -> dict[str, str]:
        """Tag name → commit id, for every store-scoped tag."""
        prefix = self._store._store_tag_prefix()
        return {
            stored[len(prefix) :]: commit
            for stored, commit in self._store._raw_tags().items()
            if stored.startswith(prefix)
        }

    def list_info(self) -> dict[str, TagInfo]:
        """Tag name → :class:`TagInfo`, for every store-scoped tag, on
        one backend open.

        The bulk read: describing N tags one at a time costs N opens,
        this costs one. :meth:`list` stays the cheaper answer when only
        the commit ids are wanted — it reads the tag table and nothing
        per tag.
        """
        return self._store._raw_tag_infos()

    def info(self, name: str) -> TagInfo | None:
        """Describe one store-scoped tag, or ``None`` if there is
        no such tag.

        Opens the backend for the read and closes it again: a store
        keeps no handle of its own, and holding a branch open by name
        would create that branch. Describing several tags at once goes
        through :meth:`list_info`, which pays that open once.
        """
        return self._store._raw_tag_info(name)

    def delete(self, name: str) -> None:
        """Drop a store-scoped tag, then sweep commits nothing else
        reaches. What it named survives only while something else
        still reaches it — a branch, or another tag."""
        self._store._delete_store_tag(name)

    def at(self, name: str, **settings: Any) -> "Workspace":
        """A frozen workspace over the tagged state.

        Reads see the tagged files, cache and cwd; nothing can be
        written or committed. Close it when done — it holds an executor
        and a store handle of its own.

        A store-scoped tag belongs to no session, so ``ws.session``
        names whichever branch the read was anchored on rather than an
        origin: the tag is the identity here, not the session. A store
        with sessions on it anchors on one of those, a store of
        publications on a publication's branch, and a store with
        neither on a reserved branch of its own — so a tag opens for as
        long as it exists, whatever became of the session that made it.

        ``settings`` are :meth:`Store.open`'s construction keywords —
        ``python``, ``mounts``, ``commands``, ``cache``,
        ``max_observation``, ``executor_factory``, ``root`` — applied
        to the workspace this returns. The store opens a tree, not a
        session, so it has no settings to inherit; an embedder that
        serves this snapshot supplies its own, and that is how a
        handler reaches a live host object (``python=PythonConfig(
        host_objects={"db": db})``). The commit holds files, never the
        objects.
        """
        _check_frozen_settings("StoreTags.at()", settings)
        return self._store._frozen_workspace(
            self._store._provider_at_store_tag(name), **settings
        )
