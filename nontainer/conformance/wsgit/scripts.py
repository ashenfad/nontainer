"""The scripts of the ``ws-git`` corpus: one per behaviour, each a short
session an agent could have typed. See the package docstring for the
step types and for ``<cN>``."""

from __future__ import annotations

from ...traces import TraceEvent, TraceRun, TraceTokens
from . import Converse, Delete, Open, Run, Script, Tag, Write

_HEREDOC_DOC = "cat > doc.txt <<'EOF'\na\nb\nc\nEOF"


def _conflict(main: str = "main", worker: str = "worker") -> tuple:
    """A shared doc.txt both sides then change on the same line."""
    return (
        Run(_HEREDOC_DOC, main),
        Run("ws-git commit -m base", main),
        Run(f"ws-git branch {worker}", main),
        Run("cat > doc.txt <<'EOF'\na\nWORKER\nc\nEOF", worker),
        Run("ws-git commit -m 'worker edit'", worker),
        Run("cat > doc.txt <<'EOF'\na\nMAIN\nc\nEOF", main),
        Run("ws-git commit -m 'main edit'", main),
    )


_SYSTEM = "You are an analyst. Work in /workspace.\n"

#: Two runs of an analyst's conversation, for the trace scripts.
_SUMMED = TraceRun(
    id="1f3a9c2e-7d41-4b8e-9a62-0c5e8f1d2b37",
    status="completed",
    started=1760529600.0,
    ended=1760529612.0,
    model="claude-sonnet-5-5",
    tokens=TraceTokens(input=1200, output=300, cache_read=5000, cache_write=0),
    system=_SYSTEM,
    events=(
        TraceEvent("user", "Total the sales in sales.csv."),
        TraceEvent(
            "tool",
            "10\n",
            tool="terminal",
            args={"command": "awk -F, '{s+=$2} END {print s}' sales.csv"},
        ),
        TraceEvent("assistant", "The total is 10."),
    ),
)
_BY_REGION = TraceRun(
    id="77b0e2d1-0a3c-4f2e-8d1b-5e6f7a8b9c0d",
    status="failed",
    started=1760529900.0,
    ended=1760529960.0,
    model="claude-sonnet-5-5",
    tokens=TraceTokens(input=900, output=120, cache_read=4800, cache_write=0),
    system=_SYSTEM,
    events=(
        TraceEvent("user", "Now split it by region."),
        TraceEvent(
            "tool",
            "Traceback (most recent call last):\n  ...\nKeyError: 'region'\n",
            tool="run_python",
            args={"code": "df.groupby('region')\n"},
            error=True,
        ),
    ),
)

