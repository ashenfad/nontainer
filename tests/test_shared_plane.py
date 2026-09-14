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


# -- two writers -------------------------------------------------------------
#
# A plane many sessions write is a plane two of them write at once. The
# loser of the CAS three-way merges onto the head that won rather than
# raising, so nothing has to serialize writers that were never told
# about each other.


def _writer(store_path, name, path, body):
    """A second PROCESS writing the plane, as an embedder's other
    worker would."""
    import subprocess
    import sys
    import textwrap

    script = textwrap.dedent(f"""
        from nontainer import Store
        with Store({str(store_path)!r}) as st:
            with st.shared({name!r}) as plane:
                plane.files.write({path!r}, {body!r})
    """)
    done = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=120
    )
    assert done.returncode == 0, done.stderr
    return done


def test_disjoint_writes_from_two_handles_both_land(store):
    with store.shared("catalog", autocommit=False) as a:
        a.files.write("/workspace/seed.txt", "seed")
        a.commit()
    with (
        store.shared("catalog", autocommit=False) as a,
        store.shared("catalog", autocommit=False) as b,
    ):
        a.files.write("/workspace/a.txt", "A")
        b.files.write("/workspace/b.txt", "B")
        a.commit()
        b.commit()  # loses the CAS, three-way merges onto a's head
    with store.shared("catalog") as after:
        assert after.files.read("/workspace/a.txt") == b"A"
        assert after.files.read("/workspace/b.txt") == b"B"
        assert after.files.read("/workspace/seed.txt") == b"seed"


def test_one_file_edited_on_both_sides_three_way_merges(store):
    with store.shared("catalog", autocommit=False) as a:
        a.files.write("/workspace/notes.md", "one\ntwo\nthree\n")
        a.commit()
    with (
        store.shared("catalog", autocommit=False) as a,
        store.shared("catalog", autocommit=False) as b,
    ):
        a.files.write("/workspace/notes.md", "ONE-FROM-A\ntwo\nthree\n")
        b.files.write("/workspace/notes.md", "one\ntwo\nTHREE-FROM-B\n")
        a.commit()
        b.commit()
    with store.shared("catalog") as after:
        body = after.files.read("/workspace/notes.md")
        assert body == b"ONE-FROM-A\ntwo\nTHREE-FROM-B\n"  # both edits, no markers
        # ...and the row beside the blob describes the merged bytes
        assert after.files.fs.stat("/workspace/notes.md").size == len(body)


def test_an_overlapping_edit_is_refused_and_names_the_path(store):
    with store.shared("catalog", autocommit=False) as a:
        a.files.write("/workspace/notes.md", "one\ntwo\nthree\n")
        a.commit()
    with (
        store.shared("catalog", autocommit=False) as a,
        store.shared("catalog", autocommit=False) as b,
    ):
        a.files.write("/workspace/notes.md", "one\nMINE\nthree\n")
        b.files.write("/workspace/notes.md", "one\nYOURS\nthree\n")
        a.commit()
        with pytest.raises(WorkspaceError, match=r"/workspace/notes\.md"):
            b.commit()
        # the loser keeps its work: the commit changed nothing
        assert b.files.read("/workspace/notes.md") == b"one\nYOURS\nthree\n"
    with store.shared("catalog") as after:
        # ...and no markers landed on the plane
        assert after.files.read("/workspace/notes.md") == b"one\nMINE\nthree\n"


def test_one_file_per_item_never_conflicts(store):
    """The convention that makes a plane multi-writer by construction:
    one file per item, so two writers never name one key."""
    handles = [store.shared("catalog", autocommit=False) for _ in range(4)]
    try:
        for n, plane in enumerate(handles):
            plane.files.write(f"/workspace/items/{n}.json", f'{{"id": {n}}}')
        for plane in handles:
            plane.commit()
    finally:
        for plane in handles:
            plane.close()
    with store.shared("catalog") as after:
        assert sorted(after.files.list("/workspace/items")) == [
            "/workspace/items/0.json",
            "/workspace/items/1.json",
            "/workspace/items/2.json",
            "/workspace/items/3.json",
        ]


