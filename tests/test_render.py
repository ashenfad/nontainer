"""Result rendering, tool-exposure heuristic, and tool descriptions."""

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
