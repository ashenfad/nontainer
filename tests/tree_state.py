"""Helpers a worker calls to start processes of its own, for the
``supervise_tree`` tests, in a module of its own: a worker process imports
it by name."""

import os
import subprocess
import sys


def spawn(seconds, ignore_term=False):
    """Start a helper that sleeps for ``seconds``; its pid, once it is
    ready. With ``ignore_term`` it outlasts SIGTERM."""
    lines = ["import os, signal, time"]
    if ignore_term:
        lines.append("signal.signal(signal.SIGTERM, signal.SIG_IGN)")
    lines += ["print(os.getpid(), flush=True)", f"time.sleep({seconds})"]
    proc = subprocess.Popen(
        [sys.executable, "-c", "\n".join(lines)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    pid = int(proc.stdout.readline())
    proc.stdout.close()
    return pid


def me():
    return os.getpid()


def block(path, seconds):
    """Create ``path``, then sleep ``seconds`` in one call, which a stop
    can't interrupt."""
    import time

    with open(path, "w"):
        pass
    time.sleep(seconds)
