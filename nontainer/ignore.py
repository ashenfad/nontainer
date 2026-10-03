"""Paths that are never work: what the framework writes beside an app.

The app runtime writes two kinds of file into the app tree for the
agent to read back: handler logs (``app/logs``) and test_app's
captures (``app/screenshots``). They are authoring output, the agent's
instruments rather than its work. A publication already leaves them
out; so does everything that decides what a session's work IS:

- ws-git's status, staging and commits, which never list or take them;
- the check for work an agent has not committed, so a capture taken
  after the last commit does not leave a session "dirty";
- diffs, and so the paths a delegate's answer says it changed;
- merges and cherry-picks between sessions, which keep this side's
  copy, so one session's captures never land in another's tree.

They are still written, kept and versioned on their own branch, so
the agent that took them can read them and they survive a turn. This
is git's ``.git/info/exclude`` rather than a ``.gitignore``: built in,
because the framework is what writes there.
"""

from __future__ import annotations

import posixpath

#: Directories under the workspace root holding authoring output. The
#: app runtime's own layout (``nontainer.apps.dispatch``) writes here.
IGNORED_DIRS = ("app/logs", "app/screenshots")


def is_ignored(path: str, root: str) -> bool:
    """Whether ``path``, an absolute workspace path, is authoring output
    under ``root``. The directories themselves count, as do any paths
    below them."""
    norm = posixpath.normpath(path)
    for rel in IGNORED_DIRS:
        top = posixpath.join(root, rel)
        if norm == top or norm.startswith(top + "/"):
            return True
    return False


def drop_ignored(paths, root: str) -> set[str]:
    """``paths`` less the ignored ones, as a set."""
    return {p for p in paths if not is_ignored(p, root)}
