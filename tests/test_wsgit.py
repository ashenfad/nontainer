"""ws-git terminal builtin: the agent's git, as verbs.

End-to-end through ``ws.terminal`` (the agent's path), pinning the
output shapes that become the cross-rung conformance corpus in PR 4.
Commit hashes are shape-pinned (``[0-9a-f]{7}``); everything else is
byte-pinned. Flat trees only for exact log goldens: directory-row
mtime drift can leave real table dirt that the terminal tail
commits, which is provider behavior (PR 2), not terminal shape.
"""

import io
import re

import pytest

from nontainer import Store, Workspace
from nontainer.providers import KvgitProvider
from nontainer.wsgit import (
    _HELP,
    _SUPPORTED,
    _USAGE,
    _WORKTREE_FORMS,
    make_wsgit_command,
    register_wsgit,
)

SHORT = r"[0-9a-f]{7}"


@pytest.fixture
def ws():
    """Memory-backed kvgit workspace with ws-git registered."""
    provider = KvgitProvider.open(None, session="wsgit")
    w = Workspace(provider)
    register_wsgit(w)
    yield w
    w.close()


def _subjects(w):
    """Log subjects newest-first with hashes normalized away."""
    out = []
    for line in w.terminal("ws-git log").stdout.splitlines():
        head, _, subject = line.partition(" ")
        assert re.fullmatch(SHORT, head), line
        out.append(subject)
    return out


def test_clean_status_silent(ws):
    ws.files.fs.write("/workspace/a.txt", b"one\n")
    ws.terminal("ws-git commit -m base")
    r = ws.terminal("ws-git status")
    assert r.exit_code == 0
    assert r.stdout == ""
    assert r.stderr == ""


def test_stage_first_composition(ws):
    ws.files.fs.write("/workspace/a.txt", b"one\n")
    ws.files.fs.write("/workspace/b.txt", b"two\n")

    # Staging is silent (git's `add` is) and commits nothing.
    r = ws.terminal("ws-git stage a.txt b.txt")
    assert r.exit_code == 0
    assert r.stdout == ""

    # The workspace goes on committing for durability underneath...
    ws.terminal("echo second >> a.txt")
    assert not ws.uncommitted
    # ... and the composition does not notice.
    r = ws.terminal("ws-git status")
    assert r.stdout == "M  a.txt\nM  b.txt\n"

    # Everything staged: plain diff is empty, --cached shows both.
    assert ws.terminal("ws-git diff").stdout == ""
    assert ws.terminal("ws-git diff --cached").stdout == (
        "diff --git a/a.txt b/a.txt\n"
        "--- a/a.txt\n"
        "+++ b/a.txt\n"
        "@@ -0,0 +1,2 @@\n"
        "+one\n"
        "+second\n"
        "diff --git a/b.txt b/b.txt\n"
        "--- a/b.txt\n"
        "+++ b/b.txt\n"
        "@@ -0,0 +1 @@\n"
        "+two\n"
    )
    assert ws.terminal("ws-git diff --check").stdout == ""

    r = ws.terminal('ws-git commit -m "compose a"')
    assert r.exit_code == 0
    assert re.fullmatch(rf"\[wsgit {SHORT}\] compose a \(2 files\)\n", r.stdout)

    assert ws.terminal("ws-git status").stdout == ""
    # The agent's log holds the agent's commit and nothing else.
    assert _subjects(ws) == ["compose a"]
    assert re.fullmatch(rf"{SHORT} compose a\n", ws.terminal("ws-git log -n 1").stdout)


def test_partial_commit_leaves_the_rest_in_the_tree(ws):
    """The agent stages one file and edits two; the commit holds one
    and the working tree keeps the other."""
    ws.terminal("echo base > a.txt; echo base > b.txt")
    ws.terminal("ws-git commit -m base")

    ws.terminal("ws-git stage a.txt")
    ws.terminal("echo edited > a.txt; echo edited > b.txt")
    r = ws.terminal('ws-git commit -m "just a"')
    assert re.fullmatch(rf"\[wsgit {SHORT}\] just a \(1 file\)\n", r.stdout)

    assert ws.terminal("ws-git status").stdout == " M b.txt\n"
    assert ws.terminal("cat b.txt").stdout == "edited\n"
    # The commit's own bookkeeping — the restore of b.txt, committed
    # keyed to it — is plumbing: the agent sees its two commits.
    assert _subjects(ws) == ["just a", "base"]
    assert any(e.info.get("tool") == "ws-git.restore" for e in ws.log(kind="all"))
    # and it is plumbing to the host's reader too: not in the default log
    assert not any(e.info.get("tool") == "ws-git.restore" for e in ws.log())
    body = ws.terminal("ws-git show HEAD").stdout
    assert "just a" in body
    assert "a/a.txt" in body and "b.txt" not in body


def test_show_takes_the_short_id_the_log_printed(ws):
    """What a verb prints, a verb accepts: the seven characters of a
    log line name that commit to show."""
    ws.terminal("echo one > a.txt")
    ws.terminal("ws-git commit -m first")
    ws.terminal("echo two > a.txt")
    ws.terminal("ws-git commit -m second")
    first = ws.terminal("ws-git log").stdout.splitlines()[1].split()[0]

    body = ws.terminal(f"ws-git show {first}").stdout
    assert "first" in body and "+one" in body


def test_unstaged_diff_and_status_columns(ws):
    ws.files.fs.write("/workspace/a.txt", b"one\n")
    assert ws.terminal("ws-git status").stdout == " M a.txt\n"
    ws.files.fs.write("/workspace/b.txt", b"two\n")
    assert ws.terminal("ws-git diff b.txt").stdout == (
        "diff --git a/b.txt b/b.txt\n--- a/b.txt\n+++ b/b.txt\n@@ -0,0 +1 @@\n+two\n"
    )
    assert ws.terminal("ws-git diff --cached").stdout == ""


def test_diff_trailing_newline_change(ws):
    ws.files.fs.write("/workspace/e.txt", b"a\n")
    ws.terminal("ws-git commit -m e")
    ws.files.fs.write("/workspace/e.txt", b"a")
    assert ws.terminal("ws-git diff").stdout == (
        "diff --git a/e.txt b/e.txt\n"
        "--- a/e.txt\n"
        "+++ b/e.txt\n"
        "@@ -1 +1 @@\n"
        "-a\n"
        "+a\n"
        "\\ No newline at end of file\n"
    )


def test_diff_check_honors_cached(ws):
    ws.files.fs.write("/workspace/m.txt", b"<<<<<<< HEAD\nx\n")
    ws.terminal("ws-git stage m.txt")
    ws.files.fs.write("/workspace/u.txt", b"y\n=======\n")
    r = ws.terminal("ws-git diff --check")
    assert r.exit_code == 2
    assert r.stdout == "u.txt:2: leftover conflict marker\n"
    r = ws.terminal("ws-git diff --cached --check")
    assert r.exit_code == 2
    assert r.stdout == "m.txt:1: leftover conflict marker\n"


def test_unstage_leaves_the_work(ws):
    ws.files.fs.write("/workspace/a.txt", b"one\n")
    ws.terminal("ws-git stage a.txt")
    r = ws.terminal("ws-git unstage a.txt")
    assert r.exit_code == 0
    assert r.stdout == ""
    assert ws.terminal("ws-git status").stdout == " M a.txt\n"


def test_reset_abandons_the_index_not_the_tree(ws):
    ws.files.fs.write("/workspace/a.txt", b"one\n")
    ws.terminal("ws-git stage a.txt")
    ws.terminal("echo second >> a.txt")
    r = ws.terminal("ws-git reset")
    assert r.exit_code == 0
    assert r.stdout == ""
    assert ws.terminal("ws-git status").stdout == " M a.txt\n"
    assert ws.terminal("cat a.txt").stdout == "one\nsecond\n"


def test_checkout_restores_a_commit(ws):
    ws.terminal("echo one > a.txt")
    ws.terminal("ws-git commit -m first")
    first = ws.terminal("ws-git log").stdout.split()[0]
    ws.terminal("echo two > a.txt; echo new > b.txt")
    ws.terminal("ws-git commit -m second")

    r = ws.terminal(f"ws-git checkout {first}")
    assert r.exit_code == 0
    assert r.stdout == f"[wsgit] restored to {first}\n"
    assert ws.terminal("cat a.txt").stdout == "one\n"
    assert ws.terminal("ls b.txt").exit_code != 0
    assert ws.terminal("ws-git status").stdout == ""
    assert _subjects(ws) == ["first"]

    # The whole-tree form still refuses a session name.
    r = ws.terminal("ws-git checkout worker")
    assert r.exit_code == 1
    assert "sessions are branches" in r.stderr


