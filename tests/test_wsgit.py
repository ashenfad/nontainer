"""ws-git terminal builtin: git-shaped staging over the index.

End-to-end through ``ws.terminal`` (the agent's path), pinning the
output shapes that become the cross-rung conformance corpus in PR 4.
Commit hashes are shape-pinned (``[0-9a-f]{7}``); everything else is
byte-pinned. Flat trees only for exact log goldens: directory-row
mtime drift can leave real table dirt that the terminal tail
checkpoints, which is provider behavior (PR 2), not terminal shape.
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
    r = ws.terminal("ws-git status")
    assert r.exit_code == 0
    assert r.stdout == ""
    assert r.stderr == ""


def test_stage_first_composition(ws):
    ws.fs.write("/workspace/a.txt", b"one\n")
    ws.fs.write("/workspace/b.txt", b"two\n")
    before = len(list(ws.history()))

    # First stage suspends autocheckpoint, stated aloud.
    r = ws.terminal("ws-git stage a.txt b.txt")
    assert r.exit_code == 0
    assert (
        r.stdout == "suspended autocheckpoint (resume: ws-git commit, ws-git reset)\n"
    )

    # Composition mints zero commits: the edit checkpoints nothing.
    ws.terminal("echo second >> a.txt")
    assert len(list(ws.history())) == before

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

    # One selective commit, nothing else: flat tree, no janitor.
    assert len(list(ws.history())) == before + 1
    assert ws.terminal("ws-git status").stdout == ""
    assert _subjects(ws) == ["compose a", "init", "?"]
    assert re.fullmatch(rf"{SHORT} compose a\n", ws.terminal("ws-git log -n 1").stdout)


def test_unstaged_diff_and_status_columns(ws):
    # Each read-only call's own tail checkpoints unsuspended dirt, so
    # every golden re-dirties with a fresh file first.
    ws.fs.write("/workspace/a.txt", b"one\n")
    assert ws.terminal("ws-git status").stdout == " M a.txt\n"
    ws.fs.write("/workspace/b.txt", b"two\n")
    assert ws.terminal("ws-git diff").stdout == (
        "diff --git a/b.txt b/b.txt\n--- a/b.txt\n+++ b/b.txt\n@@ -0,0 +1 @@\n+two\n"
    )
    ws.fs.write("/workspace/c.txt", b"three\n")
    assert ws.terminal("ws-git diff --cached").stdout == ""
    ws.fs.write("/workspace/d.txt", b"four\n")
    assert ws.terminal("ws-git diff d.txt").stdout.startswith("diff --git a/d.txt")
    assert ws.terminal("ws-git diff b.txt").stdout == ""


def test_diff_trailing_newline_change(ws):
    ws.fs.write("/workspace/e.txt", b"a\n")
    ws.checkpoint()
    ws.fs.write("/workspace/e.txt", b"a")
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
    ws.fs.write("/workspace/m.txt", b"<<<<<<< HEAD\nx\n")
    ws.terminal("ws-git stage m.txt")
    ws.fs.write("/workspace/u.txt", b"y\n=======\n")
    r = ws.terminal("ws-git diff --check")
    assert r.exit_code == 2
    assert r.stdout == "u.txt:2: leftover conflict marker\n"
    r = ws.terminal("ws-git diff --cached --check")
    assert r.exit_code == 2
    assert r.stdout == "m.txt:1: leftover conflict marker\n"


def test_unstage_last_resumes(ws):
    ws.fs.write("/workspace/a.txt", b"one\n")
    ws.terminal("ws-git stage a.txt")
    r = ws.terminal("ws-git unstage a.txt")
    assert r.exit_code == 0
    assert r.stdout == "resumed autocheckpoint\n"
    # Emptying the index resumed autocheckpoint, so the unstage call's
    # own tail checkpoint swept the file: clean again.
    assert ws.terminal("ws-git status").stdout == ""


def test_reset_abandons_index_not_tree(ws):
    ws.fs.write("/workspace/a.txt", b"one\n")
    ws.terminal("ws-git stage a.txt")
    ws.terminal("echo second >> a.txt")
    r = ws.terminal("ws-git reset")
    assert r.exit_code == 0
    assert r.stdout == "resumed autocheckpoint\n"
    # Resuming re-arms the tail checkpoint, so the reset call itself
    # sweeps the abandoned tree dirt: clean, one terminal commit.
    assert ws.terminal("ws-git status").stdout == ""
    assert _subjects(ws)[0] == "terminal"


def test_commit_nothing_staged(ws):
    r = ws.terminal("ws-git commit")
    assert r.exit_code == 1
    assert r.stderr == "ws-git: nothing staged to commit"


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
    ws.fs.write("/workspace/sub/f.txt", b"one\n")
    r = ws.terminal("cd sub; ws-git stage f.txt")
    assert r.exit_code == 0
    assert "suspended autocheckpoint" in r.stdout
    assert ws.terminal("ws-git status").stdout == "M  sub/f.txt\n"


def test_merge_status_and_diff_check(ws):
    ws.fs.write("/workspace/doc.txt", b"a\nb\n")
    ws.checkpoint()
    fork = ws.fork("worker")
    try:
        fork.fs.write("/workspace/doc.txt", b"a\nFORK\n")
        fork.checkpoint()
        ws.fs.write("/workspace/doc.txt", b"a\nMAIN\n")
        ws.checkpoint()
        out = ws._provider.merge("worker")
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


def test_edges_name_the_native_alternative(ws):
    cases = [
        ("ws-git stash", "ws-git: no stash here — a fork is a stash"),
        ("ws-git branch", "ws-git: no branches here — sessions are branches"),
        ("ws-git checkout", "ws-git: no checkout here — fork at a tag"),
        ("ws-git rebase", "ws-git: no rebase here — history is append-only"),
        ("ws-git merge", "ws-git: merge lives in Python for now"),
    ]
    for cmd, prefix in cases:
        r = ws.terminal(cmd)
        assert r.exit_code == 1, cmd
        assert r.stderr.startswith(prefix), (cmd, r.stderr)

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
    assert "Compose\nstage-first" in r.stdout


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
            "use the kvgit backend for staged mode."
        )
    finally:
        w.close()


def test_register_gated_on_supports_commands():
    seen = []

    class FakeWs:
        supports_commands = True
        _provider = object()

        def register_command(self, name, fn):
            seen.append((name, fn))

    register_wsgit(FakeWs())
    assert [name for name, _ in seen] == ["ws-git"]

    class DeafWs(FakeWs):
        supports_commands = False

    seen.clear()
    register_wsgit(DeafWs())
    assert seen == []


def test_command_closure_direct_status_shape():
    """The closure renders provider status without a shell round-trip."""
    provider = KvgitProvider.open(None, session="direct")
    w = Workspace(provider)
    fn = make_wsgit_command(w)
    try:
        w.fs.write("/workspace/a.txt", b"one\n")
        provider.stage(["/workspace/a.txt"])

        class Ctx:
            def __init__(self, args):
                self.args = args
                self.stdout = io.StringIO()
                self.fs = w.fs

        ctx = Ctx(["status"])
        assert fn(ctx) is None
        assert ctx.stdout.getvalue() == "M  a.txt\n"
    finally:
        w.close()
