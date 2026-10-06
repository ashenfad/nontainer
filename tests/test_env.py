"""Env: a session's environment as one value, round-tripped through
Store.open and inherited by forks."""

import dataclasses

import pytest

from nontainer import Env, Mount, PythonConfig, Store, workspace


def _hello(ctx):
    ctx.stdout.write("hello\n")


def _env(tmp_path, **overrides) -> Env:
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    (data / "x.csv").write_text("a,b\n1,2\n")
    fields = dict(
        python=PythonConfig(host_objects={"limit": 3}),
        mounts={"/data": Mount(str(data.resolve()))},
        commands={"hello": _hello},
        root="/workspace",
        ignore=("*.tmp",),
    )
    fields.update(overrides)
    return Env(**fields)


def test_env_round_trips_through_store_open(tmp_path):
    env = _env(tmp_path)
    st = Store(memory=True)
    ws = st.open("s", env=env)
    got = Env.of(ws)
    assert got.python == env.python
    assert got.commands == env.commands
    assert got.root == env.root
    assert got.ignore == env.ignore
    assert got.executor_factory is env.executor_factory
    assert set(got.mounts) == set(env.mounts)
    # read back once, an env is a fixed point
    again = st.open("t", env=got)
    assert Env.of(again) == got
    # and the session really runs in it
    assert ws.terminal("hello").stdout.strip() == "hello"
    assert "a,b" in ws.terminal("cat /data/x.csv").stdout
    again.close()
    ws.close()


def test_a_fork_inherits_the_env(tmp_path):
    env = _env(tmp_path)
    st = Store(memory=True)
    ws = st.open("s", env=env)
    ws.files.write("a.txt", "x")
    ws.commit(info={"tool": "seed"})
    child = ws.fork("s.child")
    assert Env.of(child) == Env.of(ws)
    child.close()
    ws.close()


def test_store_fork_opens_source_and_child_in_the_env(tmp_path):
    env = _env(tmp_path, root="/home/agent")
    st = Store(memory=True)
    ws = st.open("src", env=env)
    ws.files.write("a.txt", "x")
    ws.commit(info={"tool": "seed"})
    ws.close()
    child = st.fork("src", "dst", env=env)
    assert child.root == "/home/agent"
    assert Env.of(child) == Env.of(st.open("src", env=env))
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
def test_env_with_one_of_its_fields_is_refused(keyword, value):
    st = Store(memory=True)
    with pytest.raises(TypeError, match=keyword):
        st.open("s", env=Env(), **{keyword: value})
    with pytest.raises(TypeError, match=keyword):
        workspace("s", memory=True, env=Env(), **{keyword: value})


def test_without_env_the_defaults_are_unchanged():
    ws = Store(memory=True).open("s")
    assert ws.root == "/workspace"
    assert Env.of(ws) == Env()
    ws.close()


def test_env_cannot_change_once_built(tmp_path):
    env = _env(tmp_path)
    with pytest.raises(TypeError):
        env.mounts["/other"] = Mount(str(tmp_path))
    with pytest.raises(TypeError):
        env.commands["bye"] = _hello
    with pytest.raises(dataclasses.FrozenInstanceError):
        env.root = "/elsewhere"
    variant = dataclasses.replace(env, ignore=("*.log",))
    assert variant.ignore == ("*.log",)
    assert env.ignore == ("*.tmp",)


def test_env_normalizes_root_and_refuses_a_bare_string_ignore():
    assert Env(root="/workspace/").root == "/workspace"
    with pytest.raises(TypeError, match="sequence"):
        Env(ignore="*.tmp")


def test_workspace_helper_takes_env(tmp_path):
    env = _env(tmp_path)
    ws = workspace("s", memory=True, env=env)
    assert Env.of(ws).commands == env.commands
    assert Env.of(ws).python == env.python
    ws.close()