def test_checkout_takes_paths_from_a_ref(ws):
    """git's ``checkout <ref> -- <paths>``: those paths, nothing else."""
    ws.terminal("echo one > a.txt; echo keep > b.txt")
    ws.terminal("ws-git commit -m first")
    first = ws.terminal("ws-git log").stdout.split()[0]
    ws.terminal("echo two > a.txt; echo moved > b.txt")
    ws.terminal("ws-git commit -m second")

    r = ws.terminal(f"ws-git checkout {first} -- a.txt")
    assert r.exit_code == 0
    assert r.stdout.startswith("Updated 1 path from ")
    assert ws.terminal("cat a.txt").stdout == "one\n"
    # untouched: a take copies what it is told to and nothing else
    assert ws.terminal("cat b.txt").stdout == "moved\n"
    # ordinary work in the tree, not a commit of the agent's
    assert ws.terminal("ws-git status").stdout == " M a.txt\n"
    assert _subjects(ws) == ["second", "first"]

    r = ws.terminal(f"ws-git checkout {first} -- nope.txt")
    assert r.exit_code == 1
    assert "nothing at" in r.stderr
    r = ws.terminal("ws-git checkout -- ")
    assert r.exit_code == 2


def test_commit_without_a_message_is_still_in_the_log(ws):
    """``-m`` is optional here (git insists): a message-less commit is
    still a point in the agent's graph, listed by its tool name."""
    ws.terminal("echo one > a.txt")
    r = ws.terminal("ws-git commit")
    assert r.exit_code == 0
    assert re.fullmatch(rf"\[wsgit {SHORT}\] ws-git \(1 file\)\n", r.stdout)
    assert _subjects(ws) == ["ws-git"]


def test_commit_with_nothing_to_commit(ws):
    r = ws.terminal("ws-git commit")
    assert r.exit_code == 1
    assert r.stderr == "ws-git: nothing to commit"


def test_commit_without_an_index_takes_everything(ws):
    """No index means nothing to have forgotten about, so the verb
    commits the work in front of it (git would need -a)."""
    ws.terminal("echo one > a.txt; echo two > b.txt")
    ws.autocommit = False
    ws.terminal("echo three > c.txt")

    r = ws.terminal("ws-git commit -m 'all of it'")
    assert r.exit_code == 0, r.stderr
    assert re.fullmatch(rf"\[wsgit {SHORT}\] all of it \(3 files\)\n", r.stdout)
    assert ws.terminal("ws-git status").stdout == ""
    assert _subjects(ws)[0] == "all of it"


def test_stage_needs_paths_and_known_files(ws):
    r = ws.terminal("ws-git stage")
    assert r.exit_code == 2
    assert r.stderr == f"ws-git: stage needs at least one path.\n{_USAGE}\n{_SUPPORTED}"
    r = ws.terminal("ws-git stage nope.txt")
    assert r.exit_code == 1
    assert r.stderr == (
        "ws-git: unknown path '/workspace/nope.txt': "
        "no such file at HEAD or in the working tree"
    )


def test_relative_paths_resolve_against_cwd(ws):
    ws.files.fs.write("/workspace/sub/f.txt", b"one\n")
    r = ws.terminal("cd sub; ws-git stage f.txt")
    assert r.exit_code == 0
    assert ws.terminal("ws-git status").stdout == "M  sub/f.txt\n"


def test_merge_status_and_diff_check(ws):
    ws.files.fs.write("/workspace/doc.txt", b"a\nb\n")
    ws.commit()
    fork = ws.fork("worker")
    try:
        fork.files.fs.write("/workspace/doc.txt", b"a\nFORK\n")
        fork.commit()
        ws.files.fs.write("/workspace/doc.txt", b"a\nMAIN\n")
        ws.commit()
        out = ws.merge("worker")
        assert out.conflicts == ("/workspace/doc.txt",)
    finally:
        fork.close()

    body = ws.terminal("cat doc.txt").stdout
    marked = [
        (i, line)
        for i, line in enumerate(body.splitlines(), start=1)
        if line.startswith(("<<<<<<< ", "=======", ">>>>>>> "))
    ]
    assert marked, "merge fixture must leave conflict markers"

    r = ws.terminal("ws-git status")
    assert r.exit_code == 0
    lines = r.stdout.splitlines()
    assert re.fullmatch(rf"## merging worker@{SHORT} \(1 unresolved\)", lines[0]), lines
    assert lines[1:] == ["UU doc.txt"]

    r = ws.terminal("ws-git diff --check")
    assert r.exit_code == 2
    assert r.stdout.splitlines() == [
        f"doc.txt:{lineno}: leftover conflict marker" for lineno, _ in marked
    ]

    # Resolving it clears the context, and the agent's log holds the
    # merge and the resolution.
    ws.terminal("echo resolved > doc.txt")
    ws.terminal("ws-git commit -m resolved")
    assert ws.terminal("ws-git status").stdout == ""
    assert _subjects(ws)[0] == "resolved"
    assert "from worker" in _subjects(ws)[1]


def test_revert_appends_a_commit_that_undoes_one(ws):
    ws.files.fs.write("/workspace/a.txt", b"one\n")
    ws.terminal("ws-git commit -m first")
    ws.files.fs.write("/workspace/a.txt", b"two\n")
    ws.terminal("ws-git commit -m second")
    second = ws.terminal("ws-git log").stdout.split()[0]

    r = ws.terminal(f"ws-git revert {second}")

    assert r.exit_code == 0, r.stderr
    assert r.stdout.splitlines() == [
        "Auto-merging a.txt",
        f"[wsgit {ws.index.head[:7]}] revert {second} (1 file)",
    ]
    assert ws.terminal("cat a.txt").stdout == "one\n"
    # the commit it undid is still in the log, with the revert on top
    assert _subjects(ws) == ['Revert "second"', "second", "first"]
    assert ws.terminal("ws-git status").stdout == ""


def test_revert_of_a_commit_already_undone_says_so(ws):
    ws.files.fs.write("/workspace/a.txt", b"one\n")
    ws.terminal("ws-git commit -m first")
    ws.files.fs.write("/workspace/a.txt", b"two\n")
    ws.terminal("ws-git commit -m second")
    second = ws.terminal("ws-git log").stdout.split()[0]
    ws.terminal(f"ws-git revert {second}")
    before = _subjects(ws)

    r = ws.terminal(f"ws-git revert {second}")

    assert r.exit_code == 1
    assert r.stderr == (
        f"ws-git: nothing to revert: {second} changes nothing this tree "
        "does not already hold. Nothing changed."
    )
    assert _subjects(ws) == before


def test_revert_conflict_lands_with_markers(ws):
    ws.files.fs.write("/workspace/doc.txt", b"a\nb\nc\n")
    ws.terminal("ws-git commit -m first")
    ws.files.fs.write("/workspace/doc.txt", b"a\nSECOND\nc\n")
    ws.terminal("ws-git commit -m second")
    second = ws.terminal("ws-git log").stdout.split()[0]
    ws.files.fs.write("/workspace/doc.txt", b"a\nTHIRD\nc\n")
    ws.terminal("ws-git commit -m third")

    r = ws.terminal(f"ws-git revert {second}")

    assert r.exit_code == 1
    assert r.stdout.splitlines() == [
        "CONFLICT (content): Merge conflict in doc.txt",
        f"[wsgit {ws.index.head[:7]}] revert {second} (1 file)",
    ]
    assert r.stderr == (
        "ws-git: Revert landed with conflict markers in 1 file(s): fix "
        "them and commit (ws-git status shows them as UU)."
    )
    lines = ws.terminal("ws-git status").stdout.splitlines()
    assert re.fullmatch(rf"## merging wsgit@{SHORT} \(1 unresolved\)", lines[0]), lines
    assert lines[1:] == ["UU doc.txt"]
    assert b"<<<<<<< " in ws.files.read("/workspace/doc.txt")

    ws.files.fs.write("/workspace/doc.txt", b"a\nRESOLVED\nc\n")
    ws.terminal("ws-git commit -m resolved")
    assert ws.terminal("ws-git status").stdout == ""


def test_revert_takes_only_a_commit(ws):
    ws.files.fs.write("/workspace/a.txt", b"one\n")
    ws.terminal("ws-git commit -m first")
    r = ws.terminal("ws-git revert")
    assert r.exit_code == 2
    assert r.stderr.startswith("ws-git: revert takes one commit")
    r = ws.terminal("ws-git revert worker")
    assert r.exit_code == 1
    assert "is not a commit" in r.stderr


