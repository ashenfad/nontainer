"""The ``ws-git`` terminal builtin: the agent's git, as verbs.

The model lives in ``nontainer/agentgit.py`` — an index and a commit
graph kept as metadata over the store's own history, so the framework
can go on committing for durability without ever disturbing what the
agent is composing. This module is the parser and the formatter.

Movie-set rule (see ``scratch/ws-git-impl.md`` PR 3): follow git
wherever cheap; each deviation carries a recorded reason:

- ``status`` takes git-short ``XY`` columns; merge context rides one
  ``## merging`` line git has no equivalent for (our ``status()``
  already returns the source — dropping it would be purer but less
  useful).
- ``reset`` is mixed-only; ``--soft``/``--hard`` get the usage error.
  Going back to a commit is ``checkout``, which says so.
- ``commit`` takes ``-m/--message`` (stored in commit info) but no
  pathspec and no ``-a``: with something staged it commits the staged
  set, and with nothing staged it commits everything modified. git
  would refuse the second case ("no changes added to commit"), but git
  has ``-a`` and an agent here has no index open to have forgotten
  about. ``-m`` is optional where git insists, since a message-less
  agent commit is still a point in the graph.
- ``checkout <ref>`` restores the tree and rewinds the agent's head;
  the store APPENDS the restore, so nothing already committed leaves
  the session. ``checkout <name>`` is refused: sessions are branches
  and branches do not switch here.
- The index names paths, not snapshots, so ``--cached`` shows staged
  paths against the last commit *including* any later edits (git would
  show only the staged snapshot).
- ``log`` shows only the agent's own commits; the framework's per-call
  commits are plumbing, and showing them would bury the agent's
  history in its own tool calls.

Everything else is git-exact: silent clean status, silent ``stage``,
unified ``diff`` with ``a/``/``b/`` headers, ``diff --check`` marker
lines, ``UU`` unmerged entries, ``--porcelain`` accepted as the
stable-contract alias it is in git.
"""

from __future__ import annotations

import difflib
import posixpath
from collections.abc import Mapping
from typing import Any

from .agentgit import MERGE_TOOL, AgentGit
from .errors import CommitNotFoundError, NotSupportedError, WorkspaceError

_VERBS = (
    "stage",
    "unstage",
    "commit",
    "reset",
    "status",
    "diff",
    "log",
    "show",
    "checkout",
    "help",
)
_USAGE = (
    "usage: ws-git (stage|unstage|commit|reset|status|diff|log|show|checkout) [...]"
)
_SUPPORTED = (
    "supported: stage <paths> | unstage <paths> | commit [-m MSG] | reset | "
    "status [--porcelain] | diff [--cached] [--check] [paths...] | "
    "log [-n N] | show <ref> | checkout <ref> | help"
)
_ROOT = "/workspace"

# Movie-set edge: name the missing corner in git's own terms plus what
# an agent can actually do about it — a terminal verb, or plainly that
# this one is the host's to invoke. Exit 1: refusals, not usage errors.
_EDGE = {
    "stash": (
        "no stash here — a fork is a stash, and forking is the host's to "
        "invoke. Snapshots are cheap: commit what you have "
        "(ws-git commit -m ...) and keep going."
    ),
    "rebase": (
        "no rebase here — history is append-only. Nothing is lost by "
        "committing forward, and ws-git checkout <ref> goes back."
    ),
    "branch": (
        "no branches here — sessions are branches, and making one is the "
        "host's to invoke. ws-git log shows this session's history."
    ),
    "merge": (
        "merge is the host's to invoke — it lands as a commit you will see "
        "in ws-git log, markers and all. Terminal merge arrives with the "
        "consult flows."
    ),
}

