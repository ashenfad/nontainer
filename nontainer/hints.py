"""Intent hints for predictable agent collisions with the sandbox.

The walls are correct; these label the door. When an agent imports
subprocess to curl its own API, the block alone reads as a dead end —
the hint redirects to the tool that actually does the job.
"""

from __future__ import annotations

import re

_NO_PROCESS = (
    "the sandbox can't spawn processes — run shell commands with the "
    "terminal tool (its curl reaches workspace app endpoints directly: "
    "curl api/<name>?...)"
)
_NO_NETWORK = (
    "sandboxed python has no network access — workspace app endpoints "
    "are reachable via the terminal tool's curl (curl api/<name>?...)"
)

_BLOCKED_IMPORT_HINTS = {
    "subprocess": _NO_PROCESS,
    "requests": _NO_NETWORK,
    "urllib.request": _NO_NETWORK,
    "httpx": _NO_NETWORK,
    "aiohttp": _NO_NETWORK,
    "socket": _NO_NETWORK,
}

_IMPORT_ERROR_RE = re.compile(r"Import of '([\w.]+)' is not allowed")


def blocked_import_hint(error_text: str) -> str | None:
    """A redirection for a blocked-import error, or None. Matches the
    sandbox's ImportError phrasing; looks up the exact module, then its
    root package."""
    m = _IMPORT_ERROR_RE.search(error_text or "")
    if not m:
        return None
    module = m.group(1)
    return _BLOCKED_IMPORT_HINTS.get(module) or _BLOCKED_IMPORT_HINTS.get(
        module.partition(".")[0]
    )
