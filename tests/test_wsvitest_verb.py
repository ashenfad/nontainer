"""The ``ws-vitest`` terminal verb: its flag surface, its refusals, its
report and its exit codes.

The harness's own behaviour lives in test_wsvitest_harness.py; what is
tested here is the shell-facing half. Chromium is needed for everything
that actually runs a file, so those cases take the shared fixture.
"""

import pytest

from nontainer import Workspace
from nontainer.apps import AppsConfig, enable_apps
from nontainer.providers import KvgitProvider
from nontainer.wsvitest import register_wsvitest, run_vitest

UTIL = "export const add = (a, b) => a + b;\n"

GOOD = """\
import { add } from '../app/util.js';

describe('add', () => {
  it('adds two numbers', () => {
    expect(add(1, 2)).toBe(3);
  });
  it('adds zero', () => {
    expect(add(1, 0)).toBe(1);
  });
});
"""

BAD = """\
import { add } from '../app/util.js';

it('gets it wrong', () => {
  expect(add(1, 2)).toBe(4);
});
it('is fine', () => {
  expect(add(0, 0)).toBe(0);
});
"""

BROKEN = "throw new Error('this file does not load');\n"


@pytest.fixture
def ws(chromium_available):
    w = Workspace(KvgitProvider.open(None, session="wsvitest-verb"))
    register_wsvitest(w)
    w.files.fs.write("/workspace/app/util.js", UTIL.encode())
    w.files.fs.write("/workspace/tests/good.test.js", GOOD.encode())
    yield w
    w.close()


def out(result):
    return result.stdout + result.stderr


# -- registration -------------------------------------------------------


def test_enable_apps_registers_the_verb():
    w = Workspace(KvgitProvider.open(None, session="wsvitest-apps"))
    enable_apps(w, AppsConfig())
    assert "ws-vitest" in w.runtime.commands
    assert "ws-pytest" in w.runtime.commands
    w.close()


def test_a_workspace_without_an_app_registers_it_directly():
    w = Workspace(KvgitProvider.open(None, session="wsvitest-bare"))
    assert "ws-vitest" not in w.runtime.commands
    register_wsvitest(w)
    register_wsvitest(w)  # a second call is a no-op, not a duplicate name
    assert "ws-vitest" in w.runtime.commands
    w.close()


# -- the report ---------------------------------------------------------


def test_a_passing_run_exits_zero_in_vitests_shape(ws):
    r = ws.terminal("ws-vitest")
    assert r.exit_code == 0, out(r)
    assert " ✓ tests/good.test.js (2 tests)" in r.stdout
    assert " Test Files  1 passed (1)" in r.stdout
    assert "      Tests  2 passed (2)" in r.stdout
    assert "   Duration  " in r.stdout


def test_a_failing_run_exits_one_and_names_the_test(ws):
    ws.files.fs.write("/workspace/tests/bad.test.js", BAD.encode())
    r = ws.terminal("ws-vitest")
    assert r.exit_code == 1, out(r)
    text = r.stdout
    assert "Failed Tests 1" in text
    assert " FAIL  tests/bad.test.js > gets it wrong" in text
    assert "AssertionError: expected 3 to be 4 // Object.is equality" in text
    assert "❯ tests/bad.test.js:4:21" in text
    assert " Test Files  1 failed | 1 passed (2)" in text
    assert "      Tests  1 failed | 3 passed (4)" in text


def test_the_verbose_reporter_is_a_line_per_test(ws):
    r = ws.terminal("ws-vitest --reporter=verbose")
    assert r.exit_code == 0, out(r)
    assert "   ✓ add > adds two numbers" in r.stdout
    assert "   ✓ add > adds zero" in r.stdout


def test_the_default_reporter_is_counts_per_file(ws):
    r = ws.terminal("ws-vitest --reporter=default")
    assert "   ✓ add > adds two numbers" not in r.stdout
    assert " ✓ tests/good.test.js (2 tests)" in r.stdout


def test_a_file_that_does_not_load_is_a_failed_suite(ws):
    ws.files.fs.write("/workspace/tests/broken.test.js", BROKEN.encode())
    r = ws.terminal("ws-vitest")
    assert r.exit_code == 1, out(r)
    assert "Failed Suites 1" in r.stdout
    assert " FAIL  tests/broken.test.js" in r.stdout
    assert "this file does not load" in r.stdout


# -- the flags ----------------------------------------------------------


def test_run_is_accepted_and_ignored(ws):
    bare = ws.terminal("ws-vitest")
    run = ws.terminal("ws-vitest run")
    assert run.exit_code == bare.exit_code == 0
    assert " Test Files  1 passed (1)" in run.stdout


def test_a_name_filter_narrows_the_run(ws):
    r = ws.terminal("ws-vitest -t 'adds zero'")
    assert r.exit_code == 0, out(r)
    assert "      Tests  1 passed (1)" in r.stdout