_HELP = """ws-git: the agent's git over this session.

usage: ws-git (stage|unstage|commit|reset|status|diff|log|show|checkout) [...]
  stage <paths>     add paths to the index (optional: commit with an
                    empty index takes everything modified)
  unstage <paths>   drop paths from the index
  commit [-m MSG]   commit the staged set (everything modified, when
                    nothing is staged); no -a, no pathspec. What you
                    left out stays in the working tree, uncommitted
  reset             abandon the composition (mixed-only)
  status            staged vs unstaged (git-short XY columns)
  diff [--cached] [--check] [paths...]
                    unified diff against your last commit; --cached for
                    the staged set
                    --check finds leftover conflict markers
                    (unstaged, or staged with --cached;
                    unresolved merges always)
  log [-n N]        your own commits, newest first
  show <ref>        one commit: its message and its diff
  checkout <ref>    restore the tree to a commit of yours (history is
                    append-only: the restore is a new commit)
  help              this text

Subset, on purpose: no stash (a fork is a stash), no rebase (history is
append-only), no branch or merge (the host invokes those). There is no
.git — branches are sessions, history is commits. The workspace commits
on its own as you work; ws-git log shows only the commits you made."""


#: The dud host-object name fronting ws-git on guest rungs. A user
#: host object under the same name refuses at executor open (fail
#: closed, like RESERVED_COMMANDS) rather than shadowing either way.
DUD_OBJECT = "ws_git"

#: The shell function every dud exec prepends when ``ws-git`` is
#: registered. Calls home over ``dud-hostcall`` with the guest cwd
#: first; a tiny python3 split (present in every guest — stdlib can't
#: be withheld) separates the triple onto the real streams and exits
#: with the verb's code. ``type ws-git`` shows a function (fine);
#: sudo/env -i drop it (acceptable — the rung, not the verb).
SHELL_FUNCTION = """ws-git() {
  dud-hostcall ws_git run "$PWD" "$@" | python3 -c '
import json, sys
t = json.loads(sys.stdin.read())
sys.stdout.write(t["stdout"])
sys.stderr.write(t["stderr"])
sys.exit(t["exit_code"])
'
}
"""


def register_wsgit(ws: Any) -> None:
    """Register the ``ws-git`` terminal builtin on a workspace.

    Follows the ``enable_apps``/``register_command`` pattern. No-op
    where the executor cannot run commands (the gate doubles as the
    primer gate: agents on such executors are never told about
    terminal builtins). A dud-backed executor reports
    ``supports_commands`` False (real bash has no registry) yet
    ferries the verbs to the guest another way (PR 4) — detected by
    the guest-path mapping the handler needs, not by importing the
    executor (which would cycle).
    """
    rt = ws.runtime
    if not rt.supports_commands and not rt.supports_ws_verbs:
        return
    fn = make_wsgit_command(ws)
    # Tags OUR registration: the dud handler must not front a user's
    # own ``ws-git`` command (the ws-* prefix reservation was decided
    # but never enforced — RESERVED_COMMANDS holds only python/python3).
    fn._nontainer_wsgit = True
    # Framework-owned: a fork/snapshot rebuilds this bound to itself
    # instead of inheriting the parent-bound closure (the fork-bleed).
    ws.runtime.register_command("ws-git", fn, rebind=register_wsgit)


def _guest_ctx(args: list[str], cwd: str) -> Any:
    """A termish-shaped context for invoking the command off-rung.

    The closure only touches ``args``, ``stdout``, and ``fs.getcwd()``
    — so the fake fs is one method, returning the guest-reported cwd
    (already mapped host-side by the caller).
    """
    from io import StringIO

    class _CwdFS:
        def __init__(self, cwd: str):
            self._cwd = cwd

        def getcwd(self) -> str:
            return self._cwd

    class _Ctx:
        def __init__(self):
            self.args = args
            self.stdout = StringIO()
            self.fs = _CwdFS(cwd)

    return _Ctx()


