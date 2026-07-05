"""nontainer: a fake little computer for your agent.

Public surface:

    workspace(...)      -- factory; the one-liner entry point
    Workspace           -- files + shell + python + cache, versioned
    PythonConfig        -- what sandboxed code may touch
    TerminalResult, PythonResult
    WorkspaceProvider   -- the substrate protocol (bring your own)
    Capabilities, CheckpointInfo
    errors: WorkspaceError, NotSupportedError, SessionIdError,
            CheckpointNotFoundError

Adapters (optional extras):

    nontainer.adapters.agno  -- WorkspaceTools (agno Toolkit)
    python -m nontainer.mcp  -- MCP server (stdio)
"""

from .cache import Cache, CacheError
from .errors import (
    CheckpointNotFoundError,
    NotSupportedError,
    SessionIdError,
    WorkspaceError,
)
from .protocol import (
    SESSION_ID_RE,
    Capabilities,
    CheckpointInfo,
    WorkspaceProvider,
    validate_session_id,
)
from .workspace import (
    ModuleGrant,
    Mount,
    PythonConfig,
    PythonResult,
    TerminalResult,
    Workspace,
    workspace,
)

__all__ = [
    "workspace",
    "Workspace",
    "PythonConfig",
    "Mount",
    "ModuleGrant",
    "TerminalResult",
    "PythonResult",
    "WorkspaceProvider",
    "Capabilities",
    "CheckpointInfo",
    "SESSION_ID_RE",
    "validate_session_id",
    "Cache",
    "CacheError",
    "WorkspaceError",
    "NotSupportedError",
    "SessionIdError",
    "CheckpointNotFoundError",
]
