"""A test-only unversioned provider: a plain directory, no history.

nontainer ships only versioned providers in-tree (kvgit) plus the
optional AgentFS spike, but the provider protocol still admits
unversioned ones, and the workspace has to behave well on them: the
tools work, and every time-travel verb refuses by name. This is the
provider those tests run against.

It is the old ``DirProvider`` (removed in 0.9.0), minus the kv file it
kept inside the session's own tree: the kv here is an in-memory dict.
"""

from __future__ import annotations

from collections.abc import Iterable, MutableMapping
from pathlib import Path
from typing import Any

from monkeyfs import IsolatedFS

from nontainer.errors import NotSupportedError
from nontainer.protocol import (
    Capabilities,
    CommitInfo,
    TagInfo,
    WorkspaceDiff,
    validate_session_id,
)

_PLAIN_CAPS = Capabilities(
    versioned=False,
    staging=False,
    cheap_fork=False,
    merge=False,
    sql_audit=False,
    fuse_mount=False,
    tags=False,
)


class PlainProvider:
    """A ``WorkspaceProvider`` over a plain directory, unversioned. See the module docstring."""

    def __init__(self, root: str | Path, *, session: str) -> None:
        validate_session_id(session)
        self._session = session
        self._root = Path(root).expanduser().resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        self._fs = IsolatedFS(str(self._root))
        # In memory, so nothing about it lives in the tree agent code can
        # reach, and nothing is ever loaded from a file.
        self._kv: dict[str, Any] = {}
        self._closed = False

    # -- identity ------------------------------------------------------

    @property
    def session(self) -> str:
        return self._session

    @property
    def caps(self) -> Capabilities:
        return _PLAIN_CAPS

    @property
    def root(self) -> Path:
        """The real directory backing this workspace."""
        return self._root

    # -- surfaces ------------------------------------------------------

    @property
    def fs(self) -> Any:
        return self._fs

    @property
    def kv(self) -> MutableMapping[str, Any]:
        return self._kv

    @property
    def dirty(self) -> bool:
        return False  # no staging: writes are durable immediately

    @property
    def frozen(self) -> bool:
        """Never. A snapshot is what ``at_tag`` hands back, and there
        are no tags here to open one at."""
        return False

    @property
    def frozen_at(self) -> str | None:
        return None

    # -- versioning: unsupported ---------------------------------------

    def _unsupported(self, op: str) -> NotSupportedError:
        return NotSupportedError(
            f"DirProvider is unversioned: {op}() is not supported. "
            "Use the kvgit backend for commits, history, tags, and forking."
        )

    @property
    def head(self) -> str:
        raise self._unsupported("head")

    def commit(self, info: dict[str, Any] | None = None) -> str:
        raise self._unsupported("commit")

    def checkout(
        self, commit_id: str, *, info: dict[str, Any] | None = None, adjust: Any = None
    ) -> str:
        raise self._unsupported("checkout")

    def history(self, *, limit: int | None = None) -> Iterable[CommitInfo]:
        raise self._unsupported("history")

    def fork(self, name: str, *, at: str | None = None) -> "PlainProvider":
        if at is not None:
            from ..errors import NotSupportedError

            raise NotSupportedError(
                "fork(at=...) needs history to branch from; the dir backend "
                "keeps none. Fork the current state instead."
            )
        raise self._unsupported("fork")

    def discard(self) -> None:
        raise self._unsupported("discard")

    def merge(
        self, source: str, *, at: Any = None, info: Any = None, ignore: Any = None
    ) -> Any:
        raise self._unsupported("merge")

    def apply(
        self, base: Any, theirs: Any, *, info: Any = None, ignore: Any = None
    ) -> Any:
        raise self._unsupported("apply")

    def commit_keys(self, info: Any = None, *, keys: Any = ()) -> Any:
        raise self._unsupported("commit_keys")

    def files_at(self, commit: str) -> Any:
        raise self._unsupported("files_at")

    def working_files(self) -> Any:
        raise self._unsupported("working_files")

    def working_diff(self, commit: str) -> Any:
        raise self._unsupported("working_diff")

    # -- tags: unsupported ---------------------------------------------

    def tag(
        self,
        name: str,
        *,
        at: str | None = None,
        info: dict[str, Any] | None = None,
        scope: str = "session",
    ) -> str:
        raise self._unsupported("tag")

    def check_tag(self, name: str, *, scope: str = "session") -> None:
        raise self._unsupported("check_tag")

    def tags(self, *, scope: str = "session") -> dict[str, str]:
        raise self._unsupported("tags")

    def tag_info(self, name: str, *, scope: str = "session") -> "TagInfo | None":
        raise self._unsupported("tag_info")

    def delete_tag(self, name: str, *, scope: str = "session") -> None:
        raise self._unsupported("delete_tag")

    def at_tag(self, name: str, *, scope: str = "session") -> "PlainProvider":
        raise self._unsupported("at_tag")

    def diff(self, a: str, b: str) -> "WorkspaceDiff":
        raise self._unsupported("diff")

    # -- reading across sessions: unsupported --------------------------

    def commit_at(
        self, commit: str, *, session: str | None = None
    ) -> "CommitInfo | None":
        raise self._unsupported("commit_at")

    def key_at(self, commit: str, key: str) -> Any:
        raise self._unsupported("key_at")

    def branch_head(self, session: str) -> str:
        raise self._unsupported("branch_head")

    def expand_commit(self, commit: str, *, session: str | None = None) -> str:
        """The text unchanged: a plain directory holds no commit ids, so
        there is no prefix here to expand and nothing to refuse."""
        return commit

    # -- power modes / lifecycle ---------------------------------------

    def mount(self) -> Any:
        raise NotSupportedError(
            "PlainProvider needs no mount(): the workspace is already a real "
            f"directory at {self._root}"
        )

    def close(self) -> None:
        self._closed = True