class DudHostHandler:
    """The dud host object fronting ws-git on guest rungs (PR 4).

    One public method, ``run(cwd, *argv)`` — the shape ``dud-hostcall``
    delivers (every word past METHOD arrives verbatim). It invokes the
    SAME command closure the local rung runs, with a guest-shaped
    context, so both rungs share behavior byte-for-byte — including
    the known frozen hole (#63 fixes both at once; a snapshot here
    would merely diverge).

    The triple comes back JSON-safe (``{"stdout","stderr","exit_code"}``);
    the guest function splits it onto the real streams. ``stderr``
    carries termish's ``"ws-git: "`` prefix (added by the local shell
    layer, not the command) so both rungs read identically.
    """

    def __init__(self, ws: Any, commands: Mapping[str, Any]):
        self._ws = ws
        # The command registry of the runtime whose executor ferries
        # this verb — ``ExecutionContext.commands``, bound live. Not the
        # workspace's: a workspace can carry more than one runtime (an
        # app served from a snapshot runs on its own), and each has its
        # own registrations. A verb must dispatch the one registered
        # where it was invoked, or answer that it is not registered
        # there.
        self._commands = commands
        self._command = make_wsgit_command(ws)

    def run(self, cwd: str, *argv: str) -> dict:
        ws = self._ws
        with ws._lock:
            cmd = self._commands.get("ws-git")
            if cmd is None:
                return {
                    "stdout": "",
                    "stderr": "ws-git: not registered on this workspace",
                    "exit_code": 1,
                }
            if not getattr(cmd, "_nontainer_wsgit", False):
                return {
                    "stdout": "",
                    "stderr": (
                        "ws-git: a custom command owns that name here — "
                        "the dud rung only fronts the framework one"
                    ),
                    "exit_code": 1,
                }
            # Sync-on-verb: absorb first so every verb dispatches
            # against fresh state — ``printf x > new.txt; ws-git stage
            # new.txt`` stages instead of rejecting an unknown path.
            # Reads (status/diff) need it just as much as mutations.
            # The verbs that DO rewrite worktree files (commit's
            # revert-and-restore, checkout) push back the other way:
            # the guest shell is blocked on this hostcall, so its tree
            # is quiescent, and the executor is marked stale so the
            # next call re-materializes it. A nested harvest is safe
            # here: both locks are re-entrant on this thread.
            err = ws._absorb_before_verb("ws-git")
            if err is not None:
                return err
            executor = getattr(getattr(ws, "runtime", None), "executor", None)
            mapper = getattr(executor, "_guest_to_host", None)
            host_cwd = (mapper(cwd) if mapper else None) or cwd
            ctx = _guest_ctx(self._map_argv(mapper, list(argv)), host_cwd)
            try:
                result = self._command(ctx)
            except Exception as e:  # noqa: BLE001 — parity: termish wraps these
                return {
                    "stdout": ctx.stdout.getvalue(),
                    "stderr": f"ws-git: execution error: {e}",
                    "exit_code": 1,
                }
            out = ctx.stdout.getvalue()
            if result is None:
                return {"stdout": out, "stderr": "", "exit_code": 0}
            err = result.stderr or ""
            if err and not err.startswith("ws-git:"):
                err = f"ws-git: {err}"
            return {"stdout": out, "stderr": err, "exit_code": result.exit_code}

    @staticmethod
    def _map_argv(mapper: Any, argv: list[str]) -> list[str]:
        """Guest-absolute argv entries to host-absolute. The
        ``-m``/``--message`` value is free text, never a path.
        Unmappable entries pass through; the provider refuses them."""
        if mapper is None:
            return argv
        out: list[str] = []
        message_next = False
        for word in argv:
            if message_next:
                message_next = False
            elif word in ("-m", "--message"):
                message_next = True
            elif word.startswith("/"):
                word = mapper(word) or word
            out.append(word)
        return out


