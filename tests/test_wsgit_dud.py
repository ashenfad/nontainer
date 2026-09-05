"""ws-git on the dud rung: cross-rung conformance corpus (PR 4).

The same verbs through the guest shell function must read byte-for-byte
like the local builtin — same command closure, same goldens. Anything
that diverges here is a rung bug, not a dialect.

Requires the ``dud`` extra; skipped when it isn't installed. Pins
``backend="subprocess"`` explicitly (the only rung without a
hypervisor); the VM rungs share the guest supervisor paths, and
``DUD_BACKEND=vfkit`` conformance is the proof they carry them.
"""

import re

import pytest

from nontainer import Workspace
from nontainer.providers import KvgitProvider
from nontainer.wsgit import DudHostHandler, register_wsgit

pytest.importorskip("dud")

from nontainer.executor_dud import DudExecutor  # noqa: E402

SHORT = r"[0-9a-f]{7}"


@pytest.fixture
def ws():
    """Dud-backed kvgit workspace with ws-git registered."""
    provider = KvgitProvider.open(None, session="wsgit-dud")
    w = Workspace(provider, executor=DudExecutor(backend="subprocess"))
    register_wsgit(w)
    try:
        yield w
    finally:
        w.close()


@pytest.fixture
def bare_ws():
    """Dud-backed workspace WITHOUT ws-git registered."""
    provider = KvgitProvider.open(None, session="wsgit-bare")
    w = Workspace(provider, executor=DudExecutor(backend="subprocess"))
    try:
        yield w
    finally:
        w.close()


def _subjects(w):
    out = []
    for line in w.terminal("ws-git log").stdout.splitlines():
        head, _, subject = line.partition(" ")
        assert re.fullmatch(SHORT, head), line
        out.append(subject)
    return out


def test_dud_stage_first_composition(ws):
    ws.fs.write("/workspace/a.txt", b"one\n")
    ws.fs.write("/workspace/b.txt", b"two\n")
    before = len(list(ws.history()))

    r = ws.terminal("ws-git stage a.txt b.txt")
    assert r.exit_code == 0
    assert (
        r.stdout == "suspended autocheckpoint (resume: ws-git commit, ws-git reset)\n"
    )

    # The edit runs in the guest; suspension holds across the boundary.
    ws.terminal("echo second >> a.txt")
    assert len(list(ws.history())) == before

    assert ws.terminal("ws-git status").stdout == "M  a.txt\nM  b.txt\n"
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

    r = ws.terminal('ws-git commit -m "compose a"')
    assert r.exit_code == 0
    assert re.fullmatch(rf"\[wsgit-dud {SHORT}\] compose a \(2 files\)\n", r.stdout)

    assert len(list(ws.history())) == before + 1
    assert ws.terminal("ws-git status").stdout == ""
    assert _subjects(ws) == ["compose a", "init", "?"]


def test_dud_unregistered_name_absent(bare_ws):
    """No registration, no function: real-bash 127, like local's."""
    r = bare_ws.terminal("ws-git status")
    assert r.exit_code == 127
    assert "ws-git: command not found" in r.stdout


def test_dud_handler_refuses_unregistered_direct_call(bare_ws):
    handler = bare_ws._executor._live["ws_git"]
    assert isinstance(handler, DudHostHandler)
    out = handler.run("/workspace", "status")
    assert out == {
        "stdout": "",
        "stderr": "ws-git: not registered on this workspace",
        "exit_code": 1,
    }


def test_dud_handler_fronts_framework_only(ws):
    """A user's own ws-git command stays a local-rung creature: the
    guest function is not prepended for it, and the handler refuses
    to front it on a direct call."""

    def custom(ctx):
        return None

    ws._commands["ws-git"] = custom
    try:
        r = ws.terminal("ws-git status")
        assert r.exit_code == 127
        handler = ws._executor._live["ws_git"]
        out = handler.run("/workspace", "status")
        assert out["exit_code"] == 1
        assert "custom command owns that name" in out["stderr"]
    finally:
        del ws._commands["ws-git"]
        register_wsgit(ws)


def test_dud_cwd_relative_staging(ws):
    ws.fs.write("/workspace/sub/f.txt", b"one\n")
    r = ws.terminal("cd sub; ws-git stage f.txt")
    assert r.exit_code == 0
    assert "suspended autocheckpoint" in r.stdout
    assert ws.terminal("ws-git status").stdout == "M  sub/f.txt\n"


def test_dud_merge_status_and_check(ws):
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

    r = ws.terminal("ws-git status")
    lines = r.stdout.splitlines()
    assert re.fullmatch(rf"## merging worker@{SHORT} \(1 unresolved\)", lines[0]), lines
    assert lines[1:] == ["UU doc.txt"]
    r = ws.terminal("ws-git diff --check")
    assert r.exit_code == 2
    assert all(
        line.startswith("doc.txt:") and line.endswith(": leftover conflict marker")
        for line in r.stdout.splitlines()
    )
    assert r.stdout.strip() != ""


