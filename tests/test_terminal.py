"""Terminal tool: termish over the provider fs, stateful cwd, python bridge."""

import pytest
from plain_provider import PlainProvider

from nontainer import Workspace


def test_basic_pipeline(plain_ws):
    r = plain_ws.terminal("echo 'b\na\nc' | sort")
    assert r
    assert r.exit_code == 0
    assert r.stdout.splitlines() == ["a", "b", "c"]


def test_write_then_read_real_files(plain_ws, tmp_path):
    r = plain_ws.terminal("mkdir -p data; echo 'x,y' > data/in.csv; cat data/in.csv")
    assert r
    assert "x,y" in r.stdout
    # The files are real on disk (PlainProvider)
    assert (
        tmp_path / "ws" / "workspace" / "data" / "in.csv"
    ).read_text().strip() == "x,y"


def test_failure_is_result_not_exception(plain_ws):
    r = plain_ws.terminal("cat /no/such/file")
    assert not r
    assert r.exit_code != 0
    assert r.stderr


def test_parse_error(plain_ws):
    r = plain_ws.terminal("echo 'unclosed")
    assert not r
    assert r.exit_code == 2
    assert "parse error" in r.stderr


def test_cwd_stateful_across_calls(plain_ws):
    plain_ws.terminal("mkdir -p sub/deep")
    plain_ws.terminal("cd sub/deep")
    r = plain_ws.terminal("pwd")
    assert r.stdout.strip().endswith("sub/deep")


def test_cwd_persists_across_workspace_instances(tmp_path):
    from nontainer import Store

    store = Store(tmp_path)
    try:
        with store.open("s1") as ws1:
            ws1.terminal("mkdir -p keep; cd keep")
        with store.open("s1") as ws2:
            assert ws2.terminal("pwd").stdout.strip().endswith("keep")
    finally:
        store.close()


def test_custom_command_injection(tmp_path):
    def greet(ctx):
        name = ctx.args[0] if ctx.args else "world"
        ctx.stdout.write(f"hello {name}\n")
        return None

    p = PlainProvider(tmp_path / "ws", session="s1")
    ws = Workspace(p, commands={"greet": greet})
    r = ws.terminal("greet alice | wc -c")
    assert r
    assert r.stdout.strip() == "12"
    ws.close()


def test_fork_runs_shared_rebind_factory_once():
    """One initializer owning two commands registers itself as both
    rebinds; the fork invokes it once — a second invocation would
    collide with the first one's registrations and fail the fork."""
    from nontainer.providers import KvgitProvider

    calls = []

    def both(ctx):
        ctx.stdout.write("both\n")
        return None

    def init(ws):
        calls.append(1)
        ws.runtime.register_command("alpha", both, rebind=init)
        ws.runtime.register_command("beta", both, rebind=init)

    provider = KvgitProvider.open(None, session="cmd-rebind-once")
    ws = Workspace(provider)
    try:
        init(ws)
        fork = ws.fork("cmd-rebind-once-kid")
        try:
            assert calls == [1, 1]  # once at init, once at fork
            assert fork.terminal("alpha").stdout == "both\n"
            assert fork.terminal("beta").stdout == "both\n"
            assert ws.terminal("alpha").stdout == "both\n"
        finally:
            fork.close()
    finally:
        ws.close()


def test_ws_prefix_reserved_for_framework(tmp_path):
    from plain_provider import PlainProvider

    p = PlainProvider(tmp_path / "ws", session="s1")
    ws = Workspace(p)
    try:
        with pytest.raises(ValueError, match="Reserved terminal command prefix"):
            ws.runtime.register_command("ws-evil", lambda ctx: None)
        # Framework registrations (rebind set) are exempt.
        ws.runtime.register_command("ws-demo", lambda ctx: None, rebind=lambda w: None)
        assert "ws-demo" in ws.runtime.commands
    finally:
        ws.close()
    # The constructor path is equally public and equally refused.
    with pytest.raises(ValueError, match="Reserved terminal command prefix"):
        Workspace(p, commands={"ws-evil": lambda ctx: None})