def make_wsgit_command(ws: Any) -> Any:
    """Build the ``ws-git`` command closure over a workspace."""
    provider = ws._provider

    def wsgit(ctx: Any) -> Any:
        from termish import CommandResult

        args = list(ctx.args)
        if not args or args[0] in ("-h", "--help", "help"):
            if not args:
                return CommandResult(exit_code=2, stderr=_USAGE)
            ctx.stdout.write(_HELP + "\n")
            return None
        verb, rest = args[0], args[1:]
        if verb in _EDGE:
            return CommandResult(exit_code=1, stderr=_EDGE[verb])
        if verb not in _VERBS:
            return CommandResult(
                exit_code=2,
                stderr=f"{verb!r} is not a ws-git command. "
                f"See 'ws-git help'.\n{_SUPPORTED}",
            )
        if not getattr(provider.caps, "index", False):
            return CommandResult(
                exit_code=1,
                stderr="this provider has no index (needs caps.index) — "
                "use the kvgit backend for ws-git.",
            )
        git = AgentGit(ws)
        try:
            if verb == "status":
                return _status(git, ctx, rest)
            if verb == "stage":
                return _stage(git, ctx, rest)
            if verb == "unstage":
                return _unstage(git, ctx, rest)
            if verb == "commit":
                return _commit(git, ctx, rest)
            if verb == "reset":
                return _reset(git, ctx, rest)
            if verb == "diff":
                return _diff(git, ctx, rest)
            if verb == "log":
                return _log(git, ctx, rest)
            if verb == "show":
                return _show_verb(git, ctx, rest)
            if verb == "checkout":
                return _checkout(git, ctx, rest)
        except (
            ValueError,
            WorkspaceError,
            NotSupportedError,
            CommitNotFoundError,
        ) as e:
            # Domain errors render as messages, never "Unexpected error".
            return CommandResult(exit_code=1, stderr=f"{e}")
        raise AssertionError(f"unreachable verb {verb!r}")

    wsgit.__doc__ = (
        "The agent's git over this session: "
        "ws-git (stage|unstage|commit|reset|status|diff|log|show|checkout) [...]"
    )
    return wsgit


def _merge_hash(git: AgentGit, source: str) -> str | None:
    """Short hash of the newest merge commit from ``source``, if any."""
    for entry in git.history():
        if entry.info.get("tool") == MERGE_TOOL and entry.info.get("source") == source:
            return entry.id[:7]
    return None


def _abspath(ctx: Any, arg: str) -> str:
    """Shell spelling → workspace-absolute path (resolves against cwd)."""
    if arg.startswith("/"):
        return posixpath.normpath(arg)
    return posixpath.normpath(posixpath.join(ctx.fs.getcwd(), arg))


def _show(path: str) -> str:
    """Display path → workspace-root-relative, git-short style."""
    if path.startswith(_ROOT + "/"):
        return path[len(_ROOT) + 1 :]
    return path.lstrip("/")


def _usage_error(detail: str) -> Any:
    from termish import CommandResult

    return CommandResult(exit_code=2, stderr=f"{detail}\n{_USAGE}\n{_SUPPORTED}")


def _status(git: AgentGit, ctx: Any, rest: list[str]) -> Any:
    for flag in rest:
        if flag != "--porcelain":
            return _usage_error(f"status takes no {flag!r} (porcelain is the default).")
    st = git.status()
    lines: list[str] = []
    if st.merge_source is not None:
        short = _merge_hash(git, st.merge_source)
        at = f"@{short}" if short else ""
        lines.append(
            f"## merging {st.merge_source}{at} ({len(st.merge_unresolved)} unresolved)"
        )
    staged, unstaged = set(st.staged), set(st.unstaged)
    unresolved = set(st.merge_unresolved)
    for path in sorted(staged | unstaged | unresolved):
        if path in unresolved:
            lines.append(f"UU {_show(path)}")
        else:
            x = "M" if path in staged else " "
            y = "M" if path in unstaged else " "
            lines.append(f"{x}{y} {_show(path)}")
    if lines:
        ctx.stdout.write("\n".join(lines) + "\n")
    return None


