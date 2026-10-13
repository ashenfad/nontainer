"""Mounting a filesystem object (``Mount(fs=...)``), not only a host
directory (#242): content that isn't on local disk, read as the agent
touches it."""

import posixpath
from collections import Counter

import pytest
from monkeyfs import IsolatedFS
from monkeyfs.base import FileMetadata

from nontainer import Mount, PythonConfig, Store
from nontainer.wsgit import register_wsgit
from nontainer.wspytest import register_wspytest

DOCS = "/workspace/docs"
STAMP = "2026-10-13T00:00:00+00:00"


class Shelf:
    """A read-only filesystem over a dict, with nothing but the reads a
    mount needs (no ``islink``, ``getsize`` or ``glob``: the composition
    derives those), that counts what it is asked: a stand-in for a
    remote store fetched lazily."""

    def __init__(self, files):
        self.files = {"/" + path.lstrip("/"): data for path, data in files.items()}
        self.reads = Counter()
        self.closed = False

    def _dirs(self):
        dirs = {"/"}
        for path in self.files:
            parent = posixpath.dirname(path)
            while parent not in dirs:
                dirs.add(parent)
                parent = posixpath.dirname(parent)
        return dirs

    def exists(self, path):
        return path in self.files or path in self._dirs()

    def isfile(self, path):
        return path in self.files

    def isdir(self, path):
        return path in self._dirs()

    def list(self, path=".", recursive=False):
        if path not in self._dirs():
            raise FileNotFoundError(path)
        base = path.rstrip("/") + "/"
        names = {
            rel if recursive else rel.split("/")[0]
            for entry in [*self.files, *self._dirs()]
            if entry.startswith(base) and (rel := entry[len(base) :])
        }
        return sorted(names)

    def read(self, path, offset=0, size=-1):
        if path not in self.files:
            raise FileNotFoundError(path)
        self.reads[path] += 1
        data = self.files[path][offset:]
        return data if size < 0 else data[:size]

    def stat(self, path):
        if path in self.files:
            size, is_dir = len(self.files[path]), False
        elif path in self._dirs():
            size, is_dir = 0, True
        else:
            raise FileNotFoundError(path)
        return FileMetadata(
            size=size, created_at=STAMP, modified_at=STAMP, is_dir=is_dir
        )

    def close(self):
        self.closed = True


def shelf():
    return Shelf(
        {
            "manuals/pump/intro.md": b"the pump\novertemp alarm at 90C\n",
            "manuals/fan.md": b"the fan\n",
            "archive/big.bin": b"x" * 4096,
        }
    )


@pytest.fixture
def store():
    st = Store(memory=True)
    yield st
    st.close()


@pytest.mark.parametrize("isolation", ["none", "process"])
def test_both_tools_see_an_object_mount(store, isolation):
    docs = shelf()
    ws = store.open(
        "s",
        mounts={DOCS: Mount(fs=docs)},
        python=PythonConfig(isolation=isolation),
    )
    try:
        r = ws.terminal(f"grep -rn overtemp {DOCS}/manuals")
        assert r, r.stderr
        assert "pump/intro.md:2:overtemp alarm" in r.stdout
        assert ws.terminal("cat docs/manuals/fan.md").stdout == "the fan\n"

        r = ws.run_python(
            "import os\n"
            f"text = open('{DOCS}/manuals/pump/intro.md').read()\n"
            f"walked = sorted(f for _, _, fs in os.walk('{DOCS}') for f in fs)\n"
            "size = os.path.getsize('docs/archive/big.bin')\n"
        )
        assert r, r.error
        assert "overtemp" in r.namespace["text"]
        assert r.namespace["walked"] == ["big.bin", "fan.md", "intro.md"]
        assert r.namespace["size"] == 4096
    finally:
        ws.close()


def test_an_object_mount_is_read_only(store):
    docs = shelf()
    ws = store.open("s", mounts={DOCS: Mount(fs=docs)})
    try:
        assert not ws.terminal(f"echo no > {DOCS}/new.md")
        assert not ws.terminal(f"rm {DOCS}/manuals/fan.md")
        r = ws.run_python(f"open('{DOCS}/new.md', 'w').write('no')")
        assert not r and "PermissionError" in r.error
        assert set(docs.files) == set(shelf().files)
    finally:
        ws.close()


def test_only_what_the_agent_reads_is_read(store):
    """Nothing the framework does reaches into the object: committing,
    the agent's git and forking read none of it, and a read takes only
    the file it names."""
    docs = shelf()
    ws = store.open("s", mounts={DOCS: Mount(fs=docs)})
    register_wsgit(ws)
    try:
        assert ws.terminal("echo x > x.txt")
        for verb in ("ws-git status", "ws-git add -A", "ws-git commit -m one"):
            assert ws.terminal(verb), verb
        ws.files.write("y.txt", "y")
        ws.commit()
        ws.fork("child").close()
        assert not docs.reads

        assert ws.terminal("head -c 4 docs/archive/big.bin").stdout == "xxxx"
        assert set(docs.reads) == {"/archive/big.bin"}
    finally:
        ws.close()


