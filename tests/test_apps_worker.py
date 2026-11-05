"""test_app in a worker (#303): the complete verification run against a
pinned, read-only input, with its captures and log lines handed back as
data and its result encoded, read by an owner exactly as a direct run.

The "worker" here is the same process with nothing shared but the
store's directory and JSON: a frozen workspace resolved from a ref,
host objects built afresh from trusted settings, and both sinks.
"""

import json

import pytest

from nontainer import PythonConfig, Store
from nontainer.apps import AppsConfig, enable_apps, render_test_app
from nontainer.apps import TestAppResult as Result  # not a test class
from nontainer.apps.dispatch import AppRuntime

INDEX = """<!doctype html>
<html><body>
<button id="load">Load</button><p id="answer"></p>
<button id="rows">Rows</button><p id="out"></p>
<button id="boom">Boom</button>
<script src="app.js"></script>
</body></html>
"""

APP_JS = """document.querySelector('#load').onclick = async () => {
  const value = await (await fetch('api/answer')).json();
  document.querySelector('#answer').textContent = value.answer;
};
document.querySelector('#rows').onclick = async () => {
  const value = await (await fetch('api/rows')).json();
  document.querySelector('#out').textContent = value.rows.join(',');
};
document.querySelector('#boom').onclick = () => {
  throw new Error('from the page');
};
"""

ANSWER = "def get(req):\n    return {'answer': '42'}\n"
ROWS = "def get(req):\n    return {'rows': db.all()}\n"


class Rows:
    """A host object a handler reads through."""

    def __init__(self, *rows):
        self._rows = list(rows)

    def all(self):
        return list(self._rows)


def _host_objects():
    """Built afresh on each side, as a worker builds its own from
    trusted configuration rather than receiving live clients."""
    return {"db": Rows("live-row"), "testdb": Rows("test-row")}


CONFIG = AppsConfig(readonly_handlers=True)

CASES = {
    "a handler call and a screenshot": {
        "actions": [
            {"click": "#load"},
            {"assert": "document.querySelector('#answer').textContent === '42'"},
            {"screenshot": "after"},
        ],
    },
    "a failed assertion": {
        "actions": [
            {"click": "#load"},
            {"assert": "document.querySelector('#answer').textContent === '43'"},
        ],
        "assert_timeout_ms": 500,
    },
    "a page error": {"actions": [{"click": "#boom"}]},
    "a db/testdb swap": {
        "actions": [
            {"click": "#rows"},
            {"assert": "document.querySelector('#out').textContent === 'test-row'"},
        ],
        "bind": {"db": "testdb"},
    },
}


@pytest.fixture
def pinned(tmp_path, chromium_available):
    """A session with the app, committed and pinned: ``(store, ws,
    runtime, ref)``."""
    st = Store(tmp_path)
    ws = st.open("demo", python=PythonConfig(host_objects=_host_objects()))
    rt = enable_apps(ws, CONFIG)
    ws.files.write("/workspace/app/index.html", INDEX)
    ws.files.write("/workspace/app/app.js", APP_JS)
    ws.files.write("/workspace/app/api/answer.py", ANSWER)
    ws.files.write("/workspace/app/api/rows.py", ROWS)
    with ws.lock:
        ws.commit({"tool": "dispatch"})
        ref = str(ws.ref)
    yield st, ws, rt, ref
    ws.close()
    st.close()


def _in_a_worker(path, ref, request):
    """What a worker does with ``{ref, request}``: resolve the pinned
    state with trusted settings, run the whole verification with both
    sinks, and answer in JSON plus the captures and log lines."""
    shots, logs = {}, []
    worker = Store(path)
    try:
        with worker.resolve(
            ref, python=PythonConfig(host_objects=_host_objects())
        ) as snap:
            runtime = AppRuntime(
                snap,
                CONFIG,
                log_sink=logs.append,
                screenshot_sink=shots.__setitem__,
            )
            result = runtime.test_app(**json.loads(json.dumps(request)))
            assert not snap.files.exists("/workspace/app/screenshots")
    finally:
        worker.close()
    return json.dumps(result.to_dict()), shots, logs


@pytest.mark.parametrize("case", list(CASES))
def test_a_worker_run_reads_as_the_direct_run(tmp_path, pinned, case):
    st, ws, rt, ref = pinned
    request = CASES[case]

    payload, shots, logs = _in_a_worker(tmp_path, ref, request)
    remote = Result.from_dict(json.loads(payload))
    direct = rt.test_app(**request)

    assert remote.ok == direct.ok
    assert render_test_app(remote) == render_test_app(direct)
    assert remote.screenshots == tuple(shots) == direct.screenshots
    # The same request lines, in the log or through the sink. A run
    # that calls no handler logs nothing either way.
    log = "/workspace/app/logs/api.log"
    direct_log = ws.files.read(log).decode() if ws.files.exists(log) else ""
    assert all(line in direct_log for line in logs)
    assert bool(logs) == ("->" in direct_log)


def test_screenshots_reach_the_owner_at_the_paths_a_direct_run_writes(tmp_path, pinned):
    st, ws, rt, ref = pinned
    payload, shots, _ = _in_a_worker(
        tmp_path, ref, CASES["a handler call and a screenshot"]
    )
    remote = Result.from_dict(json.loads(payload))
    assert remote.ok
    assert all(png.startswith(b"\x89PNG") for png in shots.values())

    assert rt.save_screenshots(shots) == remote.screenshots
    for path in remote.screenshots:
        assert ws.files.read(path) == shots[path]
    # The result names the captures and carries none of them.
    assert len(payload) < min(len(png) for png in shots.values())


def test_a_frozen_input_verifies_green_without_the_sink_failing_only_the_capture(
    tmp_path, pinned
):
    """Without a sink, a pinned input still refuses the write, and only
    the screenshot's action says so, as before."""
    st, ws, rt, ref = pinned
    with st.resolve(ref) as snap:
        runtime = AppRuntime(snap, CONFIG, log_sink=lambda line: None)
        result = runtime.test_app(CASES["a handler call and a screenshot"]["actions"])
    assert not result.ok
    assert [r.ok for r in result.results] == [True, True, False]
    assert "screenshot not saved" in result.results[2].error
    assert "frozen snapshot at commit" in result.results[2].error
