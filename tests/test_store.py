"""Store: the session set, and what outlives one session.

``workspace()`` is sugar over ``Store.open`` now, so the first thing
these check is that the sugar and the object are the same call.
"""

import pytest

from nontainer import (
    CommitNotFoundError,
    NotSupportedError,
    Ref,
    Store,
    Workspace,
    WorkspaceError,
    store,
    workspace,
)
from nontainer.providers import KvgitProvider

# -- refs --------------------------------------------------------------------


def test_ref_round_trips():
    r = Ref.parse("user-42@abc123")
    assert (r.session, r.commit, r.path) == ("user-42", "abc123", None)
    assert str(r) == "user-42@abc123"

    withpath = Ref.parse("user-42@abc123:/app/index.html")
    assert withpath.path == "/app/index.html"
    assert str(withpath) == "user-42@abc123:/app/index.html"

    assert Ref.parse(withpath) is withpath  # already a ref: passes through


@pytest.mark.parametrize("bad", ["nope", "@abc", "sess@", ""])
def test_ref_rejects_what_is_not_a_ref(bad):
    with pytest.raises(ValueError, match="Not a ref"):
        Ref.parse(bad)


# -- the sugar ---------------------------------------------------------------


def test_workspace_is_sugar_for_store_open(tmp_path):
    """Same store, same session, same state — the factory is one call
    into Store now, not a second resolution path that could drift."""
    with workspace("shared-id", store=tmp_path) as viafn:
        viafn.terminal("echo sugar > note.txt")
        assert type(viafn._provider) is KvgitProvider

    with store(tmp_path).open("shared-id") as viastore:
        assert viastore.session == "shared-id"
        assert viastore.terminal("cat note.txt").stdout.strip() == "sugar"


def test_workspace_still_takes_an_explicit_provider(tmp_path):
    """A ready provider instance overrides backend/store entirely — the
    substitution Store(provider_factory=...) makes, for one session."""
    provider = KvgitProvider.open(None, session="byo")  # in-memory
    with workspace("byo", provider=provider) as ws:
        assert ws._provider is provider
        ws.terminal("echo byo > x.txt")
        assert ws.terminal("cat x.txt").stdout.strip() == "byo"


def test_store_open_passes_construction_settings_through(tmp_path):
    with store(tmp_path).open("rooted", root="/data", max_observation=99) as ws:
        assert ws.root == "/data"
        assert ws.runtime.max_observation == 99


# -- the session set ---------------------------------------------------------


def test_sessions_lists_what_the_store_holds(tmp_path):
    st = Store(tmp_path)
    assert st.sessions() == []

    for name in ("beta", "alpha"):
        with st.open(name) as ws:
            ws.terminal(f"echo {name} > who.txt")
    assert st.sessions() == ["alpha", "beta"]
    assert st.exists("alpha")
    assert not st.exists("gamma")


def test_sessions_excludes_reserved_names(tmp_path):
    """Tag refs and the store namespace are not sessions. A store tag
    is stored as a reserved branch head, so a naive branch listing
    would report it as one."""
    st = Store(tmp_path)
    with st.open("author") as ws:
        ws.terminal("echo x > x.txt")
        st.tags.add(ws, "published")
        ws.tags.add("mine")
    assert st.sessions() == ["author"]


def test_delete_removes_a_session(tmp_path):
    st = Store(tmp_path)
    with st.open("doomed") as ws:
        ws.terminal("echo bye > b.txt")
    assert st.exists("doomed")

    st.delete("doomed", min_age=0)
    assert not st.exists("doomed")
    with st.open("doomed") as fresh:
        assert not fresh.terminal("cat b.txt")


def test_delete_is_idempotent_and_plural(tmp_path):
    st = Store(tmp_path)
    with st.open("a"):
        pass
    with st.open("b"):
        pass
    st.delete(["a", "b", "never-existed"], min_age=0)
    assert st.sessions() == []
    st.delete("a", min_age=0)  # deleting nothing is not an error


def test_going_back_leaves_nothing_to_sweep(tmp_path):
    """A session's history is append-only: going back is a new commit,
    so the turns it stepped off are still reachable and clean() has
    nothing to collect."""
    st = Store(tmp_path)
    with st.open("gc") as ws:
        ws.terminal("echo one > f.txt")
        one = ws.head
        ws.terminal("echo two > f.txt")
        ws.terminal("echo three > f.txt")
        three = ws.head
        ws.checkout(one)
        assert ws.terminal("cat f.txt").stdout.strip() == "one"
        assert {one, three} <= {e.id for e in ws.log()}

    assert st.clean(min_age=0) == 0
    with st.open("gc") as ws:
        assert ws.terminal("cat f.txt").stdout.strip() == "one"


