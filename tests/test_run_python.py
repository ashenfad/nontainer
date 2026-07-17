"""run_python: sandboxed execution, namespace in/out, cache, config."""

import math

import pytest

from nontainer import (
    ModuleGrant,
    NotSupportedError,
    PythonConfig,
    Workspace,
)
from nontainer.providers import DirProvider


def test_basic_exec(dir_ws):
    r = dir_ws.run_python("total = sum(range(10))\nprint(total)")
    assert r
    assert r.error is None
    assert r.stdout.strip() == "45"
    assert r.namespace["total"] == 45
    assert r.duration >= 0


def test_error_is_result_not_exception(dir_ws):
    r = dir_ws.run_python("1/0")
    assert not r
    assert r.error is not None
    assert "ZeroDivisionError" in r.error


def test_runtime_error_renders_the_full_traceback(dir_ws):
    """Line numbers are what a repair loop aims at: 'NameError' alone
    sends an agent bisecting; 'line 3' sends it to line 3."""
    r = dir_ws.run_python("a = 1\nb = 2\nc = missing_name\n")
    assert not r
    assert "Traceback (most recent call last)" in r.error
    assert "line 3" in r.error
    assert "NameError" in r.error


def test_error_frames_hide_host_install_paths(dir_ws):
    """Frames inside granted libraries keep their module-relative path
    (that's the signal) but lose the host install prefix (that's a
    leak): json/decoder.py, not /…/python3.x/json/decoder.py."""
    r = dir_ws.run_python("import json\njson.loads('{nope')")
    assert not r
    assert 'File "json/' in r.error
    assert "python3." not in r.error


def test_machinery_frames_are_dropped(dir_ws):
    """A gate raising through __st_import__ or a monkeyfs interceptor
    is OUR plumbing — an agent reading those frames would go debugging
    the sandbox instead of its own line."""
    r = dir_ws.run_python("import subprocess")
    assert not r
    assert "line 1" in r.error  # the agent's frame survives
    assert "gates.py" not in r.error and "__st_import__" not in r.error

    r = dir_ws.run_python("open('/no/such/file')")
    assert not r
    assert "FileNotFoundError" in r.error
    assert "monkeyfs" not in r.error


def test_render_error_prefers_the_worker_rendered_text():
    """Under process isolation the traceback object doesn't survive
    pickling; sandtrap ships the rendered text on the exception and
    _render_error must use it (trimmed) rather than re-formatting the
    frameless exception."""
    from nontainer.workspace import _render_error

    e = ValueError("boom")
    e._st_traceback_text = (
        "Traceback (most recent call last):\n"
        '  File "<sandtrap:68>", line 128, in get\n'
        '  File "/host/.venv/lib/python3.12/site-packages/pandas/core/'
        'generic.py", line 4686, in _drop_axis\n'
        "ValueError: boom"
    )
    out = _render_error(e)
    assert '"<sandtrap:68>", line 128' in out
    assert 'File "pandas/core/generic.py"' in out
    assert "/host/" not in out


def test_pathological_tracebacks_get_middle_elided():
    from nontainer.workspace import _trim_rendered_traceback

    text = "\n".join(f"line {i}" for i in range(200))
    out = _trim_rendered_traceback(text)
    assert "[... 152 traceback lines elided ...]" in out
    assert "line 0" in out and "line 199" in out
    assert len(out.splitlines()) == 49


def test_namespace_out_filters_underscore(dir_ws):
    r = dir_ws.run_python("_private = 1\npublic = 2")
    assert "public" in r.namespace
    assert "_private" not in r.namespace


def test_inputs_bound_and_not_echoed_back(dir_ws):
    r = dir_ws.run_python("doubled = [x * 2 for x in xs]", inputs={"xs": [1, 2, 3]})
    assert r.namespace["doubled"] == [2, 4, 6]
    assert "xs" not in r.namespace  # injected names are not re-reported


def test_unpicklable_inputs_rejected(dir_ws):
    with pytest.raises(TypeError, match="not picklable"):
        dir_ws.run_python("pass", inputs={"f": open(__file__)})


def test_fs_round_trip_between_tools(dir_ws):
    dir_ws.run_python("open('out.txt', 'w').write('from python')")
    r = dir_ws.terminal("cat out.txt")
    assert r.stdout.strip() == "from python"


def test_registered_module(tmp_path):
    p = DirProvider(tmp_path / "ws", session="s1")
    ws = Workspace(p, python=PythonConfig(modules=[math]))
    r = ws.run_python("import math\nroot = math.sqrt(16)")
    assert r, r.error
    assert r.namespace["root"] == 4.0
    ws.close()


def test_module_grant_wraps_module(tmp_path):
    p = DirProvider(tmp_path / "ws", session="s1")
    ws = Workspace(p, python=PythonConfig(modules=[ModuleGrant(math)]))
    r = ws.run_python("import math\nv = math.floor(3.7)")
    assert r, r.error
    assert r.namespace["v"] == 3
    ws.close()


