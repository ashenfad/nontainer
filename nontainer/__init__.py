"""nontainer: a fake little computer for your agent.

Public surface:

    workspace(...)      -- factory; the one-liner entry point
    store(...)          -- the store those sessions live in
    Store               -- open/list/delete sessions, store-scoped tags,
                           publications
    Ref                 -- "session@commit": one exact state, named
    Publication, Version -- a named lineage of published states
    Workspace           -- one session: files + shell + python + cache,
                           versioned; ws.files / ws.index / ws.tags
    Runtime             -- ws.runtime: how code runs against that state
    PythonConfig        -- what sandboxed code may touch
    TerminalResult, PythonResult, WriteOutcome, EditOutcome
    WorkspaceProvider   -- the substrate protocol (bring your own)
    Executor            -- the execution protocol (bring your own)
    SessionRunner, HostObjectFactory -- the loop seam
    Job, Answer         -- what a delegation is, and what it says back
    Capabilities, CommitInfo, TagInfo, WorkspaceDiff,
    MergeOutcome, WorkspaceStatus
    errors: WorkspaceError, NotSupportedError, SessionIdError,
            CommitNotFoundError, BookkeepingLost, SessionsError,
            JobRunning

Adapters (optional extras):

    nontainer.adapters.agno  -- WorkspaceTools (agno Toolkit)
    python -m nontainer.adapters.mcp  -- MCP server (stdio)
"""

from .artifacts import ArtifactPath, artifact_kind
from .cache import Cache, CacheError
from .editing import EditOutcome
from .errors import (
    BookkeepingLost,
    CommitNotFoundError,
    JobRunning,
    NotSupportedError,
    SessionIdError,
    SessionsError,
    WorkspaceError,
)
from .protocol import (
    SESSION_ID_RE,
    Answer,
    Capabilities,
    CommitInfo,
    Executor,
    HostObjectFactory,
    Job,
    MergeOutcome,
    SessionRunner,
    TagInfo,
    WorkspaceDiff,
    WorkspaceProvider,
    WorkspaceStatus,
    validate_session_id,
)
from .runtime import Runtime
from .store import Publication, Ref, Store, Version, store
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
    "Publication",
    "Version",
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
    "Job",
    "Answer",
    "Capabilities",
    "CommitInfo",
    "MergeOutcome",
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
    "CommitNotFoundError",
    "BookkeepingLost",
    "SessionsError",
    "JobRunning",
]