SCRIPTS: tuple[Script, ...] = (
    # -- status, staging, committing --------------------------------------
    Script(
        "a-clean-status-is-silent",
        "Nothing modified prints nothing, as in git.",
        (
            Run("ws-git status"),
            Run("ws-git status --porcelain"),
            Run("ws-git status -s"),
        ),
    ),
    Script(
        "stage-then-commit",
        "Staging is silent; status shows the staged and the modified in XY "
        "columns; a commit takes the staged set and says so.",
        (
            Run("echo one > a.txt"),
            Run("echo two > b.txt"),
            Run("ws-git status"),
            Run("ws-git stage a.txt"),
            Run("ws-git status"),
            Run("ws-git commit -m 'add a'"),
            Run("ws-git status"),
            Run("ws-git log"),
            Run("ws-git commit -m 'the rest'"),
            Run("ws-git log --oneline"),
            Run("ws-git status"),
        ),
    ),
    Script(
        "status-letters",
        "A is new, D deleted, M changed, in the column that says staged or not.",
        (
            Run("echo one > a.txt; echo two > b.txt; echo three > c.txt"),
            Run("ws-git commit -m seed"),
            Run("echo changed > a.txt; rm b.txt; echo new > d.txt"),
            Run("ws-git status"),
            Run("ws-git stage b.txt d.txt"),
            Run("ws-git status --short"),
        ),
    ),
    Script(
        "a-partial-commit-leaves-the-rest-in-the-tree",
        "What the commit left out is still modified, and the commit holds the "
        "file as it was at the last commit.",
        (
            Run("echo A1 > a.py; echo B1 > b.py"),
            Run("ws-git commit -m base"),
            Run("echo A2 > a.py; echo B2 > b.py"),
            Run("ws-git add a.py"),
            Run("ws-git commit -m 'just a'"),
            Run("ws-git status"),
            Run("ws-git show HEAD:b.py"),
            Run("cat b.py"),
            Run("ws-git log"),
        ),
    ),
    Script(
        "commit-refusals-and-a-message-less-commit",
        "Nothing to commit says so; -a and a pathspec are usage errors; a "
        "commit without -m is in the log under the verb's name.",
        (
            Run("ws-git commit"),
            Run("echo one > a.txt"),
            Run("ws-git commit -a"),
            Run("ws-git commit a.txt"),
            Run("ws-git commit"),
            Run("ws-git log"),
            Run("echo two > b.txt"),
            Run("ws-git stage a.txt"),
            Run("ws-git commit"),
        ),
    ),
    Script(
        "stage-unstage-and-reset",
        "stage needs a path it knows, and a directory with nothing modified "
        "under it stages nothing; unstage and reset leave the tree alone.",
        (
            Run("ws-git stage"),
            Run("ws-git stage nope.txt"),
            Run("mkdir sub"),
            Run("ws-git stage sub"),
            Run("echo one > a.txt; echo two > b.txt"),
            Run("ws-git stage a.txt b.txt"),
            Run("ws-git unstage a.txt"),
            Run("ws-git status"),
            Run("ws-git reset"),
            Run("ws-git status"),
            Run("ws-git reset --hard"),
            Run("cat a.txt"),
        ),
    ),
    Script(
        "paths-resolve-against-the-cwd",
        "A relative path is read from where the shell stands, and printed "
        "relative to the root.",
        (
            Run(
                "mkdir -p pkg/sub && cd pkg/sub && echo x > f.txt && ws-git stage f.txt"
            ),
            Run("ws-git status"),
            Run("cd /workspace/pkg && ws-git unstage sub/f.txt && ws-git status"),
        ),
    ),
    Script(
        "add-takes-directories-and-all",
        "add stages what is modified under a directory, -A everything, and "
        "refuses what it doesn't mean; a removed directory stages its removal.",
        (
            Run(
                "mkdir -p src docs && echo a > src/a.py && echo b > src/b.py && echo d > docs/d.md"
            ),
            Run("ws-git add src"),
            Run("ws-git status"),
            Run("ws-git reset"),
            Run("ws-git add -A"),
            Run("ws-git status"),
            Run("ws-git commit -m all"),
            Run("ws-git add"),
            Run("ws-git add --patch"),
            Run("echo a2 > src/a.py && echo d2 > docs/d.md"),
            Run("ws-git add -A src/a.py"),
            Run("ws-git status"),
            Run("ws-git reset"),
            Run("rm -r docs"),
            Run("ws-git add docs"),
            Run("ws-git status"),
        ),
    ),
    Script(
        "stage-takes-what-add-takes",
        "stage and add are one verb: -A stages everything modified, a "
        "directory (or .) what is modified under it, and an option neither "
        "takes is one line naming the verb's synopsis. A path above the "
        "root is refused, never read as everything under it.",
        (
            Run("mkdir -p src && echo a > src/a.py && echo b > b.txt"),
            Run("ws-git stage -A"),
            Run("ws-git status"),
            Run("ws-git reset"),
            Run("ws-git stage /"),
            Run("ws-git add /"),
            Run("ws-git status"),
            Run("cd src && ws-git stage . && cd /workspace"),
            Run("ws-git status"),
            Run("ws-git stage -x"),
            Run("ws-git add -u"),
            Run("ws-git stage"),
        ),
    ),
    # -- diff ----------------------------------------------------------------
    Script(
        "diff-unstaged-cached-and-stat",
        "A unified diff against the last commit; --cached the staged set; "
        "--stat a summary.",
        (
            Run("cat > a.txt <<'EOF'\none\ntwo\nthree\nEOF"),
            Run("echo keep > b.txt"),
            Run("ws-git commit -m base"),
            Run("cat > a.txt <<'EOF'\none\nTWO\nthree\nfour\nEOF"),
            Run("echo new > c.txt"),
            Run("ws-git diff"),
            Run("ws-git diff --stat"),
            Run("ws-git stage a.txt"),
            Run("ws-git diff --cached"),
            Run("ws-git diff"),
            Run("ws-git diff -- a.txt"),
            Run("ws-git diff nope.txt"),
            Run("ws-git diff --word-diff"),
        ),
    ),
    Script(
        "diff-of-the-final-newline",
        "A change to the final newline shows git's marker, and counts in --stat.",
        (
            Write("/workspace/a.txt", text="one\ntwo\n"),
            Run("ws-git commit -m base"),
            Write("/workspace/a.txt", text="one\ntwo"),
            Run("ws-git diff"),
            Run("ws-git diff --stat"),
        ),
    ),
    Script(
        "diff-of-a-binary-file",
        "Bytes that aren't text are named, not shown.",
        (
            Write("/workspace/blob.bin", data="AAECAwQ="),
            Run("ws-git commit -m blob"),
            Write("/workspace/blob.bin", data="AAECAwUG"),
            Run("ws-git diff"),
            Run("ws-git diff --stat"),
            Run("ws-git show HEAD:blob.bin"),
        ),
    ),
    # -- history -------------------------------------------------------------
    Script(
        "log-shows-the-agents-commits",
        "log lists only the agent's commits; --all every commit; -n and -N "
        "limit; an empty log says where the history is.",
        (
            Run("ws-git log"),
            Run("echo one > a.txt"),
            Run("ws-git log"),
            Run("ws-git commit -m first"),
            Run("echo two > a.txt"),
            Run("ws-git commit -m second"),
            Run("ws-git log"),
            Run("ws-git log -n 1"),
            Run("ws-git log -1"),
            Run("ws-git log -n 0"),
            Run("ws-git log --all"),
            Run("ws-git log --graph"),
        ),
    ),
    Script(
        "log-pickaxe",
        "-S finds the commits where a string appeared (+) or vanished (-), "
        "not where it only moved.",
        (
            Run("echo 'needle here' > a.txt"),
            Run("ws-git commit -m 'add needle'"),
            Run("echo 'nothing' > a.txt; echo 'needle here' > b.txt"),
            Run("ws-git commit -m 'move needle'"),
            Run("echo 'gone' > b.txt"),
            Run("ws-git commit -m 'drop needle'"),
            Run("ws-git log -S needle"),
            Run("ws-git log -S nowhere"),
            Run("ws-git log -S"),
        ),
    ),
    Script(
        "show-a-commit",
        "show prints a commit's message and its change, by the short id the "
        "log printed or HEAD.",
        (
            Run("echo one > a.txt"),
            Run("ws-git commit -m first"),
            Run("echo two > a.txt; echo b > b.txt"),
            Run("ws-git commit -m second"),
            Run("ws-git log"),
            Run("ws-git show <c1>"),
            Run("ws-git show HEAD"),
            Run("ws-git show"),
            Run("ws-git show main"),
        ),
    ),
    Script(
        "show-a-path-at-a-state",
        "show <ref>:<path> prints one file, lists a directory, and refuses "
        "what the state doesn't hold.",
        (
            Run(
                "mkdir -p pkg && echo one > a.txt && echo m > pkg/mod.py && echo n > pkg/new.py"
            ),
            Run("ws-git commit -m seed"),
            Run("echo two > a.txt"),
            Run("ws-git show HEAD:a.txt"),
            Run("ws-git show HEAD:/a.txt"),
            Run("ws-git show HEAD:/workspace/a.txt"),
            Run("cd pkg && ws-git show HEAD:./mod.py"),
            Run("ws-git show HEAD:pkg"),
            Run("ws-git show HEAD:missing.txt"),
            Run("ws-git show :a.txt"),
        ),
    ),
    # -- checkout ------------------------------------------------------------
    Script(
        "checkout-restores-a-commit",
        "checkout puts the tree back as a new commit; a framework commit and "
        "a session name are refused.",
        (
            Run("echo one > a.txt"),
            Run("ws-git commit -m first"),
            Run("echo two > a.txt; echo b > b.txt"),
            Run("ws-git commit -m second"),
            Run("ws-git log"),
            Run("ws-git checkout <c1>"),
            Run("cat a.txt; ls"),
            Run("ws-git status"),
            Run("ws-git log"),
            Run("echo framework > f.txt"),
            Run("ws-git log --all -n 1"),
            Run("ws-git checkout <c3>"),
            Run("ws-git checkout worker"),
            Run("ws-git checkout"),
        ),
    ),
    Script(
        "checkout-takes-paths",
        "checkout <ref> -- <paths> makes those paths match a ref of yours or "
        "another session's, mirroring a directory.",
        (
            Run(
                "mkdir pkg && echo keep > pkg/keep.py && echo doomed > pkg/old.py && echo mine > a.txt"
            ),
            Run("ws-git commit -m seed"),
            Run("echo changed > a.txt"),
            Run("ws-git checkout -- a.txt"),
            Run("cat a.txt"),
            Run("ws-git branch worker"),
            Run(
                "rm pkg/old.py && echo new > pkg/new.py && echo theirs > a.txt",
                "worker",
            ),
            Run("ws-git commit -m work", "worker"),
            Run("ws-git checkout worker -- pkg"),
            Run("ls pkg; cat a.txt"),
            Run("ws-git checkout worker -- a.txt"),
            Run("cat a.txt"),
            Run("ws-git checkout HEAD -- ghost.txt"),
            Run("ws-git checkout --"),
            Run("ws-git checkout a b -- c"),
        ),
    ),
    # -- tags ----------------------------------------------------------------
    Script(
        "tags-name-the-agents-commits",
        "tag bookmarks a commit, lists, moves with -f, drops with -d, and is a "
        "ref the other verbs read; the log shows it.",
        (
            Run("ws-git tag"),
            Run("echo one > a.txt"),
            Run("ws-git commit -m first"),
            Run("ws-git tag v1"),
            Run("echo two > a.txt"),
            Run("ws-git commit -m second"),
            Run("ws-git tag v1"),
            Run("ws-git tag -f v1"),
            Run("ws-git tag old <c1>"),
            Run("ws-git tag"),
            Run("ws-git log"),
            Run("ws-git show old:a.txt"),
            Run("ws-git checkout old -- a.txt"),
            Run("cat a.txt"),
            Run("ws-git tag abcdef0"),
            Run("ws-git tag 'a..b'"),
            Run("ws-git tag -d old"),
            Run("ws-git tag -d old"),
            Run("ws-git tag -x"),
        ),
    ),
    Script(
        "tags-stay-with-their-session",
        "A fork starts with no tags, and a merge brings none.",
        (
            Run("echo one > a.txt"),
            Run("ws-git commit -m first"),
            Run("ws-git tag mine"),
            Run("ws-git branch worker"),
            Run("ws-git tag", "worker"),
            Run("echo w > w.txt", "worker"),
            Run("ws-git commit -m work", "worker"),
            Run("ws-git tag theirs", "worker"),
            Run("ws-git merge worker"),
            Run("ws-git tag"),
            Run("ws-git log worker"),
        ),
    ),
    # -- branches, merges ----------------------------------------------------
    Script(
        "branch-lists-and-forks",
        "branch lists the sessions with yours marked; branch <name> forks one "
        "without switching; --at forks from a commit or a tag.",
        (
            Run("ws-git branch"),
            Run("echo one > a.txt"),
            Run("ws-git commit -m first"),
            Run("ws-git tag v1"),
            Run("echo two > a.txt"),
            Run("ws-git commit -m second"),
            Run("ws-git branch worker"),
            Run("ws-git branch old --at v1"),
            Run("ws-git branch older --at <c1>"),
            Run("ws-git branch"),
            Run("cat a.txt", "old"),
            Run("ws-git branch", "old"),
            Run("ws-git branch worker"),
            Run("ws-git branch --fresh"),
            Run("ws-git branch x --sideways"),
        ),
    ),
    Script(
        "a-clean-merge",
        "diff <session> shows what a merge would bring; the merge names what "
        "it took and lands; the log shows where it came from.",
        (
            Run("echo base > a.txt"),
            Run("ws-git commit -m base"),
            Run("ws-git branch worker"),
            Run("echo theirs > b.txt", "worker"),
            Run("ws-git commit -m 'worker work'", "worker"),
            Run("echo mine > c.txt"),
            Run("ws-git commit -m 'my work'"),
            Run("ws-git diff worker"),
            Run("ws-git diff worker --stat"),
            Run("ws-git merge worker"),
            Run("ws-git log"),
            Run("ws-git status"),
            Run("ws-git merge worker"),
            Run("ws-git merge"),
            Run("ws-git merge nobody"),
        ),
    ),
    Script(
        "a-conflicted-merge",
        "A merge that conflicts lands with markers, shows as UU with a "
        "## merging line, is found by diff --check, and ends when they go.",
        (
            *_conflict(),
            Run("ws-git diff worker"),
            Run("ws-git merge worker"),
            Run("ws-git status"),
            Run("cat doc.txt"),
            Run("ws-git diff --check"),
            Run("ws-git checkout <c2>"),
            Run("echo other > x.txt && ws-git commit -m nope"),
            Run("cat > doc.txt <<'EOF'\na\nBOTH\nc\nEOF"),
            Run("ws-git diff --check"),
            Run("ws-git commit -m resolved"),
            Run("ws-git status"),
            Run("ws-git log"),
        ),
    ),
    Script(
        "a-merge-is-aborted",
        "merge --abort goes back to where the merge landed; with nothing "
        "outstanding it says so.",
        (
            *_conflict(),
            Run("ws-git merge --abort"),
            Run("ws-git merge worker"),
            Run("ws-git merge --abort"),
            Run("cat doc.txt"),
            Run("ws-git status"),
            Run("ws-git merge --abort"),
        ),
    ),
    Script(
        "merges-take-only-committed-work",
        "A merge is refused over the agent's uncommitted work and over a "
        "source with work past its last commit, in the agent's terms.",
        (
            Run("echo base > a.txt"),
            Run("ws-git commit -m base"),
            Run("ws-git branch worker"),
            Run("echo done > done.txt", "worker"),
            Run("ws-git commit -m done", "worker"),
            Run("echo wip > wip.txt", "worker"),
            Run("echo mine > mine.txt"),
            Run("ws-git merge worker"),
            Run("ws-git commit -m mine"),
            Run("ws-git merge worker"),
            Run("ws-git commit -m wip", "worker"),
            Run("cd /workspace && ws-git merge worker"),
        ),
    ),
    Script(
        "a-file-about-markers-is-not-a-conflict",
        "Marker text a side already held is neither a conflict nor a hiding "
        "place for a new one.",
        (
            Run(
                "cat > notes.md <<'EOF'\nresolve it:\n<<<<<<< HEAD\nours\n=======\n\nline\nend\nEOF"
            ),
            Run("ws-git commit -m notes"),
            Run("ws-git branch worker"),
            Run("echo w > w.txt", "worker"),
            Run("ws-git commit -m 'worker file'", "worker"),
            Run("ws-git merge worker"),
            Run("ws-git status"),
            Run("ws-git branch other"),
            Run(
                "cat > notes.md <<'EOF'\nresolve it:\n<<<<<<< HEAD\nours\n=======\n\nOTHER\nend\nEOF",
                "other",
            ),
            Run("ws-git commit -m 'other edit'", "other"),
            Run(
                "cat > notes.md <<'EOF'\nresolve it:\n<<<<<<< HEAD\nours\n=======\n\nMAIN\nend\nEOF"
            ),
            Run("ws-git commit -m 'main edit'"),
            Run("ws-git merge other"),
            Run("ws-git status"),
        ),
    ),
    # -- revert, cherry-pick --------------------------------------------------
    Script(
        "revert",
        "revert undoes one commit's change as a new commit; again, it has "
        "nothing to do; overlapping work lands with markers.",
        (
            Run("cat > a.txt <<'EOF'\none\ntwo\nthree\nEOF"),
            Run("ws-git commit -m first"),
            Run("cat > a.txt <<'EOF'\none\nTWO\nthree\nEOF"),
            Run("ws-git commit -m second"),
            Run("echo b > b.txt"),
            Run("ws-git commit -m third"),
            Run("ws-git log"),
            Run("ws-git revert <c3>"),
            Run("ls"),
            Run("ws-git revert <c3>"),
            Run("cat > a.txt <<'EOF'\none\nLATER\nthree\nEOF"),
            Run("ws-git commit -m later"),
            Run("ws-git revert <c2>"),
            Run("ws-git status"),
            Run("ws-git log"),
            Run("ws-git revert"),
            Run("ws-git revert worker"),
        ),
    ),
    Script(
        "cherry-pick",
        "cherry-pick brings one commit of another session's, measured in its "
        "graph; it names the commit and refuses a session alone.",
        (
            Run("echo base > base.txt"),
            Run("ws-git commit -m seed"),
            Run("ws-git branch worker"),
            Run("echo one > one.txt", "worker"),
            Run("ws-git commit -m one", "worker"),
            Run("echo two > two.txt", "worker"),
            Run("ws-git commit -m two", "worker"),
            Run("ws-git log worker"),
            Run("ws-git cherry-pick worker@<c3>"),
            Run("ls"),
            Run("ws-git log"),
            Run("ws-git cherry-pick worker"),
            Run("ws-git cherry-pick worker@<c1>"),
            Run("ws-git cherry-pick nobody@<c3>"),
            Run("ws-git cherry-pick worker@<c3>"),
        ),
    ),
    # -- stash ---------------------------------------------------------------
    Script(
        "stash",
        "stash puts the work aside on a branch of its own and restores the "
        "tree; list, show and pop bring it back; drop deletes it.",
        (
            Run("ws-git stash"),
            Run("echo one > a.txt"),
            Run("ws-git stash"),
            Run("ws-git commit -m first"),
            Run("ws-git stash"),
            Run("echo two > a.txt; echo b > b.txt"),
            Run("ws-git stash push -m 'half done'"),
            Run("cat a.txt; ls"),
            Run("echo three > c.txt"),
            Run("ws-git stash"),
            Run("ws-git stash list"),
            Run("ws-git branch"),
            Run("ws-git stash show stash@{1}"),
            Run("ws-git stash pop stash@{1}"),
            Run("cat a.txt"),
            Run("ws-git stash list"),
            Run("ws-git stash drop"),
            Run("ws-git stash list"),
            Run("ws-git stash pop stash@{4}"),
            Run("ws-git stash pop nope"),
            Run("ws-git stash apply"),
        ),
    ),
    Script(
        "a-stash-pop-that-conflicts-keeps-the-stash",
        "A pop is a merge: a conflicted one lands with markers and keeps the "
        "stash, and a stash is refused over an outstanding merge.",
        (
            Run("echo base > a.txt"),
            Run("ws-git commit -m base"),
            Run("echo stashed > a.txt"),
            Run("ws-git stash"),
            Run("echo committed > a.txt"),
            Run("ws-git commit -m committed"),
            Run("ws-git stash pop"),
            Run("ws-git status"),
            Run("ws-git stash list"),
            Run("echo more > b.txt"),
            Run("ws-git stash"),
        ),
    ),
    # -- worktrees -----------------------------------------------------------
    Script(
        "worktrees",
        "worktree add checks another session out under a directory, "
        "read-only; list, status and remove know about it.",
        (
            Run("ws-git worktree"),
            Run("echo base > a.txt"),
            Run("ws-git commit -m base"),
            Run("ws-git branch reviewer"),
            Run("echo 'looks fine' > review.md", "reviewer"),
            Run("ws-git commit -m review", "reviewer"),
            Run("ws-git worktree add rev reviewer"),
            Run("ws-git worktree list"),
            Run("cat rev/review.md; ls rev"),
            Run("ws-git status"),
            Run("echo mine > a.txt && ws-git status"),
            Run("ws-git worktree add rev reviewer"),
            Run("ws-git worktree add rev/inner reviewer"),
            Run("ws-git worktree add a.txt reviewer"),
            Run("ws-git worktree add self main"),
            Run("ws-git worktree add old reviewer@<c1>"),
            Run("ws-git worktree list"),
            Run("ws-git worktree remove rev"),
            Run("ws-git worktree remove rev"),
            Run("ws-git worktree list"),
            Run("ws-git worktree add /elsewhere reviewer"),
            Run("mkdir empty && ws-git worktree add empty reviewer"),
            Run("cat empty/review.md"),
            Run("ws-git status"),
            Run("ws-git worktree prune"),
        ),
    ),
    Script(
        "a-worktree-with-its-trace",
        "worktree add --trace mounts the conversation stored at the commit "
        "beside the files, at .trace, rendered and read-only; list says how "
        "many runs it holds, and remove takes both down.",
        (
            Run("echo 'q1,4' > sales.csv; echo 'q2,6' >> sales.csv", "analyst"),
            Converse(_SUMMED, "analyst"),
            Run("echo 'region?' > notes.md", "analyst"),
            Converse(_BY_REGION, "analyst"),
            Run("ws-git log analyst --all"),
            Run("ws-git worktree add --trace an analyst"),
            Run("ls -a an; ls an/.trace"),
            Run("find an/.trace -type f"),
            Run("cat an/.trace/README.md"),
            Run("cat an/.trace/index.md"),
            Run("cat an/.trace/runs/002-77b0e2d1/run.md"),
            Run("cat an/.trace/runs/002-77b0e2d1/events/002-tool-run_python.md"),
            Run("grep -rl KeyError an/.trace"),
            Run("echo x > an/.trace/x"),
            Run("ws-git worktree add --trace first analyst@<c3>"),
            Run("cat first/.trace/index.md; ls first"),
            Run("ws-git worktree list"),
            Run("ws-git worktree remove an"),
            Run("ls"),
            Run("ws-git worktree list"),
        ),
    ),
    Script(
        "what-each-run-changed",
        "Each run's changed.md lists the commits from after the run before it "
        "landed through its own landing that changed a file, the host's "
        "among them, and nothing from before the session's fork; --since "
        "marks the runs new since a state, and show reads one file of "
        "another session's trace with nothing mounted.",
        (
            Write("seed.csv", text="q0,1\n", session="analyst"),
            Run("echo 'q1,4' > sales.csv; mkdir out", "analyst"),
            Converse(_SUMMED, "analyst"),
            Run("rm seed.csv; echo 'q2,6' >> sales.csv", "analyst"),
            Run("echo 'region?' > out/notes.md", "analyst"),
            Converse(_BY_REGION, "analyst"),
            Run("ws-git log analyst --all"),
            Run("ws-git worktree add --trace an analyst"),
            Run("cat an/.trace/index.md"),
            Run("cat an/.trace/runs/001-1f3a9c2e/changed.md"),
            Run("cat an/.trace/runs/002-77b0e2d1/changed.md"),
            Run("grep -l notes.md an/.trace/runs/*/changed.md"),
            Run("ws-git worktree add --trace --since analyst@<c4> since analyst"),
            Run("cat since/.trace/index.md"),
            Run("ws-git worktree add --since <c4> --trace bare analyst"),
            Run("ws-git worktree list"),
            Run("ws-git show analyst@<c1>:.trace/runs/002-77b0e2d1/changed.md"),
            Run("ws-git show analyst@<c1>:.trace"),
            Run("ws-git show analyst@<c1>:.trace/runs"),
            Run("ws-git show analyst@<c1>:.trace/runs/009-nope/run.md"),
            Run("ws-git show analyst@<c1>:.trace", "analyst"),
            Run("ws-git worktree add --since <c4> x analyst"),
            Run("ws-git worktree add --trace x analyst --since"),
            Run("ws-git branch kid --fresh", "analyst"),
            Run("echo k > kid.txt", "kid"),
            Converse(_SUMMED, "kid"),
            Run("ws-git worktree add --trace k kid"),
            Run("cat k/.trace/runs/001-1f3a9c2e/changed.md"),
        ),
    ),
    Script(
        "a-trace-is-refused-where-there-is-none",
        "A state with no conversation has no trace, and neither has one "
        "written by a harness the store can't read; a trace hides a .trace "
        "the tree holds of its own, and says so.",
        (
            Run("echo a > a.txt", "plain"),
            Run("ws-git commit -m a", "plain"),
            Run("ws-git worktree add --trace p plain"),
            Run("ls"),
            Run("echo b > b.txt", "other"),
            Converse(_SUMMED, "other", harness="elsewhere"),
            Run("ws-git worktree add --trace o other"),
            Run("ws-git worktree add o other"),
            Run("ls -a o"),
            Run("mkdir .trace && echo mine > .trace/notes.md", "keeper"),
            Converse(_SUMMED, "keeper"),
            Run("ws-git worktree add --trace k keeper"),
            Run("ls k/.trace"),
            Run("ws-git worktree add --trace"),
        ),
    ),
    # -- views ---------------------------------------------------------------
    Script(
        "a-narrowed-branch",
        "branch --paths narrows what the fork sees; it says so in status and "
        "sparse-checkout, and a diff of it groups its seed from the rest.",
        (
            Run("mkdir src && echo a > src/a.py && echo b > b.py"),
            Run("ws-git commit -m seed"),
            Run("ws-git sparse-checkout"),
            Run("ws-git branch child --paths src"),
            Run("ls", "child"),
            Run("ws-git sparse-checkout list", "child"),
            Run("echo A > src/a.py && echo note > note.md", "child"),
            Run("ws-git status", "child"),
            Run("ws-git commit -m work", "child"),
            Run("ws-git diff child"),
            Run("ws-git sparse-checkout set src", "child"),
            Run("ws-git branch kid --paths /"),
        ),
    ),
    # -- store tags, other sessions ------------------------------------------
    Script(
        "store-tags-are-refs-for-the-verbs-that-read",
        "A store tag reads frozen: diff, log, show, checkout -- and worktree "
        "add take it, the verbs that write don't, and it outlives its session.",
        (
            Run("echo one > a.txt"),
            Run("ws-git commit -m first"),
            Run("ws-git branch pub"),
            Run("echo published > a.txt", "pub"),
            Run("ws-git commit -m publish", "pub"),
            Run("echo after > b.txt", "pub"),
            Tag("release", "pub"),
            Delete("pub"),
            Run("ws-git log release"),
            Run("ws-git show release"),
            Run("ws-git diff release"),
            Run("ws-git show release:a.txt"),
            Run("ws-git checkout release -- a.txt"),
            Run("cat a.txt"),
            Run("ws-git worktree add pub release"),
            Run("cat pub/b.txt"),
            Run("ws-git commit -m 'taken from the release'"),
            Run("ws-git merge release"),
            Run("ws-git show nothing"),
            Run("ws-git log nothing"),
        ),
    ),
    Script(
        "another-sessions-history",
        "log <session> lists that session's commits; show <session>@<commit> "
        "shows one; a bare session or one of its framework commits is refused.",
        (
            Run("echo base > a.txt"),
            Run("ws-git commit -m base"),
            Run("ws-git branch worker"),
            Run("echo w > w.txt", "worker"),
            Run("ws-git commit -m 'worker work'", "worker"),
            Run("echo framework > f.txt", "worker"),
            Run("ws-git log worker"),
            Run("ws-git log worker --all -n 1"),
            Run("ws-git show worker@<c2>"),
            Run("ws-git show worker"),
            Run("ws-git show worker@<c3>"),
            Run("ws-git show worker@0000000"),
            Run("ws-git show nobody@<c2>"),
            Run("ws-git show worker:w.txt"),
            Run("ws-git show worker@<c2>:w.txt"),
            Run("ws-git log nobody"),
        ),
    ),
    Script(
        "a-session-with-no-shared-history",
        "A diff of a session opened on its own reads as all it holds.",
        (
            Run("echo mine > a.txt"),
            Run("ws-git commit -m mine"),
            Run("echo theirs > b.txt", "stranger"),
            Run("ws-git commit -m theirs", "stranger"),
            Run("ws-git diff stranger"),
            Run("ws-git merge stranger"),
        ),
    ),
    Script(
        "a-forks-first-commit",
        "A fork's first commit is measured from where the fork began: show "
        "prints what it changed in inherited files, and -S doesn't credit it "
        "with what it inherited.",
        (
            Run("cat > a.txt <<'EOF'\none\ntwo\nEOF"),
            Run("echo doomed > b.txt; echo needle > c.txt"),
            Run("ws-git commit -m seed"),
            Run("ws-git branch worker"),
            Run("cat > a.txt <<'EOF'\none\nTWO\nEOF", "worker"),
            Run("rm b.txt", "worker"),
            Run("ws-git commit -m 'worker edits'", "worker"),
            Run("ws-git show HEAD", "worker"),
            Run("ws-git log -S needle", "worker"),
            Run("ws-git log -S doomed", "worker"),
            Run("ws-git show worker@<c2>"),
            Run("ws-git log worker -S needle"),
        ),
    ),
    Script(
        "ids-are-named-in-the-order-they-are-printed",
        "A short id and a different whole id first printed in one output are "
        "named in the order they appear.",
        (
            Run("echo one > a.txt"),
            Run(
                "ws-git commit -m one > /dev/null && ws-git log --all -n 1 && ws-git show HEAD"
            ),
            Run("ws-git checkout <c2>"),
        ),
    ),
    # -- the edges -----------------------------------------------------------
    Script(
        "the-edges",
        "A bare ws-git and an unknown verb are usage errors; rebase says what "
        "to do instead; help prints the whole surface.",
        (
            Run("ws-git"),
            Run("ws-git frobnicate"),
            Run("ws-git rebase main"),
            Run("ws-git help"),
            Run("ws-git --help"),
        ),
    ),
    # -- roots and ignore patterns ---------------------------------------------
    Script(
        "a-configured-root",
        "Paths print relative to the session's root, and outside it absolute.",
        (
            Open("main", root="/proj"),
            Run("echo a > a.txt && mkdir -p src && echo s > src/s.py"),
            Run("ws-git status"),
            Run("ws-git commit -m seed"),
            Run("echo outside > /tmp-file.txt"),
            Run("ws-git status"),
            Run("ws-git log"),
        ),
    ),
    Script(
        "ignored-paths-are-never-work",
        "Paths the ignore patterns name, and anything outside the root, are "
        "left out of status and commits, and refused by name when staged.",
        (
            Open("main", ignore=("*.log", "__pycache__/")),
            Run("echo 'x = 1' > app.py"),
            Run("ws-git commit -m start"),
            Run(
                "echo ran > run.log && mkdir -p __pycache__ && echo c > __pycache__/app.pyc"
            ),
            Run("mkdir -p /tmp && echo scratch > /tmp/t.txt"),
            Run("echo 'x = 2' > app.py"),
            Run("ws-git status --porcelain"),
            Run("ws-git add run.log"),
            Run("ws-git add /tmp/t.txt"),
            Run("ws-git add -A && ws-git commit -m two"),
            Run("ws-git status"),
            Run("cat run.log"),
        ),
    ),
)
