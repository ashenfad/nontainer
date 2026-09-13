"""The ``ws-pytest`` terminal verb: its flag surface and its refusals.

The runner's own behaviour lives in test_wspytest.py; what is tested
here is the verb — what an agent types, what comes back, and what exit
code a pipeline sees.
"""

import pytest

from nontainer import Workspace
from nontainer.apps import enable_apps
from nontainer.providers import KvgitProvider

TESTS = (
    "def test_ok():\n"
    "    assert True\n"
    "\n"
    "\n"
    "def test_bad():\n"
    "    assert 1 == 2\n"
    "\n"
    "\n"
    "def test_also_bad():\n"
    "    assert 'a' == 'b'\n"
)


@pytest.fixture
def ws():
    w = Workspace(KvgitProvider.open(None, session="wspytest-verb"))
    enable_apps(w)
    w.files.fs.write("/workspace/tests/test_a.py", TESTS.encode())
    try:
        yield w
    finally:
        w.close()


def test_enable_apps_registers_the_verb(ws):
    assert "ws-pytest" in ws.runtime.commands


def test_registering_twice_is_a_no_op(ws):
    """Two doors lead to the verb — the apps loop and a direct call —
    and an embedder that uses both must not get a duplicate-name
    error."""
    from nontainer.wspytest import register_wspytest

    register_wspytest(ws)
    assert ws.terminal("ws-pytest -q").exit_code == 1


def test_a_failing_run_exits_one_and_shows_the_failures(ws):
    r = ws.terminal("ws-pytest")
    assert r.exit_code == 1
    assert "collected 3 items" in r.stdout
    assert "tests/test_a.py .FF" in r.stdout
    assert "2 failed, 1 passed in " in r.stdout


def test_a_green_run_exits_zero(ws):
    ws.files.fs.write(
        "/workspace/tests/test_a.py", b"def test_ok():\n    assert True\n"
    )
    r = ws.terminal("ws-pytest")
    assert r.exit_code == 0
    assert "1 passed in " in r.stdout


def test_a_pipeline_reads_the_summary(ws):
    r = ws.terminal("ws-pytest -q | tail -n 1")
    assert "2 failed, 1 passed" in r.stdout


def test_one_test_by_name(ws):
    r = ws.terminal("ws-pytest tests/test_a.py::test_ok")
    assert r.exit_code == 0
    assert "collected 1 item" in r.stdout


def test_keyword_and_exitfirst(ws):
    r = ws.terminal("ws-pytest -k 'bad and not also'")
    assert r.exit_code == 1
    assert "collected 1 item" in r.stdout
    r = ws.terminal("ws-pytest -x")
    assert r.exit_code == 1
    assert "1 failed, 1 passed in " in r.stdout
    r = ws.terminal("ws-pytest --maxfail=2")
    assert "2 failed, 1 passed in " in r.stdout


def test_verbosity_and_traceback_style(ws):
    r = ws.terminal("ws-pytest -v")
    assert "tests/test_a.py::test_ok PASSED" in r.stdout
    r = ws.terminal("ws-pytest --tb=no")
    assert "FAILURES" not in r.stdout
    r = ws.terminal("ws-pytest --tb=long")
    assert ">       assert 1 == 2" in r.stdout


def test_refused_flags_name_what_to_do_instead(ws):
    for flag, phrase in (
        ("-s", "stdout is already in the report"),
        ("--lf", "name the test instead"),
        ("-p nothing", "there are no plugins here"),
        ("--co", "use -q with a path"),
        ("-m slow", "there are no markers here"),
    ):
        r = ws.terminal(f"ws-pytest {flag}")
        assert r.exit_code == 4, flag
        assert phrase in (r.stdout + r.stderr), flag


def test_an_unknown_flag_shows_the_usage(ws):
    r = ws.terminal("ws-pytest --frobnicate")
    assert r.exit_code == 4
    assert "unknown flag --frobnicate" in (r.stdout + r.stderr)
    assert "usage: ws-pytest" in (r.stdout + r.stderr)


def test_a_missing_path_is_a_usage_error(ws):
    r = ws.terminal("ws-pytest tests/test_nope.py")
    assert r.exit_code == 4
    assert "not found" in (r.stdout + r.stderr)


def test_nothing_to_collect_exits_five(ws):
    ws.files.fs.remove("/workspace/tests/test_a.py")
    r = ws.terminal("ws-pytest")
    assert r.exit_code == 5
    assert "no tests ran in " in r.stdout


def test_help_names_the_layout_rule_and_the_flags(ws):
    r = ws.terminal("ws-pytest --help")
    assert r.exit_code == 0
    assert "never under app/" in r.stdout
    assert "--maxfail=N" in r.stdout
    assert "no fixtures, no conftest" in r.stdout


def test_a_selector_naming_no_such_test_is_a_usage_error(ws):
    """A typo in a test name must not read as an empty suite: nothing
    ran either way, and only one of them is the agent's mistake."""
    r = ws.terminal("ws-pytest tests/test_a.py::test_typo")
    assert r.exit_code == 4
    said = r.stdout + r.stderr
    assert "not found: tests/test_a.py::test_typo" in said
    assert "test_ok" in said and "test_bad" in said


def test_a_keyword_matching_nothing_is_still_an_empty_run(ws):
    """-k is a filter, not a name: matching nothing collects nothing,
    which is exit 5 rather than a usage error."""
    r = ws.terminal("ws-pytest -k nosuchsubstring")
    assert r.exit_code == 5
    assert "no tests ran in " in r.stdout
