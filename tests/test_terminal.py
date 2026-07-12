"""Terminal tool: termish over the provider fs, stateful cwd, python bridge."""

import pytest

from nontainer import Workspace
from nontainer.providers import DirProvider


def test_basic_pipeline(dir_ws):
    r = dir_ws.terminal("echo 'b\na\nc' | sort")
    assert r
    assert r.exit_code == 0
    assert r.stdout.splitlines() == ["a", "b", "c"]


def test_write_then_read_real_files(dir_ws, tmp_path):
    r = dir_ws.terminal("mkdir -p data; echo 'x,y' > data/in.csv; cat data/in.csv")
    assert r
    assert "x,y" in r.stdout
    # The files are real on disk (DirProvider)
    assert (tmp_path / "ws" / "data" / "in.csv").read_text().strip() == "x,y"


def test_failure_is_result_not_exception(dir_ws):
    r = dir_ws.terminal("cat /no/such/file")
    assert not r
    assert r.exit_code != 0
    assert r.stderr


def test_parse_error(dir_ws):
    r = dir_ws.terminal("echo 'unclosed")
    assert not r
    assert r.exit_code == 2
    assert "parse error" in r.stderr


def test_cwd_stateful_across_calls(dir_ws):
    dir_ws.terminal("mkdir -p sub/deep")
    dir_ws.terminal("cd sub/deep")
    r = dir_ws.terminal("pwd")
    assert r.stdout.strip().endswith("sub/deep")


def test_cwd_persists_across_workspace_instances(tmp_path):
    p1 = DirProvider(tmp_path / "ws", session="s1")
    ws1 = Workspace(p1)
    ws1.terminal("mkdir -p keep; cd keep")
    ws1.close()

    p2 = DirProvider(tmp_path / "ws", session="s1")
    ws2 = Workspace(p2)
    r = ws2.terminal("pwd")
    assert r.stdout.strip().endswith("keep")
    ws2.close()


def test_custom_command_injection(tmp_path):
    def greet(ctx):
        name = ctx.args[0] if ctx.args else "world"
        ctx.stdout.write(f"hello {name}\n")
        return None

    p = DirProvider(tmp_path / "ws", session="s1")
    ws = Workspace(p, commands={"greet": greet})
    r = ws.terminal("greet alice | wc -c")
    assert r
    assert r.stdout.strip() == "12"
    ws.close()


def test_reserved_python_command_rejected(tmp_path):
    p = DirProvider(tmp_path / "ws", session="s1")
    with pytest.raises(ValueError, match="Reserved"):
        Workspace(p, commands={"python": lambda ctx: None})
    with pytest.raises(ValueError, match="Reserved"):
        Workspace(p, commands={"python3": lambda ctx: None})


def test_truncation(tmp_path):
    p = DirProvider(tmp_path / "ws", session="s1")
    ws = Workspace(p, max_observation=10)
    r = ws.terminal("echo aaaaaaaaaaaaaaaaaaaaaaaa")
    assert r.truncated
    assert len(r.stdout) == 10
    ws.close()


# -- the python bridge --------------------------------------------------


def test_python_dash_c(dir_ws):
    r = dir_ws.terminal("python -c 'print(sum(range(10)))'")
    assert r
    assert r.stdout.strip() == "45"


def test_python3_is_the_same_bridge(dir_ws):
    r = dir_ws.terminal("python3 -c 'print(sum(range(10)))'")
    assert r
    assert r.stdout.strip() == "45"


def test_python_file(dir_ws):
    dir_ws.terminal("echo 'print(2 + 3)' > calc.py")
    r = dir_ws.terminal("python calc.py")
    assert r
    assert r.stdout.strip() == "5"


def test_python_stdin_pipe(dir_ws):
    r = dir_ws.terminal("echo 'print(6 * 7)' | python")
    assert r
    assert r.stdout.strip() == "42"


def test_python_in_pipeline(dir_ws):
    r = dir_ws.terminal("python -c 'print(\"b\"); print(\"a\")' | sort")
    assert r
    assert r.stdout.splitlines() == ["a", "b"]


def test_python_error_maps_to_exit_code(dir_ws):
    r = dir_ws.terminal("python -c '1/0'")
    assert not r
    assert r.exit_code == 1
    assert "ZeroDivisionError" in r.stderr


def test_python_missing_file(dir_ws):
    r = dir_ws.terminal("python nope.py")
    assert not r
    assert "nope.py" in r.stderr


def test_python_sees_workspace_files(dir_ws):
    dir_ws.terminal("echo 'hello' > note.txt")
    r = dir_ws.terminal("python -c 'print(open(\"note.txt\").read().strip())'")
    assert r
    assert r.stdout.strip() == "hello"


def test_heredoc_through_workspace(dir_ws):
    r = dir_ws.terminal("cat <<'EOF' | tr a-z A-Z\nhello heredoc\nEOF")
    assert r, r.stderr
    assert r.stdout.strip() == "HELLO HEREDOC"


def test_heredoc_python_idiom(dir_ws):
    """The idiom the heredoc work was for: multiline python, no quoting."""
    r = dir_ws.terminal("python <<'PY'\nfor i in range(3):\n    print(i * 10)\nPY")
    assert r, r.stderr
    assert r.stdout.split() == ["0", "10", "20"]


def test_command_not_found_is_127(dir_ws):
    r = dir_ws.terminal("no_such_cmd")
    assert r.exit_code == 127