def test_clean_sweeps_what_a_deletion_left_behind(tmp_path):
    """What DOES strand commits is dropping a branch. Deletion sweeps
    as it goes, but its grace period spares commits younger than the
    period — clean() is the standalone sweep that collects them."""
    st = Store(tmp_path)
    with st.open("kept") as ws:
        ws.terminal("echo kept > f.txt")
    with st.open("gone") as ws:
        ws.terminal("echo one > f.txt")
        ws.terminal("echo two > f.txt")

    st.delete("gone", min_age=3600)  # young commits left where they lie

    assert st.clean(min_age=0) > 0
    assert st.clean(min_age=0) == 0  # nothing left to sweep
    with st.open("kept") as ws:
        assert ws.terminal("cat f.txt").stdout.strip() == "kept"


# -- resolve -----------------------------------------------------------------


def test_resolve_reads_one_exact_commit(tmp_path):
    st = Store(tmp_path)
    with st.open("hist") as ws:
        ws.terminal("echo first > f.txt")
        first = ws.head
        ws.terminal("echo second > f.txt")
        assert ws.terminal("cat f.txt").stdout.strip() == "second"

    with st.resolve(f"hist@{first}") as past:
        assert past.frozen
        assert past.terminal("cat f.txt").stdout.strip() == "first"

    with st.resolve(Ref(session="hist", commit=first)) as also:
        assert also.terminal("cat f.txt").stdout.strip() == "first"


def test_resolve_refuses_an_unknown_session_rather_than_creating_it(tmp_path):
    st = Store(tmp_path)
    with st.open("real"):
        pass
    with pytest.raises(WorkspaceError, match="No such session"):
        st.resolve("ghost@deadbeef")
    assert st.sessions() == ["real"]  # the miss created nothing


def test_resolve_refuses_an_unknown_commit(tmp_path):
    st = Store(tmp_path)
    with st.open("real") as ws:
        ws.terminal("echo x > x.txt")
    with pytest.raises(CommitNotFoundError):
        st.resolve("real@0123456789abcdef")


# -- store-scoped tags -------------------------------------------------------


def test_store_tags_round_trip(tmp_path):
    st = Store(tmp_path)
    with st.open("author") as ws:
        ws.terminal("echo published > report.txt")
        commit = st.tags.add(ws, "report", info={"kind": "demo"})

    assert st.tags.list() == {"report": commit}
    info = st.tags.info("report")
    assert info is not None
    assert (info.name, info.scope, info.id) == ("report", "store", commit)
    assert info.info == {"kind": "demo"}
    assert info.tree  # the content hash is read, not left blank

    with st.tags.at("report") as snap:
        assert snap.frozen
        assert snap.terminal("cat report.txt").stdout.strip() == "published"

    assert st.tags.info("absent") is None
    st.tags.delete("report")
    assert st.tags.list() == {}
    with pytest.raises(CommitNotFoundError):
        st.tags.delete("report")


def test_store_tag_can_name_a_ref(tmp_path):
    """A tag on an exact past commit, without opening the session."""
    st = Store(tmp_path)
    with st.open("author") as ws:
        ws.terminal("echo v1 > f.txt")
        first = ws.head
        ws.terminal("echo v2 > f.txt")

    assert st.tags.add(f"author@{first}", "release") == first
    with st.tags.at("release") as snap:
        assert snap.terminal("cat f.txt").stdout.strip() == "v1"


def test_store_tag_outlives_its_session(tmp_path):
    """The publication property, through the store surface: the tag and
    its commit survive the deletion of the session that made it."""
    st = Store(tmp_path)
    with st.open("author") as ws:
        ws.terminal("echo published > report.txt")
        st.tags.add(ws, "report")
    with st.open("bystander"):
        pass

    st.delete("author", min_age=0)
    assert st.sessions() == ["bystander"]
    assert "report" in st.tags.list()
    with st.tags.at("report") as snap:
        assert snap.terminal("cat report.txt").stdout.strip() == "published"


def test_store_tags_never_move(tmp_path):
    st = Store(tmp_path)
    with st.open("author") as ws:
        ws.terminal("echo x > x.txt")
        st.tags.add(ws, "pinned")
        ws.terminal("echo y > y.txt")
        with pytest.raises(WorkspaceError, match="never move"):
            st.tags.add(ws, "pinned")


