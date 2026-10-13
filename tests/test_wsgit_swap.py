"""ws-git across a directory/file swap (#246): a path that is a file in
one state and a directory in another.

A tree holds `x` or `x/...`, never both. A restore that writes the file
`x` where the tree has the directory `x` has to clear what is left of
the directory first (only directories: their files went with the
restore's removals), and a restore that fails partway has to leave the
tree as it found it.
"""

import errno

import pytest
from monkeyfs import VirtualFS

from nontainer import Store
from nontainer.agentgit import AgentGit, _put
from nontainer.providers import KvgitProvider
from nontainer.workspace import Workspace
from nontainer.wsgit import register_wsgit


@pytest.fixture
def ws():
    w = Workspace(KvgitProvider.open(None, session="swap"))
    register_wsgit(w)
    yield w
    w.close()


def tree(ws):
    """Every file and directory under the root, as `find` lists them."""
    return ws.terminal("find .").stdout.split()


def status(ws):
    return ws.terminal("ws-git status -s").stdout


def file_then_dir(ws):
    """`x` committed as a file, then a directory holding a file and an
    empty directory made with mkdir, committed too. Returns the first
    commit."""
    ws.files.write("x", "file\n")
    first = ws.index.commit("x is a file")
    ws.files.remove("x")
    assert ws.terminal("mkdir -p x/empty && echo c > x/c.txt")
    ws.index.commit("x is a directory")
    return first


def test_a_checkout_from_a_directory_back_to_a_file_clears_the_directory(ws):
    first = file_then_dir(ws)
    ws.index.checkout(first)
    assert tree(ws) == ["x"]
    assert ws.files.read("x") == b"file\n"
    assert status(ws) == ""


def test_the_verb_clears_it_too(ws):
    first = file_then_dir(ws)
    r = ws.terminal(f"ws-git checkout {first[:7]}")
    assert r, r.stderr
    assert tree(ws) == ["x"]


def test_a_checkout_from_a_file_to_a_deep_directory(ws):
    assert ws.terminal("mkdir -p x/a/b && echo deep > x/a/b/c.txt")
    first = ws.index.commit("x is a directory")
    assert ws.terminal("rm -r x")
    ws.files.write("x", "file\n")
    ws.index.commit("x is a file")
    ws.index.checkout(first)
    assert tree(ws) == ["x", "x/a", "x/a/b", "x/a/b/c.txt"]
    assert status(ws) == ""


def test_a_checkout_through_a_filesystem_that_refuses_a_write_over_a_directory(
    ws, monkeypatch
):
    """What monkeyfs#34 asks VirtualFS for: a write over a directory
    raises, as on a real filesystem. The checkout clears the way first,
    so it never asks for one."""
    write = VirtualFS.write

    def strict(self, path, *args, **kwargs):
        if self.isdir(path):
            raise IsADirectoryError(errno.EISDIR, "Is a directory", path)
        return write(self, path, *args, **kwargs)

    first = file_then_dir(ws)
    monkeypatch.setattr(VirtualFS, "write", strict)
    ws.index.checkout(first)
    assert tree(ws) == ["x"]


@pytest.mark.parametrize("fail", ["at the commit", "after the rewrite"])
def test_a_checkout_that_fails_leaves_the_tree_and_the_index_as_they_were(
    ws, monkeypatch, fail
):
    first = file_then_dir(ws)
    ws.files.write("y", "uncommitted\n")
    head, before = ws.index.head, tree(ws)
    if fail == "at the commit":

        def broken(self, *args, **kwargs):
            raise RuntimeError("the store refused the commit")

        monkeypatch.setattr(AgentGit, "_record", broken)
    else:
        # The write of `x` fails once its directory has been cleared:
        # partway through the rewrite.
        write, failed = VirtualFS.write, []

        def flaky(self, path, *args, **kwargs):
            if path.endswith("workspace/x") and not failed:
                failed.append(path)
                raise OSError("the write failed")
            return write(self, path, *args, **kwargs)

        monkeypatch.setattr(VirtualFS, "write", flaky)
    with pytest.raises((RuntimeError, OSError)):
        ws.index.checkout(first)
    monkeypatch.undo()
    assert tree(ws) == before  # x/empty included
    assert ws.files.read("x/c.txt") == b"c\n"
    assert ws.files.read("y") == b"uncommitted\n"
    assert ws.index.head == head
    assert status(ws) == " A y\n"


