"""What a workspace's calls return."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class TerminalResult:
    """Outcome of one ``terminal()`` call (a full pipeline/script)."""

    stdout: str
    """Stdout of the final pipeline stage (termish semantics)."""

    exit_code: int
    stderr: str = ""
    truncated: bool = False

    commit: str | None = None
    """Id of the commit this call's autocommit created — pins the
    workspace state after the call (``ws.checkout(result.commit)``).
    ``None`` when nothing was committed: read-only call, autocommit
    off (turn mode), or an unversioned provider. HOST-facing, like
    ``PythonResult.namespace`` — adapters must not render it into the
    model's observation."""

    def __bool__(self) -> bool:
        return self.exit_code == 0


@dataclass(frozen=True)
class PythonResult:
    """Outcome of one ``run_python()`` call."""

    stdout: str
    stderr: str = ""
    """``sys.stderr`` writes from sandboxed code and libraries —
    warnings land here. Distinct from ``error``: stderr chatter does
    not imply failure."""

    error: str | None = None
    """Rendered traceback on failure, ``None`` on success. Sandboxed
    code that raises is a *result*, not a host exception — hosts only
    see exceptions for nontainer's own failures (bad config, provider
    errors)."""

    ticks: int = 0
    duration: float = 0.0
    truncated: bool = False

    namespace: Mapping[str, Any] = field(default_factory=dict, hash=False)
    """Top-level bindings after execution (sandtrap's result namespace)
    — for the HOST, not the model. Modules and ``_``-prefixed names
    are excluded; under process/kernel isolation, unpicklable values
    are dropped in transit (sandtrap ``filter_namespace``). Adapters
    must NOT render this into the text observation — not the values,
    and not a list of the names either: the agent wrote those bindings,
    so naming them back is inventory rather than information.
    Structured payloads reach the
    embedder as plain variables by convention — e.g. an A2UI adapter
    reads ``result.namespace.get("ui")`` — no bespoke emission channel,
    no schema imposed by core."""

    commit: str | None = None
    """Id of the commit this call's autocommit created (``None``
    when nothing was committed) — see ``TerminalResult.commit``."""

    ui_problems: tuple[str, ...] = ()
    """Why a ``ui`` value did not render as intended — today the 8 MB
    artifact cap, with the remediation. Actionable text meant to reach
    the agent: it reads this in the tool result and self-corrects, and
    the human sees it where the figure would have been. Carried on the
    result because materialization happens in ``run_python`` now, so an
    adapter rendering afterwards has no other way to learn of it."""

    def __bool__(self) -> bool:
        return self.error is None


@dataclass(frozen=True)
class WriteOutcome:
    """Outcome of ``files.write`` / ``files.put``."""

    path: str
    """Workspace path written."""

    size: int
    """Bytes written."""

    created: bool
    """True for a new file, False for an overwrite."""

    commit: str | None = None
    """Commit created by this call's autocommit (``None`` when
    nothing was committed) — see ``TerminalResult.commit``."""

    def __str__(self) -> str:  # f"wrote {outcome}" reads as the path
        return self.path


@dataclass(frozen=True)
class RemoveOutcome:
    """Outcome of ``files.remove``.

    Its own record rather than a ``WriteOutcome`` with the fields
    inverted: a removal writes no bytes and creates nothing, and a
    ``size`` documented as "bytes written" would be describing the
    opposite of what happened.
    """

    path: str
    """Workspace path removed."""

    size: int
    """Bytes the file held."""

    commit: str | None = None
    """Commit created by this call's autocommit (``None`` when
    nothing was committed) — see ``TerminalResult.commit``."""

    def __str__(self) -> str:  # f"removed {outcome}" reads as the path
        return self.path
