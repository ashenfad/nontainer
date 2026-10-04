"""Paths that are never work.

Three kinds, all treated the same way:

- **Outside the workspace root.** The agent's work is what sits under
  ``ws.root``. Anything a session writes elsewhere in the filesystem —
  scratch under ``/tmp``, a library's cache under ``/.matplotlib`` or
  ``/.cache`` — is the sandbox's state, not the agent's. A dud guest
  already sees only the root's subtree; this makes everything else
  agree with it.
- **Authoring output** under the root: what the app runtime writes for
  the agent to read back, handler logs (``app/logs``) and test_app's
  captures (``app/screenshots``). The agent's instruments rather than
  its work.
- **The embedder's patterns**, ``ignore=`` on the workspace: git's
  ``.gitignore`` for what the embedder knows its sessions write and
  nobody wants as work (``__pycache__/``, a tool's cache directory).

Everything that decides what a session's work IS leaves them out:

- ws-git's status, staging and commits, which never list or take them;
- the check for work an agent has not committed, so a capture taken
  after the last commit does not leave a session "dirty";
- diffs, and so the paths a delegate's answer says it changed;
- merges and cherry-picks between sessions, which keep this side's
  copy, so one session's scratch never lands in another's tree.

They are still written and kept, so the agent that wrote them can read
them back and they survive a turn. A publication carries only the app.

**Patterns** are a subset of ``.gitignore``'s, matched against the path
relative to the root:

- a pattern with no ``/``, or only a trailing one, matches a file or
  directory of that name at any depth (``__pycache__/``, ``*.log``);
- a leading ``/`` or a ``/`` in the middle anchors it to the root
  (``/build/``, ``data/raw``);
- a trailing ``/`` matches only a directory, and so everything below it;
- ``*`` and ``?`` match within one path segment, ``**`` across segments,
  and ``[abc]`` is a character class;
- blank lines and lines starting with ``#`` are skipped.

Negation (``!``) is not supported and is refused, rather than taken to
mean something it does not.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Iterable

#: Directories under the workspace root holding authoring output. The
#: app runtime's own layout (``nontainer.apps.dispatch``) writes here.
IGNORED_DIRS = ("app/logs", "app/screenshots")


class Patterns:
    """Compiled ignore patterns: what an embedder passed as ``ignore=``."""

    __slots__ = ("source", "_rules")

    def __init__(self, lines: Iterable[str] = ()) -> None:
        if isinstance(lines, str):
            lines = lines.splitlines()
        source: list[str] = []
        rules: list[tuple[re.Pattern[str], bool]] = []
        for raw in lines:
            line = str(raw).strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("!"):
                raise ValueError(
                    f"ignore pattern {line!r}: negation ('!') is not supported"
                )
            source.append(line)
            rules.append(_compile(line))
        self.source = tuple(source)
        self._rules = tuple(rules)

    def __bool__(self) -> bool:
        return bool(self._rules)

    def __repr__(self) -> str:
        return f"Patterns({list(self.source)!r})"

    def match(self, rel: str) -> bool:
        """Whether ``rel``, a path relative to the root, is ignored: it
        matches a pattern, or one of the directories above it does."""
        if not self._rules or not rel:
            return False
        parts = rel.split("/")
        for i in range(len(parts), 0, -1):
            candidate = "/".join(parts[:i])
            is_dir = i < len(parts)
            for regex, dir_only in self._rules:
                if dir_only and not is_dir:
                    continue
                if regex.fullmatch(candidate):
                    return True
        return False


def _compile(line: str) -> tuple[re.Pattern[str], bool]:
    """One pattern → (regex over a root-relative path, directory only)."""
    dir_only = line.endswith("/")
    body = line.rstrip("/")
    anchored = body.startswith("/") or "/" in body
    body = body.lstrip("/")
    regex = _translate(body)
    if not anchored:
        regex = r"(?:.*/)?" + regex  # a name, at any depth
    return re.compile(regex), dir_only


def _translate(glob: str) -> str:
    out: list[str] = []
    i, n = 0, len(glob)
    while i < n:
        c = glob[i]
        if glob.startswith("**/", i):
            out.append(r"(?:.*/)?")
            i += 3
        elif glob.startswith("**", i):
            out.append(r".*")
            i += 2
        elif c == "*":
            out.append(r"[^/]*")
            i += 1
        elif c == "?":
            out.append(r"[^/]")
            i += 1
        elif c == "[":
            end = glob.find("]", i + 1)
            if end == -1:
                out.append(re.escape(c))
                i += 1
            else:
                cls = glob[i + 1 : end]
                if cls.startswith("!"):
                    cls = "^" + cls[1:]
                out.append("[" + cls.replace("\\", "\\\\") + "]")
                i = end + 1
        else:
            out.append(re.escape(c))
            i += 1
    return "".join(out)


NO_PATTERNS = Patterns()


def why_ignored(path: str, root: str, patterns: Patterns = NO_PATTERNS) -> str | None:
    """Why ``path``, an absolute filesystem path, is never work for a
    workspace rooted at ``root``, in words; ``None`` when it is work.
    The root itself is work."""
    norm = posixpath.normpath(path)
    top = posixpath.normpath(root or "/")
    if top == "/":
        rel = norm.lstrip("/")
    elif norm == top:
        return None
    elif norm.startswith(top + "/"):
        rel = norm[len(top) + 1 :]
    else:
        return f"it is outside the workspace root ({top})"
    for d in IGNORED_DIRS:
        if rel == d or rel.startswith(d + "/"):
            return "it is authoring output (app logs and test_app captures)"
    if patterns.match(rel):
        return "it matches the session's ignore patterns"
    return None


def is_ignored(path: str, root: str, patterns: Patterns = NO_PATTERNS) -> bool:
    """Whether ``path`` is never work: outside the root, under one of
    the authoring-output directories, or matched by ``patterns``."""
    return why_ignored(path, root, patterns) is not None


def drop_ignored(paths, root: str, patterns: Patterns = NO_PATTERNS) -> set[str]:
    """``paths`` less the ignored ones, as a set."""
    return {p for p in paths if not is_ignored(p, root, patterns)}
