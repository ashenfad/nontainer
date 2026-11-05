"""A mounted session's cwd is stored like an unmounted one's (#310).

The composition keeps its cwd under the session's own cwd key, so a
commit records it and every way of reading that commit back (a reopen,
a frozen read, a fork, a land) starts from it. Mount contents stay
outside the store; only the path is kept.
"""

import pytest

from nontainer import Mount, Store

CWD = "__vfs_cwd__"


@pytest.fixture
def reference(tmp_path):
    ref = tmp_path / "reference"
    (ref / "sub").mkdir(parents=True)
    (ref / "sub" / "notes.txt").write_text("from the mount\n")
    return ref


@pytest.fixture
def store(tmp_path):
    st = Store(tmp_path / "store")
    yield st
    st.close()


def _mounts(reference):
    return {"mounts": {"/workspace/reference": Mount(reference, readonly=True)}}


@pytest.mark.parametrize("mounted", [False, True], ids=["unmounted", "mounted"])
def test_the_cwd_round_trips_with_or_without_a_mount(store, reference, mounted):
    settings = _mounts(reference) if mounted else {}
    ws = store.open("demo", autocommit=False, **settings)
    try:
        assert ws.terminal("mkdir -p work && cd work && echo hi > here.txt")
        dispatch = ws.commit({"tool": "dispatch"})
        assert ws.files.read("/workspace/work/here.txt") == b"hi\n"
        assert ws.provider.kv.get(CWD) == "/workspace/work"

        with store.resolve(ws.ref) as frozen:
            assert frozen.files.fs.getcwd() == "/workspace/work"
            assert frozen.terminal("cat here.txt").stdout == "hi\n"
        with store.fork("demo", "task", at=dispatch, **settings) as child:
            assert child.files.fs.getcwd() == "/workspace/work"
            assert child.terminal("cat here.txt").stdout == "hi\n"
    finally:
        ws.close()

    with store.open("demo", **settings) as again:
        assert again.files.fs.getcwd() == "/workspace/work"
        r = again.run_python("text = open('here.txt').read()")
        assert r, r.error
        assert r.namespace["text"] == "hi\n"
        assert not again.uncommitted


def test_relative_paths_name_the_same_files_from_any_cwd(store, reference):
    """The filesystem underneath no longer sits at the root, so a
    relative path through the composition must not be resolved twice."""
    ws = store.open("demo", **_mounts(reference))
    try:
        assert ws.terminal("mkdir -p a/b && cd a && echo x > b/f.txt")
        assert ws.files.read("/workspace/a/b/f.txt") == b"x\n"
        assert not ws.files.exists("/workspace/a/a")
        assert ws.terminal("cd b && cat f.txt && cat ../../reference/sub/notes.txt")
        assert ws.files.fs.getcwd() == "/workspace/a/b"
        r = ws.run_python("import os; here = os.getcwd(); os.listdir('.')")
        assert r, r.error
        assert r.namespace["here"] == "/workspace/a/b"
    finally:
        ws.close()


def test_a_cwd_inside_a_mount_returns_with_the_mount(store, reference):
    ws = store.open("demo", **_mounts(reference))
    try:
        assert ws.terminal("cd reference/sub && cat notes.txt")
        assert ws.files.fs.getcwd() == "/workspace/reference/sub"
        assert not ws.terminal("echo no > new.txt")  # read-only stays so
        assert not (reference / "sub" / "new.txt").exists()
        ws.commit({"tool": "test"})
        head = ws.provider.head
    finally:
        ws.close()

    # The path is stored, not what the mount held.
    with store.resolve(f"demo@{head}") as bare:
        assert not bare.files.exists("/workspace/reference/sub/notes.txt")

    with store.open("demo", **_mounts(reference)) as again:
        assert again.files.fs.getcwd() == "/workspace/reference/sub"
        assert again.terminal("cat notes.txt").stdout == "from the mount\n"
    with store.resolve(f"demo@{head}", **_mounts(reference)) as frozen:
        assert frozen.files.fs.getcwd() == "/workspace/reference/sub"