def test_unregistered_import_blocked(dir_ws):
    r = dir_ws.run_python("import socket")
    assert not r


def test_host_objects_live_binding(tmp_path):
    class Counter:
        def __init__(self):
            self.n = 0

        def bump(self):
            self.n += 1
            return self.n

    counter = Counter()
    p = DirProvider(tmp_path / "ws", session="s1")
    ws = Workspace(p, python=PythonConfig(host_objects={"counter": counter}))
    r = ws.run_python("val = counter.bump()")
    assert r, r.error
    assert r.namespace["val"] == 1
    assert counter.n == 1  # the live host object mutated
    ws.close()


def test_plain_data_host_object(tmp_path):
    p = DirProvider(tmp_path / "ws", session="s1")
    ws = Workspace(p, python=PythonConfig(host_objects={"config": {"k": 7}}))
    r = ws.run_python("v = config['k']")
    assert r, r.error
    assert r.namespace["v"] == 7
    ws.close()


# -- cache ---------------------------------------------------------------


def test_cache_round_trip_across_calls(dir_ws):
    r1 = dir_ws.run_python("cache['score'] = 42")
    assert r1, r1.error
    r2 = dir_ws.run_python("doubled = cache['score'] * 2")
    assert r2, r2.error
    assert r2.namespace["doubled"] == 84


def test_cache_host_side_view(dir_ws):
    dir_ws.run_python("cache['k'] = [1, 2]")
    assert dir_ws.cache["k"] == [1, 2]
    dir_ws.cache["j"] = "host-written"
    r = dir_ws.run_python("v = cache['j']")
    assert r.namespace["v"] == "host-written"


def test_cache_disabled(tmp_path):
    p = DirProvider(tmp_path / "ws", session="s1")
    ws = Workspace(p, cache=False)
    with pytest.raises(NotSupportedError):
        _ = ws.cache
    r = ws.run_python("cache['k'] = 1")
    assert not r  # 'cache' is not defined in the sandbox
    ws.close()


def test_cache_persists_across_instances(tmp_path):
    p1 = DirProvider(tmp_path / "ws", session="s1")
    ws1 = Workspace(p1)
    ws1.run_python("cache['stay'] = 'put'")
    ws1.close()

    p2 = DirProvider(tmp_path / "ws", session="s1")
    ws2 = Workspace(p2)
    assert ws2.cache["stay"] == "put"
    ws2.close()


# -- workspace-level guards ----------------------------------------------


def test_versioning_verbs_raise_on_dir(dir_ws):
    with pytest.raises(NotSupportedError):
        dir_ws.checkpoint()
    with pytest.raises(NotSupportedError):
        dir_ws.fork("other")


def test_closed_workspace_rejects_calls(tmp_path):
    p = DirProvider(tmp_path / "ws", session="s1")
    ws = Workspace(p)
    ws.close()
    with pytest.raises(Exception, match="closed"):
        ws.terminal("pwd")


# -- budget-aware stdout (reprobate over print snapshots) -------------------


def test_oversized_print_gets_structural_elision(tmp_path):
    p = DirProvider(tmp_path / "ws", session="s1")
    ws = Workspace(p, max_observation=500)
    r = ws.run_python("print(list(range(100000)))")
    assert r, r.error
    assert r.truncated
    assert len(r.stdout) <= 500
    assert "more" in r.stdout           # reprobate's elision marker
    assert r.stdout.startswith("[0, 1")  # structure preserved, not a raw cut
    ws.close()


def test_small_stdout_stays_verbatim(tmp_path):
    p = DirProvider(tmp_path / "ws", session="s1")
    ws = Workspace(p, max_observation=500)
    r = ws.run_python("print('exact text', 42)")
    assert r.stdout.strip() == "exact text 42"
    assert not r.truncated
    ws.close()


def test_many_prints_share_budget(tmp_path):
    p = DirProvider(tmp_path / "ws", session="s1")
    ws = Workspace(p, max_observation=600)
    r = ws.run_python(
        "for i in range(50):\n    print(f'row {i}', list(range(1000)))"
    )
    assert r.truncated
    assert len(r.stdout) <= 700  # budget + elision note headroom
    assert "elided" in r.stdout or "more" in r.stdout
    ws.close()


def test_pure_writes_fall_back_to_head_cut(tmp_path):
    p = DirProvider(tmp_path / "ws", session="s1")
    ws = Workspace(p, max_observation=100)
    # sys.stdout.write via print's file arg isn't available; use a
    # single huge print STRING — reprobate still hard-caps it.
    r = ws.run_python("print('x' * 10000)")
    assert r.truncated
    assert len(r.stdout) <= 500
    ws.close()
