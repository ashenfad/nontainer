"""Store: the session set, and what outlives one session.

``workspace()`` is sugar over ``Store.open`` now, so the first thing
these check is that the sugar and the object are the same call.
"""

import pytest

from nontainer import (
    CheckpointNotFoundError,
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
        ws.tag("published", scope="store")
        ws.tag("mine")
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


def test_clean_sweeps_unreachable_commits(tmp_path):
    """Rolling back leaves commits nothing reaches; clean() removes
    them, and only them."""
    st = Store(tmp_path)
    with st.open("gc") as ws:
        ws.terminal("echo one > f.txt")
        ws.terminal("echo two > f.txt")
        ws.terminal("echo three > f.txt")
        ws.rollback(2)
        assert ws.terminal("cat f.txt").stdout.strip() == "one"

    assert st.clean(min_age=0) > 0
    with st.open("gc") as ws:
        assert ws.terminal("cat f.txt").stdout.strip() == "one"
    assert st.clean(min_age=0) == 0  # nothing left to sweep


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
    with pytest.raises(CheckpointNotFoundError):
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
    with pytest.raises(CheckpointNotFoundError):
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
    its checkpoint survive the deletion of the session that made it."""
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


def test_session_scoped_tags_stay_off_the_store_surface(tmp_path):
    st = Store(tmp_path)
    with st.open("author") as ws:
        ws.terminal("echo x > x.txt")
        ws.tag("mine")  # session scope
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


def test_shared_and_publish_are_declared_not_half_built(tmp_path):
    st = Store(tmp_path)
    with pytest.raises(NotImplementedError):
        st.shared("notes")
    with pytest.raises(NotImplementedError):
        st.publish(None, "app")


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