def test_cherry_pick_brings_one_commit_of_another_session(peer_ws):
    peer_ws.files.fs.write("/workspace/mine.txt", b"mine\n")
    peer_ws.terminal("ws-git commit -m mine")
    assert peer_ws.terminal("ws-git branch worker").exit_code == 0

    worker = peer_ws._store.open("worker", root=peer_ws.root)
    register_wsgit(worker)
    try:
        worker.terminal("cat > one.txt <<'EOF'\none\nEOF\nws-git commit -m one")
        worker.terminal("cat > two.txt <<'EOF'\ntwo\nEOF\nws-git commit -m two")
        wanted = worker.terminal("ws-git log").stdout.split()[0]
    finally:
        worker.close()

    r = peer_ws.terminal(f"ws-git cherry-pick worker@{wanted}")

    assert r.exit_code == 0, r.stderr
    assert r.stdout.splitlines() == [
        "Auto-merging two.txt",
        f"[main {peer_ws.index.head[:7]}] cherry-pick worker@{wanted} (1 file)",
    ]
    assert peer_ws.terminal("cat two.txt").stdout == "two\n"
    assert peer_ws.terminal("cat one.txt").exit_code != 0
    # the log says whose change it is
    line = peer_ws.terminal("ws-git log").stdout.splitlines()[0]
    assert line.endswith(f"two from worker@{wanted}")


def test_cherry_pick_refuses_a_commit_that_session_never_made(peer_ws):
    """A ref names a session and a commit: a sibling's commit under
    this session's name is not a commit it holds."""
    peer_ws.files.fs.write("/workspace/mine.txt", b"mine\n")
    peer_ws.terminal("ws-git commit -m mine")
    assert peer_ws.terminal("ws-git branch worker").exit_code == 0
    assert peer_ws.terminal("ws-git branch bystander").exit_code == 0

    worker = peer_ws._store.open("worker", root=peer_ws.root)
    register_wsgit(worker)
    try:
        worker.terminal("cat > w.txt <<'EOF'\nworker\nEOF\nws-git commit -m work")
        made = worker.terminal("ws-git log").stdout.split()[0]
    finally:
        worker.close()

    r = peer_ws.terminal(f"ws-git cherry-pick bystander@{made}")

    assert r.exit_code == 1
    assert "bystander" in r.stderr
    assert peer_ws.terminal("cat w.txt").exit_code != 0
    assert peer_ws.terminal("ws-git status").stdout == ""


def test_cherry_pick_needs_the_commit_named(peer_ws):
    peer_ws.files.fs.write("/workspace/mine.txt", b"mine\n")
    peer_ws.terminal("ws-git commit -m mine")
    assert peer_ws.terminal("ws-git branch worker").exit_code == 0

    r = peer_ws.terminal("ws-git cherry-pick worker")
    assert r.exit_code == 2
    assert r.stderr.startswith(
        "ws-git: cherry-pick takes one commit of another session, spelled "
        "<session>@<commit> (ws-git log worker lists them)."
    )
    r = peer_ws.terminal("ws-git cherry-pick")
    assert r.exit_code == 2
    assert r.stderr.startswith("ws-git: cherry-pick takes one commit")


def test_edges_name_what_the_agent_can_do(ws):
    r = ws.terminal("ws-git rebase")
    assert r.exit_code == 1
    assert r.stderr.startswith("ws-git: no rebase here — history is append-only")
    # Every hint names a terminal verb the agent can actually run —
    # never host Python it cannot reach.
    from nontainer.wsgit import _EDGE

    for verb, text in _EDGE.items():
        assert "ws.fork(" not in text and "provider." not in text, verb
        assert "ws-git " in text, verb
    # branch, merge and stash are the agent's now, not refusals
    assert ws.terminal("ws-git branch").exit_code == 0
    assert ws.terminal("ws-git merge").exit_code == 2
    assert ws.terminal("ws-git stash nonsense").exit_code == 2

    r = ws.terminal("ws-git frobnicate")
    assert r.exit_code == 2
    assert r.stderr == (
        "ws-git: 'frobnicate' is not a ws-git command. See 'ws-git help'.\n"
        f"{_SUPPORTED}"
    )

    for cmd, first in [
        ("ws-git commit -a", "ws-git: commit stages nothing itself (no -a)"),
        ("ws-git commit path.txt", "ws-git: commit takes the staged set only"),
        ("ws-git commit -m", "ws-git: commit takes the staged set only"),
        ("ws-git status --short", "ws-git: status takes no '--short'"),
        ("ws-git reset --hard", "ws-git: reset is mixed-only"),
        ("ws-git log --oneline", "ws-git: log takes no '--oneline'"),
        ("ws-git diff --stat", "ws-git: diff takes no '--stat'."),
        ("ws-git checkout", "ws-git: checkout takes one ref"),
        ("ws-git show", "ws-git: show takes one ref"),
    ]:
        r = ws.terminal(cmd)
        assert r.exit_code == 2, cmd
        assert r.stderr.startswith(f"{first}"), (cmd, r.stderr)
        assert r.stderr.endswith(f"{_USAGE}\n{_SUPPORTED}"), (cmd, r.stderr)


def test_bare_and_help(ws):
    r = ws.terminal("ws-git")
    assert r.exit_code == 2
    assert r.stderr == f"ws-git: {_USAGE}"
    r = ws.terminal("ws-git help")
    assert r.exit_code == 0
    assert r.stdout == _HELP + "\n"
    assert "ws-git log shows only the commits you made" in r.stdout


def test_fork_wsgit_binds_fork():
    """The fork-bleed fix: a fork's ws-git operates on the fork branch,
    not the parent's. Rebinding (not copying the closure) is what makes
    this hold."""
    provider = KvgitProvider.open(None, session="wsgit-forkbleed")
    w = Workspace(provider)
    register_wsgit(w)
    try:
        w.files.fs.write("/workspace/a.txt", b"one\n")
        w.terminal("ws-git commit -m base")
        fork = w.fork("wsgit-forkbleed-kid")
        try:
            assert fork.runtime.commands["ws-git"] is not w.runtime.commands["ws-git"]
            fork.files.fs.write("/workspace/kid.txt", b"kid\n")
            r = fork.terminal("ws-git stage kid.txt")
            assert r.exit_code == 0, r.stderr
            # a fork starts at no commit of its own, so everything it
            # holds reads as modified until its first one — and its
            # log is its own, not a continuation of the parent's
            assert fork.terminal("ws-git status").stdout == " M a.txt\nM  kid.txt\n"
            assert w.terminal("ws-git status").stdout == ""
            assert fork.terminal('ws-git commit -m "kid work"').exit_code == 0
            assert _subjects(w) == ["base"]
            assert _subjects(fork) == ["kid work"]
            # a.txt was never in the fork's own commit, so it is still
            # the fork's uncommitted work — the delegate said what it
            # said, and nothing commits the rest for it
            assert fork.terminal("ws-git status").stdout == " M a.txt\n"
        finally:
            fork.close()
    finally:
        w.close()


def test_snapshot_wsgit_reads_snapshot():
    """A snapshot's verbs read the tagged state, not the live parent —
    and refuse writes as frozen."""
    provider = KvgitProvider.open(None, session="wsgit-snaphole")
    w = Workspace(provider)
    register_wsgit(w)
    try:
        w.files.fs.write("/workspace/a.txt", b"one\n")
        w.terminal("ws-git commit -m base")
        w.tags.add("v1")
        snap = w.tags.at("v1")
        try:
            assert snap.runtime.commands["ws-git"] is not w.runtime.commands["ws-git"]
            # The parent moves on; the snapshot doesn't follow.
            w.files.fs.write("/workspace/b.txt", b"two\n")
            w.terminal("ws-git stage b.txt")
            assert w.terminal("ws-git status").stdout == "M  b.txt\n"
            assert snap.terminal("ws-git status").stdout == ""
            r = snap.terminal("ws-git stage a.txt")
            assert r.exit_code == 1
            assert "frozen" in r.stderr
        finally:
            snap.close()
    finally:
        w.close()


def test_no_index_provider_refused(tmp_path):
    from nontainer.providers.dir import DirProvider

    provider = DirProvider(tmp_path / "ws", session="dir")
    w = Workspace(provider)
    register_wsgit(w)
    try:
        r = w.terminal("ws-git status")
        assert r.exit_code == 1
        assert r.stderr == (
            "ws-git: this provider has no index (needs caps.index) — "
            "use the kvgit backend for ws-git."
        )
    finally:
        w.close()


def test_register_gated_on_supports_commands():
    seen = []

    class FakeRuntime:
        supports_commands = True
        supports_ws_verbs = False

        def register_command(self, name, fn, *, rebind=None):
            seen.append((name, fn, rebind))

    class FakeWs:
        _provider = object()

        def __init__(self):
            self.runtime = FakeRuntime()

    register_wsgit(FakeWs())
    assert [name for name, _, _ in seen] == ["ws-git"]
    # Framework-owned: the rebind factory travels with the registration.
    assert [rebind for _, _, rebind in seen] == [register_wsgit]

    class DeafWs(FakeWs):
        def __init__(self):
            super().__init__()
            self.runtime.supports_commands = False

    seen.clear()
    register_wsgit(DeafWs())
    assert seen == []