def test_autocommit_writes_race_without_losing_either(store):
    """The commit an embedder never types: ws.files.write with
    autocommit on is the same CAS, one write at a time."""
    with store.shared("catalog") as a, store.shared("catalog") as b:
        a.files.write("/workspace/a.txt", "A")
        b.files.write("/workspace/b.txt", "B")
        a.files.write("/workspace/a2.txt", "A2")
        b.files.write("/workspace/b2.txt", "B2")
    with store.shared("catalog") as after:
        assert sorted(after.files.list("/workspace")) == [
            "/workspace/a.txt",
            "/workspace/a2.txt",
            "/workspace/b.txt",
            "/workspace/b2.txt",
        ]


def test_a_second_process_writes_the_same_plane(store):
    with store.shared("catalog", autocommit=False) as mine:
        mine.files.write("/workspace/mine.txt", "mine")
        _writer(store.path, "catalog", "/workspace/theirs.txt", "theirs")
        mine.commit()  # onto a head another PROCESS moved
    with store.shared("catalog") as after:
        assert after.files.read("/workspace/mine.txt") == b"mine"
        assert after.files.read("/workspace/theirs.txt") == b"theirs"


def test_the_planes_own_cache_survives_the_other_handle(store):
    """A second handle on a plane is not another session: its cache and
    its stored conversation belong to the plane, so a lost CAS merges
    them instead of taking one side whole."""
    with (
        store.shared("catalog", autocommit=False) as a,
        store.shared("catalog", autocommit=False) as b,
    ):
        a.cache["from-a"] = 1
        b.cache["from-b"] = 2
        a.commit()
        b.commit()
    with store.shared("catalog") as after:
        assert after.cache["from-a"] == 1
        assert after.cache["from-b"] == 2


def test_a_session_branch_keeps_its_own_answer(store):
    """The plane's file merge is the plane's. Two handles on one SESSION
    that write one file still refuse — a session is one agent's world,
    and a file merged under it was never asked for."""
    with store.open("user-42", autocommit=False) as a:
        a.files.write("/workspace/notes.md", "one\ntwo\nthree\n")
        a.commit()
    with (
        store.open("user-42", autocommit=False) as a,
        store.open("user-42", autocommit=False) as b,
    ):
        a.files.write("/workspace/notes.md", "ONE\ntwo\nthree\n")
        b.files.write("/workspace/notes.md", "one\ntwo\nTHREE\n")
        a.commit()
        with pytest.raises(WorkspaceError, match=r"/workspace/notes\.md"):
            b.commit()


# -- mounting a plane into a session -----------------------------------------
#
# The plane is not the session's state, so it is not attached the way a
# frozen ref is: `mount_shared` is a LIVE read-only window, and what
# another writer lands on the plane shows up through it.


def test_a_session_reads_a_plane_through_a_mount(store):
    with store.shared("catalog") as plane:
        plane.files.write("/workspace/items/a.json", '{"id": "a"}')
    with store.open("user-42") as ws:
        assert ws.files.mount_shared("catalog", "/workspace/catalog") == (
            "@store/shared/catalog"
        )
        assert ws.files.read("/workspace/catalog/items/a.json") == b'{"id": "a"}'
        # the agent sees it too, at the path the embedder named
        assert "a.json" in ws.terminal("ls /workspace/catalog/items").stdout
        assert ws.files.attachments() == {"/workspace/catalog": "@store/shared/catalog"}


def test_a_mounted_plane_refuses_writes(store):
    with store.shared("catalog") as plane:
        plane.files.write("/workspace/items/a.json", "{}")
    with store.open("user-42") as ws:
        ws.files.mount_shared("catalog", "/workspace/catalog")
        with pytest.raises(Exception):
            ws.files.write("/workspace/catalog/items/b.json", "{}")
        assert ws.terminal("echo x > /workspace/catalog/items/c.json").exit_code != 0
    with store.shared("catalog") as plane:
        assert sorted(plane.files.list("/workspace/items")) == [
            "/workspace/items/a.json"
        ]


def test_a_mount_is_live(store):
    """What another handle writes after the mount is visible through
    it: a plane is a moving head, and a mount is a window on it."""
    with store.shared("catalog") as plane:
        plane.files.write("/workspace/items/a.json", "first")
    with store.open("user-42") as ws:
        ws.files.mount_shared("catalog", "/workspace/catalog")
        assert ws.files.read("/workspace/catalog/items/a.json") == b"first"
        with store.shared("catalog") as writer:
            writer.files.write("/workspace/items/b.json", "second")
            writer.files.write("/workspace/items/a.json", "rewritten")
        assert ws.files.read("/workspace/catalog/items/b.json") == b"second"
        assert ws.files.read("/workspace/catalog/items/a.json") == b"rewritten"


