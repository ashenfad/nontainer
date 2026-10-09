"""HostObject(factory=...): a host object each world gets its own of,
made as the world opens, forks included."""

import dataclasses
import sys

import pytest
from host_types import Row, World, WorldStub

from nontainer import HostObject, Profile, PythonConfig, Store


def open_ws(rung, entry, session="lead"):
    cfg = PythonConfig(host_objects={"world": entry})
    if rung == "dud":
        if sys.version_info < (3, 11):
            pytest.skip("dud needs Python 3.11+")
        pytest.importorskip("dud")
        from nontainer.executor_dud import DudExecutor

        profile = Profile(
            python=cfg, executor_factory=lambda: DudExecutor(backend="subprocess")
        )
    else:
        profile = Profile(python=dataclasses.replace(cfg, isolation=rung))
    store = Store(memory=True)
    return store.open(session, profile=profile), store


@pytest.fixture(params=["none", "process", "dud"])
def rung(request):
    return request.param


@pytest.fixture(params=["plain", "stubbed"])
def entry(request):
    if request.param == "plain":
        return HostObject(factory=World)
    return HostObject(factory=World, stub=WorldStub)


def name_in(ws):
    r = ws.run_python("print(world.name())")
    assert r.error is None, r.error
    return r.stdout.strip()


def test_each_world_gets_its_own_object(rung, entry):
    ws, store = open_ws(rung, entry)
    try:
        assert name_in(ws) == "lead"
        fork = ws.fork("lead.helper")
        try:
            assert name_in(fork) == "lead.helper"
            assert name_in(ws) == "lead"
        finally:
            fork.close()
    finally:
        ws.close()
        store.close()


def test_the_factory_is_called_once_per_world_with_it():
    seen = []

    def make(ws):
        seen.append(ws)
        return World(ws)

    ws, store = open_ws("none", HostObject(factory=make))
    try:
        assert seen == [ws]
        fork = ws.fork("lead.two")
        assert seen == [ws, fork]
        fork.close()
    finally:
        ws.close()
        store.close()


def test_the_profile_carries_the_factory_not_what_it_made():
    entry = HostObject(factory=World, stub=WorldStub)
    ws, store = open_ws("none", entry)
    try:
        assert Profile.of(ws).python.host_objects["world"] is entry
        made = ws.runtime.python_config.host_objects["world"]
        assert isinstance(made.obj, World) and made.stub is WorldStub
        other = store.open("other", profile=Profile.of(ws))
        try:
            assert name_in(other) == "other"
        finally:
            other.close()
    finally:
        ws.close()
        store.close()


def test_what_it_makes_is_checked_against_the_type():
    good = HostObject(factory=lambda ws: [Row("a", 1, None)], type=list[Row])
    assert good.spec is not None
    bad = HostObject(factory=lambda ws: "not rows", type=list[Row])
    with pytest.raises(TypeError, match="what HostObject's factory made"):
        open_ws("none", bad)


def test_a_factory_that_fails_fails_the_open():
    def broken(ws):
        raise RuntimeError("no world for you")

    with pytest.raises(RuntimeError, match="no world for you"):
        open_ws("none", HostObject(factory=broken))


@pytest.mark.parametrize(
    "make, message",
    [
        (lambda: HostObject(), "needs an obj"),
        (lambda: HostObject(World, factory=World), "not both"),
        (lambda: HostObject(factory="World"), "is a callable"),
        (lambda: HostObject(factory=World, stub="WorldStub"), "is a class"),
    ],
)
def test_what_is_not_an_entry_is_refused(make, message):
    with pytest.raises(TypeError, match=message):
        make()


def test_a_falsy_factory_is_still_called():
    class Maker:
        def __bool__(self):
            return False

        def __call__(self, ws):
            return World(ws)

    ws, store = open_ws("none", HostObject(factory=Maker()))
    try:
        assert name_in(ws) == "lead"
    finally:
        ws.close()
        store.close()
