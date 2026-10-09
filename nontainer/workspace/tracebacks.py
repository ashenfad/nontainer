"""Rendering an error for the agent: the traceback, without the plumbing."""

from __future__ import annotations

import re
import traceback
from pathlib import Path

_HOST_PREFIX_RE = re.compile(r'(File ")/[^"]*/(?:site-packages|python\d+\.\d+)/')
_FRAME_RE = re.compile(r'\s+File "([^"]*)"')


def _render_error(exc: BaseException) -> str:
    """The full traceback, not just the message — line numbers are what
    an agent's repair loop aims at.

    Under process isolation the traceback object doesn't survive the
    pickle home, so sandtrap's worker renders it in situ and attaches
    the text (``_st_traceback_text``, sandtrap >= 0.2.10); prefer that,
    fall back to formatting whatever frames we hold (in-process runs,
    older sandtraps, host-made errors like StTimeout)."""
    text = getattr(exc, "_st_traceback_text", None)
    if not isinstance(text, str) or not text:
        text = "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        ).rstrip()
    return _trim_rendered_traceback(text)


def _machinery_dirs() -> tuple[str, ...]:
    """Package dirs whose frames are sandbox plumbing, not signal."""
    import monkeyfs
    import sandtrap

    return tuple(
        str(Path(m.__file__).parent) for m in (sandtrap, monkeyfs) if m.__file__
    )


_PLUMBING_FILES = ("nontainer/remote.py", "<contract:nontainer.remote>")
"""Files whose frames are plumbing wherever they run: a stubbed host
object's call raises its refusal from ``nontainer.remote``, imported or
rebuilt from source in a guest, and what the agent needs is its own line
and the error."""


def _trim_rendered_traceback(text: str) -> str:
    """De-noise a rendered traceback for agent-visible surfaces.

    Sandtrap/monkeyfs machinery frames go entirely — a gate raising
    through ``__st_import__`` is OUR plumbing, not the agent's bug
    (``strip_internal_frames`` can only strip LEADING frames; text is
    where trailing ones can go). Host install prefixes carry zero
    signal and leak paths, so surviving library frames read
    ``pandas/core/generic.py``, not the absolute venv path. And
    pathological depth gets middle-elided — the entry frames and the
    raise site are the ends worth keeping."""
    machinery = _machinery_dirs()
    lines: list[str] = []
    dropping = False
    for line in text.splitlines():
        m = _FRAME_RE.match(line)
        if m:
            dropping = m.group(1).startswith(machinery) or m.group(1).endswith(
                _PLUMBING_FILES
            )
        elif not line.startswith(("    ", "\t")):
            dropping = False  # left column: header / exception line
        if dropping:
            continue
        lines.append(_HOST_PREFIX_RE.sub(r"\1", line))
    if len(lines) > 60:
        elided = len(lines) - 48
        lines = lines[:8] + [f"[... {elided} traceback lines elided ...]"] + lines[-40:]
    return "\n".join(lines)