def test_a_mount_belongs_to_the_session_handle_and_not_its_state(store):
    with store.shared("catalog") as plane:
        plane.files.write("/workspace/items/a.json", "{}")
    with store.open("user-42") as ws:
        ws.files.mount_shared("catalog", "/workspace/catalog")
        ws.files.write("/workspace/own.txt", "mine")
        ws.files.detach("/workspace/catalog")
        assert not ws.files.fs.exists("/workspace/catalog")
    with store.open("user-42") as again:
        assert not again.files.fs.exists("/workspace/catalog")
        assert again.files.read("/workspace/own.txt") == b"mine"


def test_mount_shared_refuses_a_plane_that_is_not_there(store):
    with store.open("user-42") as ws:
        with pytest.raises(WorkspaceError, match="No such shared plane"):
            ws.files.mount_shared("nope", "/workspace/catalog")
        assert ws.files.attachments() == {}
    assert store.shared_names() == []  # the refusal minted nothing


def test_attach_sends_a_bare_plane_name_to_mount_shared(store):
    with store.shared("catalog") as plane:
        plane.files.write("/workspace/items/a.json", "{}")
    with store.open("user-42") as ws:
        with pytest.raises(ValueError, match="mount_shared"):
            ws.files.attach("@store/shared/catalog", "/workspace/catalog")


def test_a_commit_that_loses_the_swap_merges_onto_the_head_that_won(store, monkeypatch):
    """The other way to lose the race. A head that has already moved
    when the commit starts is three-way merged on the spot; one that
    moves between the head read and the compare-and-swap fails the swap
    instead. The staged work is untouched either way, so the next
    attempt reads the head that won and merges onto it."""
    from kvgit.versioned.kv import VersionedKV

    with store.shared("catalog", autocommit=False) as a:
        a.files.write("/workspace/a.txt", "A")
        a.commit()

    original = VersionedKV._cas_head
    landed: list[bool] = []

    def lose_the_swap_once(self, old, new):
        if not landed:
            landed.append(True)
            with store.shared("catalog") as other:
                other.files.write("/workspace/c.txt", "C")
            return False
        return original(self, old, new)

    with store.shared("catalog", autocommit=False) as b:
        b.files.write("/workspace/b.txt", "B")
        monkeypatch.setattr(VersionedKV, "_cas_head", lose_the_swap_once)
        b.commit()
        monkeypatch.undo()
    assert landed  # the race really happened

    with store.shared("catalog") as after:
        assert sorted(after.files.list("/workspace")) == [
            "/workspace/a.txt",
            "/workspace/b.txt",
            "/workspace/c.txt",
        ]


# -- a plane's tags are its own ----------------------------------------------


def test_unshare_leaves_a_store_tag_that_merely_looks_like_the_planes(store):
    """A store tag may hold slashes, so ``shared/catalog/release`` is a
    name somebody can tag by hand. A plane's teardown removes the plane
    and nothing that merely reads like it."""
    with store.open("user-42") as ws:
        ws.files.write("/workspace/a.txt", "a")
        commit = store.tags.add(ws, "shared/catalog/release")
        store.shared("catalog").close()
        store.unshare("catalog", min_age=0)
        assert store.tags.list() == {"shared/catalog/release": commit}
    with store.tags.at("shared/catalog/release") as snap:
        assert snap.files.read("/workspace/a.txt") == b"a"


def test_a_planes_own_tags_are_not_store_tags(store):
    """The plane's tag scope is the plane's. A name it bookmarks is not
    a store tag, and a store tag is not a name it can read or delete."""
    with store.open("user-42") as ws:
        ws.files.write("/workspace/a.txt", "a")
        store.tags.add(ws, "shared/catalog/release")
    with store.shared("catalog") as plane:
        plane.files.write("/workspace/x.json", "{}")
        plane.tags.add("v1")
        assert sorted(plane.tags.list()) == ["v1"]
        assert sorted(store.tags.list()) == ["shared/catalog/release"]


def test_a_planes_own_tags_go_with_the_plane(store):
    with store.shared("catalog") as plane:
        plane.files.write("/workspace/x.json", "{}")
        plane.tags.add("v1")
    store.unshare("catalog", min_age=0)
    with store.shared("catalog") as again:
        assert again.tags.list() == {}
