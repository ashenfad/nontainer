"""The workspace() factory."""

import pytest

from nontainer import SessionIdError, workspace


def test_dir_backend(tmp_path):
    with workspace("user-42", store=tmp_path, backend="dir") as ws:
        assert ws.session == "user-42"
        r = ws.terminal("echo hi > f.txt; cat f.txt")
        assert r.stdout.strip() == "hi"
    assert (tmp_path / "user-42" / "f.txt").exists()


def test_session_validated(tmp_path):
    with pytest.raises(SessionIdError):
        workspace("../etc", store=tmp_path, backend="dir")


def test_unknown_backend_rejected(tmp_path):
    with pytest.raises(ValueError):
        workspace("s1", store=tmp_path, backend="docker")  # type: ignore[arg-type]


def test_provider_override(tmp_path):
    from nontainer.providers import DirProvider

    p = DirProvider(tmp_path / "custom", session="s1")
    with workspace("s1", provider=p) as ws:
        assert ws.session == "s1"
        assert ws.terminal("pwd")


def test_contract_breaking_executor_close_still_closes_provider(tmp_path):
    """Executor.close is best-effort-must-not-raise by contract, but
    executors are an extension surface: a third-party one that raises
    anyway must not skip the provider close (a held kvgit store). The
    violation surfaces as a RuntimeWarning, not silence."""
    from nontainer import Workspace
    from nontainer.executor import LocalExecutor
    from nontainer.providers import DirProvider

    closed = []

    class RudeExecutor(LocalExecutor):
        def close(self):
            raise OSError("contract? what contract")

    class WitnessProvider(DirProvider):
        def close(self):
            closed.append(True)
            super().close()

    ws = Workspace(
        WitnessProvider(tmp_path / "rude", session="s1"),
        executor=RudeExecutor(),
    )
    with pytest.warns(RuntimeWarning, match="close\\(\\) raised"):
        ws.close()
    assert closed == [True]