def test_a_store_scoped_read_needs_no_session(tmp_path):
    """The store owns an anchor of its own: a store-scoped tag opens
    with every session deleted and nothing published."""
    st = Store(tmp_path)
    with st.open("author") as ws:
        ws.terminal("echo published > report.txt")
        st.tags.add(ws, "report")
    st.delete("author", min_age=0)
    assert st.sessions() == []

    with st.tags.at("report") as snap:
        assert snap.files.read("report.txt") == b"published\n"
        anchored = snap.ref
    # The anchor is a branch, so the ref that snapshot quotes resolves.
    with st.resolve(anchored) as again:
        assert again.files.read("report.txt") == b"published\n"

    assert st.sessions() == []  # ...and the anchor is not a session
    assert "@store/anchor" in st._branches()


def test_the_anchor_is_the_last_resort(tmp_path):
    """A store with a session to read through grows no anchor branch."""
    st = Store(tmp_path)
    with st.open("author") as ws:
        ws.terminal("echo published > report.txt")
        st.tags.add(ws, "report")
        with st.tags.at("report") as snap:
            assert snap.files.read("report.txt") == b"published\n"
    assert "@store/anchor" not in st._branches()


def test_teardown_leaves_the_anchor(tmp_path):
    """Deleting every session and sweeping the store keeps the anchor
    and the tag it reads through: a branch head is a GC root, and the
    anchor's own commit holds nothing to collect."""
    st = Store(tmp_path)
    with st.open("author") as ws:
        ws.terminal("echo published > report.txt")
        st.tags.add(ws, "report")
    st.delete("author", min_age=0)
    st.tags.at("report").close()  # mints the anchor

    st.delete(["author", "reader"], min_age=0)
    assert st.clean(min_age=0) == 0  # nothing the anchor or the tag reaches
    assert "@store/anchor" in st._branches()
    with st.tags.at("report") as snap:
        assert snap.files.read("report.txt") == b"published\n"


def test_delete_deletes_sessions_only(tmp_path):
    """The reserved branches are the store's own and `delete` is the
    session verb: a name that is not a session id is refused before the
    provider sees it, so no caller takes out the read anchor or a
    publication's branch by spelling it."""
    st = Store(tmp_path)
    with st.open("author") as ws:
        ws.files.write("app/index.html", "<h1>scores</h1>")
        ws.commit()
        st.tags.add(ws, "report")
    st.delete("author", min_age=0)
    with st.tags.at("report") as snap:  # nothing to borrow: mints the anchor
        anchored = snap.ref

    with st.open("publisher") as ws:
        ws.files.write("app/index.html", "<h1>scores</h1>")
        ws.commit()
        st.publish(ws, "scoreboard")
    st.delete("publisher", min_age=0)  # an ordinary delete, unchanged
    assert st.sessions() == []

    for reserved in ("@store/anchor", "@store/pub/scoreboard/v1"):
        with pytest.raises(ValueError, match="not a session id"):
            st.delete([reserved], min_age=0)
        assert reserved in st._branches()

    # ...so a snapshot ref taken against the anchor still resolves.
    with st.resolve(anchored) as again:
        assert again.files.read("app/index.html") == b"<h1>scores</h1>"


def test_session_scoped_tags_stay_off_the_store_surface(tmp_path):
    st = Store(tmp_path)
    with st.open("author") as ws:
        ws.terminal("echo x > x.txt")
        ws.tags.add("mine")  # session scope
        st.tags.add(ws, "ours")
    assert st.tags.list() == {"ours": st.tags.list()["ours"]}
    assert "mine" not in st.tags.list()


# -- other backends ----------------------------------------------------------


def test_dir_backend_session_set(tmp_path):
    st = Store(tmp_path, backend="dir")
    with st.open("s1") as ws:
        ws.terminal("echo hi > f.txt")
    assert st.sessions() == ["s1"]
    assert st.exists("s1")
    st.delete("s1")
    assert st.sessions() == []


def test_unversioned_backends_have_no_refs_or_tags(tmp_path):
    st = Store(tmp_path, backend="dir")
    with st.open("s1"):
        pass
    with pytest.raises(NotSupportedError):
        st.resolve("s1@abc")
    with pytest.raises(NotSupportedError):
        st.tags
    assert st.clean() == 0  # a whole-directory session has nothing to sweep


# -- bring your own substrate ------------------------------------------------


def test_provider_factory_drives_open(tmp_path):
    built = []

    def factory(session):
        built.append(session)
        return KvgitProvider.open(None, session=session)

    st = Store(provider_factory=factory)
    with st.open("custom") as ws:
        assert ws.session == "custom"
        ws.terminal("echo x > x.txt")
    assert built == ["custom"]


