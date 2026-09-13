"""The ws-pytest runner: what it collects, what it reports, and how a
failure reads.

The runner is the record (``TestReport``); ``render_report`` is the
only thing that turns it into pytest's text. Both are exercised here
at the module level, on a workspace scripted per test — the terminal
verb and the cross-rung conformance live beside this.
"""

import pytest

from nontainer import Workspace
from nontainer.providers import KvgitProvider
from nontainer.wspytest import _sep, render_report, run_pytest


@pytest.fixture
def ws():
    w = Workspace(KvgitProvider.open(None, session="wspytest"))
    try:
        yield w
    finally:
        w.close()


def write(ws, rel, text):
    ws.files.fs.write(f"/workspace/{rel}", text.encode())


def test_a_passing_and_a_failing_test_are_both_reported(ws):
    write(
        ws,
        "tests/test_math.py",
        "def test_ok():\n"
        "    assert 1 + 1 == 2\n"
        "\n"
        "\n"
        "def test_bad():\n"
        "    assert 1 + 1 == 3, 'arithmetic moved'\n",
    )
    report = run_pytest(ws)

    assert report.collected == 2
    assert report.passed == 1
    assert report.failed == 1
    assert report.exit_code == 1
    assert not report.ok
    assert not report
    names = [o.name for o in report.outcomes]
    assert names == ["tests/test_math.py::test_ok", "tests/test_math.py::test_bad"]
    bad = report.outcomes[1]
    assert bad.status == "failed"
    assert bad.message == "AssertionError: arithmetic moved"
    assert bad.line == 6
    assert bad.traceback == (
        "tests/test_math.py:6: in test_bad\n"
        "    assert 1 + 1 == 3, 'arithmetic moved'\n"
        "E   AssertionError: arithmetic moved"
    )


def test_an_all_green_run_exits_zero(ws):
    write(ws, "tests/test_ok.py", "def test_one():\n    assert True\n")
    report = run_pytest(ws)
    assert report.ok
    assert report
    assert report.exit_code == 0
    assert report.passed == 1


def test_no_tests_collected_is_exit_five(ws):
    write(ws, "notes.md", "nothing to run here\n")
    report = run_pytest(ws)
    assert report.collected == 0
    assert report.exit_code == 5
    assert "no tests ran" in render_report(report)


def test_a_module_level_failure_is_a_collection_error(ws):
    write(
        ws,
        "tests/test_broken.py",
        "raise RuntimeError('module level explosion')\n"
        "\n"
        "\n"
        "def test_never():\n"
        "    assert True\n",
    )
    report = run_pytest(ws)
    assert report.exit_code == 2
    assert report.errors == 1
    assert report.collected == 0
    assert report.collection_error is not None
    assert "RuntimeError: module level explosion" in report.collection_error
    assert report.outcomes[0].frames[-1].path == "tests/test_broken.py"
    assert report.outcomes[0].frames[-1].line == 1
    text = render_report(report)
    assert "ERROR collecting tests/test_broken.py" in text
    assert "1 error during collection" in text


def test_a_syntax_error_is_a_collection_error(ws):
    write(ws, "tests/test_syntax.py", "def test_a(:\n    pass\n")
    report = run_pytest(ws)
    assert report.exit_code == 2
    assert report.errors == 1
    assert "SyntaxError" in (report.collection_error or "")


def test_a_failure_inside_a_helper_module_names_the_helper(ws):
    """The frame that matters is the one that raised, in the file that
    holds it — not the test that called into it."""
    write(
        ws,
        "app/api/_lib.py",
        "def load(db):\n    raise ValueError('no db: ' + str(db))\n",
    )
    write(
        ws,
        "tests/test_lib.py",
        "from app.api._lib import load\n\n\ndef test_load():\n    load(None)\n",
    )
    report = run_pytest(ws)
    assert report.failed == 1
    out = report.outcomes[0]
    assert out.message == "ValueError: no db: None"
    assert out.traceback == (
        "tests/test_lib.py:5: in test_load\n"
        "    load(None)\n"
        "app/api/_lib.py:2: in load\n"
        "    raise ValueError('no db: ' + str(db))\n"
        "E   ValueError: no db: None"
    )


