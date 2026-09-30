"""The workspace() factory."""

import pytest

from nontainer import SessionIdError, workspace


def test_the_removed_dir_backend_is_refused_by_name(tmp_path):
    """0.9.0 removed the 'dir' backend. Asking for it names what to use
    instead rather than failing somewhere deeper."""
    with pytest.raises(ValueError, match="'dir' backend was removed.*kvgit"):
        workspace("user-42", store=tmp_path, backend="dir")  # type: ignore[arg-type]


def test_session_validated(tmp_path):
    with pytest.raises(SessionIdError):
        workspace("../etc", store=tmp_path)


def test_unknown_backend_rejected(tmp_path):
    with pytest.raises(ValueError):
        workspace("s1", store=tmp_path, backend="docker")  # type: ignore[arg-type]


def test_provider_override(tmp_path):
    from plain_provider import PlainProvider

    p = PlainProvider(tmp_path / "custom", session="s1")
    with workspace("s1", provider=p) as ws:
        assert ws.session == "s1"
        assert ws.terminal("pwd")


def test_contract_breaking_executor_close_still_closes_provider(tmp_path):
    """Executor.close is best-effort-must-not-raise by contract, but
    executors are an extension surface: a third-party one that raises
    anyway must not skip the provider close (a held kvgit store). The
    violation surfaces as a RuntimeWarning, not silence."""
    from plain_provider import PlainProvider

    from nontainer import Workspace
    from nontainer.executor import LocalExecutor

    closed = []

    class RudeExecutor(LocalExecutor):
        def close(self):
            raise OSError("contract? what contract")

    class WitnessProvider(PlainProvider):
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
