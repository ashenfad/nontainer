"""nontainer: a fake little computer for your agent.

Public surface:

    workspace(...)      -- factory; the one-liner entry point
    store(...)          -- the store those sessions live in
    Store               -- open/list/delete sessions, store-scoped tags
    Ref                 -- "session@commit": one exact state, named
    Workspace           -- files + shell + python + cache, versioned
    Runtime             -- ws.runtime: how code runs against that state
    PythonConfig        -- what sandboxed code may touch
    TerminalResult, PythonResult, WriteOutcome, EditOutcome
    WorkspaceProvider   -- the substrate protocol (bring your own)
    Executor            -- the execution protocol (bring your own)
    SessionRunner, HostObjectFactory -- the loop seam (declared; stage 3)
    Capabilities, CheckpointInfo, TagInfo, WorkspaceDiff,
    MergeOutcome, StageResult, WorkspaceStatus
    errors: WorkspaceError, NotSupportedError, SessionIdError,
            CheckpointNotFoundError

Adapters (optional extras):

    nontainer.adapters.agno  -- WorkspaceTools (agno Toolkit)
    python -m nontainer.mcp  -- MCP server (stdio)
"""

from .artifacts import ArtifactPath, artifact_kind
from .cache import Cache, CacheError
from .editing import EditOutcome
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
    Executor,
    HostObjectFactory,
    MergeOutcome,
    SessionRunner,
    StageResult,
    TagInfo,
    WorkspaceDiff,
    WorkspaceProvider,
    WorkspaceStatus,
    validate_session_id,
)
from .runtime import Runtime
from .store import Ref, Store, store
from .workspace import (
    ModuleGrant,
    Mount,
    PythonConfig,
    PythonResult,
    TerminalResult,
    Workspace,
    WriteOutcome,
    workspace,
)

__all__ = [
    "ArtifactPath",
    "artifact_kind",
    "workspace",
    "store",
    "Store",
    "Ref",
    "Workspace",
    "Runtime",
    "PythonConfig",
    "Mount",
    "ModuleGrant",
    "TerminalResult",
    "PythonResult",
    "WriteOutcome",
    "EditOutcome",
    "WorkspaceProvider",
    "Executor",
    "SessionRunner",
    "HostObjectFactory",
    "Capabilities",
    "CheckpointInfo",
    "MergeOutcome",
    "StageResult",
    "TagInfo",
    "WorkspaceDiff",
    "WorkspaceStatus",
    "SESSION_ID_RE",
    "validate_session_id",
    "Cache",
    "CacheError",
    "WorkspaceError",
    "NotSupportedError",
    "SessionIdError",
    "CheckpointNotFoundError",
]
