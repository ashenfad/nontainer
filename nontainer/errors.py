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


class CheckpointNotFoundError(WorkspaceError):
    """A checkpoint was named and the provider doesn't have it.

    ``restore()`` given an id that isn't in history, or a tag verb
    given a name the store doesn't hold.
    """
