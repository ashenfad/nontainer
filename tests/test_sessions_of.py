"""A world's helper, found from the world: what a host object built as
the world opens delegates through, once the harness has built it."""

import asyncio

import pytest

from nontainer import Store
from nontainer.sessions import Sessions


class Echo:
    def run(self, session, task, *, budget=None):
        return f"did {task}"


class AsyncEcho:
    async def run(self, session, task, *, budget=None):
        return f"did {task}"


@pytest.fixture
def ws():
    store = Store(memory=True)
    ws = store.open("lead")
    ws.files.write("/workspace/a.md", "a")
    ws.index.commit("seed")
    yield ws
    ws.close()
    store.close()


def test_a_world_without_a_helper_has_none(ws):
    assert Sessions.of(ws) is None


def test_the_open_helper_is_the_worlds(ws):
    with Sessions(ws, Echo()) as helper:
        assert Sessions.of(ws) is helper
    assert Sessions.of(ws) is None


def test_the_latest_open_helper_wins_and_the_last_one_comes_back(ws):
    first = Sessions(ws, Echo())
    second = Sessions(ws, Echo())
    try:
        assert Sessions.of(ws) is second
        second.close()
        assert Sessions.of(ws) is first
    finally:
        first.close()
        second.close()
    assert Sessions.of(ws) is None


def test_a_delegates_helper_is_its_own_not_its_parents(ws):
    with Sessions(ws, Echo()) as helper:
        child = ws.fork("lead.child")
        try:
            assert Sessions.of(child) is None
            with Sessions(child, Echo()) as theirs:
                assert Sessions.of(child) is theirs
                assert Sessions.of(ws) is helper
        finally:
            child.close()


def test_a_helper_closed_from_a_coroutine_is_gone_too(ws):
    async def scenario():
        helper = Sessions(ws, AsyncEcho())
        assert Sessions.of(ws) is helper
        await helper.aclose()
        assert Sessions.of(ws) is None

    asyncio.run(scenario())


def test_a_helper_nobody_holds_is_not_kept_alive(ws):
    import gc
    import weakref

    helper = Sessions(ws, Echo())
    ref = weakref.ref(helper)
    helper._pool.shutdown(wait=True)  # its threads hold it while they live
    del helper
    gc.collect()
    assert ref() is None
    assert Sessions.of(ws) is None
