"""Trusted verifier supervision across detached descendant process groups.

Linux uses a subreaper and pidfds: detached or double-forked descendants are
adopted here, and a live process handle cannot accidentally signal a reused PID.
macOS tracks known descendant birth identities; it cannot adopt daemon orphans.
"""

from __future__ import annotations

import ctypes
import os
import select
import signal
import struct
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Process:
    pid: int
    parent: int
    group: int
    birth: str
    state: str
    command: str = ""


def _mac_process(pid: int) -> Process | None:
    # proc_bsdinfo, from sys/proc_info.h: microsecond birth identity, unlike
    # ps lstart's second precision. PROC_PIDTBSDINFO=3; SZOMB=5.
    lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    buffer = ctypes.create_string_buffer(136)
    size = lib.proc_pidinfo(pid, 3, 0, buffer, len(buffer))
    if size != len(buffer):
        return None
    fields = struct.unpack("=12I16s32s6I2Q", buffer.raw)
    return Process(
        pid,
        fields[4],
        fields[15],
        f"{fields[20]}:{fields[21]}",
        "Z" if fields[1] == 5 else "T" if fields[1] == 4 else "live",
        fields[12].rstrip(b"\0").decode(errors="replace"),
    )


def processes() -> dict[int, Process]:
    if sys.platform.startswith("linux"):
        result = {}
        for path in Path("/proc").iterdir():
            if not path.name.isdigit():
                continue
            try:
                raw = (path / "stat").read_text()
                fields = raw.rsplit(") ", 1)[1].split()
                command = raw.split("(", 1)[1].rsplit(")", 1)[0]
                pid = int(path.name)
                result[pid] = Process(
                    pid, int(fields[1]), int(fields[2]), fields[19], fields[0], command
                )
            except (OSError, ValueError, IndexError):
                continue
        return result
    if sys.platform == "darwin":
        listing = subprocess.check_output(["ps", "-axo", "pid="], text=True)
        return {
            process.pid: process
            for value in listing.split()
            if (process := _mac_process(int(value))) is not None
        }
    raise RuntimeError("Verifier ownership requires Linux or macOS process identities")


class Descendants:
    def __init__(self):
        self.root = os.getpid()
        self.known: dict[int, Process] = {}
        self.handles: dict[int, int] = {}
        self.exit_codes: dict[int, int] = {}
        self.linux = sys.platform.startswith("linux")
        if self.linux:
            # Install before any factory child exists. No privilege is needed.
            library = ctypes.CDLL(None, use_errno=True)
            if library.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
                code = ctypes.get_errno()
                raise OSError(code, os.strerror(code))
            if not hasattr(os, "pidfd_open") or not hasattr(
                signal, "pidfd_send_signal"
            ):
                raise RuntimeError("Linux verifier ownership requires pidfd support")
            handle = os.pidfd_open(self.root)
            os.close(handle)

    @property
    def mode(self):
        return "linux_subreaper_pidfd" if self.linux else "macos_tracked_births"

    def capture(self) -> dict[int, Process]:
        snapshot = processes()
        roots = {self.root} | {
            pid
            for pid, value in self.known.items()
            if pid in snapshot and snapshot[pid].birth == value.birth
        }
        while True:
            expanded = roots | {
                pid for pid, value in snapshot.items() if value.parent in roots
            }
            if expanded == roots:
                break
            roots = expanded
        for pid in roots - {self.root}:
            value = snapshot.get(pid)
            if value is None or value.state == "Z":
                continue
            existing = self.known.get(pid)
            if existing is not None and existing.birth == value.birth:
                continue
            if pid in self.handles:
                os.close(self.handles.pop(pid))
            if self.linux:
                try:
                    handle = os.pidfd_open(pid)
                except ProcessLookupError:
                    continue
                # The PID may have changed between enumeration and opening.
                current = processes().get(pid)
                if current is None or current.birth != value.birth:
                    os.close(handle)
                    continue
                self.handles[pid] = handle
            self.known[pid] = value
        return snapshot

    def live(self, snapshot=None) -> list[Process]:
        snapshot = processes() if snapshot is None else snapshot
        return [
            current
            for pid, previous in self.known.items()
            if (current := snapshot.get(pid)) is not None
            and current.birth == previous.birth
            and current.state != "Z"
        ]

    def send(self, process: Process, sig):
        try:
            if self.linux:
                signal.pidfd_send_signal(self.handles[process.pid], sig)
            else:
                # macOS lacks pidfds. Refuse a changed birth identity; unlike
                # Linux this is a tracked-tree guarantee, not orphan adoption.
                current = _mac_process(process.pid)
                if current is not None and current.birth == process.birth:
                    os.kill(process.pid, sig)
        except ProcessLookupError:
            pass

    def reap(self) -> bool:
        while True:
            try:
                pid, status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                return True
            if not pid:
                return False
            self.exit_codes[pid] = os.waitstatus_to_exitcode(status)

    def cleanup(self, grace: float) -> None:
        # Capture/freeze while ancestry is intact. On Linux any orphan created
        # by concurrent parent exit is readopted here, independent of PG/session.
        deadline = time.monotonic() + grace
        while True:
            snapshot = self.capture()
            live = self.live(snapshot)
            for process in live:
                self.send(process, signal.SIGSTOP)
            self.reap()
            if (
                not live
                or all(p.state in {"T", "t"} for p in live)
                or time.monotonic() >= deadline
            ):
                break
            time.sleep(0.01)
        while True:
            snapshot = self.capture()
            for process in self.live(snapshot):
                self.send(process, signal.SIGKILL)
            children_gone = self.reap()
            # All owned parents must be gone too, so no process can fork after
            # this fixed point. D-state tasks retain supervision/admission.
            if children_gone and not self.live(self.capture()):
                break
            time.sleep(0.01)
        for handle in self.handles.values():
            os.close(handle)
        self.handles.clear()


def supervise(root: Path, owner_fd: int, data: dict) -> int:
    import json

    try:
        descendants = Descendants()
    except Exception:
        # Setup is refused before any factory/native/browser child is started.
        os.close(owner_fd)
        (root / "cleanup.json").write_text(
            json.dumps(
                dict(
                    job_id=data["job_id"],
                    cleanup_ack=True,
                    ownership_mode="startup_refused",
                    owned_process_count=0,
                )
            )
        )
        raise
    body = None
    return_code = 1
    try:
        # Owner death before start acknowledgment still enters cleanup.
        while not (root / "start").exists():
            if select.select([owner_fd], [], [], 0.02)[0]:
                return_code = 1
                break
        else:
            body = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "nontainer.apps.verification",
                    "--body",
                    str(root),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
            while True:
                descendants.capture()
                descendants.reap()
                if body.pid in descendants.exit_codes:
                    break
                if select.select([owner_fd], [], [], 0.02)[0]:
                    break
            return_code = descendants.exit_codes.get(body.pid, 1)
    finally:
        os.close(owner_fd)
        descendants.cleanup(data["terminate_grace_seconds"])
        if body is not None:
            body.returncode = descendants.exit_codes.get(body.pid, return_code)
        (root / "cleanup.json").write_text(
            json.dumps(
                dict(
                    job_id=data["job_id"],
                    cleanup_ack=True,
                    ownership_mode=descendants.mode,
                    owned_process_count=len(descendants.known),
                )
            )
        )
    return return_code
