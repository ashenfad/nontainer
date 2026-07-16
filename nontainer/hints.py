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
    "shutil": (
        "shutil isn't granted — copy/move files with the terminal tool "
        "(cp, mv) or read/write them with plain open()"
    ),
}

_IMPORT_ERROR_RE = re.compile(r"Import of '([\w.]+)' is not allowed")

_DUNDER_IMPORT = (
    "dynamic __import__ is blocked, but ordinary import statements work "
    "here — an `import numpy` at the top of the file does the job"
)
_KALEIDO = (
    "plotly's write_image needs kaleido, which can't run here — assign "
    "figures to `ui = {...}` to render them inline, or use matplotlib "
    "savefig for an image file"
)
_TICK_LIMIT = (
    "the tick limit counts interpreted Python loop iterations — "
    "vectorized pandas/numpy calls run native and don't tick, so prefer "
    "them over row-by-row loops"
)


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


def error_hint(error_text: str) -> str | None:
    """The intent hint for a sandbox error's rendered text, or None.

    One entry point for every collision we can label: blocked imports,
    the __import__ workaround, plotly's kaleido dead end (its own
    message says ``pip install``, which can't happen here), and the
    tick limit (where "stop looping" is the actual fix)."""
    text = error_text or ""
    hint = blocked_import_hint(text)
    if hint:
        return hint
    if "Cannot access '__import__'" in text:
        return _DUNDER_IMPORT
    if "Kaleido" in text and "pip install" in text:
        return _KALEIDO
    if "Execution exceeded" in text and "tick limit" in text:
        return _TICK_LIMIT
    return None
