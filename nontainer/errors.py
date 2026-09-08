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
    """Something landed in the store and the record naming it did not.

    An agent commit, or a merge, is one store commit; the head that
    names it as the session's is another. Only a concurrent writer on
    the same session can come between the two, and only after the retry
    that reconciles with it has also failed. What landed is in the
    store either way — the message names it — but the session's ws-git
    head still points at the commit before it, and the message says
    what gets it back: running the verb again where the operation can
    be repeated (a commit), or ``ws.index.checkout(<id>)`` where it
    cannot (a merge, which is already in the store).
    """
