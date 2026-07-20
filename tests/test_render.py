"""Result rendering, tool-exposure heuristic, and tool descriptions."""

import pytest

from nontainer import PythonConfig, Workspace
from nontainer.adapters.render import (
    render_python,
    render_terminal,
    resolve_tools_mode,
    terminal_description,
)
from nontainer.providers import KvgitProvider
from nontainer.workspace import PythonResult, TerminalResult


def make_ws(**kwargs) -> Workspace:
    return Workspace(KvgitProvider.open(None, session="s1"), **kwargs)


# -- rendering ---------------------------------------------------------------


def test_render_terminal_success():
    assert render_terminal(TerminalResult(stdout="hi\n", exit_code=0)) == "hi"


def test_render_terminal_failure_and_truncation():
    out = render_terminal(
        TerminalResult(stdout="part", exit_code=1, stderr="boom", truncated=True)
    )
    assert "[exit code 1]" in out
    assert "[stderr]\nboom" in out
    assert "[output truncated]" in out


def test_render_python_never_inlines_namespace():
    r = PythonResult(stdout="", namespace={"ui": {"secret": list(range(1000))}})
    out = render_python(r)
    assert "secret" not in out
    assert "[namespace kept for host: ui]" in out


def test_render_python_error():
    out = render_python(PythonResult(stdout="x", error="Traceback...ZeroDivision"))
    assert "[error]" in out and "ZeroDivision" in out


# -- exposure heuristic --------------------------------------------------------


def test_auto_mode_split_when_cache_enabled():
    ws = make_ws()  # cache on by default
    assert resolve_tools_mode(ws, "auto") == "split"
    ws.close()


def test_auto_mode_terminal_when_plain():
    ws = make_ws(cache=False)
    assert resolve_tools_mode(ws, "auto") == "terminal"
    ws.close()


def test_auto_mode_split_when_host_objects():
    ws = make_ws(cache=False, python=PythonConfig(host_objects={"db": {"x": 1}}))
    assert resolve_tools_mode(ws, "auto") == "split"
    ws.close()


def test_explicit_mode_wins():
    ws = make_ws()
    assert resolve_tools_mode(ws, "terminal") == "terminal"
    ws.close()


# -- tool descriptions ---------------------------------------------------------


def test_terminal_description_mentions_cache_only_when_terminal_only():
    ws = make_ws()
    assert "cache" not in terminal_description(ws, split=True)
    assert "cache" in terminal_description(ws, split=False)
    ws.close()


def test_terminal_description_includes_apps_contract():
    ws = make_ws()
    plain = terminal_description(ws, split=True, apps=False)
    with_apps = terminal_description(ws, split=True, apps=True)
    assert "def get(req)" not in plain
    for marker in ("def get(req)", "HttpError", "curl /api/scores",
                   "RELATIVE urls", "/workspace/app/logs/api.log", "READ-ONLY"):
        assert marker in with_apps, marker
    ws.close()


def test_apps_notes_derive_from_config():
    """The script-host sentence states what the walls actually enforce,
    and apps_primer (embedder guidance) lands at the end — the agent is
    never taught an allowlist the config has replaced."""
    from nontainer.adapters.render import apps_notes
    from nontainer.apps import AppsConfig

    assert "esm.sh, unpkg.com" in apps_notes()  # defaults

    cfg = AppsConfig(
        script_hosts=("esm.corp.internal",),
        apps_primer="Design system: import from "
        "'https://esm.corp.internal/@acme/ds@3'",
    )
    notes = apps_notes(cfg)
    assert "esm.corp.internal" in notes
    assert "unpkg.com" not in notes
    assert notes.rstrip().endswith("'https://esm.corp.internal/@acme/ds@3'")


def test_terminal_description_carries_apps_config():
    """The adapter path: an AppRuntime's config (not a bare bool) flows
    into the terminal description."""
    from nontainer.apps import AppsConfig

    ws = make_ws()
    cfg = AppsConfig(script_hosts=("esm.corp.internal",),
                     apps_primer="HOUSE RULES")
    desc = terminal_description(ws, split=True, apps=cfg)
    assert "esm.corp.internal" in desc
    assert "HOUSE RULES" in desc
    ws.close()

def test_apps_notes_teach_curl_only_where_it_exists():
    """curl is an injected terminal command, so it exists only where
    the executor honors those. Teaching it to an agent running real
    bash costs turns: the primer promises a tool, the shell answers
    'command not found', and the agent debugs the app instead."""
    from nontainer.adapters.render import apps_notes

    with_curl = apps_notes(commands=True)
    assert "curl /api/scores?limit=3" in with_curl
    assert "no curl here" not in with_curl

    without = apps_notes(commands=False)
    assert "curl /api/scores" not in without
    assert "There is no curl here" in without
    # steered to the path that exists, and warned off the one that
    # looks equivalent but isn't (direct calls skip the read-only GET)
    assert "test_app" in without
    assert "read-only filesystem" in without

    for notes in (with_curl, without):
        assert "__CURL_NOTE__" not in notes


def test_terminal_description_gates_curl_on_the_executor():
    """End to end: the capability rides from the executor through the
    workspace into the tool description the agent actually reads."""
    pytest.importorskip("dud")
    from nontainer.executor_dud import DudExecutor

    local = Workspace(KvgitProvider.open(None, session="primer-local"))
    try:
        assert local.supports_commands is True
        assert "curl /api/scores?limit=3" in terminal_description(
            local, apps=True, split=False
        )
    finally:
        local.close()

    guest = Workspace(
        KvgitProvider.open(None, session="primer-guest"),
        executor=DudExecutor(backend="subprocess"),
    )
    try:
        assert guest.supports_commands is False
        assert "There is no curl here" in terminal_description(
            guest, apps=True, split=False
        )
    finally:
        guest.close()


def test_unknown_executors_keep_the_historical_default():
    """A third-party executor predating the flag keeps curl in its
    primer — losing it silently would be the worse failure."""

    class OldExecutor:
        pass

    ws = Workspace.__new__(Workspace)
    ws._executor = OldExecutor()
    assert ws.supports_commands is True
