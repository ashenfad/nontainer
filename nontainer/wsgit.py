"""The ``ws-git`` terminal builtin: git-shaped staging over the index.

Movie-set rule (see ``scratch/ws-git-impl.md`` PR 3): follow git
wherever cheap; each deviation carries a recorded reason:

- ``status`` takes git-short ``XY`` columns; merge context rides one
  ``## merging`` line git has no equivalent for (our ``status()``
  already returns the source — dropping it would be purer but less
  useful).
- ``reset`` is mixed-only; ``--soft``/``--hard`` get the usage error.
- ``log`` shows the ``-m`` message when a commit has one, else the
  checkpoint tool name.
- ``commit`` takes ``-m/--message`` (stored in checkpoint info) but no
  pathspec and no ``-a``: it commits the staged set only.
- The index names keys, not snapshots, so ``--cached`` shows staged
  paths against HEAD *including* any later edits (git would show only
  the staged snapshot).

Everything else is git-exact: silent clean status, unified ``diff``
with ``a/``/``b/`` headers, ``diff --check`` marker lines, ``UU``
unmerged entries, ``--porcelain`` accepted as the stable-contract
alias it is in git.
"""

from __future__ import annotations

import difflib
import posixpath
from typing import Any

from .errors import NotSupportedError, WorkspaceError

_VERBS = ("stage", "unstage", "commit", "reset", "status", "diff", "log", "help")
_USAGE = "usage: ws-git (stage|unstage|commit|reset|status|diff|log) [...]"
_SUPPORTED = (
    "supported: stage <paths> | unstage <paths> | commit [-m MSG] | reset | "
    "status [--porcelain] | diff [--cached] [--check] [paths...] | "
    "log [-n N] | help"
)
_ROOT = "/workspace"

# Movie-set edge: name the missing corner in git's own terms plus the
# native alternative. Exit 1: refusals, not usage errors.
_EDGE = {
    "stash": (
        'no stash here — a fork is a stash (try: ws.fork("experiment")). '
        "Snapshots are cheap; nothing needs shelving."
    ),
    "rebase": (
        "no rebase here — history is append-only. Fork from the commit "
        "you want and merge forward."
    ),
    "branch": ('no branches here — sessions are branches (try: ws.fork("name")).'),
    "checkout": (
        'no checkout here — fork at a tag (ws.fork("name", at=...)) '
        "or restore (ws.restore(...))."
    ),
    "merge": (
        "merge lives in Python for now (provider.merge(source)) — "
        "terminal merge arrives with the consult flows."
    ),
}