@pytest.mark.parametrize("to", ["dir", "file"])
def test_a_harvest_that_swaps_lands_its_deletes_first(to):
    """What a guest rung hands back after the agent's shell swapped a
    file for a directory (or back): deletes and writes of the same
    path's two shapes. Writes first could not land either way."""
    from nontainer.executor import _apply_diff

    fs = VirtualFS({})
    if to == "dir":
        fs.write("/x", b"file")
        _apply_diff(fs, {"x/c.txt": b"c"}, ["x"])
        assert fs.isdir("/x") and fs.read("/x/c.txt") == b"c"
    else:
        fs.write("/x/c.txt", b"c")
        _apply_diff(fs, {"x": b"file"}, ["x/c.txt"])
        assert fs.list("/", recursive=True) == ["x"] and fs.read("/x") == b"file"


def test_put_refuses_a_directory_that_still_holds_a_file():
    fs = VirtualFS({})
    fs.makedirs("x/sub")
    fs.write("x/sub/f", b"1")
    with pytest.raises(IsADirectoryError, match="holding 1 file"):
        _put(fs, "x", b"file")
    assert fs.isdir("x") and fs.read("x/sub/f") == b"1"


def test_put_clears_only_directories_and_says_which():
    fs = VirtualFS({})
    fs.makedirs("x/a/b")
    fs.makedirs("x/c")
    cleared = []
    _put(fs, "x", b"file", cleared)
    assert cleared == ["x/a/b", "x/a", "x/c", "x"]
    assert fs.isfile("x") and fs.list("/", recursive=True) == ["x"]


# -- a partial commit: the paths it leaves out go back around it ------------


def swapped(ws, *, to):
    """A base commit with `keep.txt` and `x` (a file, or a directory with
    a file), then the swap and an edit to `keep.txt`, uncommitted."""
    if to == "dir":
        ws.files.write("x", "f\n")
    else:
        ws.files.write("x/c.txt", "c\n")
    ws.files.write("keep.txt", "1\n")
    ws.index.commit("base")
    if to == "dir":
        ws.files.remove("x")
        assert ws.terminal("mkdir -p x/empty && echo c > x/c.txt")
    else:
        assert ws.terminal("rm -r x")
        ws.files.write("x", "f\n")
    ws.files.write("keep.txt", "2\n")


def committed(ws):
    return sorted(ws._provider.files_at(ws.index.head))


@pytest.mark.parametrize(
    ("to", "stage", "files"),
    [
        # Staging one side of the swap takes the other with it, as
        # `git add` does: the commit holds `x` or `x/...`, not both.
        ("dir", "x/c.txt", ["/workspace/keep.txt", "/workspace/x/c.txt"]),
        ("file", "x", ["/workspace/keep.txt", "/workspace/x"]),
    ],
)
def test_staging_one_side_of_a_swap_commits_the_swap(ws, to, stage, files):
    swapped(ws, to=to)
    before = tree(ws)
    r = ws.terminal(f"ws-git add {stage} && ws-git commit -m swap")
    assert r, r.stderr
    assert committed(ws) == files
    assert tree(ws) == before
    assert status(ws) == " M keep.txt\n"


@pytest.mark.parametrize(
    ("to", "files", "left"),
    [
        ("dir", ["/workspace/keep.txt", "/workspace/x"], " D x\n A x/c.txt\n"),
        ("file", ["/workspace/keep.txt", "/workspace/x/c.txt"], " A x\n D x/c.txt\n"),
    ],
)
def test_a_swap_left_out_of_a_commit_stays_in_the_tree(ws, to, files, left):
    swapped(ws, to=to)
    before = tree(ws)
    r = ws.terminal("ws-git add keep.txt && ws-git commit -m keep")
    assert r, r.stderr
    assert committed(ws) == files
    assert tree(ws) == before  # x/empty included
    assert status(ws) == left


def test_stash_push_across_a_swap():
    """The stash branch gets the swap written onto it (its removals
    first), and the session goes back to its head."""
    store = Store(memory=True)
    w = store.open("s")
    register_wsgit(w)
    try:
        swapped(w, to="file")
        r = w.terminal("ws-git stash")
        assert r, r.stderr
        assert tree(w) == ["keep.txt", "x", "x/c.txt"]
        assert w.files.read("keep.txt") == b"1\n"
        stash = store.open("s.stash-0")
        try:
            stashed = sorted(stash.provider.working_files())
        finally:
            stash.close()
        assert stashed == ["/workspace/keep.txt", "/workspace/x"]
    finally:
        w.close()
        store.close()
