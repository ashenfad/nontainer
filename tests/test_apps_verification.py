"""Whole verifier ownership, cancellation and immutable current-draft parity."""

import os
import threading
import time
from pathlib import Path

import pytest

from nontainer import Store
from nontainer.apps import AppsConfig, VerificationWorker, enable_apps
from nontainer.apps.verification import _members


@pytest.fixture
def app(tmp_path, monkeypatch, chromium_available):
    root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv(
        "PYTHONPATH",
        os.pathsep.join(
            [str(root), str(root / "tests"), os.environ.get("PYTHONPATH", "")]
        ),
    )
    store = Store(tmp_path / "store")
    workspace = store.open("draft")
    workspace.files.write(
        "/workspace/app/index.html", "<html><body><h1 id='title'>old</h1></body></html>"
    )
    workspace.commit()
    runtime = enable_apps(workspace, AppsConfig(readonly_handlers=True))
    yield workspace, runtime, store
    workspace.close()
    store.close()


def worker(app, **kwargs):
    return VerificationWorker(
        "verification_worker_support:factory", {"store": str(app[2].path)}, **kwargs
    )


def test_current_staged_draft_is_verified_with_native_result_and_no_stage_or_cwd_loss(
    app,
):
    ws, runtime, store = app
    ws.files.fs.write(
        "/workspace/app/index.html",
        b"<html><body><h1 id='title'>new draft</h1></body></html>",
    )
    ws.index.stage("/workspace/app/index.html")
    ws.files.fs.chdir("/workspace/app")
    staged = ws.index.status()
    cwd = ws.files.fs.getcwd()
    states = []
    result = worker(app, timeout_seconds=15, observer=states.append).verify(
        runtime,
        [
            {"assert": "document.querySelector('#title').textContent === 'new draft'"},
            {"screenshot": True},
        ],
    )
    assert result.ok, result
    assert len(result.screenshots) == 1 and ws.files.fs.isfile(result.screenshots[0])
    assert ws.files.fs.read("/workspace/app/index.html").find(b"new draft") >= 0
    assert ws.files.fs.getcwd() == cwd
    after = ws.index.status()
    assert after.staged == staged.staged
    assert states[-1]["phase"] == "reaped" and states[-1]["cleanup_ack"]
    assert not _members(states[-1]["process_group"])


def test_entire_test_deadline_stops_never_resolving_eval_and_reaps_descendants(app):
    ws, runtime, _ = app
    states = []
    started = time.monotonic()
    result = worker(
        app, timeout_seconds=3, terminate_grace_seconds=0.2, observer=states.append
    ).verify(runtime, [{"eval": "new Promise(() => {})"}])
    assert not result.ok and "whole-test deadline" in result.load_error
    assert time.monotonic() - started < 8
    assert states[-1]["phase"] == "reaped" and states[-1]["cleanup_ack"]
    assert not _members(states[-1]["process_group"])
    assert not ws.files.fs.isdir("/workspace/app/screenshots")


def test_stop_kills_synchronous_handler_before_return_and_prevents_late_write(
    app, tmp_path
):
    ws, runtime, store = app
    entered, late = tmp_path / "entered", tmp_path / "late"
    stop = threading.Event()
    states, outputs = [], []
    verifier = VerificationWorker(
        "verification_worker_support:blocked_handler",
        {"store": str(store.path), "entered": str(entered), "late_write": str(late)},
        timeout_seconds=15,
        terminate_grace_seconds=0.2,
        observer=states.append,
    )
    thread = threading.Thread(
        target=lambda: outputs.append(
            verifier.verify(
                runtime,
                [
                    {"screenshot": True},
                    {"eval": "fetch('api/blocked').then(r => r.json())"},
                ],
                cancelled=stop,
            )
        )
    )
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not entered.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert entered.exists()
        stop.set()
        thread.join(5)
        assert not thread.is_alive()
        assert not outputs[0].ok and "cancelled" in outputs[0].load_error
        assert states[-1]["cleanup_ack"] and not _members(states[-1]["process_group"])
        assert not late.exists()
        assert not ws.files.fs.isdir("/workspace/app/screenshots")
    finally:
        stop.set()
        thread.join(5)


def test_worker_construction_failure_cannot_import_partial_results(app):
    ws, runtime, store = app
    result = VerificationWorker(
        "verification_worker_support:failing_factory",
        {"store": str(store.path)},
        timeout_seconds=5,
    ).verify(runtime, [])
    assert not result.ok and "worker failed" in result.load_error
    assert not ws.files.fs.isdir("/workspace/app/screenshots")


def test_cancellation_before_start_does_not_commit_or_launch(app):
    ws, runtime, _ = app
    ws.files.fs.write("/workspace/app/pending.txt", b"draft remains")
    before = str(ws.ref)
    stop = threading.Event()
    stop.set()
    result = worker(app).verify(runtime, [], cancelled=stop)
    assert not result.ok and "before it started" in result.load_error
    assert str(ws.ref) == before and ws.uncommitted


@pytest.mark.parametrize(
    "changes",
    [
        {"timeout_seconds": 0},
        {"timeout_seconds": float("inf")},
        {"terminate_grace_seconds": -1},
        {"max_result_bytes": False},
    ],
)
def test_invalid_host_limits_are_rejected(changes):
    with pytest.raises(ValueError):
        VerificationWorker("verification_worker_support:factory", {}, **changes)


def test_oversized_capture_is_not_imported(app):
    ws, runtime, _ = app
    result = worker(app, max_artifact_bytes=1).verify(runtime, [{"screenshot": True}])
    assert not result.ok
    assert not ws.files.fs.isdir("/workspace/app/screenshots")


def test_ownership_observer_failure_cannot_start_work_or_skip_worker_teardown(app):
    states = []

    def broken_observer(event):
        states.append(event)
        if event["phase"] == "running":
            raise RuntimeError("synthetic ownership recording failure")

    with pytest.raises(RuntimeError, match="ownership recording"):
        worker(app, observer=broken_observer).verify(app[1], [])
    assert states[-1]["phase"] == "reaped" and states[-1]["cleanup_ack"]
    assert not _members(states[-1]["process_group"])


def test_owner_process_death_also_terminates_worker_and_browser(app, tmp_path):
    import json
    import subprocess
    import sys

    phases, entered, late = (tmp_path / name for name in ("phases", "entered", "late"))
    command = (
        "from verification_worker_support import owner; owner("
        + ",".join(repr(str(p)) for p in (app[2].path, phases, entered, late))
        + ")"
    )
    process = subprocess.Popen([sys.executable, "-c", command])
    group = None
    try:
        deadline = time.monotonic() + 10
        while not entered.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert entered.exists()
        group = json.loads(phases.read_text())["process_group"]
        process.kill()
        process.wait(timeout=5)
        deadline = time.monotonic() + 5
        while _members(group) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not _members(group)
        assert not late.exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        if group is not None and _members(group):
            import signal

            os.killpg(group, signal.SIGKILL)
        import shutil

        for directory in tmp_path.glob("nontainer-verify-*"):
            shutil.rmtree(directory)