def test_command_closure_direct_status_shape():
    """The closure renders status without a shell round-trip."""
    provider = KvgitProvider.open(None, session="direct")
    w = Workspace(provider)
    fn = make_wsgit_command(w)
    try:
        w.files.fs.write("/workspace/a.txt", b"one\n")
        w.index.stage(["/workspace/a.txt"])

        class Ctx:
            def __init__(self, args):
                self.args = args
                self.stdout = io.StringIO()
                self.fs = w.files.fs

        ctx = Ctx(["status"])
        assert fn(ctx) is None
        assert ctx.stdout.getvalue() == "M  a.txt\n"
    finally:
        w.close()


def test_a_failed_commit_leaves_the_agent_where_it_was(ws, monkeypatch):
    """A refused commit must cost the agent nothing: same tree, same
    status, same composition — ready to try again."""
    from nontainer import WorkspaceError

    ws.terminal("echo base > a.txt; echo base > b.txt")
    ws.terminal("ws-git commit -m base")
    ws.terminal("ws-git stage a.txt")
    ws.terminal("echo edited > a.txt; echo edited > b.txt")
    before = ws.terminal("ws-git status").stdout
    before_diff = ws.terminal("ws-git diff --cached").stdout

    def boom(*args, **kwargs):
        raise WorkspaceError("commit failed: conflicting concurrent commit (CAS)")

    monkeypatch.setattr(ws._provider, "commit_keys", boom)
    r = ws.terminal("ws-git commit -m doomed")
    assert r.exit_code == 1
    assert "CAS" in r.stderr
    monkeypatch.undo()

    assert ws.terminal("ws-git status").stdout == before
    assert ws.terminal("ws-git diff --cached").stdout == before_diff
    assert ws.terminal("cat a.txt").stdout == "edited\n"
    assert ws.terminal("cat b.txt").stdout == "edited\n"
    assert _subjects(ws) == ["base"]


def test_an_ambiguous_short_id_is_refused_by_show_and_checkout(ws, monkeypatch):
    """One ambiguity rule everywhere: two commits under one prefix are
    named, never silently picked between."""
    from nontainer.agentgit import AgentGit
    from nontainer.protocol import CommitInfo

    ws.terminal("echo one > a.txt")
    ws.terminal("ws-git commit -m base")
    twins = [
        CommitInfo(
            id="abc1234" + c * 33, time=0.0, info={"tool": "ws-git", "message": m}
        )
        for c, m in (("f", "one"), ("e", "two"))
    ]
    monkeypatch.setattr(AgentGit, "log", lambda self, limit=None: twins)

    for cmd in ("ws-git show abc1234", "ws-git checkout abc1234"):
        r = ws.terminal(cmd)
        assert r.exit_code == 1, (cmd, r.stdout)
        assert "ambiguous commit 'abc1234'" in r.stderr, (cmd, r.stderr)
        assert twins[0].id[:12] in r.stderr and twins[1].id[:12] in r.stderr, r.stderr


# -- log -S: where a string appeared or vanished -------------------------------


def _pickaxe(w, args=""):
    """(sign, subject) per line of a pickaxe log, hashes checked away."""
    out = []
    for line in w.terminal(f"ws-git log -S needle {args}").stdout.splitlines():
        head, sign, subject = line.split(" ", 2)
        assert re.fullmatch(SHORT, head), line
        out.append((sign, subject))
    return out


def test_log_S_finds_where_a_string_appeared_and_vanished(ws):
    """git's pickaxe rule: the commits where the count of the string in
    the tree changed, and nothing else."""
    ws.terminal("echo plain > a.txt")
    ws.terminal("ws-git commit -m base")
    ws.terminal("echo needle > a.txt")
    ws.terminal("ws-git commit -m adds")
    ws.terminal("echo other > b.txt")
    ws.terminal("ws-git commit -m unrelated")
    ws.terminal("echo plain > a.txt")
    ws.terminal("ws-git commit -m drops")

    assert _pickaxe(ws) == [("-", "drops"), ("+", "adds")]


def test_log_S_ignores_a_commit_that_only_moved_the_string(ws):
    """The count in the tree is what changed or did not: moving the
    string within a file is not an appearance."""
    ws.terminal("echo needle > a.txt; echo tail >> a.txt")
    ws.terminal("ws-git commit -m adds")
    ws.terminal("echo tail > a.txt; echo needle >> a.txt")
    ws.terminal("ws-git commit -m moved")

    assert _pickaxe(ws) == [("+", "adds")]


def test_log_S_reads_undecodable_bytes_as_not_containing_it(ws):
    """A file that is not text holds no string to count."""
    ws.files.fs.write("/workspace/blob.bin", b"\xff\xfeneedle\xff")
    ws.terminal("ws-git commit -m binary")

    assert _pickaxe(ws) == []


def test_log_S_all_reaches_a_framework_commit(ws):
    """The default walk is the agent's own commits; --all is every
    commit the session holds."""
    ws.terminal("echo plain > a.txt")
    ws.terminal("ws-git commit -m base")
    ws.files.fs.write("/workspace/a.txt", b"needle\n")
    framework = ws.commit(info={"tool": "framework"})
    assert framework not in [e.id for e in ws.index.log()]

    assert _pickaxe(ws) == []
    assert _pickaxe(ws, "--all") == [("+", "framework")]


def test_log_all_lists_the_commits_log_hides(ws):
    """--all on its own is the session's history, bookkeeping and all."""
    ws.terminal("echo one > a.txt")
    ws.terminal("ws-git commit -m base")
    ws.files.fs.write("/workspace/b.txt", b"two\n")
    ws.commit(info={"tool": "framework"})

    assert _subjects(ws) == ["base"]
    every = [
        line.split(" ", 1)[1]
        for line in ws.terminal("ws-git log --all").stdout.splitlines()
    ]
    assert every[0] == "framework"
    assert "base" in every and len(every) > len(_subjects(ws))


def test_log_of_nothing_is_nothing(ws):
    """A limit that asks for nothing gets nothing, on every walk."""
    ws.terminal("echo needle > a.txt")
    ws.terminal("ws-git commit -m base")
    ws.files.fs.write("/workspace/b.txt", b"two\n")
    ws.commit(info={"tool": "framework"})

    assert ws.terminal("ws-git log -n 0").stdout == ""
    assert ws.terminal("ws-git log --all -n 0").stdout == ""
    assert ws.terminal("ws-git log -S needle -n 0").stdout == ""
    # and the host's own reader of the agent's graph agrees
    assert list(ws.index.log(limit=0)) == []
    # a limit that asks for something still gets it
    assert len(ws.terminal("ws-git log -n 1").stdout.splitlines()) == 1


def test_log_S_needs_a_string(ws):
    r = ws.terminal("ws-git log -S")
    assert r.exit_code == 2
    assert r.stderr.startswith("ws-git: log takes no '-S'")


# -- worktrees: another session's tree, read in place --------------------------


@pytest.fixture
def store(tmp_path):
    """A store, so a session has neighbours to read."""
    s = Store(tmp_path / "store")
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def peer_ws(store):
    """A ws-git session on a store, with a committed peer to attach."""
    peer = store.open("peer")
    peer.files.write("/workspace/note.md", "first\n")
    peer.commit(info={"tool": "test"})
    peer.close()
    w = store.open("main")
    register_wsgit(w)
    try:
        yield w
    finally:
        w.close()


def _advance_peer(store):
    """A newer commit on the peer, so a second add has something to see."""
    peer = store.open("peer")
    peer.files.write("/workspace/note.md", "second\n")
    peer.commit(info={"tool": "test"})
    peer.close()


def test_worktree_add_list_remove(peer_ws):
    """The round trip: nothing, one worktree in the list shape, nothing."""
    assert peer_ws.terminal("ws-git worktree list").stdout == "(none)\n"

    r = peer_ws.terminal("ws-git worktree add peek peer")
    assert r.exit_code == 0, r.stderr
    assert re.fullmatch(rf"worktree peek: peer@{SHORT} \(read-only\)\n", r.stdout), (
        r.stdout
    )

    assert peer_ws.terminal("ws-git worktree list").stdout == r.stdout

    r = peer_ws.terminal("ws-git worktree remove peek")
    assert r.exit_code == 0, r.stderr
    assert r.stdout == ""
    assert peer_ws.terminal("ws-git worktree list").stdout == "(none)\n"


def test_worktree_reads_the_other_session(peer_ws):
    """Ordinary tools read it: the point of a worktree over a verb."""
    assert peer_ws.terminal("ws-git worktree add peek peer").exit_code == 0
    assert peer_ws.terminal("cat peek/note.md").stdout == "first\n"
    assert peer_ws.terminal("ls peek").stdout == "note.md\n"


