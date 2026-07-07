"""terminal_primer / python_primer: embedder guidance in tool descriptions."""

import pytest

from nontainer import Workspace
from nontainer.adapters.render import python_description, terminal_description
from nontainer.providers import KvgitProvider


def make_ws(**kwargs) -> Workspace:
    return Workspace(KvgitProvider.open(None, session="s1"), **kwargs)


_TP = "TERMINAL-NOTE: /data is pre-seeded."
_PP = "PYTHON-NOTE: db is a sqlite connection; use SQL for state."


# -- description builders --------------------------------------------------


def test_primers_land_in_their_own_surface_when_split():
    ws = make_ws()  # cache on → split
    term = terminal_description(ws, split=True, primer=_TP, python_primer=_PP)
    py = python_description(ws, primer=_PP)
    assert _TP in term and _PP not in term  # python_primer NOT in terminal (split)
    assert _PP in py


def test_python_primer_lands_in_terminal_when_terminal_only():
    ws = make_ws(cache=False)  # plain → terminal-only
    term = terminal_description(
        ws, split=False, primer=_TP, python_primer=_PP
    )
    assert _TP in term and _PP in term  # both in the single terminal tool


def test_no_primer_is_unchanged():
    ws = make_ws()
    assert "NOTE" not in terminal_description(ws, split=True)
    assert "NOTE" not in python_description(ws)


# -- agno adapter ----------------------------------------------------------


def test_agno_primers_reach_the_right_tools():
    pytest.importorskip("agno")
    from nontainer.adapters.agno import WorkspaceTools

    ws = make_ws()  # split
    tk = WorkspaceTools(ws, terminal_primer=_TP, python_primer=_PP)
    assert _TP in (tk.functions["terminal"].entrypoint.__doc__ or "")
    assert _PP in (tk.functions["run_python"].entrypoint.__doc__ or "")
    # python_primer must not bleed into the terminal tool in split mode
    assert _PP not in (tk.functions["terminal"].entrypoint.__doc__ or "")
    ws.close()


def test_agno_warns_when_python_primer_but_terminal_only():
    pytest.importorskip("agno")
    from nontainer.adapters.agno import WorkspaceTools

    ws = make_ws(cache=False)  # terminal-only
    with pytest.warns(UserWarning, match="terminal-only"):
        tk = WorkspaceTools(ws, python_primer=_PP)
    # it still lands (in the terminal tool's python section)
    assert _PP in (tk.functions["terminal"].entrypoint.__doc__ or "")
    ws.close()
