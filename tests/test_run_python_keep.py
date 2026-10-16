"""Choosing what ``run_python`` returns (``keep``).

``keep`` names the bindings ``result.namespace`` carries. Under process
isolation the rest stay in the worker, unpickled. The agent's tool reads
only ``ui``; apps and ws-pytest read their own wire bindings.
"""

from __future__ import annotations

import pytest
from plain_provider import PlainProvider

from nontainer import PythonConfig, Workspace
from nontainer.adapters.tools import Toolset
from nontainer.providers.kvgit import KvgitProvider
from nontainer.runtime import _accepts_keep
from nontainer.workspace import PythonResult

ISOLATIONS = ("none", "process")


@pytest.fixture(params=ISOLATIONS)
def ws(request, tmp_path):
    workspace = Workspace(
        PlainProvider(tmp_path / "ws", session="keep"),
        python=PythonConfig(isolation=request.param),
    )
    yield workspace
    workspace.close()


def test_keep_returns_only_the_named_bindings(ws):
    result = ws.run_python("a = 1\nb = 2\nc = 3\n", keep=("a", "c"))
    assert result.error is None, result.error
    assert result.namespace == {"a": 1, "c": 3}


def test_without_keep_every_binding_comes_back(ws):
    result = ws.run_python("a = 1\nb = 2\n")
    assert result.error is None, result.error
    assert result.namespace == {"a": 1, "b": 2}


def test_an_empty_keep_returns_nothing(ws):
    result = ws.run_python("a = 1\n", keep=())
    assert result.error is None, result.error
    assert result.namespace == {}


def test_a_binding_left_out_need_not_be_picklable(ws):
    """Out of a worker, an unpicklable binding used to be dropped with a
    warning; one the caller didn't ask for is now never tried."""
    result = ws.run_python("fn = lambda: 1\nn = 2\n", keep=("n",))
    assert result.error is None, result.error
    assert result.namespace == {"n": 2}


@pytest.mark.parametrize("keep", ["ui", b"ui", 3, ("ui", 3)])
def test_keep_refuses_what_is_not_a_collection_of_names(ws, keep):
    with pytest.raises(TypeError, match="keep"):
        ws.run_python("a = 1\n", keep=keep)


async def test_arun_python_takes_keep_too(ws):
    result = await ws.arun_python("a = 1\nb = 2\n", keep=("b",))
    assert result.namespace == {"b": 2}


def test_ui_is_materialized_even_when_not_kept():
    """Turning rich ``ui`` values into artifacts is part of what the call
    did, so it happens whatever the caller reads back."""
    pytest.importorskip("pandas")
    from nontainer.presets import dataframes

    ws = Workspace(
        KvgitProvider.open(None, session="keep-ui"),
        python=PythonConfig(modules=[dataframes()]),
    )
    try:
        result = ws.run_python(
            "import pandas as pd\nui = {'t': pd.DataFrame({'x': [1]})}\nn = 1\n",
            keep=("n",),
        )
        assert result.error is None, result.error
        assert result.namespace == {"n": 1}
        assert any(p.endswith(".table.json") for p in ws.files.fs.list("/workspace/ui"))
    finally:
        ws.close()


def test_the_agents_tool_asks_only_for_ui(ws, monkeypatch):
    seen = {}
    original = ws.run_python

    def spy(code, **kwargs):
        seen.update(kwargs)
        return original(code, **kwargs)

    monkeypatch.setattr(ws, "run_python", spy)
    out = Toolset(ws).run_python("scratch = list(range(10))\nprint('hi')\n")
    assert seen.get("keep") == ("ui",)
    assert "hi" in out.text


# ------------------------------------------------------------------
# Executors written before keep existed
# ------------------------------------------------------------------


class _OldExecutor:
    def exec_python(
        self, code, *, inputs=None, stdin=None, argv=None, echo=None, view=None
    ):
        return PythonResult(stdout="", namespace={"a": 1, "b": 2})


class _NewExecutor(_OldExecutor):
    def exec_python(self, code, *, keep=None, **kwargs):
        return PythonResult(stdout="", namespace={"kept": sorted(keep or ())})


class _KwargsExecutor(_OldExecutor):
    def exec_python(self, code, **kwargs):
        return PythonResult(stdout="", namespace={})


def test_an_executor_is_asked_to_select_only_when_it_can():
    assert _accepts_keep(_NewExecutor())
    assert _accepts_keep(_KwargsExecutor())
    assert not _accepts_keep(_OldExecutor())


def _bare_runtime(executor):
    from nontainer.runtime import Runtime

    rt = Runtime.__new__(Runtime)
    rt._executor = executor
    rt.sync_if_stale = lambda: None
    return rt


def test_an_old_executor_still_serves_a_caller_that_selects():
    """The runtime narrows the namespace itself for an executor that
    can't, so the agent's tool works on any executor."""
    result = _bare_runtime(_OldExecutor()).exec_python("x", keep=("b",))
    assert result.namespace == {"b": 2}


def test_an_old_executor_serves_a_caller_that_doesnt_select():
    result = _bare_runtime(_OldExecutor()).exec_python("x")
    assert result.namespace == {"a": 1, "b": 2}


def test_an_executor_that_selects_gets_the_names():
    result = _bare_runtime(_NewExecutor()).exec_python("x", keep=["b", "a"])
    assert result.namespace == {"kept": ["a", "b"]}


def test_the_dud_rung_narrows_what_the_guest_returned():
    from nontainer.executor_dud import _kept

    result = PythonResult(stdout="", namespace={"ui": 1, "scratch": 2})
    assert _kept(result, ("ui",)).namespace == {"ui": 1}
    assert _kept(result, None) is result
