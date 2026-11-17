"""Bounded whole-app verification in an owned worker process.

An independent supervisor owns browser and synchronous callback descendants.
Linux subreaping and pidfds cover detached process groups before admission ends.
Only a pinned ref and JSON configuration cross; live workspaces/clients do not.
"""

from __future__ import annotations

import importlib
import json
import logging
import math
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .testapp import TestAppResult, coerce_actions, coerce_bind, coerce_viewport


@dataclass(frozen=True)
class VerificationContext:
    """Trusted factory input. ``config`` is supplied by the embedder, not tools.

    The factory is importable in a fresh interpreter and returns a context
    manager yielding an AppRuntime over ``ref`` with the supplied log and
    screenshot sinks. It reconstructs its own grants and host objects.
    """

    ref: str
    session_id: str
    config: Mapping[str, Any]
    job_id: str


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n").encode()


def _positive(name: str, value: Any) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{name} must be positive and finite")


def _members(group: int) -> list[int]:
    """Living members of this owned session/group; zombies cannot allocate.

    The worker and trusted runtimes must not detach subprocesses from this
    ownership scope. A different executor can use stronger job/cgroup ownership.
    """
    if sys.platform.startswith("linux"):
        members = []
        for path in Path("/proc").iterdir():
            if not path.name.isdigit():
                continue
            try:
                fields = (path / "stat").read_text().rsplit(") ", 1)[1].split()
                if int(fields[2]) == group and fields[0] != "Z":
                    members.append(int(path.name))
            except (OSError, ValueError, IndexError):
                continue
        return members
    listing = subprocess.check_output(["ps", "-axo", "pid=,pgid=,stat="], text=True)
    return [
        int(fields[0])
        for line in listing.splitlines()
        if len(fields := line.split()) == 3
        and int(fields[1]) == group
        and not fields[2].startswith("Z")
    ]


def _stop(process: subprocess.Popen, root: Path, job_id: str) -> dict:
    """Wait for the independent supervisor's physical descendant acknowledgment.

    It is never killed as a timeout shortcut. Missing acknowledgment keeps the
    caller charged rather than declaring still-possible native work completed.
    """
    warned = False
    while True:
        process.poll()
        path = root / "cleanup.json"
        if process.returncode is not None and path.is_file():
            data = json.loads(path.read_text())
            if (
                data.get("job_id") == job_id
                and data.get("cleanup_ack") is True
                and not _members(process.pid)
            ):
                return data
        if process.returncode is not None and not warned:
            logging.getLogger(__name__).critical(
                "Verifier supervisor exited without confirmed descendant cleanup; allocation remains charged"
            )
            warned = True
        time.sleep(0.02)