def test_dud_edges_match_local(ws):
    # Byte parity modulo the documented dud delta: stderr merges into
    # the transcript (TerminalResult.stderr stays empty), so the corpus
    # asserts the same text one stream over. Exit codes carry untouched.
    for cmd, prefix, code in [
        ("ws-git stash", "ws-git: no stash here", 1),
        ("ws-git frobnicate", "ws-git: 'frobnicate' is not a ws-git command", 2),
        ("ws-git commit -a", "ws-git: commit stages nothing itself (no -a)", 2),
        ("ws-git status --short", "ws-git: status takes no '--short'", 2),
    ]:
        r = ws.terminal(cmd)
        assert r.exit_code == code, cmd
        assert r.stderr == "", (cmd, r.stderr)
        assert r.stdout.startswith(prefix), (cmd, r.stdout)


def test_guest_to_host_mapping(ws):
    ex = ws._executor
    work = ex._work
    assert work, "subprocess ping must report a workspace"
    assert ex._guest_to_host(f"{work}/sub/f.txt") == "/workspace/sub/f.txt"
    assert ex._guest_to_host(work) == "/workspace"
    assert ex._guest_to_host("/elsewhere/x.txt") is None
    # A sibling sharing the string prefix is outside, not inside.
    assert ex._guest_to_host(f"{work}-other/f.txt") is None
    assert ex._guest_to_host(f"{work}-other") is None


def test_mid_call_absorb_failure_rolls_back_and_flags_repush(tmp_path, monkeypatch):
    """A mid-call harvest the provider refuses must neither checkpoint
    partials nor poison the guest: the verb reads errored, the call
    unwinds like a torn call, history holds, and the guest is flagged
    for rematerialization on the next call.

    The harvest shape is supplied directly: the subprocess rung runs
    against host paths, so it cannot see workspace mounts guest-side
    (a VM rung materializes them via the tree push — that end-to-end
    half is VM-only). What this pins is the unwind/stale/pending
    plumbing from the refused StagedDiff onward.
    """
    from nontainer import Mount
    from nontainer.executor import StagedDiff

    src = tmp_path / "ro"
    src.mkdir()
    provider = KvgitProvider.open(None, session=f"wsgit-ro-{tmp_path.name}")
    w = Workspace(
        provider,
        executor=DudExecutor(backend="subprocess"),
        mounts={"/data": Mount(src)},
    )
    register_wsgit(w)
    try:
        before = len(list(w.history()))
        calls = []

        def fake_diff():
            calls.append(1)
            if len(calls) == 1:
                # One appliable write, then the mount refusal.
                return StagedDiff(
                    writes={"workspace/ok.txt": b"ok\n", "data/evil.txt": b"x"},
                    deletes=(),
                )
            return None

        monkeypatch.setattr(w._executor, "diff", fake_diff)
        r = w.terminal("ws-git status")
        assert r.exit_code != 0
        # The verb's triple rides the merged dud transcript...
        assert "mid-call sync failed" in r.stdout
        # ...while the outer unwind lands on the result like a torn call.
        assert "rolled back" in (r.stderr or "")
        assert len(list(w.history())) == before
        assert not w.fs.exists("/workspace/ok.txt")
        assert not (src / "evil.txt").exists()
        # Guest flagged for rematerialization; the next call re-syncs.
        assert w._executor_stale is True
        assert w.terminal("ws-git status").exit_code == 0
        assert w._executor_stale is False
    finally:
        w.close()


def test_plain_data_ws_git_name_refused():
    """A plain-data host object named ``ws_git`` fails closed like a
    live one: it rides ``_plain`` past a live-only check, so the guard
    reads the pre-split config names."""
    from nontainer import PythonConfig

    provider = KvgitProvider.open(None, session="wsgit-collide")
    with pytest.raises(ValueError, match="Reserved host object name"):
        Workspace(
            provider,
            executor=DudExecutor(backend="subprocess"),
            python=PythonConfig(host_objects={"ws_git": {"key": "value"}}),
        )


def test_map_argv_leaves_message_text_alone():
    from nontainer.wsgit import DudHostHandler as H

    assert H._map_argv(None, ["commit", "-m", "hi"]) == ["commit", "-m", "hi"]
    assert H._map_argv(lambda p: "/HOST" + p, ["stage", "/workspace/a.txt"]) == [
        "stage",
        "/HOST/workspace/a.txt",
    ]
    assert H._map_argv(lambda p: "/HOST" + p, ["commit", "-m", "/not/a/path"]) == [
        "commit",
        "-m",
        "/not/a/path",
    ]
    assert H._map_argv(lambda p: None, ["stage", "/workspace/a.txt"]) == [
        "stage",
        "/workspace/a.txt",
    ]