def test_worktree_is_read_only(peer_ws):
    """Work moves between sessions by merge and take, never by writing
    into someone else's tree."""
    peer_ws.terminal("ws-git worktree add peek peer")
    r = peer_ws.terminal("echo edited > peek/note.md")
    assert r.exit_code != 0
    assert "Read-only filesystem" in r.stderr
    assert peer_ws.terminal("cat peek/note.md").stdout == "first\n"


def test_worktree_paths_are_never_modified(peer_ws):
    """An attachment sits outside the versioned tree, so no verb that
    measures the tree ever names it."""
    peer_ws.files.fs.write("/workspace/a.txt", b"mine\n")
    peer_ws.terminal("ws-git commit -m base")
    peer_ws.terminal("ws-git worktree add peek peer")

    assert "note.md" not in peer_ws.terminal("ws-git status").stdout
    assert peer_ws.terminal("ws-git diff").stdout == ""
    assert peer_ws.terminal("ws-git diff --cached").stdout == ""


def test_worktree_add_again_sees_newer_work(peer_ws, store):
    """A worktree is pinned at a commit, so taking it down and putting
    it up again is how you see what the session has done since."""
    peer_ws.terminal("ws-git worktree add peek peer")
    first = peer_ws.terminal("ws-git worktree list").stdout
    _advance_peer(store)
    assert peer_ws.terminal("cat peek/note.md").stdout == "first\n"

    peer_ws.terminal("ws-git worktree remove peek")
    r = peer_ws.terminal("ws-git worktree add peek peer")
    assert r.exit_code == 0, r.stderr
    assert r.stdout != first
    assert peer_ws.terminal("cat peek/note.md").stdout == "second\n"


def test_worktree_add_over_a_worktree_names_the_way_to_refresh(peer_ws):
    """A worktree is pinned, so seeing newer work means taking it down
    and putting it up again. The refusal has to say so: adding again
    over a live worktree earned the generic not-empty reason, which
    sent an agent looking for a directory to clear instead."""
    peer_ws.terminal("ws-git worktree add peek peer")
    r = peer_ws.terminal("ws-git worktree add peek peer")
    assert r.exit_code == 1
    assert r.stderr == (
        "ws-git: there is already a worktree at 'peek': it is pinned at a "
        "commit, so seeing newer work means taking it down and putting it "
        "up again (ws-git worktree remove peek)."
    )


def test_help_says_all_walks_the_session_named():
    """--all with a session name walks THAT session's history, so the
    help cannot call it every commit this one holds."""
    flat = " ".join(_HELP.split())
    assert "--all every commit this session holds" not in flat
    assert "--all every commit the selected session holds" in flat


def test_help_says_how_a_worktree_is_refreshed():
    """The help said 'add it again', which the verb refuses."""
    flat = " ".join(_HELP.split())  # the help is wrapped; the rule is not
    assert "add it again to see newer work" not in flat
    assert "to see newer work, remove it and add it again" in flat


def test_worktree_add_at_a_commit(peer_ws, store):
    """The session@commit spelling pins an older state."""
    peer_ws.terminal("ws-git worktree add peek peer")
    pinned = (
        peer_ws.terminal("ws-git worktree list").stdout.split(": ")[1].split(" ")[0]
    )
    peer_ws.terminal("ws-git worktree remove peek")
    _advance_peer(store)

    r = peer_ws.terminal(f"ws-git worktree add old {pinned}")
    assert r.exit_code == 0, r.stderr
    assert peer_ws.terminal("cat old/note.md").stdout == "first\n"


def test_worktree_add_refusals(peer_ws):
    """Three refusals, each with its one-line reason."""
    peer_ws.files.fs.write("/workspace/full/a.txt", b"a\n")
    r = peer_ws.terminal("ws-git worktree add full peer")
    assert r.exit_code == 1
    assert r.stderr == (
        "ws-git: cannot add a worktree at 'full': that directory is not "
        "empty (a worktree needs a new or empty one)."
    )

    peer_ws.terminal("ws-git worktree add peek peer")
    r = peer_ws.terminal("ws-git worktree add peek/inner peer")
    assert r.exit_code == 1
    assert r.stderr == (
        "ws-git: cannot add a worktree at 'peek/inner': that is inside the "
        "worktree peek."
    )

    r = peer_ws.terminal("ws-git worktree add mine main")
    assert r.exit_code == 1
    assert r.stderr == (
        "ws-git: cannot add a worktree of 'main': a worktree reads another "
        "session, and this one is already your own tree."
    )


def test_worktree_remove_unknown_dir(peer_ws):
    """A usage error that names what is actually there."""
    r = peer_ws.terminal("ws-git worktree remove nope")
    assert r.exit_code == 2
    assert r.stderr.startswith("ws-git: no worktree at 'nope' (no worktrees here).")

    peer_ws.terminal("ws-git worktree add peek peer")
    r = peer_ws.terminal("ws-git worktree remove nope")
    assert r.exit_code == 2
    assert r.stderr.startswith("ws-git: no worktree at 'nope' (worktrees here: peek).")


def test_worktree_bare_prints_the_forms(peer_ws):
    r = peer_ws.terminal("ws-git worktree")
    assert r.exit_code == 0
    assert r.stdout == _WORKTREE_FORMS + "\n"
    assert "worktree add <dir> <session>[@<commit>]" in r.stdout


def test_status_ends_with_the_worktrees(peer_ws):
    """A directory that never shows as modified would puzzle an agent,
    so status says it is there and what it holds."""
    peer_ws.terminal("ws-git worktree add peek peer")
    block = peer_ws.terminal("ws-git worktree list").stdout

    out = peer_ws.terminal("ws-git status").stdout
    assert out == "worktrees:\n" + block
    assert peer_ws.terminal("ws-git status --porcelain").stdout == out

    # file rows first, the block last
    peer_ws.files.fs.write("/workspace/a.txt", b"mine\n")
    out = peer_ws.terminal("ws-git status").stdout
    assert out == " M a.txt\nworktrees:\n" + block
    assert peer_ws.terminal("ws-git status --porcelain").stdout == out


def test_status_omits_the_block_with_no_worktrees(peer_ws):
    assert peer_ws.terminal("ws-git status").stdout == ""
    peer_ws.terminal("ws-git worktree add peek peer")
    peer_ws.terminal("ws-git worktree remove peek")
    assert peer_ws.terminal("ws-git status").stdout == ""
    assert peer_ws.terminal("ws-git status --porcelain").stdout == ""


def test_worktree_outside_the_root_keeps_its_absolute_path(peer_ws):
    """What the verb prints, the verb takes back: a point outside the
    root has no relative spelling, so it is printed absolute."""
    r = peer_ws.terminal("ws-git worktree add /reviews peer")
    assert r.exit_code == 0, r.stderr
    assert re.fullmatch(
        rf"worktree /reviews: peer@{SHORT} \(read-only\)\n", r.stdout
    ), r.stdout
    assert peer_ws.terminal("ws-git worktree list").stdout == r.stdout
    assert peer_ws.terminal("ws-git status").stdout == "worktrees:\n" + r.stdout

    printed = r.stdout.split(" ", 2)[1].rstrip(":")
    assert printed == "/reviews"
    assert peer_ws.terminal(f"ws-git worktree remove {printed}").exit_code == 0
    assert peer_ws.terminal("ws-git worktree list").stdout == "(none)\n"


def test_worktree_under_a_configured_root(store):
    """A session at a root of its own renders a worktree against THAT
    root, not against a default spelled into the code."""
    peer = store.open("peer", root="/data")
    peer.files.write("/data/note.md", "first\n")
    peer.commit(info={"tool": "test"})
    peer.close()

    w = store.open("main", root="/data")
    register_wsgit(w)
    try:
        r = w.terminal("ws-git worktree add peek peer")
        assert r.exit_code == 0, r.stderr
        assert re.fullmatch(
            rf"worktree peek: peer@{SHORT} \(read-only\)\n", r.stdout
        ), r.stdout
        assert w.terminal("ws-git worktree list").stdout == r.stdout
        assert w.terminal("ws-git status").stdout.endswith("worktrees:\n" + r.stdout)
        assert w.terminal("cat peek/note.md").stdout == "first\n"

        assert w.terminal("ws-git worktree remove peek").exit_code == 0
        assert w.terminal("ws-git worktree list").stdout == "(none)\n"
    finally:
        w.close()


def test_file_rows_use_the_sessions_root(store):
    """A session at a root of its own names its files the way its agent
    typed them: status rows and diff headers are root-relative here as
    everywhere, and the root is the session's, not a default."""
    w = store.open("main", root="/data")
    register_wsgit(w)
    try:
        w.terminal("echo one > note.md")
        assert w.terminal("ws-git status").stdout == " M note.md\n"

        w.terminal("ws-git stage note.md")
        assert w.terminal("ws-git status").stdout == "M  note.md\n"
        assert w.terminal("ws-git diff --cached").stdout == (
            "diff --git a/note.md b/note.md\n"
            "--- a/note.md\n"
            "+++ b/note.md\n"
            "@@ -0,0 +1 @@\n"
            "+one\n"
        )
    finally:
        w.close()