def test_a_cwd_this_handle_cannot_reach_starts_at_the_root_and_writes_nothing(
    store, reference
):
    """Without the mount, the stored cwd isn't there: the handle starts
    at the workspace root, opening writes nothing, and the stored cwd
    stays until the agent changes directory."""
    ws = store.open("demo", **_mounts(reference))
    try:
        assert ws.terminal("cd reference/sub")
        ws.commit({"tool": "test"})
        head = ws.provider.head
    finally:
        ws.close()

    with store.resolve(f"demo@{head}") as frozen:
        assert frozen.files.fs.getcwd() == "/workspace"
        assert frozen.terminal("pwd").stdout.strip() == "/workspace"

    other = store.open("demo")
    try:
        assert other.files.fs.getcwd() == "/workspace"
        assert other.provider.head == head and not other.uncommitted
        assert other.provider.kv.get(CWD) == "/workspace/reference/sub"
        assert other.terminal("echo x > top.txt")
        assert other.files.read("/workspace/top.txt") == b"x\n"
        assert other.terminal("mkdir -p elsewhere && cd elsewhere")
        assert other.provider.kv.get(CWD) == "/workspace/elsewhere"
    finally:
        other.close()


def test_a_cwd_that_is_gone_starts_at_the_root(store):
    ws = store.open("demo")
    try:
        assert ws.terminal("mkdir -p work && cd work")
        ws.files.fs.rmdir("/workspace/work")
        ws.commit({"tool": "test"})
        head = ws.provider.head
        assert ws.provider.kv.get(CWD) == "/workspace/work"
    finally:
        ws.close()

    with store.open("demo") as again:
        assert again.files.fs.getcwd() == "/workspace"
        assert again.provider.head == head and not again.uncommitted


@pytest.mark.parametrize("mounted", [False, True], ids=["unmounted", "mounted"])
def test_a_root_an_earlier_release_committed_still_reads_as_none(
    store, reference, mounted
):
    """Before this fix a composition committed the filesystem root as
    its cwd. That still reads as no cwd: the workspace root, with
    nothing written."""
    with store.open("demo") as ws:
        ws.files.write("/workspace/a.txt", "a")
        ws.provider.kv[CWD] = "/"
        ws.commit({"tool": "older"})
        head = ws.provider.head

    settings = _mounts(reference) if mounted else {}
    with store.open("demo", **settings) as again:
        assert again.files.fs.getcwd() == "/workspace"
        assert again.terminal("cat a.txt").stdout == "a"
        assert again.provider.head == head and not again.uncommitted
    with store.resolve(f"demo@{head}", **settings) as frozen:
        assert frozen.files.fs.getcwd() == "/workspace"


def test_an_attachment_keeps_the_cwd_stored(store):
    with store.open("reviewer") as other:
        other.files.write("/workspace/review.md", "ok\n")
        other.commit({"tool": "test"})

    ws = store.open("main")
    try:
        assert ws.terminal("mkdir -p deep && cd deep")
        ws.files.attach("reviewer", "reviews")
        assert ws.provider.kv.get(CWD) == "/workspace/deep"
        assert ws.terminal("cat ../reviews/review.md").stdout == "ok\n"
        assert ws.terminal("cd ../reviews")
        assert ws.provider.kv.get(CWD) == "/workspace/reviews"
        ws.files.detach("reviews")  # from inside it: back to the root
        assert ws.terminal("pwd").stdout.strip() == "/workspace"
        assert ws.terminal("cd deep")
        ws.files.attach("reviewer", "reviews")
        ws.commit({"tool": "test"})
    finally:
        ws.close()

    with store.open("main") as again:
        assert again.files.fs.getcwd() == "/workspace/deep"
