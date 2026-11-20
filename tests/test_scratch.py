"""store.scratch: a writable session at the exact commit a ref names."""

import threading

import pytest

from nontainer import Store
from nontainer.errors import (
    CommitNotFoundError,
    NotSupportedError,
    SessionIdError,
    WorkspaceError,
)
from nontainer.store import Ref


@pytest.fixture
def store(tmp_path):
    st = Store(tmp_path)
    yield st
    st.close()


def _source(store, name="source"):
    """A session with one committed file; returns its ref."""
    with store.open(name, autocommit=False) as ws:
        ws.files.write("input.txt", "v1")
        ws.commit()
        return ws.ref


def test_the_head_is_the_commit_and_opening_writes_nothing(store):
    ref = _source(store)
    with store.scratch(ref, "copy") as child:  # autocommit, on purpose
        assert child.head == ref.commit
        assert child.ref == Ref("copy", ref.commit)
        assert child.files.read("input.txt") == b"v1"
    assert store.repo.branches["copy"] == ref.commit
    assert "copy" in store.sessions()


def test_one_commit_has_the_commit_as_its_only_parent(store):
    ref = _source(store)
    with store.scratch(ref, "copy", autocommit=False) as child:
        child.files.write("out.txt", "x")
        result = child.commit()
        theirs = child.ref
    assert store.repo.get_commit(result).parents == (ref.commit,)

    # the source never moved, and takes the work when its owner lands it
    with store.open("source", autocommit=False) as ws:
        assert ws.head == ref.commit
        ws.land(ref, theirs, expect_head=ref.commit)
        assert ws.files.read("out.txt") == b"x"


def test_it_holds_the_commits_cache_and_cwd(store):
    with store.open("source", autocommit=False) as ws:
        ws.cache["count"] = 42
        ws.terminal("mkdir work && cd work")
        ws.commit()
        ref = ws.ref
    with store.scratch(ref, "copy") as child:
        assert child.cache["count"] == 42
        assert child.files.fs.getcwd() == "/workspace/work"


def test_a_short_commit_and_a_string_ref_are_accepted(store):
    ref = _source(store)
    with store.scratch(f"source@{ref.commit[:7]}", "copy") as child:
        assert child.head == ref.commit


def test_an_earlier_commit_of_the_session_is_a_source(store):
    first = _source(store)
    with store.open("source", autocommit=False) as ws:
        ws.files.write("input.txt", "v2")
        ws.commit()
    with store.scratch(first, "copy") as child:
        assert child.files.read("input.txt") == b"v1"


def test_a_refused_source_creates_nothing(store):
    ref = _source(store)
    _source(store, "other")
    with pytest.raises(WorkspaceError, match="No such session"):
        store.scratch(Ref("nobody", ref.commit), "copy")
    # a whole id another session made is not this session's state
    other = store.repo.branches["other"]
    with pytest.raises(CommitNotFoundError):
        store.scratch(Ref("source", other), "copy")
    with pytest.raises(CommitNotFoundError):
        store.scratch(Ref("source", "f" * 40), "copy")
    assert "copy" not in store.sessions()


def test_an_existing_name_is_refused_and_left_alone(store):
    ref = _source(store)
    with store.open("taken", autocommit=False) as ws:
        ws.files.write("mine.txt", "keep")
        ws.commit()
        head = ws.head
    with pytest.raises(WorkspaceError, match="already exists"):
        store.scratch(ref, "taken")
    assert store.repo.branches["taken"] == head
    with store.open("taken") as ws:
        assert ws.files.read("mine.txt") == b"keep"


def test_two_callers_racing_for_one_name_get_one_session(store, tmp_path):
    ref = _source(store)
    other = Store(tmp_path)
    barrier = threading.Barrier(2)
    outcomes = []

    def scratch(st):
        barrier.wait(timeout=10)
        try:
            st.scratch(ref, "copy").close()
            outcomes.append("created")
        except WorkspaceError as e:
            outcomes.append(str(e))

    threads = [threading.Thread(target=scratch, args=(st,)) for st in (store, other)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    other.close()
    assert outcomes.count("created") == 1
    assert any("already exists" in o for o in outcomes if o != "created")
    assert store.repo.branches["copy"] == ref.commit


def test_a_name_that_is_not_a_session_id_is_refused(store):
    ref = _source(store)
    for name in ("@store/mine", "a/b", ".hidden"):
        with pytest.raises(SessionIdError):
            store.scratch(ref, name)
    assert store.sessions() == ["source"]


def test_a_failed_open_removes_the_session(store, monkeypatch):
    ref = _source(store)

    def refuses(*args, **kwargs):
        raise RuntimeError("the open failed")

    monkeypatch.setattr(store, "open", refuses)
    with pytest.raises(RuntimeError):
        store.scratch(ref, "copy")
    monkeypatch.undo()
    assert "copy" not in store.sessions()


def test_a_profile_with_its_own_fields_is_refused_before_creating(store):
    from nontainer import PythonConfig
    from nontainer.workspace import Profile

    ref = _source(store)
    with pytest.raises((TypeError, ValueError)):
        store.scratch(ref, "copy", profile=Profile(), python=PythonConfig())
    assert "copy" not in store.sessions()


def test_a_store_tag_and_a_published_version_are_sources(store):
    with store.open("source", autocommit=False) as ws:
        ws.files.write("app/index.html", "<p>hi</p>")
        ws.commit()
        tagged = store.tags.add(ws, "kept")
        version = store.publish(ws, "demo").current_version

    with store.scratch(Ref.at_tag("kept", tagged), "from-tag") as child:
        assert child.head == tagged
    with store.scratch(version.ref, "from-pub") as child:
        assert child.head == version.ref.commit
        assert child.files.read("app/index.html") == b"<p>hi</p>"

    store.publish(store.open("source"), "demo", version="v2")
    store.set_current("demo", "v2")
    store.unpublish("demo", "v1", min_age=0)
    with pytest.raises(WorkspaceError, match="No longer published"):
        store.scratch(version.ref, "from-gone")
    assert "from-gone" not in store.sessions()


def test_a_session_at_another_root_passes_its_root(store):
    with store.open("source", autocommit=False, root="/srv") as ws:
        ws.files.write("data.txt", "here")
        ws.commit()
        ref = ws.ref
    with store.scratch(ref, "copy", root="/srv") as child:
        assert child.root == "/srv"
        assert child.files.read("data.txt") == b"here"


def test_a_store_without_versions_refuses(tmp_path):
    with Store(tmp_path, backend="agentfs") as st:
        with pytest.raises(NotSupportedError):
            st.scratch("source@" + "a" * 40, "copy")