_HELP = """ws-git: git-shaped staging over workspace branches.

usage: ws-git (stage|unstage|commit|reset|status|diff|log) [...]
  stage <paths>     stage files (first stage suspends autocheckpoint)
  unstage <paths>   unstage files (emptying resumes autocheckpoint)
  commit [-m MSG]   commit staged files; unstaged work stays dirty
                    (-m stored in checkpoint info; no -a, no pathspec)
  reset             abandon the composition (mixed-only), resume
  status            staged vs unstaged (git-short XY columns)
  diff [--cached] [--check] [paths...]
                    unified worktree diff; --cached for staged
                    --check finds leftover conflict markers
                    (unstaged, or staged with --cached;
                    unresolved merges always)
  log [-n N]        newest-first checkpoint hashes and messages
  help              this text

Subset, on purpose: no stash (a fork is a stash), no rebase (history
is append-only), no checkout (fork at a tag, or restore). There is no
.git — branches are sessions, history is checkpoints. Compose
stage-first: stage paths (suspends autocheckpoint), then edit, then
commit — writes before the first stage checkpoint immediately."""


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
    if not ws.supports_commands and not hasattr(
        getattr(ws, "_executor", None), "_guest_to_host"
    ):
        return
    fn = make_wsgit_command(ws)
    # Tags OUR registration: the dud handler must not front a user's
    # own ``ws-git`` command (the ws-* prefix reservation was decided
    # but never enforced — RESERVED_COMMANDS holds only python/python3).
    fn._nontainer_wsgit = True
    ws.register_command("ws-git", fn)


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

    def __init__(self, ws: Any):
        self._ws = ws
        self._command = make_wsgit_command(ws)

    def run(self, cwd: str, *argv: str) -> dict:
        ws = self._ws
        with ws._lock:
            cmd = ws._commands.get("ws-git")
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
            # Sync-on-verb: the guest tree may hold writes this script
            # made before invoking ws-git, and the provider doesn't see
            # them until harvest (which runs after exec_shell returns).
            # Absorb first so every verb dispatches against fresh state
            # — ``printf x > new.txt; ws-git stage new.txt`` stages
            # instead of rejecting an unknown path. Reads (status/diff)
            # need it just as much as mutations. No push-back is needed:
            # no verb rewrites worktree files (reset is mixed-only), so
            # the guest stays in agreement and the post-call harvest
            # picks up whatever the script writes afterwards. A nested
            # harvest is safe here: the guest shell is blocked on this
            # hostcall (its tree is quiescent), and both locks are
            # re-entrant on this thread.
            try:
                torn = ws._absorb_or_unwind(ws._provider.dirty)
            except Exception as e:  # noqa: BLE001 — honest triple, below
                # Absorption failed AFTER the guest baseline advanced
                # (rebase rides the harvest): the guest holds content
                # the provider refuses, staging may hold partial writes,
                # and the post-call harvest will read empty. So the
                # triple alone would let terminal() checkpoint the
                # partials as a routine success: rematerialize the guest
                # next call, and stash the failure for terminal(), which
                # unwinds the partial staging like a torn call instead.
                ws._mark_executor_stale()
                ws._pending_sync_error = f"mid-call sync failed: {e}"
                return {
                    "stdout": "",
                    "stderr": f"ws-git: mid-call sync failed: {e}",
                    "exit_code": 1,
                }
            if torn is not None:
                return {"stdout": "", "stderr": f"ws-git: {torn}", "exit_code": 1}
            mapper = getattr(getattr(ws, "_executor", None), "_guest_to_host", None)
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
                "use the kvgit backend for staged mode.",
            )
        try:
            if verb == "status":
                return _status(provider, ctx, rest)
            if verb == "stage":
                return _stage(provider, ctx, rest)
            if verb == "unstage":
                return _unstage(provider, ctx, rest)
            if verb == "commit":
                return _commit(provider, ctx, rest)
            if verb == "reset":
                return _reset(provider, ctx, rest)
            if verb == "diff":
                return _diff(provider, ctx, rest)
            if verb == "log":
                return _log(provider, ctx, rest)
        except (ValueError, WorkspaceError, NotSupportedError) as e:
            # Domain errors render as messages, never "Unexpected error".
            return CommandResult(exit_code=1, stderr=f"{e}")
        raise AssertionError(f"unreachable verb {verb!r}")

    wsgit.__doc__ = (
        "Git-shaped staging over workspace branches: "
        "ws-git (stage|unstage|commit|reset|status|diff|log) [...]"
    )
    return wsgit


def _merge_hash(provider: Any, source: str) -> str | None:
    """Short hash of the newest merge commit from ``source``, if any."""
    for entry in provider.history():
        if (
            entry.info.get("tool") == "ws-git.merge"
            and entry.info.get("source") == source
        ):
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


def _status(provider: Any, ctx: Any, rest: list[str]) -> Any:
    for flag in rest:
        if flag != "--porcelain":
            return _usage_error(f"status takes no {flag!r} (porcelain is the default).")
    st = provider.status()
    lines: list[str] = []
    if st.merge_source is not None:
        short = _merge_hash(provider, st.merge_source)
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


def _stage(provider: Any, ctx: Any, rest: list[str]) -> Any:
    if not rest or any(a.startswith("-") for a in rest):
        return _usage_error("stage needs at least one path.")
    out = provider.stage([_abspath(ctx, a) for a in rest])
    if out.suspended:
        ctx.stdout.write(
            "suspended autocheckpoint (resume: ws-git commit, ws-git reset)\n"
        )
    return None


