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

from .agentgit import HASH_RE, MERGE_TOOL, AgentGit
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
    "branch",
    "merge",
    "help",
)
_USAGE = (
    "usage: ws-git (stage|unstage|commit|reset|status|diff|log|show|"
    "checkout|branch|merge) [...]"
)
_SUPPORTED = (
    "supported: stage <paths> | unstage <paths> | commit [-m MSG] | reset | "
    "status [--porcelain] | diff [<session>] [--cached] [--check] [paths...] | "
    "log [<session>] [-n N] | show <ref> | checkout <ref> [-- <paths>] | "
    "branch [<name> [--at <ref>] [--fresh] [--paths <paths>]] | "
    "merge <session> | help"
)
_ROOT = "/workspace"

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

_HELP = """ws-git: the agent's git over this session.

usage: ws-git (stage|unstage|commit|reset|status|diff|log|show|checkout|
               branch|merge) [...]
  stage <paths>     add paths to the index (optional: commit with an
                    empty index takes everything modified)
  unstage <paths>   drop paths from the index
  commit [-m MSG]   commit the staged set (everything modified, when
                    nothing is staged); no -a, no pathspec. What you
                    left out stays in the working tree, uncommitted
  reset             abandon the composition (mixed-only)
  status [--porcelain]
                    staged vs unstaged (git-short XY columns; porcelain
                    is the default, so the flag changes nothing)
  diff [<session>] [--cached] [--check] [paths...]
                    unified diff against your last commit; --cached for
                    the staged set; a session name diffs against that
                    session instead
                    --check finds leftover conflict markers
                    (unstaged, or staged with --cached;
                    unresolved merges always)
  log [<session>] [-n N]
                    your own commits, newest first; a session name
                    shows that session's
  show <ref>        one commit: its message and its diff
  checkout <ref>    restore the tree to a commit of yours (history is
                    append-only: the restore is a new commit)
  checkout <ref> -- <paths>
                    make just those paths match that ref (another
                    session, or a commit of yours). A directory is
                    mirrored: a file it holds here and the ref does
                    not is removed
  branch            sessions on this store, yours marked *
  branch <name> [--at <ref>] [--fresh] [--paths <paths>]
                    fork a session (does not switch, as in git).
                    --fresh starts it with no conversation; --paths
                    narrows what it can SEE, not what its branch holds
  merge <session>   merge that session's last commit into yours.
                    Conflicts land as markers in the merge commit and
                    show as UU in status; fix them and commit
  help              this text

Subset, on purpose: no stash (a fork is a stash: ws-git branch <name>),
no rebase (history is append-only). There is no .git — branches are
sessions, history is commits. The workspace commits on its own as you
work; ws-git log shows only the commits you made."""


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
                return _diff(git, ws, ctx, rest)
            if verb == "log":
                return _log(git, ws, ctx, rest)
            if verb == "show":
                return _show_verb(git, ctx, rest)
            if verb == "checkout":
                return _checkout(git, ws, ctx, rest)
            if verb == "branch":
                return _branch(ws, ctx, rest)
            if verb == "merge":
                return _merge(git, ws, ctx, rest)
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
        "(stage|unstage|commit|reset|status|diff|log|show|checkout|"
        "branch|merge) [...]"
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

    ``HEAD`` and a bare hash are this session's and go through the
    agent's own resolution, so a framework commit is refused by name
    the way the whole-tree form refuses it. Everything else — a
    session name, a ``session@commit`` — is passed on for the
    workspace to resolve, because a take may reach anywhere.
    """
    if ref == "HEAD" or HASH_RE.fullmatch(ref):
        return git.resolve(ref)
    return ref


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

    if len(rest) != 1 or rest[0].startswith("-"):
        return _usage_error("merge takes one session name (ws-git branch lists them).")
    source = rest[0]
    out = ws.merge(source)
    if not out.merged:
        lines = [f"CONFLICT: {_show(path)}" for path in out.conflicts]
        lines.append(
            f"Merge of {source} refused: contested state no rule resolves. "
            "Nothing changed."
        )
        return CommandResult(exit_code=1, stderr="\n".join(lines))
    for path in out.auto_merged:
        ctx.stdout.write(f"Auto-merging {_show(path)}\n")
    for path in out.conflicts:
        ctx.stdout.write(f"CONFLICT (content): Merge conflict in {_show(path)}\n")
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


def _under_any(path: str, pathspec: list[str]) -> bool:
    """Whether a path is named by a pathspec — itself, or a directory
    above it, as git's pathspecs work."""
    return any(path == p or path.startswith(p + "/") for p in pathspec)


def _names_a_path(git: AgentGit, ws: Any, path: str) -> bool:
    """Whether a resolved path names something in this session's tree —
    a file it holds or held at the agent's head, or a directory."""
    if path in set(git.working_files()) | set(git.head_files()):
        return True
    try:
        return bool(ws._fs.isdir(path))
    except Exception:  # noqa: BLE001 - a path the filesystem cannot judge is not one
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
        return _diff_check(git, ctx, st, paths, cached)
    want = set(st.staged) if cached else set(st.unstaged)
    if paths:
        want = {p for p in want if _under_any(p, paths)}
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
        want = {p for p in want if _under_any(p, paths)}
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
        body = _render_diff(changed, old, new)
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
        lines.extend(_render_diff(group, old, new))
    if lines:
        ctx.stdout.write("\n".join(lines) + "\n")
    return None


def _log(git: AgentGit, ws: Any, ctx: Any, rest: list[str]) -> Any:
    limit: int | None = None
    session: str | None = None
    args = list(rest)
    while args:
        flag = args.pop(0)
        if flag in ("-n", "--max-count") and args and args[0].isdigit():
            limit = int(args.pop(0))
        elif not flag.startswith("-") and session is None:
            session = flag
        else:
            return _usage_error(f"log takes no {flag!r} (try: -n N).")
    if session is not None and session != ws.session:
        other = _other_session(ws, session)
        try:
            return _log_lines(AgentGit(other).log(limit), ctx)
        finally:
            other.close()
    return _log_lines(git.log(limit), ctx)


def _log_lines(entries: Any, ctx: Any) -> Any:
    lines: list[str] = []
    for entry in entries:
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
