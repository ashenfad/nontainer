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

from nontainer import Workspace
from nontainer.providers import KvgitProvider
from nontainer.wsgit import (
    _HELP,
    _SUPPORTED,
    _USAGE,
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
    assert any(e.info["tool"] == "ws-git.restore" for e in ws.log())
    body = ws.terminal("ws-git show HEAD").stdout
    assert "just a" in body
    assert "a/a.txt" in body and "b.txt" not in body


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


def test_edges_name_what_the_agent_can_do(ws):
    cases = [
        ("ws-git stash", "ws-git: no stash here — a fork IS a stash"),
        ("ws-git rebase", "ws-git: no rebase here — history is append-only"),
    ]
    for cmd, prefix in cases:
        r = ws.terminal(cmd)
        assert r.exit_code == 1, cmd
        assert r.stderr.startswith(prefix), (cmd, r.stderr)
    # Every hint names a terminal verb or says plainly whose job it
    # is — never host Python the agent cannot reach.
    from nontainer.wsgit import _EDGE

    for verb, text in _EDGE.items():
        assert "ws.fork(" not in text and "provider." not in text, verb
        assert "ws-git" in text or "host's to invoke" in text, verb
    # branch and merge are the agent's now, not refusals
    assert ws.terminal("ws-git branch").exit_code == 0
    assert ws.terminal("ws-git merge").exit_code == 2

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
            assert fork.terminal("ws-git status").stdout == "M  kid.txt\n"
            assert w.terminal("ws-git status").stdout == ""
            assert fork.terminal('ws-git commit -m "kid work"').exit_code == 0
            assert _subjects(w) == ["base"]
            assert _subjects(fork) == ["kid work", "base"]
            assert fork.terminal("ws-git status").stdout == ""
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