def test_a_fork_shares_the_object_and_closing_leaves_it_open(store):
    docs = shelf()
    ws = store.open("s", mounts={DOCS: Mount(fs=docs)})
    child = ws.fork("child")
    try:
        assert child.terminal("cat docs/manuals/fan.md").stdout == "the fan\n"
        docs.files["/manuals/new.md"] = b"added at the source\n"
        assert "added" in ws.terminal("cat docs/manuals/new.md").stdout
        assert "added" in child.terminal("cat docs/manuals/new.md").stdout
    finally:
        child.close()
        ws.close()
    assert not docs.closed


def test_an_object_mount_is_not_in_any_commit(store):
    docs = shelf()
    ws = store.open("s", mounts={DOCS: Mount(fs=docs)})
    try:
        ws.files.write("notes.md", "mine")
        ws.commit()
    finally:
        ws.close()
    plain = store.open("s")
    try:
        assert plain.files.exists("notes.md")
        assert not plain.files.exists(DOCS)
    finally:
        plain.close()


def test_a_directory_through_isolatedfs_reads_like_a_directory_mount(store, tmp_path):
    src = tmp_path / "docs"
    (src / "sub").mkdir(parents=True)
    (src / "a.md").write_text("a\n")
    (src / "sub" / "b.md").write_text("b\n")
    seen = []
    for mount in (Mount(src), Mount(fs=IsolatedFS(str(src)))):
        ws = store.open(f"s{len(seen)}", mounts={DOCS: mount})
        try:
            seen.append(
                [
                    ws.terminal(cmd).stdout
                    for cmd in (f"ls -R {DOCS}", f"find {DOCS}", "cat docs/sub/b.md")
                ]
            )
        finally:
            ws.close()
    assert seen[0] == seen[1]


def test_a_mount_takes_one_source(tmp_path):
    with pytest.raises(ValueError, match="one source"):
        Mount()
    with pytest.raises(ValueError, match="one source"):
        Mount(tmp_path, fs=shelf())


def test_an_object_mount_is_read_only_from_the_start():
    with pytest.raises(ValueError, match="read-only"):
        Mount(fs=shelf(), readonly=False)


def test_an_object_without_the_reads_is_refused():
    class Partial:
        def read(self, path, offset=0, size=-1):
            return b""

    with pytest.raises(TypeError, match="Partial has no") as caught:
        Mount(fs=Partial())
    assert "'stat'" in str(caught.value) and "'read'" not in str(caught.value)


class _Copying:
    """An executor that copies the tree into a machine of its own. The
    refusal comes before it would be opened."""

    copies_tree = True


def test_an_executor_that_copies_the_tree_refuses_an_object_mount(store):
    with pytest.raises(ValueError, match=r"_Copying copies the workspace tree"):
        store.open("s", mounts={DOCS: Mount(fs=shelf())}, executor_factory=_Copying)


def test_a_runtimes_own_object_mount_is_refused_there_too(store):
    from nontainer.runtime import Runtime

    ws = store.open("s")
    try:
        with pytest.raises(ValueError, match=r"at \['/extra'\]"):
            Runtime(ws, executor=_Copying(), mounts={"/extra": Mount(fs=shelf())})
    finally:
        ws.close()


def test_a_runtime_over_a_workspace_with_an_object_mount_is_refused(store):
    """A second runtime hands the executor the workspace's filesystem,
    its object mounts included, so a copying one is refused there too."""
    from nontainer.runtime import Runtime

    ws = store.open("s", mounts={DOCS: Mount(fs=shelf())})
    try:
        with pytest.raises(ValueError, match=r"at \['/workspace/docs'\]"):
            Runtime(ws, executor=_Copying())
    finally:
        ws.close()


def test_dud_copies_the_tree_so_refuses_an_object_mount(store):
    pytest.importorskip("dud")
    from nontainer.executor_dud import DudExecutor

    assert DudExecutor.copies_tree is True
    with pytest.raises(ValueError, match="DudExecutor copies the workspace tree"):
        store.open("s", mounts={DOCS: Mount(fs=shelf())}, executor_factory=DudExecutor)


# -- test discovery reaches into mounts ---------------------------------------

PASSING = b"def test_ok():\n    assert True\n"


def test_ws_pytest_runs_the_tests_in_an_object_mount(store):
    """Discovery globs the directory it's given, and an object with no
    ``glob`` of its own is globbed through its listing."""
    docs = Shelf({"test_doc.py": PASSING, "sub/test_deep.py": PASSING})
    ws = store.open("s", mounts={DOCS: Mount(fs=docs)})
    register_wspytest(ws)
    try:
        r = ws.terminal("ws-pytest docs")
        assert r.exit_code == 0, r.stdout + r.stderr
        assert "2 passed" in r.stdout
    finally:
        ws.close()


def test_ws_pytest_from_the_root_finds_the_tests_in_a_mount(store, tmp_path):
    """A pattern rooted above a mount searches it too, for a directory
    mount and an object mount alike. (A bare ``ws-pytest`` collects
    ``tests/`` only, by design; naming the root collects everything.)"""
    src = tmp_path / "lib"
    src.mkdir()
    (src / "test_lib.py").write_bytes(PASSING)
    ws = store.open(
        "s",
        mounts={
            DOCS: Mount(fs=Shelf({"test_doc.py": PASSING})),
            "/workspace/lib": Mount(src),
        },
    )
    register_wspytest(ws)
    try:
        ws.files.write("/workspace/test_own.py", PASSING.decode())
        r = ws.terminal("ws-pytest .")
        assert r.exit_code == 0, r.stdout + r.stderr
        assert "3 passed" in r.stdout
    finally:
        ws.close()