# -- tag: the agent's own bookmark ---------------------------------------------


def test_tag_creates_lists_and_deletes(ws):
    """The round trip: nothing, a name pointing at the head, nothing."""
    assert ws.terminal("ws-git tag").stdout == "(none)\n"

    ws.files.fs.write("/workspace/a.txt", b"one\n")
    ws.terminal("ws-git commit -m first")
    first = ws.terminal("ws-git log").stdout.split()[0]

    r = ws.terminal("ws-git tag v1")
    assert r.exit_code == 0
    assert r.stdout == ""  # silent, like git tag

    assert ws.terminal("ws-git tag").stdout == f"v1 -> {first}\n"

    ws.files.fs.write("/workspace/a.txt", b"two\n")
    ws.terminal("ws-git commit -m second")
    second = ws.terminal("ws-git log").stdout.split()[0]
    assert ws.terminal(f"ws-git tag v2 {second}").exit_code == 0
    assert ws.terminal("ws-git tag").stdout == f"v1 -> {first}\nv2 -> {second}\n"

    r = ws.terminal("ws-git tag -d v1")
    assert r.exit_code == 0
    assert r.stdout == f"Deleted tag 'v1' (was {first})\n"
    assert ws.terminal("ws-git tag").stdout == f"v2 -> {second}\n"


def test_tag_refusals(ws):
    ws.files.fs.write("/workspace/a.txt", b"one\n")
    ws.terminal("ws-git commit -m first")
    first = ws.terminal("ws-git log").stdout.split()[0]
    ws.terminal("ws-git tag v1")

    # a name that is taken is refused, and -f moves it
    r = ws.terminal("ws-git tag v1")
    assert r.exit_code == 1
    assert r.stderr == ("ws-git: tag 'v1' already exists — ws-git tag -f v1 moves it.")
    ws.files.fs.write("/workspace/a.txt", b"two\n")
    ws.terminal("ws-git commit -m second")
    second = ws.terminal("ws-git log").stdout.split()[0]
    assert ws.terminal("ws-git tag -f v1").exit_code == 0
    assert ws.terminal("ws-git tag").stdout == f"v1 -> {second}\n"

    # a name that is not a tag name
    r = ws.terminal("ws-git tag ..bad")
    assert r.exit_code == 1
    assert r.stderr.startswith("ws-git: '..bad' is not a tag name")

    # a name a ref reader would take for a commit id
    r = ws.terminal("ws-git tag deadbee")
    assert r.exit_code == 1
    assert "hex characters" in r.stderr

    # deleting one that is not there
    r = ws.terminal("ws-git tag -d nope")
    assert r.exit_code == 1
    assert r.stderr == "ws-git: tag 'nope' not found"

    # unchanged by all of that
    assert ws.terminal("ws-git tag").stdout == f"v1 -> {second}\n"
    assert first != second


def test_tag_is_a_ref_for_checkout_show_and_take(ws):
    ws.files.fs.write("/workspace/a.txt", b"one\n")
    ws.terminal("ws-git commit -m first")
    ws.terminal("ws-git tag start")
    first = ws.terminal("ws-git log").stdout.split()[0]
    ws.files.fs.write("/workspace/a.txt", b"two\n")
    ws.files.fs.write("/workspace/b.txt", b"new\n")
    ws.terminal("ws-git commit -m second")

    # show
    r = ws.terminal("ws-git show start")
    assert r.exit_code == 0, r.stderr
    assert r.stdout.splitlines()[0].startswith("commit ")
    assert "+one" in r.stdout

    # take
    r = ws.terminal("ws-git checkout start -- a.txt")
    assert r.exit_code == 0, r.stderr
    assert ws.terminal("cat a.txt").stdout == "one\n"

    # and the whole-tree restore
    r = ws.terminal("ws-git checkout start")
    assert r.exit_code == 0, r.stderr
    assert r.stdout == f"[{ws.session}] restored to {first}\n"
    assert ws.terminal("ws-git status").stdout == ""
    assert ws.terminal("ls b.txt").exit_code != 0


def test_tag_decorates_the_log(ws):
    ws.files.fs.write("/workspace/a.txt", b"one\n")
    ws.terminal("ws-git commit -m first")
    ws.terminal("ws-git tag v1")
    ws.terminal("ws-git tag ship")
    ws.files.fs.write("/workspace/a.txt", b"two\n")
    ws.terminal("ws-git commit -m second")

    lines = ws.terminal("ws-git log").stdout.splitlines()
    assert re.fullmatch(rf"{SHORT} second", lines[0]), lines
    assert re.fullmatch(rf"{SHORT} \(tag: ship, v1\) first", lines[1]), lines


def test_tags_are_not_inherited_by_a_fork(ws):
    ws.files.fs.write("/workspace/a.txt", b"one\n")
    ws.terminal("ws-git commit -m first")
    ws.terminal("ws-git tag v1")

    child = ws.fork("worker")
    try:
        assert child.index.tags() == {}
    finally:
        child.close()
    assert set(ws.index.tags()) == {"v1"}


def test_a_merge_does_not_bring_the_sources_tags(ws):
    ws.files.fs.write("/workspace/a.txt", b"one\n")
    ws.commit()
    fork = ws.fork("worker")
    try:
        fork.files.fs.write("/workspace/b.txt", b"worker\n")
        fork.index.commit("work")
        fork.index.tag("theirs")
        assert set(fork.index.tags()) == {"theirs"}
    finally:
        fork.close()
    ws.index.commit("mine")
    ws.index.tag("mine")
    ws.commit()
    ws.merge("worker")
    assert set(ws.index.tags()) == {"mine"}


def test_index_tags_is_a_record_not_a_live_view(ws):
    ws.files.fs.write("/workspace/a.txt", b"one\n")
    ws.terminal("ws-git commit -m first")
    ws.index.tag("v1")
    got = ws.index.tags()
    assert list(got) == ["v1"]
    got["sneaky"] = "abc"
    assert list(ws.index.tags()) == ["v1"]


def test_another_sessions_tag_is_a_ref(peer_ws, store):
    """``session@tag`` reads the other session's own bookmark, for the
    verbs that name one exact state on one session."""
    peer = store.open("peer")
    register_wsgit(peer)
    try:
        peer.files.fs.write("/workspace/note.md", "second\n".encode())
        peer.terminal("ws-git commit -m note")
        peer.terminal("ws-git tag good")
        tagged = peer.terminal("ws-git log").stdout.split()[0]
    finally:
        peer.close()

    r = peer_ws.terminal("ws-git worktree add peek peer@good")
    assert r.exit_code == 0, r.stderr
    assert r.stdout == f"worktree peek: peer@{tagged} (read-only)\n"
    assert peer_ws.terminal("cat peek/note.md").stdout == "second\n"
    peer_ws.terminal("ws-git worktree remove peek")

    r = peer_ws.terminal("ws-git cherry-pick peer@good")
    assert r.exit_code == 0, r.stderr
    assert f"] cherry-pick peer@{tagged} (1 file)" in r.stdout
    assert peer_ws.terminal("cat note.md").stdout == "second\n"


# -- merge --abort -------------------------------------------------------------


def _conflicted_merge(ws):
    """A merge that landed with markers, and the commit it landed on."""
    ws.files.fs.write("/workspace/doc.txt", b"a\nb\n")
    ws.terminal("ws-git commit -m base")
    fork = ws.fork("worker")
    try:
        fork.files.fs.write("/workspace/doc.txt", b"a\nFORK\n")
        fork.index.commit("theirs")
    finally:
        fork.close()
    ws.files.fs.write("/workspace/doc.txt", b"a\nMAIN\n")
    ws.terminal("ws-git commit -m mine")
    before = ws.index.head
    r = ws.terminal("ws-git merge worker")
    assert r.exit_code == 1, (r.stdout, r.stderr)
    assert "UU doc.txt" in ws.terminal("ws-git status").stdout
    return before


def test_merge_abort_returns_to_the_pre_merge_commit(ws):
    before = _conflicted_merge(ws)
    was = dict(ws._provider.files_at(before))
    grew = len(ws.terminal("ws-git log --all").stdout.splitlines())
    log = ws.terminal("ws-git log").stdout

    r = ws.terminal("ws-git merge --abort")
    assert r.exit_code == 0, (r.stdout, r.stderr)
    assert r.stdout == (
        f"[{ws.session}] aborted merge of worker, restored to {before[:7]}\n"
    )

    # the tree the merge landed on, markers and all, is gone
    assert ws.terminal("ws-git status").stdout == ""
    assert ws.terminal("cat doc.txt").stdout == "a\nMAIN\n"
    assert dict(ws._provider.working_files()) == was
    assert ws.index.head == before
    assert ws.terminal("ws-git log").stdout == log.split("\n", 1)[1]

    # append-only: the abort is a commit of its own, and the merge it
    # stepped off is still in the session's history
    after = ws.terminal("ws-git log --all").stdout.splitlines()
    assert len(after) == grew + 1