def test_provider_factory_refuses_the_store_level_verbs():
    """The factory owns where state lives, so the store cannot list,
    delete or sweep it — saying so beats guessing a layout."""
    st = Store(provider_factory=lambda s: KvgitProvider.open(None, session=s))
    for call in (
        st.sessions,
        lambda: st.delete("x"),
        st.clean,
        lambda: st.tags,
        lambda: st.resolve("a@b"),
    ):
        with pytest.raises(NotSupportedError, match="provider_factory"):
            call()


# -- planned surface ---------------------------------------------------------


def test_shared_is_declared_not_half_built(tmp_path):
    """`publish` landed (tests/test_publish.py); the shared plane has
    not."""
    st = Store(tmp_path)
    with pytest.raises(NotImplementedError):
        st.shared("notes")


def test_store_is_a_context_manager(tmp_path):
    with Store(tmp_path) as st:
        with st.open("s"):
            pass
        assert st.sessions() == ["s"]


def test_tags_add_refuses_a_workspace_from_another_store(tmp_path):
    """Tagging goes through the workspace's own provider, so a
    workspace from elsewhere would write to ITS store and leave this
    one's listing empty — a silent no-op. It is refused instead."""
    a = Store(tmp_path / "a")
    b = Store(tmp_path / "b")
    with b.open("s") as from_b:
        from_b.terminal("echo x > x.txt")
        with pytest.raises(WorkspaceError, match="not to"):
            a.tags.add(from_b, "release")
        assert a.tags.list() == {}
        assert b.tags.list() == {}


def test_tags_add_refuses_an_unowned_workspace(tmp_path):
    """A workspace built straight from a provider has no store to
    speak for it, so no store may tag through it."""
    ws = Workspace(KvgitProvider.open(None, session="loose"))
    try:
        ws.terminal("echo x > x.txt")
        with pytest.raises(WorkspaceError, match="no store owns it"):
            Store(tmp_path).tags.add(ws, "release")
    finally:
        ws.close()


def test_tags_add_accepts_forks_and_snapshots_of_its_own(tmp_path):
    """The stamp travels with fork() and at_tag(): a fork is still on
    the store its parent came from."""
    st = Store(tmp_path)
    with st.open("parent") as ws:
        ws.terminal("echo x > x.txt")
        with ws.fork("kid") as kid:
            kid.terminal("echo y > y.txt")
            st.tags.add(kid, "from-a-fork")
    assert "from-a-fork" in st.tags.list()


def test_dir_backend_lists_directories_only(tmp_path):
    """A session IS a directory on this backend, so a stray file in the
    store is not one — reporting it would hand back a name open()
    cannot use."""
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "notes.txt").write_text("not a session")
    (tmp_path / "loose").write_text("nor is this")
    st = Store(tmp_path, backend="dir")
    with st.open("real"):
        pass
    assert st.sessions() == ["real"]
    assert not st.exists("loose")
    assert not st.exists("notes.txt")


def test_agentfs_backend_lists_files_only(tmp_path):
    """The mirror rule: a session is one db file, so a directory that
    happens to be named like one is not a session."""
    (tmp_path / "impostor.db").mkdir()
    (tmp_path / "real.db").write_bytes(b"")
    st = Store(tmp_path, backend="agentfs")
    assert st.sessions() == ["real"]
    assert not st.exists("impostor")


# -- execution settings on a frozen open -------------------------------------


class Db:
    """A live host object: the thing a commit cannot hold."""

    def __init__(self, answer="live"):
        self.answer = answer

    def read(self):
        return self.answer


def _snapshot_source(st, session="app"):
    """A committed session, its ref, and a store tag naming its head."""
    ws = st.open(session)
    ws.files.write("app/api/board.py", "def get(req):\n    return {'n': 1}\n")
    ws.commit()
    ref = ws.ref
    st.tags.add(ws, "published")
    ws.close()
    return ref


def test_the_frozen_settings_are_store_opens_settings(tmp_path):
    """The two surfaces are hand-written lists, so they can drift. A
    frozen open takes every construction keyword ``Store.open`` takes
    except ``autocommit``, which a provider that commits nothing has
    nothing to switch."""
    import inspect

    from nontainer.store import _FROZEN_SETTINGS

    live = {
        name
        for name, p in inspect.signature(Store.open).parameters.items()
        if p.kind is inspect.Parameter.KEYWORD_ONLY
    }
    assert set(_FROZEN_SETTINGS) == live - {"autocommit"}
    # and each one is really a Workspace construction argument
    built = inspect.signature(Workspace.__init__).parameters
    assert set(_FROZEN_SETTINGS) <= set(built)


