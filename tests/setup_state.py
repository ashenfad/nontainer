"""State a worker keeps between executions, for the worker_setup tests, in
a module of its own: a worker process imports it by name."""

import os
import time
from pathlib import Path

prepared_in = None


def prepare():
    global prepared_in
    prepared_in = os.getpid()


def state():
    return os.getpid(), prepared_in


def hold(entered, release):
    """Signal ``entered``, then keep this worker busy until ``release``."""
    Path(entered).touch()
    deadline = time.monotonic() + 10
    while not Path(release).exists():
        if time.monotonic() > deadline:
            raise TimeoutError("barrier expired")
        time.sleep(0.01)
    return state()
