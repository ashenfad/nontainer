"""ws.apply: the change between two named states, landed here.

A merge's three-way with the base named: what changed from ``base`` to
``theirs`` lands on this tree as one commit in the agent's graph, and
``paths`` narrows it, which is how a vendored library upgrades with a
local fix kept.
"""

import pytest

from nontainer import NotSupportedError, Store, WorkspaceError
from nontainer.wsgit import register_wsgit

V1 = "def cell(x):\n    return x\n\n\ndef row(r):\n    return r\n"
V2 = "def cell(x):\n    return x.strip()\n\n\ndef row(r):\n    return r\n"


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "store")
    yield s
    s.close()


@pytest.fixture
def ws(store):
    w = store.open("main")
    register_wsgit(w)
    yield w
    w.close()


@pytest.fixture
def releases(store):
    """A library's two releases, tagged in the store, with a file
    outside the library that changes between them too."""
    lib = store.open("lib")
    lib.files.write("/workspace/skills/sheets/cells.py", V1)
    lib.files.write("/workspace/skills/sheets/SKILL.md", "# sheets\n")
    lib.files.write("/workspace/notes.md", "v1 notes\n")
    store.tags.add(lib, "sheets/v1")
    lib.files.write("/workspace/skills/sheets/cells.py", V2)
    lib.files.write("/workspace/skills/sheets/new.py", "NEW = 1\n")
    lib.files.remove("/workspace/skills/sheets/SKILL.md")
    lib.files.write("/workspace/notes.md", "v2 notes\n")
    store.tags.add(lib, "sheets/v2")
    yield
    lib.close()


def _vendor_v1(ws):
    ws.checkout("sheets/v1", paths=["skills/sheets"])
    ws.index.commit("vendor sheets v1")


def test_an_upgrade_brings_the_release_and_keeps_a_local_fix(ws, releases):
    _vendor_v1(ws)
    ws.files.write(
        "/workspace/skills/sheets/cells.py", V1.replace("return r", "return list(r)")
    )
    ws.index.commit("fix row")

    out = ws.apply("sheets/v1", "sheets/v2", paths="skills/sheets")

    assert out.merged and out.conflicts == ()
    cells = ws.files.read("/workspace/skills/sheets/cells.py").decode()
    assert "x.strip()" in cells and "list(r)" in cells
    assert ws.files.read("/workspace/skills/sheets/new.py") == b"NEW = 1\n"
    assert not ws.files.exists("/workspace/skills/sheets/SKILL.md")
    # only the paths named: the release's other change stays out
    assert not ws.files.exists("/workspace/notes.md")


def test_it_lands_in_the_agents_graph(ws, releases):
    _vendor_v1(ws)
    before = ws.index.head

    out = ws.apply("sheets/v1", "sheets/v2", paths=["skills/sheets"])

    assert ws.index.head == out.commit
    assert [e.id for e in ws.index.log()][:2] == [out.commit, before]
    entry = next(e for e in ws.log(kind="agent") if e.id == out.commit)
    assert entry.info["tool"] == "ws-git.apply"
    assert entry.info["message"] == "Apply sheets/v1..sheets/v2"
    assert entry.info["paths"] == ["/workspace/skills/sheets"]
    log = ws.terminal("ws-git log").stdout
    assert log.splitlines()[0].endswith("Apply sheets/v1..sheets/v2")
    assert ws.terminal("ws-git status").stdout == ""


def test_from_nothing_it_brings_everything_theirs_holds(ws, releases):
    out = ws.apply(None, "sheets/v1", paths="skills/sheets", message="install sheets")

    assert out.merged
    assert ws.files.read("/workspace/skills/sheets/cells.py").decode() == V1
    assert not ws.files.exists("/workspace/notes.md")
    entry = next(e for e in ws.log(kind="agent") if e.id == out.commit)
    assert entry.info["message"] == "install sheets"
    assert entry.info["applied"] == [None, entry.info["applied"][1]]


