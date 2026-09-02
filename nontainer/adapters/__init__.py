"""Harness adapters: thin skins over one Workspace core.

- ``nontainer.adapters.agno`` — ``WorkspaceTools`` (agno Toolkit);
  requires the ``[agno]`` extra.
- ``nontainer.adapters.agno_db`` — ``KvgitSessionDb``, an agno db that
  keeps the conversation in the workspace branch beside the files, so
  one commit holds the whole turn and rewind/fork cover memory too.
- ``nontainer.adapters.mcp`` — FastMCP server; requires the ``[mcp]``
  extra. Run via ``python -m nontainer.adapters.mcp``.

Shared behavior lives in ``nontainer.adapters.render``: observation
rendering (never inline ``namespace``; surface truncation), dynamic
tool descriptions, and the exposure-mode heuristic (``"auto"`` →
terminal-only when the python environment is plain, split tools when
it's augmented with cache/host objects).
"""

from .render import resolve_tools_mode

__all__ = ["resolve_tools_mode"]
