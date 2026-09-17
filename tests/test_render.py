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
from nontainer.runtime import Runtime
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


def test_render_python_never_renders_namespace():
    """Not the values, and not the names either — the agent wrote them."""
    r = PythonResult(stdout="", namespace={"ui": {"secret": list(range(1000))}})
    out = render_python(r)
    assert "secret" not in out
    assert "ui" not in out
    assert "namespace" not in out


def test_render_python_confirms_a_silent_run():
    """Bindings alone are not output, so the success signal must show —
    it used to be masked by the namespace note."""
    r = PythonResult(stdout="", namespace={"df": [1, 2], "n": 2})
    assert render_python(r) == "(no output; success)"


def test_render_python_namespace_does_not_mask_real_output():
    r = PythonResult(stdout="42\n", namespace={"n": 42})
    assert render_python(r) == "42"


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
    for marker in (
        "def get(req)",
        "HttpError",
        "ws-curl $APP_ORIGIN/api/scores",
        "RELATIVE urls",
        "/workspace/app/logs/api.log",
        "READ-ONLY",
    ):
        assert marker in with_apps, marker
    ws.close()


def test_shared_backend_code_lives_under_app():
    """A publication carries app/ and nothing else, so the advice the
    agent gets about where shared handler code goes has to point
    there. helpers/ stays the answer where there is no app to
    publish."""
    from nontainer.adapters.render import apps_notes, python_description

    notes = apps_notes(root="/workspace")
    assert "/workspace/app/api/_data.py" in notes
    assert "from app.api._data import load" in notes
    # the claim the _-prefixed line already contradicted
    assert "imports between" not in notes

    ws = make_ws()
    with_apps = python_description(ws, apps=True)
    assert "from app.api._mymod import fn" in with_apps
    plain = python_description(ws)
    assert "app/api/" not in plain
    assert "from helpers import mymod" in plain
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
        apps_primer="Design system: import from 'https://esm.corp.internal/@acme/ds@3'",
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
    cfg = AppsConfig(script_hosts=("esm.corp.internal",), apps_primer="HOUSE RULES")
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
    assert "ws-curl $APP_ORIGIN/api/scores?limit=3" in with_curl
    assert "no curl here" not in with_curl

    without = apps_notes(commands=False)
    assert "ws-curl" not in without
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
        assert local.runtime.supports_commands is True
        assert "ws-curl $APP_ORIGIN/api/scores?limit=3" in terminal_description(
            local, apps=True, split=False
        )
    finally:
        local.close()

    guest = Workspace(
        KvgitProvider.open(None, session="primer-guest"),
        executor=DudExecutor(backend="subprocess"),
    )
    try:
        assert guest.runtime.supports_commands is False
        # The dud rung ferries ws-* verbs: the primer teaches the
        # portable spelling instead of the no-curl note.
        assert guest.runtime.supports_ws_verbs is True
        desc = terminal_description(guest, apps=True, split=False)
        assert "ws-curl $APP_ORIGIN/api/scores?limit=3" in desc
        assert "There is no curl here" not in desc
    finally:
        guest.close()


def test_unknown_executors_keep_the_historical_default():
    """A third-party executor predating the flag keeps curl in its
    primer — losing it silently would be the worse failure."""

    class OldExecutor:
        pass

    ws = Workspace.__new__(Workspace)
    ws._runtime = Runtime.__new__(Runtime)
    ws._runtime._executor = OldExecutor()
    assert ws.runtime.supports_commands is True


def test_injected_names_carry_the_import_spelling():
    """A bare name is right where the code is a REPL and wrong in a
    module, so the line that names the injected objects also names the
    spelling that works everywhere."""
    from nontainer.adapters.render import python_description

    class Db:
        def query(self):
            return []

    ws = make_ws(python=PythonConfig(host_objects={"db": Db()}))
    desc = python_description(ws)
    assert "injected objects available by name: db" in desc
    assert "from host import db" in desc
    ws.close()


def test_apps_notes_offer_the_import_to_a_shared_module():
    """The recommendation stays arguments — that is what a test can call
    with a fake — but a module that wants the ambient object is told how
    to reach it instead of being left with the NameError."""
    from nontainer.adapters.render import apps_notes

    notes = apps_notes()
    assert "def load(db, limit)" in notes  # still the recommendation
    assert "from host import db" in notes


def test_frontend_tests_are_never_offered_a_home_under_app():
    """app/ is what publishes, so a .test.js there ships with the app.
    The tool descriptions name one home for a frontend test: tests/."""
    from nontainer.adapters.render import apps_notes, python_description
    from nontainer.apps import AppsConfig, enable_apps

    ws = make_ws()
    enable_apps(ws, AppsConfig())  # registers ws-pytest / ws-vitest
    surfaces = [
        apps_notes(),
        terminal_description(ws, split=True, apps=True),
        python_description(ws, apps=True),
    ]
    ws.close()
    for text in surfaces:
        assert "beside the module" not in text
        assert "tests/" in text


def test_each_test_tier_is_one_pointer_at_its_help():
    """The contract for writing a test is in `ws-pytest --help` /
    `ws-vitest --help`, which the agent reads when it needs it. The
    descriptions ride on every request, so they name the verb, the
    directory and the help, and stop there."""
    from nontainer.adapters.render import apps_notes

    notes = apps_notes()
    assert "ws-pytest --help" in notes and "ws-vitest --help" in notes
    # the details that used to be re-stated here live in the help now
    for detail in ("MagicMock", "stubFetch", "describe/it/expect"):
        assert detail not in notes, detail


def test_one_call_per_turn_is_said_once_per_description():
    """Every duplicated sentence in a tool description is paid on every
    request. The rule also rides in the toolkit instructions, read once
    a session; here it gets one sentence per tool."""
    from nontainer.adapters.render import python_description

    ws = make_ws()
    term = terminal_description(ws, split=True, apps=True)
    py = python_description(ws, apps=True)
    ws.close()
    assert term.count("call per turn") == 1
    assert py.count("call per turn") == 1


def test_handler_example_is_the_embedders_where_the_store_differs():
    """The example is what an agent copies, so an embedder whose
    handlers must use a different store replaces it rather than
    correcting it from a primer underneath."""
    from nontainer.adapters.render import apps_notes
    from nontainer.apps import AppsConfig

    default = apps_notes(AppsConfig())
    assert 'cache.get("scores", [])' in default  # the default block, verbatim
    assert "def post(req):" in default

    house = apps_notes(
        AppsConfig(
            handler_example=(
                "Handlers export verb functions; example "
                "__WS__/app/api/notes.py:\n\n"
                "    from host import db\n\n"
                "    def get(req):\n"
                "        return {'notes': db.list()}\n"
            )
        )
    )
    assert "/workspace/app/api/notes.py" in house  # __WS__ substituted
    assert "db.list()" in house
    assert 'cache.get("scores", [])' not in house
    # nontainer's own contract is untouched either way
    for notes in (default, house):
        assert "ONLY verb functions" in notes
        assert "HttpError(404, 'msg')" in notes
        assert "READ-ONLY" in notes
        assert "__HANDLER_EXAMPLE__" not in notes

    empty = apps_notes(AppsConfig(handler_example=""))
    assert "def get(req)" not in empty
    assert "ONLY verb functions" in empty
