"""Workspace substrate providers."""

from typing import Any

from .dir import DirProvider
from .kvgit import KvgitProvider

__all__ = ["DirProvider", "KvgitProvider", "AgentFSProvider"]


def __getattr__(name: str) -> Any:
    # Lazy: AgentFSProvider needs the optional agentfs-sdk dependency.
    if name == "AgentFSProvider":
        from .agentfs import AgentFSProvider

        return AgentFSProvider
    raise AttributeError(name)