def test_overlapping_work_lands_with_markers(ws, releases):
    _vendor_v1(ws)
    ws.files.write(
        "/workspace/skills/sheets/cells.py",
        V1.replace("return x\n", "return x.upper()\n"),
    )
    ws.index.commit("my cell")

    out = ws.apply("sheets/v1", "sheets/v2", paths="skills/sheets")

    assert out.merged
    assert out.conflicts == ("/workspace/skills/sheets/cells.py",)
    assert b"<<<<<<< " in ws.files.read("/workspace/skills/sheets/cells.py")
    assert ws.index.status().merge_unresolved == ("/workspace/skills/sheets/cells.py",)


def test_a_change_already_here_commits_nothing(ws, releases):
    _vendor_v1(ws)
    ws.apply("sheets/v1", "sheets/v2", paths="skills/sheets")
    head = ws.head

    out = ws.apply("sheets/v1", "sheets/v2", paths="skills/sheets")

    assert not out.merged and out.commit is None
    assert ws.head == head


def test_sessions_and_their_commits_are_states_too(ws, store):
    other = store.open("other")
    other.files.write("/workspace/a.txt", "one\n")
    first = other.head
    other.files.write("/workspace/a.txt", "two\n")
    other.files.write("/workspace/b.txt", "b\n")
    other.close()
    ws.files.write("/workspace/a.txt", "one\n")
    ws.index.commit("a")

    out = ws.apply(f"other@{first[:7]}", "other", paths="a.txt")

    assert out.merged
    assert ws.files.read("/workspace/a.txt") == b"two\n"
    assert not ws.files.exists("/workspace/b.txt")


def test_refusals(ws, releases):
    with pytest.raises(ValueError, match="at least one side"):
        ws.apply(None, None)
    _vendor_v1(ws)
    ws.files.write("/workspace/x.txt", "x")
    with pytest.raises(WorkspaceError, match="uncommitted ws-git work"):
        ws.apply("sheets/v1", "sheets/v2")


def test_a_provider_without_merge_says_so(tmp_path):
    from plain_provider import PlainProvider

    from nontainer import Workspace

    ws = Workspace(PlainProvider(tmp_path / "ws", session="dir"))
    try:
        with pytest.raises(NotSupportedError, match="cannot apply"):
            ws.apply("a", "b")
    finally:
        ws.close()


# -- a narrowed view -----------------------------------------------------------


def _narrowed(ws, paths):
    child = ws.fork("child", paths=paths)
    register_wsgit(child)
    return child


@pytest.mark.parametrize("paths", [None, "/workspace"])
def test_a_hidden_file_the_change_touches_is_refused(ws, releases, paths):
    ws.checkout("sheets/v1", paths=["skills/sheets", "notes.md"])
    ws.index.commit("vendor sheets v1, with its notes")
    child = _narrowed(ws, ["skills/sheets"])
    try:
        head = child.head
        with pytest.raises(PermissionError, match="outside this session's view"):
            child.apply("sheets/v1", "sheets/v2", paths=paths)
        # nothing of it landed, in the view or out of it
        assert child.head == head
        assert child.files.read("/workspace/skills/sheets/cells.py").decode() == V1
        assert child._provider.fs.read("/workspace/notes.md") == b"v1 notes\n"
        assert not child._provider.dirty
    finally:
        child.close()


def test_a_file_the_change_creates_joins_the_view(ws, store, releases):
    _vendor_v1(ws)
    child = _narrowed(ws, ["skills/sheets"])
    try:
        out = child.apply(None, "sheets/v1")
        assert out.merged
        assert child.files.read("/workspace/notes.md") == b"v1 notes\n"
        assert not child._provider.dirty
    finally:
        child.close()
    again = store.open("child")
    try:
        assert again.files.read("/workspace/notes.md") == b"v1 notes\n"
    finally:
        again.close()


def test_a_cherry_pick_is_held_to_the_view_too(ws, store):
    ws.files.write("/workspace/mine/a.txt", "a\n")
    ws.files.write("/workspace/theirs.txt", "one\n")
    ws.index.commit("seed")
    other = ws.fork("other")
    other.files.write("/workspace/theirs.txt", "two\n")
    picked = other.head
    other.close()
    child = _narrowed(ws, ["mine"])
    try:
        head = child.head
        with pytest.raises(PermissionError, match="outside this session's view"):
            child.cherry_pick(f"other@{picked[:7]}")
        assert child.head == head
    finally:
        child.close()