def test_merge_abort_with_nothing_outstanding_refuses(ws):
    ws.files.fs.write("/workspace/a.txt", b"one\n")
    ws.terminal("ws-git commit -m first")
    r = ws.terminal("ws-git merge --abort")
    assert r.exit_code == 1
    assert r.stderr == (
        "ws-git: no merge to abort: nothing is outstanding here. A merge "
        "that conflicts stays outstanding until its markers are gone — "
        "start one with ws-git merge <session>."
    )
    assert ws.terminal("ws-git status").stdout == ""

    # and once the conflict is resolved the merge is no longer one to abort
    _conflicted_merge(ws)
    ws.terminal("echo resolved > doc.txt")
    ws.terminal("ws-git commit -m resolved")
    assert ws.terminal("ws-git merge --abort").exit_code == 1


# -- stash: sugar over fork and checkout ---------------------------------------


def _stash_base(w):
    """A committed base with work in progress on top of it."""
    w.files.fs.write("/workspace/a.txt", b"one\n")
    w.terminal("ws-git commit -m base")
    w.files.fs.write("/workspace/a.txt", b"edited\n")
    w.files.fs.write("/workspace/new.txt", b"fresh\n")


def test_stash_takes_the_work_aside_and_pops_it_back(peer_ws, store):
    _stash_base(peer_ws)
    assert peer_ws.terminal("ws-git stash list").stdout == "(none)\n"

    r = peer_ws.terminal("ws-git stash")
    assert r.exit_code == 0, r.stderr
    assert r.stdout == "Saved working directory and index state stash@{0}: base\n"

    # the tree is what the head holds, and the work is on a branch
    assert peer_ws.terminal("ws-git status").stdout == ""
    assert peer_ws.terminal("cat a.txt").stdout == "one\n"
    assert peer_ws.terminal("ls new.txt").exit_code != 0
    assert peer_ws.terminal("ws-git stash list").stdout == "stash@{0}: base\n"
    assert "main.stash-0" in store.sessions()

    r = peer_ws.terminal("ws-git stash pop")
    assert r.exit_code == 0, r.stderr
    assert "Auto-merging a.txt" in r.stdout
    assert r.stdout.endswith("Dropped stash@{0} (main.stash-0)\n")
    assert peer_ws.terminal("cat a.txt").stdout == "edited\n"
    assert peer_ws.terminal("cat new.txt").stdout == "fresh\n"

    # a clean pop takes the branch with it
    assert peer_ws.terminal("ws-git stash list").stdout == "(none)\n"
    assert "main.stash-0" not in store.sessions()


def test_stash_push_message_list_and_show(peer_ws):
    _stash_base(peer_ws)
    assert peer_ws.terminal('ws-git stash push -m "wip auth"').exit_code == 0
    peer_ws.files.fs.write("/workspace/a.txt", b"again\n")
    assert peer_ws.terminal("ws-git stash push").exit_code == 0

    # newest first
    assert peer_ws.terminal("ws-git stash list").stdout == (
        "stash@{0}: base\nstash@{1}: wip auth\n"
    )

    # show diffs one against your head
    r = peer_ws.terminal("ws-git stash show stash@{1}")
    assert r.exit_code == 0, r.stderr
    assert "diff --git a/a.txt b/a.txt" in r.stdout
    assert "+edited" in r.stdout
    assert "diff --git a/new.txt b/new.txt" in r.stdout

    r = peer_ws.terminal("ws-git stash show")
    assert r.exit_code == 0, r.stderr
    assert "+again" in r.stdout


def test_stash_drop_leaves_the_tree_alone(peer_ws, store):
    _stash_base(peer_ws)
    peer_ws.terminal("ws-git stash")
    r = peer_ws.terminal("ws-git stash drop")
    assert r.exit_code == 0, r.stderr
    assert r.stdout == "Dropped stash@{0} (main.stash-0)\n"
    assert peer_ws.terminal("ws-git stash list").stdout == "(none)\n"
    assert "main.stash-0" not in store.sessions()
    assert peer_ws.terminal("cat a.txt").stdout == "one\n"


def test_stash_with_nothing_to_save(peer_ws):
    peer_ws.files.fs.write("/workspace/a.txt", b"one\n")
    peer_ws.terminal("ws-git commit -m base")
    r = peer_ws.terminal("ws-git stash")
    assert r.exit_code == 1
    assert r.stderr == "ws-git: No local changes to save"

    # and a session that has committed nothing has nowhere to go back to
    r = peer_ws.terminal("ws-git stash pop stash@{0}")
    assert r.exit_code == 1
    assert r.stderr == "ws-git: no stash@{0}: this session has no stashes"


def test_stash_pop_conflict_keeps_the_stash(peer_ws, store):
    peer_ws.files.fs.write("/workspace/doc.txt", b"a\nb\n")
    peer_ws.terminal("ws-git commit -m base")
    peer_ws.files.fs.write("/workspace/doc.txt", b"a\nSTASHED\n")
    assert peer_ws.terminal("ws-git stash").exit_code == 0

    peer_ws.files.fs.write("/workspace/doc.txt", b"a\nHERE\n")
    peer_ws.terminal("ws-git commit -m here")

    r = peer_ws.terminal("ws-git stash pop")
    assert r.exit_code == 1
    assert "CONFLICT (content): Merge conflict in doc.txt" in r.stdout
    assert "The stash is kept" in r.stderr
    assert "<<<<<<< " in peer_ws.terminal("cat doc.txt").stdout
    assert peer_ws.terminal("ws-git stash list").stdout == "stash@{0}: base\n"
    assert "main.stash-0" in store.sessions()


def test_stash_is_no_longer_refused(ws):
    from nontainer.wsgit import _EDGE

    assert "stash" not in _EDGE
    assert "no stash here" not in _HELP


def test_stash_push_refuses_over_an_outstanding_merge(peer_ws):
    """A conflicted merge is a composition in progress. Putting it
    aside would restore the merge commit — markers and all — while
    clearing the context that says the markers are there, so status
    would read clean over a tree full of them."""
    peer_ws.files.fs.write("/workspace/doc.txt", b"a\nb\n")
    peer_ws.terminal("ws-git commit -m base")
    fork = peer_ws.fork("worker")
    try:
        fork.files.fs.write("/workspace/doc.txt", b"a\nFORK\n")
        fork.index.commit("theirs")
    finally:
        fork.close()
    peer_ws.files.fs.write("/workspace/doc.txt", b"a\nMAIN\n")
    peer_ws.terminal("ws-git commit -m mine")
    assert peer_ws.terminal("ws-git merge worker").exit_code == 1
    was = peer_ws.terminal("ws-git status").stdout
    assert "UU doc.txt" in was

    # an edit to a marked file is what makes the tree look stashable
    peer_ws.files.fs.write("/workspace/doc.txt", b"a\n<<<<<<< here\nhalf\n")

    r = peer_ws.terminal("ws-git stash")
    assert r.exit_code == 1
    assert r.stderr.startswith(
        "ws-git: an unresolved merge from worker is outstanding"
    ), r.stderr
    assert "ws-git commit" in r.stderr
    assert "ws-git merge --abort" in r.stderr

    # nothing moved: the context stands and the markers are still there
    assert peer_ws.terminal("ws-git stash list").stdout == "(none)\n"
    assert "UU doc.txt" in peer_ws.terminal("ws-git status").stdout
    assert "<<<<<<< " in peer_ws.terminal("cat doc.txt").stdout


def test_stash_pop_refuses_over_an_outstanding_merge(peer_ws, store):
    """A pop is a merge, so it lands a second set of markers over the
    first and takes the first merge's context with it — after which
    neither status nor merge --abort can see the markers already in the
    tree."""
    peer_ws.files.fs.write("/workspace/doc.txt", b"a\nb\n")
    peer_ws.files.fs.write("/workspace/side.txt", b"side\n")
    peer_ws.terminal("ws-git commit -m base")
    peer_ws.files.fs.write("/workspace/side.txt", b"stashed\n")
    assert peer_ws.terminal("ws-git stash").exit_code == 0

    fork = peer_ws.fork("worker")
    try:
        fork.files.fs.write("/workspace/doc.txt", b"a\nFORK\n")
        fork.index.commit("theirs")
    finally:
        fork.close()
    peer_ws.files.fs.write("/workspace/doc.txt", b"a\nMAIN\n")
    peer_ws.terminal("ws-git commit -m mine")
    assert peer_ws.terminal("ws-git merge worker").exit_code == 1
    was = peer_ws.terminal("ws-git status").stdout
    assert "UU doc.txt" in was

    r = peer_ws.terminal("ws-git stash pop")
    assert r.exit_code == 1
    assert r.stderr.startswith(
        "ws-git: an unresolved merge from worker is outstanding"
    ), r.stderr
    assert "ws-git merge --abort" in r.stderr

    # the first merge's context is exactly as it was, and the stash
    # is still there to pop once it is resolved
    assert peer_ws.terminal("ws-git status").stdout == was
    assert peer_ws.terminal("ws-git stash list").stdout == "stash@{0}: base\n"
    assert "main.stash-0" in store.sessions()


