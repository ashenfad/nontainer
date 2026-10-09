"""A host object's calls are the host's: off the run's clock on every
rung, and, through a stub, run as host code in-process as they are in
the host half elsewhere."""

import dataclasses
import sys

import pytest
from host_types import MARK, Probe, ProbeStub

from nontainer import HostObject, Profile, PythonConfig, Store

# Two waits each fit the timeout; together they don't.
WAIT = 0.6
TIMEOUT = 1.0


def open_ws(rung, host_objects, **kw):
    cfg = PythonConfig(host_objects=host_objects, **kw)
    if rung == "dud":
        if sys.version_info < (3, 11):
            pytest.skip("dud needs Python 3.11+")
        pytest.importorskip("dud")
        from nontainer import Workspace
        from nontainer.executor_dud import DudExecutor
        from nontainer.providers import KvgitProvider

        ws = Workspace(
            KvgitProvider.open(None, session="host-calls"),
            python=cfg,
            executor=DudExecutor(backend="subprocess"),
        )
        return ws, None
    store = Store(memory=True)
    return store.open(
        "h", profile=Profile(python=dataclasses.replace(cfg, isolation=rung))
    ), store


@pytest.fixture(params=["none", "process", "dud"])
def rung(request):
    return request.param


@pytest.fixture(params=["plain", "stubbed"])
def probe(request):
    if request.param == "plain":
        return Probe()
    return HostObject(Probe(), stub=ProbeStub)


@pytest.fixture
def probe_ws(rung, probe):
    ws, store = open_ws(rung, {"p": probe}, timeout=TIMEOUT)
    yield ws
    ws.close()
    if store is not None:
        store.close()


def test_a_slow_host_call_is_not_the_codes_time(probe_ws):
    r = probe_ws.run_python(
        f"a = p.wait({WAIT})\nb = p.wait({WAIT})\nfor i in range(100):\n    pass\nprint(a, b)"
    )
    assert r.error is None, r.error
    assert r.stdout.strip() == "waited waited"


def test_the_code_after_a_slow_host_call_is_still_timed(probe_ws):
    r = probe_ws.run_python(f"p.wait({WAIT})\nwhile True:\n    pass")
    assert r.error is not None
    assert "timeout" in str(r.error).lower() or "timed out" in str(r.error).lower()


@pytest.fixture
def stubbed_ws(rung):
    ws, store = open_ws(rung, {"p": HostObject(Probe(), stub=ProbeStub)})
    yield ws
    ws.close()
    if store is not None:
        store.close()


def test_a_stubs_host_half_reads_the_hosts_files(stubbed_ws, tmp_path):
    """In-process the call ran in the sandbox's context, so the host
    object's own open() was the sandbox's: the file wasn't there. Its
    network was refused the same way, and work it scheduled inherited
    both."""
    real = tmp_path / "real.txt"
    real.write_text("the host's file")
    r = stubbed_ws.run_python(f"print(p.read({str(real)!r}))")
    assert r.error is None, r.error
    assert r.stdout.strip() == "the host's file"


def test_a_stubs_host_half_runs_in_the_embedders_context(stubbed_ws):
    token = MARK.set("the embedder's")
    try:
        r = stubbed_ws.run_python("print(p.mark())")
    finally:
        MARK.reset(token)
    assert r.error is None, r.error
    assert r.stdout.strip() == "the embedder's"


def test_a_live_argument_keeps_the_call_in_the_sandbox(tmp_path):
    """A generator's body runs where it is iterated. Handed to the host
    half in-process, it would read the host's files there, so that call
    stays in the sandbox's context."""
    real = tmp_path / "real.txt"
    real.write_text("the host's file")
    ws, store = open_ws("none", {"p": HostObject(Probe(), stub=ProbeStub)})
    try:
        r = ws.run_python(
            "try:\n"
            f"    print(p.drain(open({str(real)!r}).read() for _ in [0]))\n"
            "except OSError as e:\n"
            "    print('refused', type(e).__name__)"
        )
    finally:
        ws.close()
        store.close()
    assert r.error is None, r.error
    assert r.stdout.strip() == "refused FileNotFoundError"


def test_a_stubs_own_work_is_the_codes_time(rung):
    """Only a stub's calls through ``remote`` are the host's: what its
    methods do themselves is the sandbox's, and the timeout bounds it."""
    ws, store = open_ws(
        rung, {"p": HostObject(Probe(), stub=ProbeStub)}, timeout=TIMEOUT
    )
    try:
        r = ws.run_python(f"p.dawdle({2 * WAIT})\nfor i in range(100):\n    pass")
    finally:
        ws.close()
        if store is not None:
            store.close()
    assert r.error is not None
    assert "timeout" in str(r.error).lower() or "timed out" in str(r.error).lower()