def test_the_sandbox_frames_are_dropped(ws):
    """A report shows the agent's own files. The sandbox's gate
    wrappers sit between every pair of frames and teach nothing."""
    write(ws, "tests/test_deep.py", "def test_deep():\n    raise KeyError('k')\n")
    report = run_pytest(ws)
    assert "gates.py" not in (report.outcomes[0].traceback or "")
    assert "<sandtrap" not in (report.outcomes[0].traceback or "")


def test_tests_under_app_are_not_collected_and_say_so(ws):
    write(ws, "app/api/test_handler.py", "def test_x():\n    assert True\n")
    write(ws, "tests/test_real.py", "def test_y():\n    assert True\n")
    report = run_pytest(ws)
    assert report.collected == 1
    assert any("publishes" in note for note in report.notes)


def test_a_conftest_is_reported_not_ignored(ws):
    write(ws, "tests/conftest.py", "import pytest\n")
    write(ws, "tests/test_a.py", "def test_a():\n    assert True\n")
    report = run_pytest(ws)
    assert any("conftest.py is not read" in note for note in report.notes)


def test_a_test_asking_for_a_fixture_is_an_error(ws):
    write(ws, "tests/test_fix.py", "def test_needs(db):\n    assert db\n")
    report = run_pytest(ws)
    assert report.errors == 1
    assert report.exit_code == 1
    assert "fixture(s) not found: db" in (report.outcomes[0].message or "")


def test_a_selector_picks_one_test(ws):
    write(
        ws,
        "tests/test_pick.py",
        "def test_a():\n    assert True\n\n\ndef test_b():\n    assert False\n",
    )
    report = run_pytest(ws, ["tests/test_pick.py::test_a"])
    assert report.collected == 1
    assert report.ok


def test_a_missing_selector_is_a_usage_error(ws):
    report = run_pytest(ws, ["tests/nope.py"])
    assert report.exit_code == 4
    assert "not found" in (report.collection_error or "")


def test_stdout_from_a_test_rides_the_report(ws):
    write(
        ws,
        "tests/test_out.py",
        "def test_talks():\n    print('hello from a test')\n",
    )
    report = run_pytest(ws)
    assert "hello from a test" in report.stdout
    assert "hello from a test" in render_report(report)


def test_the_default_rendering_is_pytest_shaped(ws):
    write(
        ws,
        "tests/test_shape.py",
        "def test_a():\n"
        "    assert True\n"
        "\n"
        "\n"
        "def test_b():\n"
        "    assert True\n"
        "\n"
        "\n"
        "def test_c():\n"
        "    assert 0\n",
    )
    text = render_report(run_pytest(ws))
    lines = text.splitlines()
    assert lines[0] == "=" * 29 + " test session starts " + "=" * 30
    assert lines[1] == "collected 3 items"
    assert lines[3] == "tests/test_shape.py ..F" + " " * 51 + "[100%]"
    assert all(len(line) == 80 for line in lines if line.startswith("="))
    assert _sep("FAILURES") in lines
    assert _sep("test_c", "_") in lines
    assert lines[-1].startswith("=") and " 1 failed, 2 passed in " in lines[-1]


def test_verbose_names_every_test(ws):
    write(
        ws,
        "tests/test_v.py",
        "def test_a():\n    assert True\n\n\ndef test_b():\n    assert 0\n",
    )
    text = render_report(run_pytest(ws), verbosity=1)
    assert any(
        line.startswith("tests/test_v.py::test_a PASSED") for line in text.splitlines()
    )
    assert any(
        line.startswith("tests/test_v.py::test_b FAILED") for line in text.splitlines()
    )


def test_quiet_drops_the_header_and_the_filenames(ws):
    write(ws, "tests/test_q.py", "def test_a():\n    assert True\n")
    text = render_report(run_pytest(ws), verbosity=-1)
    assert "test session starts" not in text
    assert "tests/test_q.py" not in text.split("in ")[0]
    assert text.splitlines()[0].startswith(".")


