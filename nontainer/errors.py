"""nontainer exceptions."""


class WorkspaceError(Exception):
    """Base class for nontainer errors."""


class NotSupportedError(WorkspaceError):
    """The active provider lacks the capability for this operation.

    Raised by e.g. ``Workspace.fork()`` on a plain-dir provider. Check
    ``workspace.caps`` before calling capability-gated methods.
    """


class SessionIdError(WorkspaceError):
    """Session id failed validation (see ``SESSION_ID_RE``).

    Session ids often flow from untrusted input and become storage
    paths / branch names; invalid ids are rejected before any lookup.
    """


class CommitNotFoundError(WorkspaceError):
    """A commit was named and the provider doesn't have it.

    ``checkout()`` given an id that is not a commit on the session, or
    a tag verb given a name the store doesn't hold.
    """


class BookkeepingLost(WorkspaceError):
    """An agent commit landed and the bookkeeping that names it did not.

    ``ws-git commit`` and ``ws-git checkout`` each make two commits: the
    agent's, and the one that records the new head and puts the working
    tree back. Only a concurrent writer on the same session can come
    between them, and only after the retry that reconciles with it has
    also failed. The agent's commit is in the store either way — the
    message names it — but the session's ws-git head still points at
    the commit before it, so the verb has to be run again.
    """
