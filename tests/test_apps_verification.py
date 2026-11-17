"""Whole verifier ownership, cancellation and immutable current-draft parity."""

import os
import signal
import sys
import threading
import time
from pathlib import Path

import pytest

from nontainer import Store
from nontainer.apps import AppsConfig, VerificationWorker, enable_apps
from nontainer.apps._verification_process import processes
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


def _family(root):
    snapshot = processes()
    family = {root}
    while True:
        expanded = family | {pid for pid, p in snapshot.items() if p.parent in family}
        if expanded == family:
            return [snapshot[pid] for pid in family if pid in snapshot]
        family = expanded


def _living_original(family):
    current = processes()
    return [
        p
        for old in family
        if (p := current.get(old.pid)) is not None
        and p.birth == old.birth
        and p.state != "Z"
    ]


def _pause_browser(family):
    # Pause Node too: otherwise Playwright's own graceful cleanup can hide a
    # supervisor that cannot kill Chromium's detached process group.
    browser = [p for p in family if "chrome" in p.command.lower()]
    # Node changes its process/thread title to MainThread on some builds.
    # Identify it by the detached browser root's parent, not a display name.
    launchers = {p.parent for p in browser if p.pid == p.group}
    node = [p for p in family if p.pid in launchers]
    assert browser and node
    for process in [*node, *browser]:
        os.kill(process.pid, signal.SIGSTOP)
    return browser


def test_hard_stop_reaps_paused_node_and_detached_chromium_before_ack(app, tmp_path):
    ws, runtime, store = app
    entered, late = tmp_path / "entered", tmp_path / "late"
    stop = threading.Event()
    states, output, family, alive_at_ack = [], [], [], []

    def observe(event):
        states.append(event)
        if event["phase"] == "reaped":
            alive_at_ack.extend(_living_original(family))

    verifier = VerificationWorker(
        "verification_worker_support:blocked_handler",
        {"store": str(store.path), "entered": str(entered), "late_write": str(late)},
        timeout_seconds=20,
        terminate_grace_seconds=0.2,
        observer=observe,
    )
    thread = threading.Thread(
        target=lambda: output.append(
            verifier.verify(
                runtime,
                [
                    {"screenshot": True},
                    {"eval": "fetch('api/blocked').then(r=>r.json())"},
                ],
                cancelled=stop,
            )
        )
    )
    thread.start()
    try:
        deadline = time.monotonic() + 15
        while not entered.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert entered.exists()
        group = next(s["process_group"] for s in states if s["phase"] == "running")
        family.extend(_family(group))
        browser = _pause_browser(family)
        assert any(p.group != group for p in browser)
        stop.set()
        thread.join(10)
        assert not thread.is_alive() and not output[0].ok
        assert states[-1]["cleanup_ack"] and not alive_at_ack
        assert not _living_original(family)
        assert not late.exists() and not ws.files.fs.isdir("/workspace/app/screenshots")
    finally:
        stop.set()
        thread.join(5)
        if thread.is_alive():
            supervisor = next(
                (s["process_group"] for s in states if s["phase"] == "running"), None
            )
            for process in _living_original(family):
                if process.pid == supervisor:
                    continue
                try:
                    os.kill(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            thread.join(5)


@pytest.mark.parametrize("before_start", [False, True])
def test_owner_process_death_reaps_full_browser_tree_or_pre_start_supervisor(
    app, tmp_path, before_start
):
    import json
    import shutil
    import subprocess

    phases, entered, late = (tmp_path / name for name in ("phases", "entered", "late"))
    command = (
        "from verification_worker_support import owner; owner("
        + ",".join(repr(str(p)) for p in (app[2].path, phases, entered, late))
        + ", wait_before_ack="
        + repr(before_start)
        + ")"
    )
    process = subprocess.Popen([sys.executable, "-c", command])
    family = []
    try:
        marker = phases if before_start else entered
        deadline = time.monotonic() + 15
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert marker.exists()
        group = json.loads(phases.read_text())["process_group"]
        family = _family(group)
        if not before_start:
            _pause_browser(family)
        process.kill()
        process.wait(timeout=5)
        deadline = time.monotonic() + 10
        while _living_original(family) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not _living_original(family)
        assert not late.exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        for owned in _living_original(family):
            try:
                os.kill(owned.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        for directory in tmp_path.glob("nontainer-verify-*"):
            shutil.rmtree(directory)


@pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="Linux subreaper adoption guarantee"
)
def test_supervisor_adopts_double_forked_setsid_child_and_reaps_before_ack(
    app, tmp_path
):
    pid_path, late = tmp_path / "daemon-pid", tmp_path / "daemon-late"
    states = []
    result = VerificationWorker(
        "verification_worker_support:daemonized",
        {
            "store": str(app[2].path),
            "daemon_pid": str(pid_path),
            "late_write": str(late),
        },
        observer=states.append,
    ).verify(app[1], [])
    assert result.ok, result
    assert pid_path.exists()
    assert int(pid_path.read_text()) not in processes()
    assert not late.exists()
    assert states[-1]["ownership_mode"] == "linux_subreaper_pidfd"
    assert states[-1]["cleanup_ack"]
