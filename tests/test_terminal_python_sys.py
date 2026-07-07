"""Terminal `python` faithfulness: sys.stdin/argv/input (sandtrap >= 0.2.3)."""

import pytest

from nontainer import Workspace
from nontainer.providers import KvgitProvider


@pytest.fixture
def ws():
    w = Workspace(KvgitProvider.open(None, session="s1"), cache=False)
    yield w
    w.close()


def test_pipe_into_python_reads_stdin(ws):
    ws.write_file("data.txt", "10\n20\n30\n")
    ws.write_file("sum.py", "import sys\nprint(sum(int(x) for x in sys.stdin))\n")
    r = ws.terminal("cat data.txt | python sum.py")
    assert r, r.stderr
    assert r.stdout.strip() == "60"


def test_dash_c_reads_stdin(ws):
    r = ws.terminal(
        "echo hello | python -c 'import sys; print(sys.stdin.read().upper())'"
    )
    assert r, r.stderr
    assert r.stdout.strip() == "HELLO"


def test_argv_for_file(ws):
    ws.write_file("a.py", "import sys\nprint(sys.argv)\n")
    r = ws.terminal("python a.py foo bar")
    assert r, r.stderr
    assert r.stdout.strip() == "['a.py', 'foo', 'bar']"


def test_argv_for_dash_c(ws):
    r = ws.terminal("python -c 'import sys; print(sys.argv)' x y")
    assert r, r.stderr
    assert r.stdout.strip() == "['-c', 'x', 'y']"


def test_input_reads_stdin(ws):
    r = ws.terminal("echo ada | python -c 'print(\"hi\", input())'")
    assert r, r.stderr
    assert r.stdout.strip() == "hi ada"


def test_no_pipe_stdin_is_empty(ws):
    # sys is available even without a pipe; stdin just reads empty
    r = ws.terminal("python -c 'import sys; print(repr(sys.stdin.read()))'")
    assert r, r.stderr
    assert r.stdout.strip() == "''"


def test_heredoc_multiline_still_works(ws):
    r = ws.terminal("""python <<'EOF'
import sys
print("argv0:", repr(sys.argv[0]))
for i in range(2):
    print(i)
EOF""")
    assert r, r.stderr
    # heredoc form: stdin was the code, so argv[0] is '' and stdin empty
    assert r.stdout == "argv0: ''\n0\n1\n"


def test_dangerous_sys_still_blocked(ws):
    r = ws.terminal("python -c 'import sys; print(sys.modules)'")
    assert not r  # sys.modules unreachable even with synthetic sys
