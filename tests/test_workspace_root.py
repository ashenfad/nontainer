"""The workspace root knob: one agent-visible path contract.

Files live under ``ws.root`` (default ``/workspace``); cwd starts
there; skills/apps derive their trees from it; VFS imports resolve
from it; DudExecutor maps host ``<root>/x`` to guest ``<work>/x``.
``root="/"`` is the flat legacy layout and must keep working — it's a
knob, not a migration.
"""

import pytest

from nontainer import Workspace
from nontainer.providers import KvgitProvider


def _kv_ws(**kwargs) -> Workspace:
    return Workspace(KvgitProvider.open(None, session="s1"), **kwargs)


def test_default_root_is_workspace():
    ws = _kv_ws()
    try:
        assert ws.root == "/workspace"
        assert ws.fs.isdir("/workspace")
        assert ws.fs.getcwd() == "/workspace"
        # relative writes land under the root
        r = ws.terminal("echo hi > f.txt")
        assert r
        assert ws.fs.read("/workspace/f.txt").strip() == b"hi"
    finally:
        ws.close()


def test_root_slash_is_the_legacy_layout():
    ws = _kv_ws(root="/")
    try:
        assert ws.root == "/"
        assert ws.fs.getcwd() == "/"
        r = ws.terminal("echo hi > f.txt")
        assert r
        assert ws.fs.read("/f.txt").strip() == b"hi"
    finally:
        ws.close()


def test_root_must_be_absolute():
    with pytest.raises(ValueError, match="absolute"):
        _kv_ws(root="workspace")


def test_custom_root_and_fork_inherits_it():
    ws = _kv_ws(root="/mnt/ws")
    try:
        assert ws.root == "/mnt/ws"
        assert ws.fs.getcwd() == "/mnt/ws"
        ws.terminal("echo x > f.txt")
        child = ws.fork("s2")
        try:
            assert child.root == "/mnt/ws"
            assert child.fs.read("/mnt/ws/f.txt").strip() == b"x"
        finally:
            child.close()
    finally:
        ws.close()


def test_vfs_imports_resolve_from_the_root():
    """`from helpers import mod` finds <root>/helpers/mod.py — the
    sandtrap module_root knob, threaded through ExecutionContext."""
    ws = _kv_ws()
    try:
        ws.fs.makedirs("/workspace/helpers", exist_ok=True)
        ws.fs.write("/workspace/helpers/util.py", b"def triple(x): return x * 3")
        r = ws.run_python("from helpers import util\nout = util.triple(3)")
        assert r, r.error
        assert r.namespace["out"] == 9
    finally:
        ws.close()


def test_vfs_import_error_names_the_root():
    ws = _kv_ws()
    try:
        ws.fs.makedirs("/workspace/helpers", exist_ok=True)
        ws.fs.write("/workspace/helpers/util.py", b"x = 1")
        r = ws.run_python("import util")
        assert not r
        assert "resolve from '/workspace'" in r.error
        assert "from helpers import util" in r.error
        # never a dotted path that rides the prefix itself
        assert "workspace.helpers" not in r.error
    finally:
        ws.close()


def test_skills_ride_the_root():
    """Skills install under <root>/skills and the catalog names it."""
    from nontainer import skills

    ws = _kv_ws()
    try:
        name = skills.install(ws, b"---\nname: probe\n---\nbody")
        assert name == "probe"
        assert ws.fs.exists("/workspace/skills/probe/SKILL.md")
        assert "/workspace/skills" in skills.catalog(ws)
    finally:
        ws.close()


def test_apps_dispatch_serves_from_the_root():
    from nontainer.apps import enable_apps, request

    ws = _kv_ws()
    try:
        runtime = enable_apps(ws)
        ws.fs.makedirs("/workspace/app/api", exist_ok=True)
        ws.fs.write(
            "/workspace/app/api/ping.py",
            b"def get(req):\n    return {'pong': True}\n",
        )
        r = runtime.dispatch(request("GET", "/api/ping"))
        assert r.status == 200
        # handler log rides the root too
        ws.fs.write(
            "/workspace/app/api/boom.py", b"def get(req):\n    raise ValueError('x')\n"
        )
        assert runtime.dispatch(request("GET", "/api/boom")).status == 500
        assert b"ValueError" in ws.fs.read("/workspace/app/logs/api.log")
    finally:
        ws.close()


def test_apps_notes_are_written_against_the_root():
    from nontainer.adapters.render import apps_notes

    default = apps_notes()
    assert "/workspace/app/index.html" in default
    assert "__WS__" not in default
    legacy = apps_notes(root="/")
    assert "/app/index.html" in legacy
    assert "/workspace" not in legacy
