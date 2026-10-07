"""Profile: a session's world as one value, round-tripped through
Store.open and inherited by forks."""

import dataclasses

import pytest

from nontainer import Mount, Profile, PythonConfig, Store, workspace


def _hello(ctx):
    ctx.stdout.write("hello\n")


def _profile(tmp_path, **overrides) -> Profile:
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    (data / "x.csv").write_text("a,b\n1,2\n")
    fields = dict(
        python=PythonConfig(host_objects={"limit": 3}),
        mounts={"/data": Mount(str(data.resolve()))},
        commands={"hello": _hello},
        root="/workspace",
        ignore=("*.tmp",),
        variables={"GREETING": "hi there", "RETRIES": 3},
    )
    fields.update(overrides)
    return Profile(**fields)


def test_profile_round_trips_through_store_open(tmp_path):
    profile = _profile(tmp_path)
    st = Store(memory=True)
    ws = st.open("s", profile=profile)
    got = Profile.of(ws)
    assert got.python == profile.python
    assert got.commands == profile.commands
    assert got.root == profile.root
    assert got.ignore == profile.ignore
    assert got.executor_factory is profile.executor_factory
    assert set(got.mounts) == set(profile.mounts)
    # read back once, a profile is a fixed point
    again = st.open("t", profile=got)
    assert Profile.of(again) == got
    # and the session really runs in it
    assert ws.terminal("hello").stdout.strip() == "hello"
    assert "a,b" in ws.terminal("cat /data/x.csv").stdout
    again.close()
    ws.close()


def test_a_fork_inherits_the_profile(tmp_path):
    profile = _profile(tmp_path)
    st = Store(memory=True)
    ws = st.open("s", profile=profile)
    ws.files.write("a.txt", "x")
    ws.commit(info={"tool": "seed"})
    child = ws.fork("s.child")
    assert Profile.of(child) == Profile.of(ws)
    child.close()
    ws.close()


def test_store_fork_opens_source_and_child_in_the_profile(tmp_path):
    profile = _profile(tmp_path, root="/home/agent")
    st = Store(memory=True)
    ws = st.open("src", profile=profile)
    ws.files.write("a.txt", "x")
    ws.commit(info={"tool": "seed"})
    ws.close()
    child = st.fork("src", "dst", profile=profile)
    assert child.root == "/home/agent"
    assert Profile.of(child) == Profile.of(st.open("src", profile=profile))
    assert child.files.read("a.txt") == b"x"
    child.close()


@pytest.mark.parametrize(
    "keyword, value",
    [
        ("python", PythonConfig()),
        ("mounts", {}),
        ("commands", {}),
        ("executor_factory", lambda: None),
        ("root", "/workspace"),
        ("ignore", ()),
    ],
)
def test_profile_with_one_of_its_fields_is_refused(keyword, value):
    st = Store(memory=True)
    with pytest.raises(TypeError, match=keyword):
        st.open("s", profile=Profile(), **{keyword: value})
    with pytest.raises(TypeError, match=keyword):
        workspace("s", memory=True, profile=Profile(), **{keyword: value})


def test_without_env_the_defaults_are_unchanged():
    ws = Store(memory=True).open("s")
    assert ws.root == "/workspace"
    assert Profile.of(ws) == Profile()
    ws.close()


def test_profile_cannot_change_once_built(tmp_path):
    profile = _profile(tmp_path)
    with pytest.raises(TypeError):
        profile.mounts["/other"] = Mount(str(tmp_path))
    with pytest.raises(TypeError):
        profile.commands["bye"] = _hello
    with pytest.raises(dataclasses.FrozenInstanceError):
        profile.root = "/elsewhere"
    variant = dataclasses.replace(profile, ignore=("*.log",))
    assert variant.ignore == ("*.log",)
    assert profile.ignore == ("*.tmp",)


def test_profile_normalizes_root_and_refuses_a_bare_string_ignore():
    assert Profile(root="/workspace/").root == "/workspace"
    with pytest.raises(TypeError, match="sequence"):
        Profile(ignore="*.tmp")


def test_workspace_helper_takes_a_profile(tmp_path):
    profile = _profile(tmp_path)
    ws = workspace("s", memory=True, profile=profile)
    assert Profile.of(ws).commands == profile.commands
    assert Profile.of(ws).python == profile.python
    ws.close()


def test_profile_variables_reach_the_shell(tmp_path):
    profile = _profile(tmp_path)
    ws = Store(memory=True).open("s", profile=profile)
    assert ws.runtime.env["GREETING"] == "hi there"
    assert ws.runtime.env["RETRIES"] == "3"  # coerced, as runtime.env does
    assert ws.terminal("echo $GREETING").stdout.strip() == "hi there"
    assert Profile.of(ws).variables == {"GREETING": "hi there", "RETRIES": "3"}
    child = ws.fork("s.child")
    assert child.runtime.env["GREETING"] == "hi there"
    child.close()
    ws.close()


def test_profile_refuses_a_variable_no_shell_could_expand():
    with pytest.raises(ValueError, match="shell variable"):
        Profile(variables={"NOT-A-NAME": "x"})


def test_variables_ride_only_in_a_profile():
    """They are not an open keyword: Store.open takes them through a
    profile, and runtime.env stays the way to set them afterwards."""
    import inspect

    assert "variables" not in inspect.signature(Store.open).parameters
