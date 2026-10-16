"""``memory_limit_mb`` is the worker's: refused for in-process code.

sandtrap sets the limit on the process the code runs in. In-process
that is the host, so the limit would cap the host and every thread in
it rather than the sandboxed code.
"""

from __future__ import annotations

import pytest
from plain_provider import PlainProvider

from nontainer import NotSupportedError, PythonConfig, Workspace


def _open(tmp_path, **python):
    return Workspace(
        PlainProvider(tmp_path / "ws", session="mem"),
        python=PythonConfig(**python),
    )


def test_a_memory_limit_in_process_is_refused_at_construction(tmp_path):
    with pytest.raises(NotSupportedError, match="whole host process") as info:
        _open(tmp_path, memory_limit_mb=256)
    assert "memory_limit_mb=256" in str(info.value)
    assert "isolation='process'" in str(info.value)


def test_a_policy_with_a_memory_limit_in_process_is_refused(tmp_path):
    from sandtrap import Policy

    with pytest.raises(NotSupportedError, match="policy.memory_limit=256"):
        _open(tmp_path, policy=Policy(timeout=30.0, memory_limit=256))


def test_in_process_without_a_limit_opens(tmp_path):
    ws = _open(tmp_path)
    try:
        assert ws.run_python("n = 1\n").namespace == {"n": 1}
    finally:
        ws.close()


@pytest.mark.parametrize("isolation", ["process", "kernel"])
def test_a_worker_takes_a_memory_limit(tmp_path, isolation):
    from sandtrap import IsolationUnavailable

    ws = _open(tmp_path, isolation=isolation, memory_limit_mb=256)
    # The worker starts with the first execution, which is where an
    # isolation level the machine lacks is reported: warm it inside the
    # guard, so a missing rung skips rather than fails.
    try:
        ws.runtime.warm()
    except IsolationUnavailable as e:
        ws.close()
        pytest.skip(f"isolation={isolation!r} unavailable here: {e}")
    try:
        result = ws.run_python("n = 1\n")
        assert result.error is None, result.error
        assert result.namespace == {"n": 1}
    finally:
        ws.close()
