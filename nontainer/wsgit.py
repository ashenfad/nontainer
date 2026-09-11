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
  and branches do not switch here. ``checkout <ref> -- <paths>`` is
  git-exact (make those paths match that ref, mirroring a directory
  it names), and ``<ref>`` may name another session, which means its
  last commit.
- ``branch <name>`` forks a SESSION rather than making a pointer, and
  ``merge`` lands even when it conflicts (the markers commit, and
  ``status`` carries the merge context) — both because a session is a
  branch and there is no working tree to leave things in.
- ``revert`` and ``cherry-pick`` land the same way, so neither has a
  ``--continue`` or an ``--abort``: the commit is already made, its
  markers are in it, and the next commit that removes them ends it.
  ``revert`` takes no ``-m``: a merge commit reverts to ours, which is
  the only side a session has.
- ``worktree add`` checks out another session under a directory of
  its own, as git's does, but read-only and frozen at a commit: work
  moves between sessions by merge and take, and a second tree an agent
  could edit in place would be a third way with no way back.
- ``diff <name>`` and ``log <name>`` read another session, and where
  that session has a narrowed view ``diff`` groups its changes under
  ``# ... in <name>'s view`` / ``# ... elsewhere``. git has no such
  headers; the collateral a delegate touched outside what it was sent
  to do is the thing a caller must not miss.
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

from .agentgit import HASH_RE, MERGE_TOOL, PICK_TOOL, AgentGit
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
    "tag",
    "branch",
    "merge",
    "revert",
    "cherry-pick",
    "worktree",
    "sparse-checkout",
    "help",
)
_USAGE = (
    "usage: ws-git (stage|unstage|commit|reset|status|diff|log|show|"
    "checkout|tag|branch|merge|revert|cherry-pick|worktree|"
    "sparse-checkout) [...]"
)
_SUPPORTED = (
    "supported: stage <paths> | unstage <paths> | commit [-m MSG] | reset | "
    "status [--porcelain] | diff [<session>] [--cached] [--check] [paths...] | "
    "log [<session>] [-n N] [--all] [-S <string>] | show <ref> | "
    "checkout <ref> [-- <paths>] | "
    "tag [[-f] <name> [<commit>] | -d <name>] | "
    "branch [<name> [--at <ref>] [--fresh] [--paths <paths>]] | "
    "merge (<session> | --abort) | revert <commit> | "
    "cherry-pick <session>@<commit> | "
    "worktree (add <dir> <session>[@<commit>] | list | remove <dir>) | "
    "sparse-checkout [list] | help"
)
# Movie-set edge: name the missing corner in git's own terms plus the
# terminal verb an agent can reach for instead — never host Python it
# cannot run. Exit 1: refusals, not usage errors.
_EDGE = {
    "stash": (
        "no stash here — a fork IS a stash: ws-git branch <name> takes your "
        "state to a session of its own and leaves this one where it is. "
        "Or commit what you have (ws-git commit -m ...) and keep going; "
        "snapshots are cheap."
    ),
    "rebase": (
        "no rebase here — history is append-only. Branch from the commit "
        "you want (ws-git branch <name> --at <ref>) and merge forward; "
        "ws-git checkout <ref> goes back."
    ),
}

#: The three forms, printed by a bare ``ws-git worktree``.
_WORKTREE_FORMS = """usage: ws-git worktree add <dir> <session>[@<commit>]
       ws-git worktree list
       ws-git worktree remove <dir>"""