def _unstage(provider: Any, ctx: Any, rest: list[str]) -> Any:
    if not rest or any(a.startswith("-") for a in rest):
        return _usage_error("unstage needs at least one path.")
    was = provider.stage_suspended()
    provider.unstage([_abspath(ctx, a) for a in rest])
    if was and not provider.stage_suspended():
        ctx.stdout.write("resumed autocheckpoint\n")
    return None


def _commit(provider: Any, ctx: Any, rest: list[str]) -> Any:
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
    before = provider.status()
    info = {"message": message} if message is not None else None
    head = provider.commit(info)
    n = len(before.staged)
    subject = message if message is not None else "ws-git.commit"
    ctx.stdout.write(
        f"[{before.branch} {head[:7]}] {subject} ({n} file{'s' if n != 1 else ''})\n"
    )
    return None


def _reset(provider: Any, ctx: Any, rest: list[str]) -> Any:
    if rest:
        return _usage_error("reset is mixed-only (no --soft/--hard).")
    was = provider.stage_suspended()
    provider.discard_staged()
    if was and not provider.stage_suspended():
        ctx.stdout.write("resumed autocheckpoint\n")
    return None


def _content_maps(provider: Any) -> tuple[dict[str, str], dict[str, str], Any]:
    """(live display→key, HEAD display→key, HEAD handle) for diffing.

    Reads kvgit internals behind the ``caps.index`` gate above — today
    only kvgit sets it, and the key↔path codec is the same one status
    already relies on.
    """
    staged = provider._staged
    live = {d: k for k, d in provider._file_keys(staged.keys()).items()}
    head_handle = staged.checkout(staged.current_commit)
    head = (
        {d: k for k, d in provider._file_keys(head_handle.keys()).items()}
        if head_handle is not None
        else {}
    )
    return live, head, head_handle


def _decode(value: Any) -> bytes | None:
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode()
    return None


def _diff(provider: Any, ctx: Any, rest: list[str]) -> Any:
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
    st = provider.status()
    if check:
        return _diff_check(provider, ctx, st, paths, cached)
    want = set(st.staged) if cached else set(st.unstaged)
    if paths:
        want &= set(paths)
    if not want:
        return None
    live, head, head_handle = _content_maps(provider)
    out: list[str] = []
    for path in sorted(want):
        show = _show(path)
        old = _decode(head_handle.get(head[path])) if path in head else b""
        new = _decode(provider._staged.get(live[path])) if path in live else b""
        if old is None or new is None or b"\x00" in (old or b"") + (new or b""):
            out.append(f"Binary files a/{show} and b/{show} differ")
            continue
        if old == new:
            continue
        out.append(f"diff --git a/{show} b/{show}")
        out.extend(
            _unified_with_newline_markers(
                old.decode("utf-8", errors="replace"),
                new.decode("utf-8", errors="replace"),
                show,
            )
        )
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
    provider: Any, ctx: Any, st: Any, paths: list[str], cached: bool
) -> Any:
    from termish import CommandResult

    live, _, _ = _content_maps(provider)
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
        value = _decode(provider._staged.get(live[path]))
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


def _log(provider: Any, ctx: Any, rest: list[str]) -> Any:
    limit: int | None = None
    args = list(rest)
    while args:
        flag = args.pop(0)
        if flag in ("-n", "--max-count") and args and args[0].isdigit():
            limit = int(args.pop(0))
        else:
            return _usage_error(f"log takes no {flag!r} (try: -n N).")
    lines: list[str] = []
    for entry in provider.history():
        if limit is not None and len(lines) >= limit:
            break
        tool = entry.info.get("tool", "?")
        subject = entry.info.get("message") or tool
        line = f"{entry.id[:7]} {subject}"
        if tool == "ws-git.merge" and entry.info.get("source"):
            line += f" from {entry.info['source']}"
            if entry.info.get("sizes"):
                line += " (sizes)"
        lines.append(line)
    if lines:
        ctx.stdout.write("\n".join(lines) + "\n")
    return None