def test_merge_refuses_over_an_outstanding_merge(peer_ws):
    """A second merge lands its own markers and records its own
    context, dropping the first merge's — after which status reads
    clean over a tree that still holds the first merge's markers, and
    merge --abort has nothing left to find."""
    peer_ws.files.fs.write("/workspace/doc.txt", b"a\nb\n")
    peer_ws.terminal("ws-git commit -m base")
    for name, body in (("worker", b"a\nFORK\n"), ("other", b"a\nb\nOTHER\n")):
        fork = peer_ws.fork(name)
        try:
            fork.files.fs.write("/workspace/doc.txt", body)
            fork.index.commit("theirs")
        finally:
            fork.close()
    peer_ws.files.fs.write("/workspace/doc.txt", b"a\nMAIN\n")
    peer_ws.terminal("ws-git commit -m mine")
    assert peer_ws.terminal("ws-git merge worker").exit_code == 1
    was = peer_ws.terminal("ws-git status").stdout
    assert "UU doc.txt" in was

    r = peer_ws.terminal("ws-git merge other")
    assert r.exit_code == 1
    assert r.stderr.startswith(
        "ws-git: an unresolved merge from worker is outstanding"
    ), r.stderr
    assert "ws-git merge --abort" in r.stderr

    # nothing moved: the first merge's context and markers stand
    assert peer_ws.terminal("ws-git status").stdout == was
    assert "<<<<<<< " in peer_ws.terminal("cat doc.txt").stdout


def test_checkout_refuses_over_an_outstanding_merge(peer_ws):
    """A checkout moves the tree somewhere else and clears the merge
    context with it, so the markers ride along with nothing left
    recording them — including a checkout of the merge commit itself,
    which restores the marked tree under a clean status."""
    peer_ws.files.fs.write("/workspace/doc.txt", b"a\nb\n")
    peer_ws.files.fs.write("/workspace/side.txt", b"side\n")
    peer_ws.terminal("ws-git commit -m base")
    base = peer_ws.index.head
    fork = peer_ws.fork("worker")
    try:
        fork.files.fs.write("/workspace/doc.txt", b"a\nFORK\n")
        fork.files.fs.write("/workspace/side.txt", b"worker\n")
        fork.index.commit("theirs")
    finally:
        fork.close()
    peer_ws.files.fs.write("/workspace/doc.txt", b"a\nMAIN\n")
    peer_ws.terminal("ws-git commit -m mine")
    assert peer_ws.terminal("ws-git merge worker").exit_code == 1
    merge = peer_ws.index.head
    was = peer_ws.terminal("ws-git status").stdout
    assert "UU doc.txt" in was

    for cmd in (
        f"ws-git checkout {merge[:7]}",
        f"ws-git checkout {base[:7]}",
        "ws-git checkout worker -- side.txt",
    ):
        r = peer_ws.terminal(cmd)
        assert r.exit_code == 1, (cmd, r.stdout)
        assert r.stderr.startswith(
            "ws-git: an unresolved merge from worker is outstanding"
        ), (cmd, r.stderr)
        assert "ws-git merge --abort" in r.stderr, cmd
        assert peer_ws.terminal("ws-git status").stdout == was, cmd

    # the way out, then the checkout the agent wanted
    assert peer_ws.terminal("ws-git merge --abort").exit_code == 0
    r = peer_ws.terminal(f"ws-git checkout {base[:7]}")
    assert r.exit_code == 0, r.stderr
    assert peer_ws.files.read("/workspace/doc.txt") == b"a\nb\n"


def test_index_checkout_refuses_over_an_outstanding_merge(peer_ws):
    """The host half of the agent's checkout takes the same rule."""
    peer_ws.files.fs.write("/workspace/doc.txt", b"a\nb\n")
    peer_ws.terminal("ws-git commit -m base")
    base = peer_ws.index.head
    fork = peer_ws.fork("worker")
    try:
        fork.files.fs.write("/workspace/doc.txt", b"a\nFORK\n")
        fork.index.commit("theirs")
    finally:
        fork.close()
    peer_ws.files.fs.write("/workspace/doc.txt", b"a\nMAIN\n")
    peer_ws.terminal("ws-git commit -m mine")
    assert peer_ws.terminal("ws-git merge worker").exit_code == 1

    from nontainer import WorkspaceError

    with pytest.raises(WorkspaceError, match="unresolved merge from worker"):
        peer_ws.index.checkout(base)


def test_an_unknown_session_in_a_ref_says_the_session_is_unknown(peer_ws):
    """A ref names a session and a commit, and the session is the half
    that is checked first: a typo in the name earned a report about the
    commit half, sending a reader to list the commits of a session that
    is not there."""
    peer_ws.files.fs.write("/workspace/a.txt", b"one\n")
    peer_ws.terminal("ws-git commit -m base")

    want = "ws-git: unknown session 'typo' (ws-git branch lists them)"
    for cmd in (
        "ws-git worktree add review typo@nosuch",
        "ws-git cherry-pick typo@nosuch",
        "ws-git worktree add review typo@deadbeef1234",
        "ws-git worktree add review typo",
    ):
        r = peer_ws.terminal(cmd)
        assert r.exit_code == 1, (cmd, r.stdout)
        assert r.stderr == want, (cmd, r.stderr)


def test_a_bad_ref_says_what_was_looked_up_and_where(peer_ws, store):
    """The commit half of a <session>@<x> ref is resolved in one place,
    so a word that is none of the three spellings earns one message
    naming the session it was looked for on — not the bare word."""
    peer_ws.files.fs.write("/workspace/a.txt", b"one\n")
    peer_ws.terminal("ws-git commit -m base")
    peer_ws.terminal("ws-git branch polish")

    want = (
        "ws-git: nosuch is not a commit, a short id or a tag on session "
        "'polish' (ws-git log polish, where its commits and its tags "
        "both show)"
    )
    for cmd in (
        "ws-git worktree add review polish@nosuch",
        "ws-git cherry-pick polish@nosuch",
    ):
        r = peer_ws.terminal(cmd)
        assert r.exit_code == 1, (cmd, r.stdout)
        assert r.stderr == want, (cmd, r.stderr)

    # this session's own: the log that lists its commits takes no name
    r = peer_ws.terminal("ws-git cherry-pick main@nosuch")
    assert r.stderr == (
        "ws-git: nosuch is not a commit, a short id or a tag on session "
        "'main' (ws-git log, ws-git tag)"
    ), r.stderr

    # a commit id shaped right but held by nobody names its session too
    r = peer_ws.terminal("ws-git worktree add review polish@deadbeef1234")
    assert r.exit_code == 1
    assert r.stderr == (
        "ws-git: no commit 'deadbeef1234' on session 'polish' "
        "(ws-git log polish lists what it holds)"
    ), r.stderr

    # and a real tag of that session still resolves
    other = store.open("polish")
    register_wsgit(other)
    try:
        other.files.fs.write("/workspace/a.txt", b"two\n")
        other.terminal("ws-git commit -m theirs")
        other.terminal("ws-git tag shipped")
    finally:
        other.close()
    assert peer_ws.terminal("ws-git worktree add review polish@shipped").exit_code == 0


def test_branch_at_takes_a_tag_and_a_short_id(peer_ws, store):
    """`--at` is a ref, so every spelling of one works there: what the
    log printed, and what the session bookmarked."""
    peer_ws.files.fs.write("/workspace/a.txt", b"one\n")
    peer_ws.terminal("ws-git commit -m first")
    first = peer_ws.index.head
    peer_ws.terminal("ws-git tag start")
    peer_ws.files.fs.write("/workspace/a.txt", b"two\n")
    peer_ws.terminal("ws-git commit -m second")

    for name, ref in (("by-tag", "start"), ("by-id", first[:7])):
        r = peer_ws.terminal(f"ws-git branch {name} --at {ref}")
        assert r.exit_code == 0, (ref, r.stderr)
        child = store.open(name)
        try:
            assert child.files.read("/workspace/a.txt") == b"one\n"
        finally:
            child.close()