def test_a_store_tag_opens_with_the_embedders_host_objects(tmp_path):
    """The store opens a tree, not a session, so it inherits no
    settings — the embedder supplies them at the call."""
    from nontainer import PythonConfig

    st = Store(tmp_path)
    _snapshot_source(st)
    db = Db()
    with st.tags.at("published", python=PythonConfig(host_objects={"db": db})) as snap:
        assert snap.frozen
        assert snap.runtime.python_config.host_objects["db"] is db
        assert snap.run_python("out = db.read()").namespace["out"] == "live"


def test_resolve_opens_with_the_embedders_host_objects(tmp_path):
    from nontainer import PythonConfig

    st = Store(tmp_path)
    ref = _snapshot_source(st)
    db = Db("from the ref")
    with st.resolve(ref, python=PythonConfig(host_objects={"db": db})) as snap:
        assert snap.runtime.python_config.host_objects["db"] is db
        assert snap.run_python("out = db.read()").namespace["out"] == "from the ref"


def test_a_frozen_open_with_no_settings_is_bare(tmp_path):
    """Passing nothing is the old call: a readable, writable-nowhere
    snapshot with the default config and no host objects."""
    st = Store(tmp_path)
    ref = _snapshot_source(st)
    for snap in (st.tags.at("published"), st.resolve(ref)):
        assert snap.frozen
        assert snap.root == "/workspace"
        assert snap.runtime.python_config.host_objects == {}
        assert snap.files.exists("app/api/board.py")
        with pytest.raises(NotSupportedError):
            snap.files.write("app/api/board.py", "nope")
        snap.close()


def test_mounts_reach_a_frozen_open_read_only(tmp_path):
    """A mount is a live host directory, so it is settings, not state —
    and the frozen wrapper sits over the whole composed filesystem, so
    the mounted bytes read and refuse writes like the rest."""
    from nontainer import Mount

    src = tmp_path / "share"
    src.mkdir()
    (src / "seed.txt").write_text("from the host")
    st = Store(tmp_path / "store")
    _snapshot_source(st)
    with st.tags.at("published", mounts={"/data": Mount(src)}) as snap:
        assert snap.files.read("/data/seed.txt") == b"from the host"
        assert "from the host" in snap.terminal("cat /data/seed.txt").stdout
        assert (
            snap.run_python("open('/data/seed.txt', 'w').write('nope')").error
            is not None
        )
        # the host-side escape hatch reaches the same composed filesystem,
        # so the mount refuses it too
        with pytest.raises(PermissionError):
            snap.files.fs.write("/data/seed.txt", b"nope")
    assert (src / "seed.txt").read_text() == "from the host"


@pytest.mark.parametrize(
    "call",
    [
        lambda st, ref, pub, m: st.tags.at("published", mounts=m),
        lambda st, ref, pub, m: st.resolve(ref, mounts=m),
        lambda st, ref, pub, m: pub.open(mounts=m),
    ],
)
def test_a_frozen_open_refuses_a_writable_mount(tmp_path, call):
    """A frozen workspace accepts no writes from anyone. A mount is the
    one part of its filesystem that is a real host directory, and
    ``files.fs`` would carry a write straight into it — so the flag is
    refused rather than coerced, and the caller hears about it."""
    from nontainer import Mount

    src = tmp_path / "share"
    src.mkdir()
    (src / "seed.txt").write_text("from the host")
    st = Store(tmp_path / "store")
    ref = _snapshot_source(st)
    with st.open("app") as ws:
        pub = st.publish(ws, "board")

    mounts = {"/data": Mount(src, readonly=False)}
    with pytest.raises(ValueError, match=r"'/data'.*read-only mounts only"):
        call(st, ref, pub, mounts)
    assert (src / "seed.txt").read_text() == "from the host"


@pytest.mark.parametrize(
    "call",
    [
        lambda st, ref: st.tags.at("published", autocommit=False),
        lambda st, ref: st.tags.at("published", provider=None),
        lambda st, ref: st.resolve(ref, executor=None),
        lambda st, ref: st.resolve(ref, pythn=None),
    ],
)
def test_a_frozen_open_names_the_settings_it_takes(tmp_path, call):
    """``**settings`` accepts any name, so an unknown one has to be
    refused here or it is silently dropped."""
    st = Store(tmp_path)
    ref = _snapshot_source(st)
    with pytest.raises(TypeError, match="a frozen open takes python, mounts"):
        call(st, ref)
