"""Workspace: the top-level API. One instance == one session's world.

:mod:`.core` holds :class:`Workspace` and the :func:`workspace` factory,
:mod:`.config` what one is opened with, :mod:`.results` what its calls
return, :mod:`.facades` ``ws.files``, ``ws.index`` and ``ws.tags``,
:mod:`.fs` the filesystem wrappers, and :mod:`.tracebacks` how an
error reads to the agent. Everything is importable from here.
"""

from ..host_objects import HostObject
from .config import (
    Isolation,
    ModuleGrant,
    Mount,
    Profile,
    PythonConfig,
    _profile_fields,
    _Settings,
    normalize_root,
)
from .core import Workspace, _state_identity, workspace
from .facades import WorkspaceFiles, WorkspaceIndex, WorkspaceIndexTags, WorkspaceTags
from .fs import _frozen_fs
from .results import PythonResult, RemoveOutcome, TerminalResult, WriteOutcome
from .tracebacks import _render_error, _trim_rendered_traceback

__all__ = [
    "HostObject",
    "Isolation",
    "ModuleGrant",
    "Mount",
    "Profile",
    "PythonConfig",
    "PythonResult",
    "RemoveOutcome",
    "TerminalResult",
    "Workspace",
    "WorkspaceFiles",
    "WorkspaceIndex",
    "WorkspaceIndexTags",
    "WorkspaceTags",
    "WriteOutcome",
    "normalize_root",
    "workspace",
    # internal, for the modules that share them
    "_Settings",
    "_frozen_fs",
    "_profile_fields",
    "_render_error",
    "_state_identity",
    "_trim_rendered_traceback",
]