def _stage(git: AgentGit, ctx: Any, rest: list[str]) -> Any:
    if not rest or any(a.startswith("-") for a in rest):
        return _usage_error("stage needs at least one path.")
    git.stage([_abspath(ctx, a) for a in rest])
    return None  # silent, like git add


def _unstage(git: AgentGit, ctx: Any, rest: list[str]) -> Any:
    if not rest or any(a.startswith("-") for a in rest):
        return _usage_error("unstage needs at least one path.")
    git.unstage([_abspath(ctx, a) for a in rest])
    return None


def _commit(git: AgentGit, ctx: Any, rest: list[str]) -> Any:
    message: str | None = None
    args = list(rest)
    while args:
        flag = args.pop(0)
        if flag in ("-m", "--message") and args:
            message = args.pop(0)
        elif flag == "-a":
            return _usage_error(
                "commit stages nothing itself (no -a) — stage first, then commit."
            )
        else:
            return _usage_error(
                "commit takes the staged set only (no pathspec; try: -m MSG)."
            )
    branch = git.status().branch
    commit, files = git.commit(message)
    subject = message if message is not None else "ws-git"
    n = len(files)
    ctx.stdout.write(
        f"[{branch} {commit[:7]}] {subject} ({n} file{'s' if n != 1 else ''})\n"
    )
    return None


def _reset(git: AgentGit, ctx: Any, rest: list[str]) -> Any:
    if rest:
        return _usage_error(
            "reset is mixed-only (no --soft/--hard) — "
            "going back to a commit is: ws-git checkout <ref>."
        )
    git.discard()
    return None


def _checkout(git: AgentGit, ctx: Any, rest: list[str]) -> Any:
    from termish import CommandResult

    if "--" in rest:
        return CommandResult(
            exit_code=1,
            stderr=(
                "checkout of individual paths is not here yet — "
                "ws-git checkout <ref> restores the whole tree."
            ),
        )
    if len(rest) != 1 or rest[0].startswith("-"):
        return _usage_error("checkout takes one ref (a commit from ws-git log).")
    commit = git.resolve(rest[0])
    git.checkout(commit)
    st = git.status()
    ctx.stdout.write(f"[{st.branch}] restored to {commit[:7]}\n")
    return None


def _show_verb(git: AgentGit, ctx: Any, rest: list[str]) -> Any:
    from termish import CommandResult

    if len(rest) != 1 or rest[0].startswith("-"):
        return _usage_error("show takes one ref (a commit from ws-git log).")
    commit = git.resolve(rest[0])
    entry = git.entry(commit)
    if entry is None:
        return CommandResult(exit_code=1, stderr=f"no commit {rest[0]!r} here")
    parents = entry.info.get("virtual_parents") or []
    parent = parents[0] if parents else None
    files = entry.info.get("files")
    new = git.files_at(commit)
    old = git.files_at(parent) if parent else {}
    want = set(files) if isinstance(files, list) else (set(new) | set(old))
    lines = [f"commit {entry.id}"]
    message = entry.info.get("message")
    if message:
        lines.append("")
        lines.append(f"    {message}")
    lines.append("")
    ctx.stdout.write("\n".join(lines) + "\n")
    body = _render_diff(sorted(want), old, new)
    if body:
        ctx.stdout.write("\n".join(body) + "\n")
    return None


def _decode(value: Any) -> bytes | None:
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode()
    return None


def _render_diff(paths: list[str], old: Mapping[str, Any], new: Mapping[str, Any]):
    """Unified diff lines for these paths between two trees."""
    out: list[str] = []
    for path in paths:
        show = _show(path)
        before = _decode(old.get(path)) if path in old else b""
        after = _decode(new.get(path)) if path in new else b""
        if (
            before is None
            or after is None
            or b"\x00" in (before or b"") + (after or b"")
        ):
            out.append(f"Binary files a/{show} and b/{show} differ")
            continue
        if before == after:
            continue
        out.append(f"diff --git a/{show} b/{show}")
        out.extend(
            _unified_with_newline_markers(
                before.decode("utf-8", errors="replace"),
                after.decode("utf-8", errors="replace"),
                show,
            )
        )
    return out


