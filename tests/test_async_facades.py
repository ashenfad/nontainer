"""aterminal / arun_python: async host facades over the sync tools."""

import asyncio

import pytest

from nontainer import Workspace
from nontainer.providers import KvgitProvider


@pytest.fixture
def kv_ws():
    ws = Workspace(KvgitProvider.open(None, session="s1"))
    yield ws
    ws.close()


async def test_aterminal_matches_terminal(kv_ws):
    r = await kv_ws.aterminal("echo hi > f.txt; cat f.txt")
    assert r.stdout.strip() == "hi"
    assert r.checkpoint  # mutating call committed, same as sync
    # visible to a following sync call — one shared world
    assert kv_ws.terminal("cat f.txt").stdout.strip() == "hi"


async def test_arun_python_carries_inputs_and_cache(kv_ws):
    r = await kv_ws.arun_python(
        "cache['n'] = n * 2\nprint(cache['n'])", inputs={"n": 21}
    )
    assert r, r.error
    assert r.stdout.strip() == "42"
    assert kv_ws.cache["n"] == 42
    assert r.checkpoint


async def test_facades_interleave_across_workspaces(tmp_path):
    """Two independent workspaces' async calls run concurrently on one
    loop without blocking each other (different sessions, no shared lock)."""
    a = Workspace(KvgitProvider.open(None, session="a"))
    b = Workspace(KvgitProvider.open(None, session="b"))
    ra, rb = await asyncio.gather(
        a.aterminal("echo aaa > who.txt; cat who.txt"),
        b.aterminal("echo bbb > who.txt; cat who.txt"),
    )
    assert ra.stdout.strip() == "aaa"
    assert rb.stdout.strip() == "bbb"
    a.close()
    b.close()