@dataclass(frozen=True)
class VerificationWorker:
    """Host-owned isolated test_app runner; successful tool results stay native.

    ``factory`` names a trusted ``module:callable``. It receives a
    VerificationContext and keyword ``log_sink``/``screenshot_sink``; its
    context manager yields an AppRuntime opened at the exact pinned draft.
    The factory must use readonly handlers. Linux supervision adopts detached
    descendants and signals them through pidfds; macOS tracks descendant birth
    identities but cannot adopt daemon orphans. Host objects are reconstructed
    from trusted configuration, never transported as live clients.

    ``observer`` sees lifecycle metadata in the parent, including job id,
    deadline, worker pid and cleanup acknowledgment. It is never sent to the
    worker. Admission should wrap verify() until it returns.
    """

    factory: str
    config: Mapping[str, Any] = field(default_factory=dict)
    timeout_seconds: float = 180
    terminate_grace_seconds: float = 2
    max_request_bytes: int = 1024 * 1024
    max_result_bytes: int = 4 * 1024 * 1024
    max_artifact_bytes: int = 16 * 1024 * 1024
    max_log_bytes: int = 1024 * 1024
    observer: Callable[[Mapping[str, Any]], None] | None = field(
        default=None, compare=False, repr=False
    )

    def __post_init__(self):
        if not re.fullmatch(
            r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*", self.factory
        ):
            raise ValueError("factory must be a trusted module:callable")
        for name in ("timeout_seconds", "terminate_grace_seconds"):
            _positive(name, getattr(self, name))
        for name in (
            "max_request_bytes",
            "max_result_bytes",
            "max_artifact_bytes",
            "max_log_bytes",
        ):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        _json_bytes(self.config)

    def verify(
        self,
        runtime,
        actions=None,
        *,
        viewport="desktop",
        bind=None,
        cancelled: threading.Event | None = None,
    ):
        if not (sys.platform.startswith("linux") or sys.platform == "darwin"):
            raise RuntimeError(
                "Isolated verification requires Linux or macOS process ownership"
            )
        if not runtime.config.readonly_handlers:
            raise ValueError(
                "Isolated verification requires readonly_handlers=True for pinned-state parity"
            )
        request = {
            "actions": coerce_actions(actions),
            "viewport": coerce_viewport(viewport),
            "bind": coerce_bind(bind),
        }
        if (
            len(_json_bytes({"config": self.config, "request": request}))
            > self.max_request_bytes
        ):
            return TestAppResult(
                False,
                load_error="Verification request exceeds the configured byte budget",
            )
        if cancelled is not None and cancelled.is_set():
            return TestAppResult(
                False, load_error="Verification cancelled before it started"
            )

        job_id = uuid.uuid4().hex
        started = time.monotonic()
        deadline = started + self.timeout_seconds
        deadline_at = time.time() + self.timeout_seconds

        def notify(phase, **fields):
            if self.observer is not None:
                try:
                    self.observer(
                        {
                            "job_id": job_id,
                            "phase": phase,
                            "deadline_at": deadline_at,
                            **fields,
                        }
                    )
                except Exception:
                    # Before heavy work starts, ownership recording must succeed.
                    # Cleanup diagnostics can never skip ownership teardown.
                    if phase == "running":
                        raise
                    logging.getLogger(__name__).exception(
                        "Verification lifecycle observer failed"
                    )

        ws = runtime.workspace
        with ws.lock:
            # A framework durability commit preserves the agent's own git
            # staging/cwd and includes edits made earlier in this active turn.
            ws.commit({"tool": "verification_snapshot", "job_id": job_id})
            ref = str(ws.ref)
            session_id = ws.session
        notify("starting")
        with tempfile.TemporaryDirectory(prefix="nontainer-verify-") as directory:
            root = Path(directory)
            root.chmod(0o700)
            payload = {
                "factory": self.factory,
                "config": self.config,
                "request": request,
                "ref": ref,
                "session_id": session_id,
                "job_id": job_id,
                "max_result_bytes": self.max_result_bytes,
                "max_artifact_bytes": self.max_artifact_bytes,
                "max_log_bytes": self.max_log_bytes,
                "terminate_grace_seconds": self.terminate_grace_seconds,
            }
            raw = _json_bytes(payload)
            if len(raw) > self.max_request_bytes:
                return TestAppResult(
                    False,
                    load_error="Verification request exceeds the configured byte budget",
                )
            request_path = root / "request.json"
            request_path.write_bytes(raw)
            request_path.chmod(0o600)
            reason = None
            owner_read, owner_write = os.pipe()
            try:
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "nontainer.apps.verification",
                        "--worker",
                        str(root),
                        str(owner_read),
                    ],
                    start_new_session=True,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                    pass_fds=(owner_read,),
                )
            except BaseException:
                os.close(owner_write)
                raise
            finally:
                os.close(owner_read)
            try:
                notify("running", worker_pid=process.pid, process_group=process.pid)
                (root / "start").touch()
                while process.poll() is None:
                    if cancelled is not None and cancelled.is_set():
                        reason = "Verification cancelled; worker cleanup completed"
                        break
                    if time.monotonic() >= deadline:
                        reason = f"Verification exceeded its {self.timeout_seconds:g}s whole-test deadline; worker cleanup completed"
                        break
                    time.sleep(0.02)
            finally:
                notify("stopping", worker_pid=process.pid, process_group=process.pid)
                os.close(owner_write)
                cleanup = _stop(process, root, job_id)
                notify(
                    "reaped",
                    worker_pid=process.pid,
                    process_group=process.pid,
                    cleanup_ack=True,
                    ownership_mode=cleanup["ownership_mode"],
                    owned_process_count=cleanup["owned_process_count"],
                )
            if reason is not None:
                return TestAppResult(False, load_error=reason)
            if cancelled is not None and cancelled.is_set():
                return TestAppResult(
                    False, load_error="Verification cancelled; worker cleanup completed"
                )
            if process.returncode != 0:
                return TestAppResult(
                    False,
                    load_error="Verification worker failed; no partial result was imported",
                )
            path = root / "result.json"
            if (
                not path.is_file()
                or path.is_symlink()
                or path.stat().st_size > self.max_result_bytes
            ):
                return TestAppResult(
                    False,
                    load_error="Verification worker result is missing or exceeds its byte budget",
                )
            data = json.loads(path.read_text())
            if data.get("job_id") != job_id or data.get("ref") != ref:
                return TestAppResult(
                    False,
                    load_error="Verification worker returned a mismatched pinned draft",
                )
            result = TestAppResult.from_dict(data["result"])
            artifacts = data.get("artifacts", [])
            if len(artifacts) > 5 or len(set(result.screenshots)) != len(
                result.screenshots
            ):
                raise ValueError("Malformed verification screenshots")
            images = {}
            total = 0
            for entry in artifacts:
                name, relative = entry["path"], entry["file"]
                artifact = root / relative
                if (
                    not re.fullmatch(r"shot-\d+\.png", relative)
                    or artifact.is_symlink()
                ):
                    raise ValueError("Malformed verification screenshot path")
                runtime._check_screenshot_path(name)
                size = artifact.stat().st_size
                total += size
                if total > self.max_artifact_bytes:
                    raise ValueError(
                        "Verification screenshots exceed their byte budget"
                    )
                images[name] = artifact.read_bytes()
            if set(images) != set(result.screenshots):
                raise ValueError(
                    "Verification screenshot inventory differs from the result"
                )
            logs = data.get("logs", [])
            if (
                not isinstance(logs, list)
                or not all(isinstance(line, str) for line in logs)
                or len(_json_bytes(logs)) > self.max_log_bytes
            ):
                raise ValueError("Verification logs exceed their byte budget")
            # Only the parent imports final, validated artifacts. A timed-out
            # worker cannot later write into the author's workspace.
            runtime.save_screenshots(images)
            for line in logs:
                runtime._write_log(line)
            return result


