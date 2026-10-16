"""Agent tools can keep UI outputs without transporting scratch data."""

import pytest

from nontainer import PythonConfig, workspace


@pytest.mark.parametrize("isolation", ["none", "process"])
def test_selected_outputs_keep_cache_and_ui(isolation):
    with workspace(
        "outputs",
        memory=True,
        python=PythonConfig(isolation=isolation, result_names=("ui",)),
    ) as ws:
        result = ws.run_python(
            "scratch = list(range(10000))\n"
            "cache['answer'] = 42\nui = {'answer': 42}\nprint('done')"
        )
        assert result.error is None
        assert result.namespace == {"ui": {"answer": 42}}
        assert result.stdout == "done\n"
        assert ws.cache["answer"] == 42


def test_default_namespace_still_returns_locals():
    with workspace("outputs-default", memory=True) as ws:
        assert ws.run_python("answer = 42").namespace == {"answer": 42}
