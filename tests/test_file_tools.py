"""file_write / file_edit at the Workspace level (adapter-independent)."""

import pytest

from nontainer import Workspace, WorkspaceError
from nontainer.providers import KvgitProvider


def make_ws(**kwargs) -> Workspace:
    return Workspace(KvgitProvider.open(None, session="s1"), **kwargs)


def test_workspace_write_and_edit_file():
    ws = make_ws()
    ws.write_file("src/app.py", "def main():\n    return 1\n")
    assert ws.fs.read("src/app.py").decode().endswith("return 1\n")

    out = ws.edit_file("src/app.py", "return 1", "return 2")
    assert out.count == 1 and out.mode == "exact"
    assert "return 2" in ws.fs.read("src/app.py").decode()

    with pytest.raises(WorkspaceError, match="not found"):
        ws.edit_file("src/app.py", "no such text", "x")

    ws.write_file("dup.txt", "a a a")
    with pytest.raises(WorkspaceError, match="3 times"):
        ws.edit_file("dup.txt", "a", "b")
    assert ws.edit_file("dup.txt", "a", "b", replace_all=True).count == 3

    infos = [c.info.get("tool") for c in ws.history(limit=6)]
    assert "file_write" in infos and "file_edit" in infos
    ws.close()


def test_edit_file_agent_tolerant_matching():
    """The agex strategy set, ported: trailing-ws, indent-flex, no-op."""
    ws = make_ws()

    # trailing whitespace in the file, clean search from the agent
    ws.write_file("a.py", "def f():   \n    return 1\n")
    out = ws.edit_file("a.py", "def f():\n    return 1", "def f():\n    return 2")
    assert out.mode == "trailing_ws"
    assert "return 2" in ws.fs.read("a.py").decode()

    # agent quotes the block at the wrong baseline (uniformly shifted):
    # match anyway, and shift the replacement to the file's baseline.
    # (Constant-delta re-indent, per agex: internal steps are preserved,
    # not rescaled.)
    ws.write_file("b.py", "class C:\n    def m(self):\n        return 'old'\n")
    out = ws.edit_file(
        "b.py",
        "def m(self):\n    return 'old'",
        "def m(self):\n    return 'new'",
    )
    assert out.mode == "indent_flexible"
    assert "        return 'new'" in ws.fs.read("b.py").decode()  # file's indent

    # idempotent retry: replacement already present → no-op, not an error
    out = ws.edit_file("b.py", "return 'old'", "return 'new'")
    assert out.mode == "already_applied" and out.count == 0

    # actionable failure: near-miss shows "did you mean" with line numbers
    ws.write_file("c.py", "def compute(x):\n    return x * 42\n")
    with pytest.raises(WorkspaceError, match="Did you mean"):
        ws.edit_file("c.py", "def compute(x):\n    return x * 43", "zzz")

    # backslash-safe replacement through the regex (trailing-ws) path
    ws.write_file("d.txt", "value:  \nend\n")
    out = ws.edit_file("d.txt", "value:", r"value: \1 \g<0>")
    assert out.count == 1
    assert r"\1 \g<0>" in ws.fs.read("d.txt").decode()
    ws.close()