def _worker(root: Path, owner_fd: int) -> None:
    from ._verification_process import supervise

    data = json.loads((root / "request.json").read_text())
    raise SystemExit(supervise(root, owner_fd, data))


def _body(root: Path) -> None:
    os.umask(0o077)
    data = json.loads((root / "request.json").read_text())
    module, name = data["factory"].split(":", 1)
    factory = getattr(importlib.import_module(module), name)
    context = VerificationContext(
        data["ref"], data["session_id"], data["config"], data["job_id"]
    )
    logs, artifacts = [], []
    artifact_bytes = 0

    def log_sink(line):
        line = str(line)
        if len(_json_bytes([*logs, line])) > data["max_log_bytes"]:
            raise ValueError("Verification logs exceed the configured byte budget")
        logs.append(line)

    def screenshot_sink(path, image):
        nonlocal artifact_bytes
        artifact_bytes += len(image)
        if len(artifacts) >= 5 or artifact_bytes > data["max_artifact_bytes"]:
            raise ValueError(
                "Verification screenshots exceed the configured byte budget"
            )
        filename = f"shot-{len(artifacts)}.png"
        (root / filename).write_bytes(image)
        artifacts.append({"path": path, "file": filename})

    with factory(
        context, log_sink=log_sink, screenshot_sink=screenshot_sink
    ) as runtime:
        if not runtime.workspace.frozen or not runtime.config.readonly_handlers:
            raise ValueError(
                "Verification factory must open the pinned draft with readonly handlers"
            )
        if str(runtime.workspace.ref) != context.ref:
            raise ValueError("Verification factory opened a different draft")
        result = runtime.test_app(**data["request"])
        output = _json_bytes(
            {
                "job_id": context.job_id,
                "ref": context.ref,
                "result": result.to_dict(),
                "artifacts": artifacts,
                "logs": logs,
            }
        )
        if len(output) > data["max_result_bytes"]:
            raise ValueError("Verification result exceeds its configured byte budget")
        (root / "result.json").write_bytes(output)


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--worker":
        _worker(Path(sys.argv[2]), int(sys.argv[3]))
    elif len(sys.argv) == 3 and sys.argv[1] == "--body":
        _body(Path(sys.argv[2]))
    else:
        raise SystemExit("This entrypoint is used by VerificationWorker")
