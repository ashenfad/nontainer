"""Regression tests for things agents actually do — the documented
conventions and the common footguns, exercised end to end.
"""

import pytest

from nontainer import PythonConfig, Workspace
from nontainer.providers import KvgitProvider


@pytest.fixture
def ws():
    w = Workspace(KvgitProvider.open(None, session="s1"))
    yield w
    w.close()


# -- the flagship convention: reusable code in helpers/, imported later --


def test_helpers_import_across_calls(ws):
    # write a helper module in one call, import it in a later call
    ws.write_file("helpers/mathx.py", "def double(x):\n    return x * 2\n")
    r = ws.run_python("from helpers.mathx import double\nprint(double(21))")
    assert r, r.error
    assert r.stdout.strip() == "42"


def test_helpers_authored_by_agent_then_used(ws):
    # the agent writes the helper via run_python itself (open + write)
    ws.run_python(
        "import os\n"
        "os.makedirs('helpers', exist_ok=True)\n"
        "with open('helpers/greet.py', 'w') as f:\n"
        "    f.write('def hi(n):\\n    return f\"hi {n}\"\\n')"
    )
    r = ws.run_python("from helpers.greet import hi\nprint(hi('ada'))")
    assert r, r.error
    assert r.stdout.strip() == "hi ada"


# -- runaway code is bounded, not a hang --------------------------------


def test_runaway_loop_is_bounded():
    w = Workspace(
        KvgitProvider.open(None, session="loop"),
        python=PythonConfig(tick_limit=50_000),
    )
    r = w.run_python("while True:\n    pass")
    assert not r  # an error result, NOT a hang
    assert r.error is not None
    w.close()


def test_unbounded_recursion_is_a_result_not_a_crash(ws):
    r = ws.run_python("def f(n):\n    return f(n + 1)\nf(0)")
    assert not r
    assert r.error is not None  # RecursionError surfaced as a result


# -- shared cwd across the two tools ------------------------------------


def test_cd_in_terminal_then_relative_open_in_python(ws):
    ws.terminal("mkdir -p proj && cd proj && echo hi > note.txt")
    r = ws.run_python("print(open('note.txt').read().strip())")
    assert r, r.error
    assert r.stdout.strip() == "hi"


def test_python_relative_write_visible_to_terminal_after_cd(ws):
    ws.terminal("mkdir -p out && cd out")
    ws.run_python("open('made.txt', 'w').write('by python')")
    r = ws.terminal("cat made.txt")  # terminal still in out/
    assert r, r.stderr
    assert r.stdout.strip() == "by python"


# -- unicode round-trips through files, print, and json -----------------


def test_unicode_file_round_trip(ws):
    ws.run_python("open('u.txt', 'w', encoding='utf-8').write('café ☕ 日本')")
    r = ws.run_python("print(open('u.txt', encoding='utf-8').read())")
    assert r, r.error
    assert r.stdout.strip() == "café ☕ 日本"


def test_unicode_through_terminal_and_json(ws):
    r = ws.run_python(
        "import json\nprint(json.dumps({'city': 'São Paulo'}, ensure_ascii=False))"
    )
    assert r, r.error
    assert "São Paulo" in r.stdout
    # and the same content survives a shell round-trip
    ws.terminal("echo 'Zürich' > city.txt")
    r = ws.terminal("cat city.txt")
    assert r.stdout.strip() == "Zürich"


# -- malformed code is a legible result, never a host exception ---------


def test_syntax_error_is_a_result(ws):
    r = ws.run_python("def broken(:\n    pass")
    assert not r
    assert r.error is not None  # SyntaxError reported, not raised to the host


def test_traceback_points_at_the_offending_line(ws):
    r = ws.run_python("a = 1\nb = 2\nc = a / 0\n")
    assert not r
    assert "ZeroDivisionError" in r.error
    assert "line 3" in r.error  # the actual failing line, for the repair loop
