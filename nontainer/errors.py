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
    """``restore()`` was given an id that doesn't exist in history."""