def test_a_path_filter_narrows_the_files(ws):
    ws.files.fs.write("/workspace/tests/bad.test.js", BAD.encode())
    r = ws.terminal("ws-vitest good")
    assert r.exit_code == 0, out(r)
    assert " Test Files  1 passed (1)" in r.stdout
    r = ws.terminal("ws-vitest tests/bad.test.js")
    assert r.exit_code == 1
    assert " Test Files  1 failed (1)" in r.stdout


def test_an_absolute_path_filter_is_a_path_like_every_other_verbs(ws):
    r = ws.terminal("ws-vitest /workspace/tests/good.test.js")
    assert r.exit_code == 0, out(r)
    assert " Test Files  1 passed (1)" in r.stdout


def test_bail_stops_after_the_first_failing_file(ws):
    ws.files.fs.write("/workspace/tests/a_bad.test.js", BAD.encode())
    ws.files.fs.write("/workspace/tests/b_bad.test.js", BAD.encode())
    full = ws.terminal("ws-vitest")
    assert " Test Files  2 failed | 1 passed (3)" in full.stdout, out(full)
    r = ws.terminal("ws-vitest --bail")
    assert r.exit_code == 1
    assert " Test Files  1 failed (1)" in r.stdout, out(r)


def test_a_filter_that_matches_nothing_exits_one_and_says_so(ws):
    r = ws.terminal("ws-vitest nosuchfile")
    assert r.exit_code == 1
    assert "no test files matched" in out(r)
    assert "nosuchfile" in out(r)


def test_no_test_files_at_all_exits_one(chromium_available):
    w = Workspace(KvgitProvider.open(None, session="wsvitest-empty"))
    register_wsvitest(w)
    try:
        r = w.terminal("ws-vitest")
        assert r.exit_code == 1
        assert "no test files found" in out(r)
        assert "tests/" in out(r)
    finally:
        w.close()


# -- the refusals -------------------------------------------------------


@pytest.mark.parametrize(
    "flag,word",
    [
        ("--coverage", "coverage"),
        ("--ui", "--reporter=verbose"),
        ("--config", "tests/"),
        ("--watch", "re-run"),
        ("-w", "re-run"),
    ],
)
def test_a_refused_flag_names_what_to_do_instead(ws, flag, word):
    r = ws.terminal(f"ws-vitest {flag}")
    assert r.exit_code == 1
    assert flag.split("=")[0] in out(r)
    assert word in out(r)


def test_an_unknown_flag_is_a_usage_error_with_the_usage(ws):
    r = ws.terminal("ws-vitest --frobnicate")
    assert r.exit_code == 1
    assert "unknown flag --frobnicate" in out(r)
    assert "usage: ws-vitest" in out(r)


def test_an_unknown_reporter_is_refused_by_name(ws):
    r = ws.terminal("ws-vitest --reporter=json")
    assert r.exit_code == 1
    assert "'json'" in out(r)
    assert "default" in out(r) and "verbose" in out(r)


def test_help_states_the_exit_codes_and_where_the_browser_runs(ws):
    r = ws.terminal("ws-vitest --help")
    assert r.exit_code == 0
    assert "usage: ws-vitest" in r.stdout
    assert "vitest has no separate exit code for a usage error" in r.stdout
    assert "browser on the host" in r.stdout


# -- the record ---------------------------------------------------------


def test_the_record_is_the_product(ws):
    ws.files.fs.write("/workspace/tests/bad.test.js", BAD.encode())
    report = run_vitest(ws)
    assert report.tool == "vitest"
    assert bool(report) is False
    assert report.ok is False
    assert report.exit_code == 1
    assert (report.passed, report.failed, report.errors) == (3, 1, 0)
    assert report.collected == 4
    failed = [o for o in report.outcomes if o.status == "failed"]
    assert failed[0].name == "tests/bad.test.js > gets it wrong"
    assert failed[0].file == "tests/bad.test.js"
    assert failed[0].frames[0].column == 21


def test_the_record_gates_a_merge(ws):
    assert run_vitest(ws).ok is True
    assert run_vitest(ws, ["-t", "nothing matches this"]).ok is False


def test_a_missing_import_names_the_file_that_was_not_served(ws):
    """The browser reports a failed dynamic import against the
    IMPORTER, never the file that 404'd, so the driver carries the
    miss home."""
    ws.files.fs.write(
        "/workspace/tests/missing.test.js",
        b"import { nope } from '../app/missing.js';\nit('x', () => {});\n",
    )
    r = ws.terminal("ws-vitest tests/missing.test.js")
    assert r.exit_code == 1
    assert "nothing is served at app/missing.js" in r.stdout, out(r)
    assert "nontainer.test" not in r.stdout, out(r)


def test_a_suite_that_would_not_load_is_not_counted_as_a_test(ws):
    ws.files.fs.write("/workspace/tests/broken.test.js", BROKEN.encode())
    r = ws.terminal("ws-vitest")
    assert " Test Files  1 failed | 1 passed (2)" in r.stdout, out(r)
    assert "      Tests  2 passed (2)" in r.stdout, out(r)