_HELP = """ws-git: the agent's git over this session.

usage: ws-git (stage|unstage|commit|reset|status|diff|log|show|checkout|
               tag|branch|merge|revert|cherry-pick|worktree|
               sparse-checkout) [...]
  stage <paths>     add paths to the index (optional: commit with an
                    empty index takes everything modified)
  unstage <paths>   drop paths from the index
  commit [-m MSG]   commit the staged set (everything modified, when
                    nothing is staged); no -a, no pathspec. What you
                    left out stays in the working tree, uncommitted
  reset             abandon the composition (mixed-only)
  status [--porcelain]
                    a view: line when this session was given part of
                    the tree, then staged vs unstaged (git-short XY
                    columns; porcelain is the default, so the flag
                    changes nothing), then a worktrees: block — one
                    line per worktree, the shape worktree list prints —
                    when any are up
  diff [<session>] [--cached] [--check] [paths...]
                    unified diff against your last commit; --cached for
                    the staged set; a session name diffs against that
                    session instead
                    --check finds leftover conflict markers
                    (unstaged, or staged with --cached;
                    unresolved merges always)
  log [<session>] [-n N] [--all] [-S <string>]
                    your own commits, newest first; a session name
                    shows that session's; --all is every commit the
                    session holds, the framework's included
                    -S <string> keeps only the commits where the number
                    of times <string> occurs in the tree changed, with
                    + or - after the id for appeared or vanished
  show <ref>        one commit: its message and its diff
  checkout <ref>    restore the tree to a commit of yours (history is
                    append-only: the restore is a new commit)
  checkout <ref> -- <paths>
                    make just those paths match that ref (another
                    session, or a commit of yours). A directory is
                    mirrored: a file it holds here and the ref does
                    not is removed
  tag               your bookmarks, one "name -> commit" per line
  tag [-f] <name> [<commit>]
                    bookmark a commit of yours (your head by default)
                    by name, and use that name wherever a ref is taken.
                    -f moves a name that is taken. A tag is yours and
                    this session's: it is gone when the session is, a
                    fork starts with none, and a merge brings none over
  tag -d <name>     drop a bookmark. The commit stays where it is
  branch            sessions on this store, yours marked *
  branch <name> [--at <ref>] [--fresh] [--paths <paths>]
                    fork a session (does not switch, as in git).
                    --fresh starts it with no conversation; --paths
                    narrows what it can SEE, not what its branch holds
  merge <session>   merge that session's last commit into yours.
                    Conflicts land as markers in the merge commit and
                    show as UU in status; fix them and commit
  merge --abort     restore the tree to the commit the merge landed on
                    and clear the merge, while the merge itself stays
                    in the session's history (the restore is a new
                    commit). Only while a merge is outstanding
  revert <commit>   a new commit undoing that commit's change. What was
                    done since stands, and the commit it undoes stays
                    in the log: history is append-only
  cherry-pick <session>@<commit>
                    a new commit applying one commit's change from
                    another session. Its change, not its tree — what
                    the commit before it holds is left as yours
  worktree add <dir> <session>[@<commit>]
                    check another session's tree out under <dir> and
                    read it with ordinary tools (cat, ls, grep). A
                    session name takes its head at this moment, and it
                    stays there: to see newer work, remove it and add
                    it again
  worktree list     the worktrees here, one line each
  worktree remove <dir>
                    take one down
  sparse-checkout list
                    the paths this session was given to see, one per
                    line, or (full) when it sees the whole tree. A view
                    is given when the session is forked (ws-git branch
                    <name> --paths <paths>) and cannot be changed here
  help              this text

A worktree is READ-ONLY and pinned at a commit, because work moves
between sessions only by merge and take. To change another session's
branch, ask it, or take its files with ws-git checkout <session> --
<paths> and change yours. It lives outside your versioned tree, so no
commit of yours carries it and no status or diff of yours ever names a
file in it; ws-git status ends with a worktrees: block instead.

A revert and a cherry-pick land even when they conflict, as a merge
does: the markers are in the commit, status shows them as UU on a
## merging line naming what was applied, and the next commit that
removes them ends it. There is no --continue and no --abort.

Subset, on purpose: no stash (a fork is a stash: ws-git branch <name>),
no rebase (history is append-only). There is no .git — branches are
sessions, history is commits. The workspace commits on its own as you
work; ws-git log shows only the commits you made (--all for every
commit this session holds)."""


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
                return _status(git, ws, ctx, rest)
            if verb == "stage":
                return _stage(git, ctx, rest)
            if verb == "unstage":
                return _unstage(git, ctx, rest)
            if verb == "commit":
                return _commit(git, ctx, rest)
            if verb == "reset":
                return _reset(git, ctx, rest)
            if verb == "diff":
                return _diff(git, ws, ctx, rest)
            if verb == "log":
                return _log(git, ws, ctx, rest)
            if verb == "show":
                return _show_verb(git, ws, ctx, rest)
            if verb == "checkout":
                return _checkout(git, ws, ctx, rest)
            if verb == "tag":
                return _tag(git, ctx, rest)
            if verb == "branch":
                return _branch(ws, ctx, rest)
            if verb == "merge":
                return _merge(git, ws, ctx, rest)
            if verb == "revert":
                return _revert(git, ws, ctx, rest)
            if verb == "cherry-pick":
                return _cherry_pick(git, ws, ctx, rest)
            if verb == "worktree":
                return _worktree(ws, ctx, rest)
            if verb == "sparse-checkout":
                return _sparse_checkout(ws, ctx, rest)
        except (
            ValueError,
            WorkspaceError,
            NotSupportedError,
            CommitNotFoundError,
            PermissionError,
        ) as e:
            # Domain errors render as messages, never "Unexpected error".
            return CommandResult(exit_code=1, stderr=f"{e}")
        raise AssertionError(f"unreachable verb {verb!r}")

    wsgit.__doc__ = (
        "The agent's git over this session and its neighbours: ws-git "
        "(stage|unstage|commit|reset|status|diff|log|show|checkout|tag|"
        "branch|merge|revert|cherry-pick|worktree|sparse-checkout) [...]"
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


def _show(ws: Any, path: str) -> str:
    """Display path → what the agent can type back: relative to this
    session's root when it is under it, git-short style, and absolute
    when it is not.

    What a verb prints, the verb accepts — and a path outside the root
    has no relative spelling, so printing one would name a different
    file than the one that is there.
    """
    root = ws.root.rstrip("/")
    if root and path.startswith(root + "/"):
        return path[len(root) + 1 :]
    return path


def _usage_error(detail: str) -> Any:
    from termish import CommandResult

    return CommandResult(exit_code=2, stderr=f"{detail}\n{_USAGE}\n{_SUPPORTED}")


def _status(git: AgentGit, ws: Any, ctx: Any, rest: list[str]) -> Any:
    for flag in rest:
        if flag != "--porcelain":
            return _usage_error(f"status takes no {flag!r} (porcelain is the default).")
    st = git.status()
    lines: list[str] = []
    # A narrowed session discovers what it cannot see by being refused,
    # unless it is told: what it was given leads, so a reader knows
    # what the rows below are measured over.
    seed = _seed_paths(ws)
    if seed:
        lines.append("view: " + ", ".join(seed))
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
            lines.append(f"UU {_show(ws, path)}")
        else:
            x = "M" if path in staged else " "
            y = "M" if path in unstaged else " "
            lines.append(f"{x}{y} {_show(ws, path)}")
    # A worktree sits outside the versioned tree, so no row above can
    # ever name a file in one; without this block a directory full of
    # files that never show as modified is a puzzle.
    worktrees = _worktree_lines(ws)
    if worktrees:
        lines.append("worktrees:")
        lines.extend(worktrees)
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


def _sessions(ws: Any) -> list[str]:
    """Sessions on this store, or just this one when no store opened it.

    The permission question — which of them an agent may SEE — belongs
    to the embedder's registry and is not answered here; today every
    session on the store is listed.
    """
    store = getattr(ws, "_store", None)
    if store is None:
        return [ws.session]
    return list(store.sessions())


def _is_session(ws: Any, name: str) -> bool:
    """Whether a word an agent typed names another session.

    Asked of the store where one opened this workspace, and of the
    substrate otherwise: a workspace built straight from a provider
    still has neighbours on the same store, and a verb that could not
    see them there would behave differently for no reason the agent
    can observe.
    """
    if name == ws.session:
        return False
    store = getattr(ws, "_store", None)
    if store is not None:
        return name in set(store.sessions())
    try:
        ws._provider.branch_head(name)
    except Exception:  # noqa: BLE001 - not a branch is the answer
        return False
    return True


def _other_session(ws: Any, name: str) -> Any:
    """A read handle on another session, opened for one verb.

    Opened through the store rather than reached through this
    session's provider: a session is a workspace, and the verbs that
    read one (``log``) want the whole fiction over it, not a branch
    handle. The caller closes it.

    At THIS session's root, because a lineage shares one: opening a
    session at a root it does not use creates that directory and can
    commit the making of it, so a read-only verb would write to the
    session it was only asked to read.
    """
    store = getattr(ws, "_store", None)
    if store is None:
        raise ValueError(
            f"cannot reach {name!r}: this session was not opened from a store"
        )
    if name not in set(store.sessions()):
        raise ValueError(f"unknown session {name!r} (ws-git branch lists them)")
    return store.open(name, root=ws.root)


def _checkout(git: AgentGit, ws: Any, ctx: Any, rest: list[str]) -> Any:
    if "--" in rest:
        cut = rest.index("--")
        head, paths = rest[:cut], rest[cut + 1 :]
        if len(head) > 1 or any(a.startswith("-") for a in head):
            return _usage_error("checkout takes one ref before --.")
        if not paths:
            return _usage_error("checkout -- needs at least one path.")
        ref = _take_ref(git, head[0] if head else "HEAD")
        ws.checkout(ref, paths=[_abspath(ctx, a) for a in paths])
        n = len(paths)
        plural = "" if n == 1 else "s"
        ctx.stdout.write(f"Updated {n} path{plural} from {ref}\n")
        return None
    if len(rest) != 1 or rest[0].startswith("-"):
        return _usage_error("checkout takes one ref (a commit from ws-git log).")
    commit = git.resolve(rest[0])
    git.checkout(commit)
    st = git.status()
    ctx.stdout.write(f"[{st.branch}] restored to {commit[:7]}\n")
    return None


def _take_ref(git: AgentGit, ref: str) -> str:
    """A ref an agent typed, for the take form.

    ``HEAD``, a bare hash and a tag of this session's are this
    session's and go through the agent's own resolution, so a framework
    commit is refused by name the way the whole-tree form refuses it. A
    tag wins over a session of the same name: it is the spelling this
    session chose for a state of its own. Everything else — a session
    name, a ``session@commit`` — is passed on for the workspace to
    resolve, because a take may reach anywhere.
    """
    if ref == "HEAD" or HASH_RE.fullmatch(ref) or ref in git.tags():
        return git.resolve(ref)
    return ref


def _tag(git: AgentGit, ctx: Any, rest: list[str]) -> Any:
    if not rest:
        tags = git.tags()
        lines = [f"{name} -> {commit[:7]}" for name, commit in sorted(tags.items())]
        ctx.stdout.write("\n".join(lines or ["(none)"]) + "\n")
        return None
    if rest[0] == "-d":
        if len(rest) != 2 or rest[1].startswith("-"):
            return _usage_error("tag -d takes one name.")
        commit = git.delete_tag(rest[1])
        ctx.stdout.write(f"Deleted tag {rest[1]!r} (was {commit[:7]})\n")
        return None
    force = rest[0] == "-f"
    args = rest[1:] if force else rest
    if not 1 <= len(args) <= 2 or any(a.startswith("-") for a in args):
        return _usage_error(
            "tag takes a name and optionally a commit (try: -f to move a "
            "name that is taken, -d to drop one)."
        )
    git.tag(args[0], args[1] if len(args) > 1 else None, force=force)
    return None  # silent, like git tag


def _branch(ws: Any, ctx: Any, rest: list[str]) -> Any:
    if not rest:
        mine = ws.session
        lines = [
            f"{'*' if name == mine else ' '} {name}" for name in sorted(_sessions(ws))
        ]
        if lines:
            ctx.stdout.write("\n".join(lines) + "\n")
        return None
    name, at, fresh, paths = rest[0], None, False, None
    if name.startswith("-"):
        return _usage_error("branch takes a name first.")
    args = rest[1:]
    while args:
        flag = args.pop(0)
        if flag == "--at" and args:
            at = args.pop(0)
        elif flag == "--fresh":
            fresh = True
        elif flag == "--paths":
            # The leading run of non-flag words, so a flag after them
            # is still a flag: "--paths a b --fresh" is two paths and
            # a fresh conversation, not three paths.
            paths = []
            while args and not args[0].startswith("-"):
                paths.append(args.pop(0))
            if not paths:
                return _usage_error("--paths needs at least one path.")
        else:
            return _usage_error(
                f"branch takes no {flag!r} (try: --at <ref>, --fresh, --paths <paths>)."
            )
    child = ws.fork(
        name,
        at=at,
        inherit="fresh" if fresh else "full",
        paths=[_abspath(ctx, a) for a in paths] if paths else None,
    )
    # The branch is what the verb makes; the handle it came back on is
    # this call's and holds an executor of its own.
    child.close()
    return None  # silent, like git branch


def _merge(git: AgentGit, ws: Any, ctx: Any, rest: list[str]) -> Any:
    from termish import CommandResult

    if rest == ["--abort"]:
        source, commit = git.abort_merge()
        ctx.stdout.write(
            f"[{git.status().branch}] aborted merge of {source}, "
            f"restored to {commit[:7]}\n"
        )
        return None
    if len(rest) != 1 or rest[0].startswith("-"):
        return _usage_error(
            "merge takes one session name (ws-git branch lists them), or --abort."
        )
    source = rest[0]
    out = ws.merge(source)
    if not out.merged:
        lines = [f"CONFLICT: {_show(ws, path)}" for path in out.conflicts]
        lines.append(
            f"Merge of {source} refused: contested state no rule resolves. "
            "Nothing changed."
        )
        return CommandResult(exit_code=1, stderr="\n".join(lines))
    for path in out.auto_merged:
        ctx.stdout.write(f"Auto-merging {_show(ws, path)}\n")
    for path in out.conflicts:
        ctx.stdout.write(f"CONFLICT (content): Merge conflict in {_show(ws, path)}\n")
    n = len(out.auto_merged) + len(out.conflicts)
    ctx.stdout.write(
        f"[{git.status().branch} {out.commit[:7]}] merge {source} "
        f"({n} file{'' if n == 1 else 's'})\n"
    )
    if out.conflicts:
        # git leaves conflicts uncommitted; here the merge always
        # lands, markers and all, so the news is what to do next.
        return CommandResult(
            exit_code=1,
            stderr=(
                "Merge landed with conflict markers in "
                f"{len(out.conflicts)} file(s): fix them and commit "
                "(ws-git status shows them as UU)."
            ),
        )
    return None


def _revert(git: AgentGit, ws: Any, ctx: Any, rest: list[str]) -> Any:
    if len(rest) != 1 or rest[0].startswith("-"):
        return _usage_error("revert takes one commit (a commit from ws-git log).")
    commit = git.resolve(rest[0])
    return _applied(
        git, ws, ctx, ws.revert(commit), verb="revert", what=commit[:7], noun="Revert"
    )


def _cherry_pick(git: AgentGit, ws: Any, ctx: Any, rest: list[str]) -> Any:
    if len(rest) != 1 or rest[0].startswith("-") or "@" not in rest[0]:
        named = rest[0] if rest and not rest[0].startswith("-") else "<session>"
        return _usage_error(
            "cherry-pick takes one commit of another session, spelled "
            f"<session>@<commit> (ws-git log {named} lists them)."
        )
    ref = _short_ref(_whole_ref(ws, rest[0]))
    return _applied(
        git,
        ws,
        ctx,
        ws.cherry_pick(rest[0]),
        verb="cherry-pick",
        what=ref,
        noun="Cherry-pick",
    )


def _applied(
    git: AgentGit, ws: Any, ctx: Any, out: Any, *, verb: str, what: str, noun: str
) -> Any:
    """Render what applying one commit's change did, in merge's shapes.

    A revert and a cherry-pick are the same operation with the two
    commits swapped, and both are a three-way against this tree, so
    what they print is what a merge prints: the files taken without
    judgment, the ones left marked, and one line naming the commit
    that landed. A change that is already here changes nothing and
    says so.
    """
    from termish import CommandResult

    if not out.merged and not out.conflicts:
        return CommandResult(
            exit_code=1,
            stderr=(
                f"nothing to {verb}: {what} changes nothing this tree does "
                "not already hold. Nothing changed."
            ),
        )
    if not out.merged:
        lines = [f"CONFLICT: {_show(ws, path)}" for path in out.conflicts]
        lines.append(
            f"{noun} of {what} refused: contested state no rule resolves. "
            "Nothing changed."
        )
        return CommandResult(exit_code=1, stderr="\n".join(lines))
    for path in out.auto_merged:
        ctx.stdout.write(f"Auto-merging {_show(ws, path)}\n")
    for path in out.conflicts:
        ctx.stdout.write(f"CONFLICT (content): Merge conflict in {_show(ws, path)}\n")
    n = len(out.auto_merged) + len(out.conflicts)
    ctx.stdout.write(
        f"[{git.status().branch} {out.commit[:7]}] {verb} {what} "
        f"({n} file{'' if n == 1 else 's'})\n"
    )
    if out.conflicts:
        # The change lands, markers and all, so the news is what to do
        # next — there is no state to continue or abort from.
        return CommandResult(
            exit_code=1,
            stderr=(
                f"{noun} landed with conflict markers in "
                f"{len(out.conflicts)} file(s): fix them and commit "
                "(ws-git status shows them as UU)."
            ),
        )
    return None


def _worktree(ws: Any, ctx: Any, rest: list[str]) -> Any:
    if not rest:
        ctx.stdout.write(_WORKTREE_FORMS + "\n")
        return None
    sub, args = rest[0], rest[1:]
    if sub == "add":
        return _worktree_add(ws, ctx, args)
    if sub == "list":
        if args:
            return _usage_error("worktree list takes no arguments.")
        ctx.stdout.write("\n".join(_worktree_lines(ws) or ["(none)"]) + "\n")
        return None
    if sub == "remove":
        return _worktree_remove(ws, ctx, args)
    return _usage_error(f"worktree takes no {sub!r} (add, list, remove).")


def _worktree_lines(ws: Any) -> list[str]:
    """One line per worktree: where it is, what it holds, and that it
    cannot be written to."""
    return [
        f"worktree {_show(ws, at)}: {_short_ref(ref)} (read-only)"
        for at, ref in sorted(ws.files.attachments().items())
    ]


def _worktree_add(ws: Any, ctx: Any, rest: list[str]) -> Any:
    from termish import CommandResult

    if len(rest) != 2 or any(a.startswith("-") for a in rest):
        return _usage_error(
            "worktree add takes a directory and a session (ws-git branch lists them)."
        )
    where, ref = rest
    point = _abspath(ctx, where)
    session = ref.rpartition("@")[0] or ref
    if session == ws.session:
        return CommandResult(
            exit_code=1,
            stderr=(
                f"cannot add a worktree of {session!r}: a worktree reads "
                "another session, and this one is already your own tree."
            ),
        )
    held_here = ws.files.attachments()
    if point in held_here:
        # Adding over a live worktree cannot refresh it: what is pinned
        # stays pinned, and the directory a worktree fills reads as a
        # directory with something in it. Naming the two steps beats
        # the not-empty reason, which sends a reader looking for files
        # to delete out of a tree it does not own.
        return CommandResult(
            exit_code=1,
            stderr=(
                f"there is already a worktree at {where!r}: it is pinned "
                "at a commit, so seeing newer work means taking it down "
                f"and putting it up again (ws-git worktree remove {where})."
            ),
        )
    for held in sorted(held_here):
        if point.startswith(held + "/"):
            return CommandResult(
                exit_code=1,
                stderr=(
                    f"cannot add a worktree at {where!r}: that is inside "
                    f"the worktree {_show(ws, held)}."
                ),
            )
    fs = ws.files.fs
    if fs.exists(point):
        # A worktree is a mount: what it lands on is hidden rather than
        # merged, so anything already there would silently disappear.
        reason = None
        if not fs.isdir(point):
            reason = "a file is already there"
        elif fs.list(point):
            reason = "that directory is not empty (a worktree needs a new or empty one)"
        if reason is not None:
            return CommandResult(
                exit_code=1,
                stderr=f"cannot add a worktree at {where!r}: {reason}.",
            )
    landed = ws.files.attach(ref, point)
    ctx.stdout.write(f"worktree {_show(ws, point)}: {_short_ref(landed)} (read-only)\n")
    return None


def _whole_ref(ws: Any, ref: str) -> str:
    """A ``session@x`` an agent typed, with ``x`` spelled as a commit.

    What the verb PRINTS has to be a state, not the spelling that
    reached it: a tag is one session's private name for a commit, so a
    line naming ``peer@good`` says nothing about which commit landed
    and nothing another session can look up. Anything the funnel
    cannot resolve comes back as it came, for the verb itself to
    refuse in its own words.
    """
    from .store import Ref

    parsed = Ref.parse(ref)
    return str(Ref(parsed.session, ws._ref_commit(parsed.session, parsed.commit)))


def _short_ref(ref: str) -> str:
    """Ref → display shape: the session and seven characters of the
    commit, as every other ws-git verb prints one."""
    from .store import Ref

    parsed = Ref.parse(ref)
    return f"{parsed.session}@{parsed.commit[:7]}"


def _worktree_remove(ws: Any, ctx: Any, rest: list[str]) -> Any:
    if len(rest) != 1 or rest[0].startswith("-"):
        return _usage_error("worktree remove takes one directory.")
    where = rest[0]
    point = _abspath(ctx, where)
    held = sorted(ws.files.attachments())
    if point not in held:
        known = ", ".join(_show(ws, p) for p in held)
        return _usage_error(
            f"no worktree at {where!r} "
            f"({'worktrees here: ' + known if held else 'no worktrees here'})."
        )
    ws.files.detach(point)
    return None  # silent, like git worktree remove


def _seed_paths(ws: Any) -> list[str]:
    """The paths this session was GIVEN to see, rendered the way every
    other path this verb prints is: relative to the root, a directory
    with its slash. Empty for a session that sees the whole tree.

    The seed, not the view as it stands: a file the session created
    joined its view because it made it, while what it was narrowed to
    is what it was given at the fork.
    """
    from .views import VIEW_KEY, parse_seed

    return [
        _show(ws, path) + ("/" if _is_dir(ws, path) else "")
        for path in parse_seed(ws._provider.kv.get(VIEW_KEY))
    ]


def _sparse_checkout(ws: Any, ctx: Any, rest: list[str]) -> Any:
    if rest and rest[0] != "list":
        return _usage_error(
            f"sparse-checkout takes no {rest[0]!r} (list is all there is): "
            "a view is given when the session is forked (ws-git branch "
            "<name> --paths <paths>) and cannot be changed here."
        )
    if len(rest) > 1:
        return _usage_error("sparse-checkout list takes no arguments.")
    ctx.stdout.write("\n".join(_seed_paths(ws) or ["(full)"]) + "\n")
    return None


def _show_verb(git: AgentGit, ws: Any, ctx: Any, rest: list[str]) -> Any:
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
    body = _render_diff(ws, sorted(want), old, new)
    if body:
        ctx.stdout.write("\n".join(body) + "\n")
    return None


def _decode(value: Any) -> bytes | None:
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode()
    return None


def _render_diff(
    ws: Any, paths: list[str], old: Mapping[str, Any], new: Mapping[str, Any]
):
    """Unified diff lines for these paths between two trees."""
    out: list[str] = []
    for path in paths:
        show = _show(ws, path)
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


def _under_any(path: str, pathspec: list[str]) -> bool:
    """Whether a path is named by a pathspec — itself, or a directory
    above it, as git's pathspecs work."""
    return any(path == p or path.startswith(p + "/") for p in pathspec)


def _names_a_path(git: AgentGit, ws: Any, path: str) -> bool:
    """Whether a resolved path names something in this session's tree —
    a file it holds or held at the agent's head, or a directory."""
    if path in set(git.working_files()) | set(git.head_files()):
        return True
    return _is_dir(ws, path)


def _is_dir(ws: Any, path: str) -> bool:
    """Whether a workspace path is a directory here. A path the
    filesystem cannot judge is not one."""
    try:
        return bool(ws._fs.isdir(path))
    except Exception:  # noqa: BLE001 - unjudgeable is the answer
        return False


def _unknown_pathspec(
    git: AgentGit, ws: Any, words: list[str], paths: list[str]
) -> Any:
    """Refuse a word that names neither a session nor anything in the
    tree, instead of reading it as a pathspec that matches nothing.

    Silence means "no differences" here, and a mistyped session name
    would earn it — an agent asking about a delegate would read "no
    differences" as an answer. git refuses the same case by name.
    """
    from termish import CommandResult

    for word, path in zip(words, paths):
        if _names_a_path(git, ws, path):
            continue
        return CommandResult(
            exit_code=1,
            stderr=(
                f"ambiguous argument {word!r}: unknown session or path. "
                "ws-git branch lists the sessions; a pathspec has to name "
                "something in your tree."
            ),
        )
    return None


def _diff(git: AgentGit, ws: Any, ctx: Any, rest: list[str]) -> Any:
    cached = False
    check = False
    words: list[str] = []
    for flag in rest:
        if flag == "--cached":
            cached = True
        elif flag == "--check":
            check = True
        elif flag.startswith("-"):
            return _usage_error(f"diff takes no {flag!r}.")
        else:
            words.append(flag)
    paths = [_abspath(ctx, word) for word in words]
    # A word that is both a path here and a session elsewhere is the
    # PATH: the pathspec reading is the one that still works when the
    # two collide (name the file, not the branch), and the diff of
    # another session is always reachable as `ws-git diff <name>` from
    # a session that has no such file.
    if words and not _names_a_path(git, ws, paths[0]) and _is_session(ws, words[0]):
        if len(words) > 1 or cached or check:
            return _usage_error(
                "diff <session> takes no other argument (no pathspec, "
                "--cached or --check against another session)."
            )
        return _diff_branch(git, ws, ctx, words[0])
    if paths:
        unknown = _unknown_pathspec(git, ws, words, paths)
        if unknown is not None:
            return unknown
    st = git.status()
    if check:
        return _diff_check(git, ws, ctx, st, paths, cached)
    want = set(st.staged) if cached else set(st.unstaged)
    if paths:
        want = {p for p in want if _under_any(p, paths)}
    if not want:
        return None
    out = _render_diff(ws, sorted(want), git.head_files(), git.working_files())
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
    git: AgentGit, ws: Any, ctx: Any, st: Any, paths: list[str], cached: bool
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
        want = {p for p in want if _under_any(p, paths)}
    hits: list[str] = []
    for path in sorted(want):
        value = _decode(live.get(path))
        if value is None:
            continue
        for lineno, line in enumerate(value.split(b"\n"), start=1):
            if line.startswith(_MARKERS):
                hits.append(f"{_show(ws, path)}:{lineno}: leftover conflict marker")
    if hits:
        # Findings go to stdout like git; only the exit code signals.
        ctx.stdout.write("\n".join(hits) + "\n")
        return CommandResult(exit_code=2)
    return None


def _diff_branch(git: AgentGit, ws: Any, ctx: Any, name: str) -> Any:
    """This session against another session's last commit.

    Grouped by the OTHER session's view when it has one: what a
    delegate was sent to do, then what else it touched. The merge takes
    both — the grouping is what keeps the second from going unnoticed.
    """
    from .views import VIEW_KEY, parse_seed

    provider = ws._provider
    theirs = git.source_commit(name)
    ours = git.head or provider.head
    old = provider.files_at(ours)
    new = provider.files_at(theirs)
    changed = sorted(
        path for path in set(old) | set(new) if old.get(path) != new.get(path)
    )
    if not changed:
        return None
    seed = parse_seed(provider.key_at(theirs, VIEW_KEY))
    if not seed:
        body = _render_diff(ws, changed, old, new)
        if body:
            ctx.stdout.write("\n".join(body) + "\n")
        return None

    def seeded(path: str) -> bool:
        return any(path == e or path.startswith(e + "/") for e in seed)

    lines: list[str] = []
    for label, group in (
        (f"in {name}'s seed", [p for p in changed if seeded(p)]),
        ("elsewhere", [p for p in changed if not seeded(p)]),
    ):
        if not group:
            continue
        lines.append(f"# {len(group)} path(s) {label}")
        lines.extend(_render_diff(ws, group, old, new))
    if lines:
        ctx.stdout.write("\n".join(lines) + "\n")
    return None


def _log(git: AgentGit, ws: Any, ctx: Any, rest: list[str]) -> Any:
    limit: int | None = None
    session: str | None = None
    needle: str | None = None
    every = False
    args = list(rest)
    while args:
        flag = args.pop(0)
        if flag in ("-n", "--max-count") and args and args[0].isdigit():
            limit = int(args.pop(0))
        elif flag == "-S" and args:
            needle = args.pop(0)
        elif flag == "--all":
            every = True
        elif not flag.startswith("-") and session is None:
            session = flag
        else:
            return _usage_error(
                f"log takes no {flag!r} (try: -n N, --all, -S <string>)."
            )
    if session is not None and session != ws.session:
        other = _other_session(ws, session)
        try:
            return _log_out(AgentGit(other), other, ctx, limit, every, needle)
        finally:
            other.close()
    return _log_out(git, ws, ctx, limit, every, needle)


def _log_out(
    git: AgentGit,
    ws: Any,
    ctx: Any,
    limit: int | None,
    every: bool,
    needle: str | None,
) -> Any:
    """One session's log: the agent's commits, or every commit the
    session holds with ``--all``, filtered to where a string appeared
    or vanished when one was named.

    The tags are that session's own, so a log of a neighbour carries
    the neighbour's bookmarks and not this session's."""
    walk = _walk(git, every)
    tags = _by_commit(git.tags())
    if needle is None:
        entries = [entry for entry, _ in _capped(walk, limit)]
        return _log_lines(entries, ctx, tags)
    found = _capped(_pickaxe(ws._provider, walk, needle), limit)
    return _log_lines(
        [entry for entry, _ in found], ctx, tags, {e.id: s for e, s in found}
    )


def _by_commit(tags: Mapping[str, str]) -> dict[str, list[str]]:
    """Tag name → commit, turned around: commit → its names, sorted, so
    a log line can say what points at it."""
    out: dict[str, list[str]] = {}
    for name, commit in sorted(tags.items()):
        out.setdefault(commit, []).append(name)
    return out


def _capped(pairs: Any, limit: int | None) -> list:
    """The first ``limit`` of what a walk yields, or all of it.

    A limit that asks for nothing gets nothing: ``-n 0`` is an empty
    log, and so is a negative one.
    """
    if limit is not None and limit <= 0:
        return []
    out = []
    for pair in pairs:
        out.append(pair)
        if limit is not None and len(out) >= limit:
            break
    return out


def _walk(git: AgentGit, every: bool):
    """``(commit, its parent)`` newest first.

    The agent's own commits walk the agent's graph, so the parent is
    the previous commit the agent made and the framework's commits
    between the two are part of that step. ``--all`` walks the
    session's history instead, where the parent is the commit before
    this one in the store.
    """
    from .agentgit import virtual_parents

    if every:
        for entry in git.history():
            yield entry, (entry.parents[0] if entry.parents else None)
        return
    for entry in git.log():
        parents = virtual_parents(entry.info)
        yield entry, (parents[0] if parents else None)


def _pickaxe(provider: Any, walk: Any, needle: str):
    """``(commit, "+" or "-")`` for the commits where the number of
    times ``needle`` occurs in the tree changed — git's pickaxe rule.

    Only the files that changed between a commit and its parent are
    read: a file both sides hold unchanged contributes the same count
    to each, so it cannot move the total. Bytes that are not text hold
    no occurrences, so a binary file is never a match.
    """
    for entry, parent in walk:
        new = provider.files_at(entry.id)
        old = provider.files_at(parent) if parent else {}
        if parent is None:
            changed: Any = set(new)
        else:
            changed = provider.diff(parent, entry.id).paths
        delta = sum(_occurrences(new, p, needle) for p in changed) - sum(
            _occurrences(old, p, needle) for p in changed
        )
        if delta:
            yield entry, ("+" if delta > 0 else "-")


def _occurrences(files: Mapping[str, Any], path: str, needle: str) -> int:
    """How many times ``needle`` occurs in one file of a tree. Absent
    and undecodable both count none."""
    raw = _decode(files.get(path)) if path in files else None
    if raw is None:
        return 0
    try:
        return raw.decode().count(needle)
    except UnicodeDecodeError:
        return 0


def _log_lines(
    entries: Any,
    ctx: Any,
    tags: "Mapping[str, list[str]] | None" = None,
    signs: "Mapping[str, str] | None" = None,
) -> Any:
    lines: list[str] = []
    for entry in entries:
        tool = entry.info.get("tool", "?")
        subject = entry.info.get("message") or tool
        mark = f"{signs[entry.id]} " if signs else ""
        # git decorates the id with what points at it; here the only
        # thing that can is one of this session's own tags.
        named = (tags or {}).get(entry.id)
        deco = f" (tag: {', '.join(named)})" if named else ""
        line = f"{entry.id[:7]}{deco} {mark}{subject}"
        if tool == MERGE_TOOL and entry.info.get("source"):
            line += f" from {entry.info['source']}"
            if entry.info.get("sizes"):
                line += " (sizes)"
        elif tool == PICK_TOOL and entry.info.get("picked_from"):
            # A cherry-pick keeps the subject the other session wrote,
            # so the line has to say whose change it is.
            line += f" from {_short_ref(entry.info['picked_from'])}"
        lines.append(line)
    if lines:
        ctx.stdout.write("\n".join(lines) + "\n")
    return None
