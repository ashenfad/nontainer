"""The shared plane: a workspace on a reserved branch, written by many.

Everything else nontainer versions belongs to one session. A shared
plane belongs to none of them: it is a ``Workspace`` on
``@store/shared/<name>``, created on first open, readable and writable
from any session or process, and merged rather than locked when two
writers land at once. nontainer supplies the branch, the workspace and
the merge; what lives on the plane is the embedder's word.
"""

import pytest

from nontainer import NotSupportedError, Ref, Store, WorkspaceError


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path) as st:
        yield st


# -- the plane itself --------------------------------------------------------


def test_shared_opens_a_writable_workspace_on_a_reserved_branch(store):
    with store.shared("catalog") as plane:
        assert plane.session == "@store/shared/catalog"
        assert plane.caps.versioned
        plane.files.write("/workspace/items/a.json", '{"id": "a"}')
        head = plane.head
    assert head is not None

    # A second handle — a different session, a different process — opens
    # the same plane and sees the same tree.
    with store.shared("catalog") as again:
        assert again.files.read("/workspace/items/a.json") == b'{"id": "a"}'


def test_shared_takes_the_construction_kwargs_open_takes(store):
    with store.shared("catalog", root="/plane", autocommit=False) as plane:
        before = plane.head
        plane.files.write("/plane/note.txt", "hi")
        assert plane.head == before  # autocommit off: nothing landed yet
        plane.commit()
        assert plane.files.read("/plane/note.txt") == b"hi"
    with pytest.raises(TypeError, match="unexpected keyword argument 'nope'"):
        store.shared("catalog", nope=1)


def test_shared_names_lists_the_planes(store):
    assert store.shared_names() == []
    for name in ("catalog", "memory"):
        store.shared(name).close()
    assert store.shared_names() == ["catalog", "memory"]


def test_unshare_removes_one_plane(store):
    store.shared("catalog").close()
    store.shared("memory").close()
    store.unshare("catalog", min_age=0)
    assert store.shared_names() == ["memory"]


def test_unshare_says_so_when_there_is_no_such_plane(store):
    with pytest.raises(ValueError, match="No such shared plane: 'nope'"):
        store.unshare("nope", min_age=0)


@pytest.mark.parametrize("bad", ["with/slash", "@sneaky", ".dot", "", 7])
def test_shared_refuses_a_name_that_is_not_session_shaped(store, bad):
    with pytest.raises(ValueError, match="Invalid shared plane name"):
        store.shared(bad)
    with pytest.raises(ValueError, match="Invalid shared plane name"):
        store.unshare(bad)


def test_a_shared_plane_is_not_a_session(store):
    store.shared("catalog").close()
    store.open("user-42").close()
    assert store.sessions() == ["user-42"]
    assert not store.exists("@store/shared/catalog")
    with pytest.raises(ValueError, match="is not a session id"):
        store.delete("@store/shared/catalog")


def test_shared_needs_a_versioned_store(tmp_path):
    st = Store(tmp_path, backend="dir")
    with pytest.raises(NotSupportedError):
        st.shared("catalog")
    with pytest.raises(NotSupportedError):
        st.shared_names()
    with pytest.raises(NotSupportedError):
        st.unshare("catalog")


# -- a shared ref ------------------------------------------------------------


def test_a_shared_ref_resolves_and_attaches(store):
    with store.shared("catalog") as plane:
        plane.files.write("/workspace/items/a.json", '{"id": "a"}')
        ref = plane.ref
    assert str(ref).startswith("@store/shared/catalog@")
    assert Ref.parse(str(ref)).session == "@store/shared/catalog"

    with store.resolve(ref) as frozen:
        assert frozen.files.read("/workspace/items/a.json") == b'{"id": "a"}'

    with store.open("user-42") as ws:
        ws.files.attach(str(ref), "/workspace/plane")
        assert ws.files.read("/workspace/plane/items/a.json") == b'{"id": "a"}'


def test_a_ref_into_a_plane_that_is_gone_is_refused(store):
    with store.shared("catalog") as plane:
        plane.files.write("/workspace/a.txt", "a")
        ref = str(plane.ref)
    store.unshare("catalog", min_age=0)
    with pytest.raises(WorkspaceError, match="No such shared plane"):
        store.resolve(ref)