def test_reserved_python_command_rejected(tmp_path):
    p = PlainProvider(tmp_path / "ws", session="s1")
    with pytest.raises(ValueError, match="Reserved"):
        Workspace(p, commands={"python": lambda ctx: None})
    with pytest.raises(ValueError, match="Reserved"):
        Workspace(p, commands={"python3": lambda ctx: None})


def test_truncation(tmp_path):
    p = PlainProvider(tmp_path / "ws", session="s1")
    ws = Workspace(p, max_observation=10)
    r = ws.terminal("echo aaaaaaaaaaaaaaaaaaaaaaaa")
    assert r.truncated
    assert len(r.stdout) == 10
    ws.close()


# -- the python bridge --------------------------------------------------


def test_python_dash_c(plain_ws):
    r = plain_ws.terminal("python -c 'print(sum(range(10)))'")
    assert r
    assert r.stdout.strip() == "45"


def test_python3_is_the_same_bridge(plain_ws):
    r = plain_ws.terminal("python3 -c 'print(sum(range(10)))'")
    assert r
    assert r.stdout.strip() == "45"


def test_python_file(plain_ws):
    plain_ws.terminal("echo 'print(2 + 3)' > calc.py")
    r = plain_ws.terminal("python calc.py")
    assert r
    assert r.stdout.strip() == "5"


def test_a_script_runs_as_main(plain_ws):
    """`python file.py` runs the file as __main__, every form of it:
    the usual `if __name__ == "__main__": main()` guard is how scripts
    are written, and under any other name the script exits 0 having
    done nothing. A delegate's data generator did exactly that."""
    plain_ws.files.write(
        "/workspace/gen.py",
        "def main():\n    open('/workspace/out.txt', 'w').write('made')\n"
        "    print('ran', __name__)\n\n"
        "if __name__ == '__main__':\n    main()\n",
    )
    r = plain_ws.terminal("python3 gen.py")
    assert r and r.stdout.strip() == "ran __main__"
    assert plain_ws.files.fs.read("/workspace/out.txt") == b"made"
    for form in ("python -c 'print(__name__)'", "echo 'print(__name__)' | python"):
        assert plain_ws.terminal(form).stdout.strip() == "__main__"


def test_python_stdin_pipe(plain_ws):
    r = plain_ws.terminal("echo 'print(6 * 7)' | python")
    assert r
    assert r.stdout.strip() == "42"


def test_python_in_pipeline(plain_ws):
    r = plain_ws.terminal('python -c \'print("b"); print("a")\' | sort')
    assert r
    assert r.stdout.splitlines() == ["a", "b"]


def test_python_error_maps_to_exit_code(plain_ws):
    r = plain_ws.terminal("python -c '1/0'")
    assert not r
    assert r.exit_code == 1
    assert "ZeroDivisionError" in r.stderr


def test_python_missing_file(plain_ws):
    r = plain_ws.terminal("python nope.py")
    assert not r
    assert "nope.py" in r.stderr


def test_python_sees_workspace_files(plain_ws):
    plain_ws.terminal("echo 'hello' > note.txt")
    r = plain_ws.terminal("python -c 'print(open(\"note.txt\").read().strip())'")
    assert r
    assert r.stdout.strip() == "hello"


def test_heredoc_through_workspace(plain_ws):
    r = plain_ws.terminal("cat <<'EOF' | tr a-z A-Z\nhello heredoc\nEOF")
    assert r, r.stderr
    assert r.stdout.strip() == "HELLO HEREDOC"


def test_heredoc_python_idiom(plain_ws):
    """The idiom the heredoc work was for: multiline python, no quoting."""
    r = plain_ws.terminal("python <<'PY'\nfor i in range(3):\n    print(i * 10)\nPY")
    assert r, r.stderr
    assert r.stdout.split() == ["0", "10", "20"]


def test_command_not_found_is_127(plain_ws):
    r = plain_ws.terminal("no_such_cmd")
    assert r.exit_code == 127