def test_tb_no_drops_the_failures_section(ws):
    write(ws, "tests/test_n.py", "def test_a():\n    assert 0\n")
    text = render_report(run_pytest(ws), tb="no")
    assert "FAILURES" not in text
    assert "1 failed" in text


def test_tb_long_shows_the_function_and_the_marker(ws):
    write(
        ws,
        "tests/test_l.py",
        "def test_a():\n    total = 1 + 1\n    assert total == 3\n",
    )
    text = render_report(run_pytest(ws), tb="long")
    assert "    def test_a():" in text
    assert "        total = 1 + 1" in text
    assert ">       assert total == 3" in text
    assert "E       AssertionError" in text
    assert "tests/test_l.py:3: AssertionError" in text


def test_maxfail_stops_the_run(ws):
    write(
        ws,
        "tests/test_m.py",
        "def test_a():\n"
        "    assert 0\n"
        "\n"
        "\n"
        "def test_b():\n"
        "    assert 0\n"
        "\n"
        "\n"
        "def test_c():\n"
        "    assert 0\n",
    )
    from nontainer.wspytest import Options, run_options

    report = run_options(ws, Options(maxfail=2))
    assert report.failed == 2
    assert len(report.outcomes) == 2


def test_keyword_filters_by_name(ws):
    write(
        ws,
        "tests/test_k.py",
        "def test_alpha():\n"
        "    assert True\n"
        "\n"
        "\n"
        "def test_beta():\n"
        "    assert True\n"
        "\n"
        "\n"
        "def test_alpha_beta():\n"
        "    assert True\n",
    )
    from nontainer.wspytest import Options, run_options

    assert run_options(ws, Options(keyword="alpha")).collected == 2
    assert run_options(ws, Options(keyword="alpha and beta")).collected == 1
    assert run_options(ws, Options(keyword="alpha and not beta")).collected == 1
    assert run_options(ws, Options(keyword="alpha or beta")).collected == 3


def test_a_future_import_in_a_plain_test_file_runs(ws):
    """No handler to compose, but the rule is the same: what the file
    puts first stays first, and the sandbox allows the import."""
    write(
        ws,
        "tests/test_future.py",
        '"""A module docstring."""\n'
        "\n"
        "from __future__ import annotations\n"
        "\n"
        "\n"
        "def test_annotated() -> None:\n"
        "    total: int = 1\n"
        "    assert total == 1\n"
        "\n"
        "\n"
        "def test_plain() -> None:\n"
        "    assert 1 == 2\n",
    )
    report = run_pytest(ws)
    assert report.collection_error is None, report.collection_error
    assert report.failed == 1
    assert report.errors == 0
    assert report.passed == 1
    assert report.outcomes[-1].line == 12


def test_an_absolute_selector_names_the_file_it_says(ws):
    write(ws, "tests/test_abs.py", "def test_a():\n    assert True\n")
    report = run_pytest(ws, ["/workspace/tests/test_abs.py"])
    assert report.collected == 1, report.collection_error
    assert report.outcomes[0].name == "tests/test_abs.py::test_a"
    report = run_pytest(ws, ["/workspace/tests/test_abs.py::test_a"])
    assert report.collected == 1, report.collection_error


def test_a_relative_selector_resolves_against_the_cwd(ws):
    write(ws, "tests/test_rel.py", "def test_a():\n    assert True\n")
    report = run_pytest(ws, ["test_rel.py"], cwd="/workspace/tests")
    assert report.collected == 1, report.collection_error
    assert report.outcomes[0].name == "tests/test_rel.py::test_a"


def test_a_test_name_nothing_defines_is_a_usage_error(ws):
    write(
        ws,
        "tests/test_named.py",
        "def test_a():\n    assert True\n\n\ndef test_b():\n    assert True\n",
    )
    report = run_pytest(ws, ["tests/test_named.py::test_c"])
    assert report.exit_code == 4
    assert report.collection_error == (
        "not found: tests/test_named.py::test_c\n"
        "(tests/test_named.py defines test_a, test_b)"
    )
    # A filter that matches nothing is not the same mistake.
    from nontainer.wspytest import Options, run_options

    assert run_options(ws, Options(keyword="nosuch")).exit_code == 5
