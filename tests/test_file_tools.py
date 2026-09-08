"""file_write / file_edit at the Workspace level (adapter-independent)."""

import pytest

from nontainer import Workspace, WorkspaceError
from nontainer.providers import KvgitProvider


def make_ws(**kwargs) -> Workspace:
    return Workspace(KvgitProvider.open(None, session="s1"), **kwargs)


def test_workspace_write_and_edit_file():
    ws = make_ws()
    ws.files.write("src/app.py", "def main():\n    return 1\n")
    assert ws.files.fs.read("src/app.py").decode().endswith("return 1\n")

    out = ws.files.edit("src/app.py", "return 1", "return 2")
    assert out.count == 1 and out.mode == "exact"
    assert "return 2" in ws.files.fs.read("src/app.py").decode()

    with pytest.raises(WorkspaceError, match="not found"):
        ws.files.edit("src/app.py", "no such text", "x")

    ws.files.write("dup.txt", "a a a")
    with pytest.raises(WorkspaceError, match="3 times"):
        ws.files.edit("dup.txt", "a", "b")
    assert ws.files.edit("dup.txt", "a", "b", replace_all=True).count == 3

    infos = [c.info.get("tool") for c in ws.log(limit=6)]
    assert "file_write" in infos and "file_edit" in infos
    ws.close()


def test_edit_file_agent_tolerant_matching():
    """The agex strategy set, ported: trailing-ws, indent-flex, no-op."""
    ws = make_ws()

    # trailing whitespace in the file, clean search from the agent
    ws.files.write("a.py", "def f():   \n    return 1\n")
    out = ws.files.edit("a.py", "def f():\n    return 1", "def f():\n    return 2")
    assert out.mode == "trailing_ws"
    assert "return 2" in ws.files.fs.read("a.py").decode()

    # agent quotes the block at the wrong baseline (uniformly shifted):
    # match anyway, and shift the replacement to the file's baseline.
    # (Constant-delta re-indent, per agex: internal steps are preserved,
    # not rescaled.)
    ws.files.write("b.py", "class C:\n    def m(self):\n        return 'old'\n")
    out = ws.files.edit(
        "b.py",
        "def m(self):\n    return 'old'",
        "def m(self):\n    return 'new'",
    )
    assert out.mode == "indent_flexible"
    assert "        return 'new'" in ws.files.fs.read("b.py").decode()  # file's indent

    # idempotent retry: replacement already present → no-op, not an error
    out = ws.files.edit("b.py", "return 'old'", "return 'new'")
    assert out.mode == "already_applied" and out.count == 0

    # actionable failure: near-miss shows "did you mean" with line numbers
    ws.files.write("c.py", "def compute(x):\n    return x * 42\n")
    with pytest.raises(WorkspaceError, match="Did you mean"):
        ws.files.edit("c.py", "def compute(x):\n    return x * 43", "zzz")

    # backslash-safe replacement through the regex (trailing-ws) path
    ws.files.write("d.txt", "value:  \nend\n")
    out = ws.files.edit("d.txt", "value:", r"value: \1 \g<0>")
    assert out.count == 1
    assert r"\1 \g<0>" in ws.files.fs.read("d.txt").decode()
    ws.close()


def test_files_read_list_and_exists():
    """The read side of ``ws.files``: bytes, presence, and a listing
    spelled the way the directory was asked for, so an entry can be
    handed straight back to ``read``."""
    ws = make_ws()
    try:
        ws.files.write("/workspace/notes/a.txt", "alpha")
        ws.files.write("/workspace/notes/deep/b.txt", "beta")

        assert ws.files.read("/workspace/notes/a.txt") == b"alpha"
        assert ws.files.exists("/workspace/notes/deep") is True
        assert ws.files.exists("/workspace/nope.txt") is False
        with pytest.raises(Exception):
            ws.files.read("/workspace/nope.txt")

        assert ws.files.list("/workspace/notes") == [
            "/workspace/notes/a.txt",
            "/workspace/notes/deep",
        ]
        assert ws.files.list("/workspace/notes", recursive=True) == [
            "/workspace/notes/a.txt",
            "/workspace/notes/deep/b.txt",
        ]
        assert ws.files.read(ws.files.list("/workspace/notes")[0]) == b"alpha"
    finally:
        ws.close()


def test_files_export_is_the_providers_mount(monkeypatch):
    """``ws.files.export()`` is the old ``ws.mount()``: the provider's
    context manager, unwrapped, refusal and all."""
    from contextlib import contextmanager
    from pathlib import Path

    from nontainer.errors import NotSupportedError

    ws = make_ws()
    try:
        with pytest.raises(NotSupportedError):
            ws.files.export()

        @contextmanager
        def fake_mount():
            yield Path("/tmp/exported")

        monkeypatch.setattr(ws._provider, "mount", fake_mount)
        with ws.files.export() as real:
            assert real == Path("/tmp/exported")
    finally:
        ws.close()