def _diff(git: AgentGit, ctx: Any, rest: list[str]) -> Any:
    cached = False
    check = False
    paths: list[str] = []
    for flag in rest:
        if flag == "--cached":
            cached = True
        elif flag == "--check":
            check = True
        elif flag.startswith("-"):
            return _usage_error(f"diff takes no {flag!r}.")
        else:
            paths.append(_abspath(ctx, flag))
    st = git.status()
    if check:
        return _diff_check(git, ctx, st, paths, cached)
    want = set(st.staged) if cached else set(st.unstaged)
    if paths:
        want &= set(paths)
    if not want:
        return None
    out = _render_diff(sorted(want), git.head_files(), git.working_files())
    if out:
        ctx.stdout.write("\n".join(out) + "\n")
    return None


def _unified_with_newline_markers(old: str, new: str, show: str) -> list[str]:
    """Unified diff lines with git's end-of-file newline markers.

    ``splitlines(keepends=True)`` preserves a trailing-newline-only
    change (plain ``splitlines()`` erases it, emitting a bare header
    with no hunk); difflib itself never marks the missing newline, so
    a content line yielded without one earns git's
    ``\\ No newline at end of file`` line. Output lines carry no
    endings — the caller joins them.
    """
    lines: list[str] = []
    chunks = difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=f"a/{show}",
        tofile=f"b/{show}",
        lineterm="",
    )
    for chunk in chunks:
        body = chunk[:-1] if chunk.endswith("\n") else chunk
        lines.append(body)
        if (
            not chunk.endswith("\n")
            and body[:1] in ("-", "+", " ")
            and body[:3] not in ("---", "+++")
        ):
            lines.append("\\ No newline at end of file")
    return lines


_MARKERS = (b"<<<<<<< ", b"=======", b">>>>>>> ")


def _diff_check(
    git: AgentGit, ctx: Any, st: Any, paths: list[str], cached: bool
) -> Any:
    from termish import CommandResult

    live = git.working_files()
    # Like git: bare --check scans the unstaged worktree, --cached scans
    # the staged set. Unresolved merge paths always count: their markers
    # committed WITH the merge (PR 1), so a clean tree can still be
    # mid-resolution — that is exactly what --check is for.
    base = set(st.staged) if cached else set(st.unstaged)
    want = (base | set(st.merge_unresolved)) & set(live)
    if paths:
        want &= set(paths)
    hits: list[str] = []
    for path in sorted(want):
        value = _decode(live.get(path))
        if value is None:
            continue
        for lineno, line in enumerate(value.split(b"\n"), start=1):
            if line.startswith(_MARKERS):
                hits.append(f"{_show(path)}:{lineno}: leftover conflict marker")
    if hits:
        # Findings go to stdout like git; only the exit code signals.
        ctx.stdout.write("\n".join(hits) + "\n")
        return CommandResult(exit_code=2)
    return None


def _log(git: AgentGit, ctx: Any, rest: list[str]) -> Any:
    limit: int | None = None
    args = list(rest)
    while args:
        flag = args.pop(0)
        if flag in ("-n", "--max-count") and args and args[0].isdigit():
            limit = int(args.pop(0))
        else:
            return _usage_error(f"log takes no {flag!r} (try: -n N).")
    lines: list[str] = []
    for entry in git.log(limit):
        tool = entry.info.get("tool", "?")
        subject = entry.info.get("message") or tool
        line = f"{entry.id[:7]} {subject}"
        if tool == MERGE_TOOL and entry.info.get("source"):
            line += f" from {entry.info['source']}"
            if entry.info.get("sizes"):
                line += " (sizes)"
        lines.append(line)
    if lines:
        ctx.stdout.write("\n".join(lines) + "\n")
    return None
