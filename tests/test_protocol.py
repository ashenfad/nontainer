import pytest

from nontainer import (
    Answer,
    Capabilities,
    Job,
    SessionIdError,
    SessionRunner,
    Workspace,
    WorkspaceProvider,
    validate_session_id,
)


@pytest.mark.parametrize(
    "good", ["abc", "user-42", "a.b-c_d", "A1", "x" * 100, "1session", "-abc"]
)
def test_valid_session_ids(good):
    assert validate_session_id(good) == good


@pytest.mark.parametrize(
    "bad",
    ["", ".hidden", "../escape", "a/b", "a\\b", "sp ace", "semi;colon", "nul\x00"],
)
def test_invalid_session_ids(bad):
    with pytest.raises(SessionIdError):
        validate_session_id(bad)


# -- the delegation records ----------------------------------------------------


def test_an_answer_is_its_text():
    """``str`` is the reply, so printing one or putting it in a prompt
    yields prose."""
    answer = Answer(text="the rates are sampled monthly.")
    assert str(answer) == "the rates are sampled monthly."
    assert f"{answer}" == answer.text


def test_an_answer_repr_is_one_line_and_never_the_body():
    answer = Answer(
        text="a very long reply\n" * 200,
        ref="analyst.sleepy-tiger@a3f9c2e1d0",
        branch="analyst.sleepy-tiger",
        changed={"seed": ("/workspace/report.md",), "elsewhere": ("/workspace/m.py",)},
    )
    text = repr(answer)
    assert "\n" not in text
    assert "a very long reply" not in text
    assert "answered" in text
    assert "analyst.sleepy-tiger@a3f9c2e" in text  # shortened commit
    assert "2 path(s)" in text


def test_a_declined_answer_is_an_answer():
    """Declining and running out of budget RESOLVE — the caller reads
    a status, never catches an exception."""
    for status in ("declined", "capped", "failed"):
        assert repr(Answer(text="no", status=status)).startswith(f"<Answer {status}")


def test_the_records_are_pure_data():
    """Strings, numbers, tuples in a dict — nothing that holds a
    workspace, a thread or a store handle."""
    import dataclasses

    job = Job(
        name="analyst.brave-otter",
        task="polish the report",
        status="answered",
        ref="analyst.brave-otter@abc",
        started=1.0,
        finished=2.0,
        changed={"seed": ("/workspace/report.md",), "elsewhere": ()},
    )
    assert dataclasses.asdict(job) == dataclasses.asdict(Job(**dataclasses.asdict(job)))
    with pytest.raises(dataclasses.FrozenInstanceError):
        job.status = "running"


def test_a_runner_returning_a_string_satisfies_the_protocol():
    class Scripted:
        def run(self, session, task, *, budget=None):
            return "done"

    assert isinstance(Scripted(), SessionRunner)


# -- the substrate seam --------------------------------------------------------


class _MinimalProvider:
    """Every member ``WorkspaceProvider`` declares, and nothing else.

    A third-party substrate written against the contract alone: if this
    stops satisfying the protocol, the contract grew a member the
    document did not mention.
    """

    session = "s"
    caps = Capabilities()
    fs = None
    kv: dict = {}
    dirty = False
    head = "c0"
    frozen = False
    frozen_at = None

    def commit(self, info=None): ...
    def checkout(self, commit_id, *, info=None): ...
    def history(self, *, limit=None): ...
    def fork(self, name, *, at=None): ...
    def discard(self): ...
    def tag(self, name, *, at=None, info=None, scope="session"): ...
    def check_tag(self, name, *, scope="session"): ...
    def tags(self, *, scope="session"): ...
    def tag_info(self, name, *, scope="session"): ...
    def delete_tag(self, name, *, scope="session"): ...
    def at_tag(self, name, *, scope="session"): ...
    def diff(self, a, b): ...
    def merge(self, source, *, at=None, info=None): ...
    def apply(self, base, theirs, *, info=None): ...
    def commit_at(self, commit, *, session=None): ...
    def commit_keys(self, info=None, *, keys=()): ...
    def files_at(self, commit): ...
    def working_files(self): ...
    def key_at(self, commit, key): ...
    def branch_head(self, session): ...
    def expand_commit(self, commit, *, session=None): ...
    def mount(self): ...
    def close(self): ...


def test_a_minimal_provider_satisfies_the_protocol():
    assert isinstance(_MinimalProvider(), WorkspaceProvider)


@pytest.mark.parametrize(
    "member",
    ["branch_head", "key_at", "expand_commit", "frozen", "frozen_at", "commit_at"],
)
def test_the_protocol_names_every_member_core_calls(member):
    """Core calls these without a getattr guard, so a provider missing
    one is not a provider the framework can drive."""
    assert member in WorkspaceProvider.__protocol_attrs__
    body = {
        k: v
        for k, v in vars(_MinimalProvider).items()
        if k != member and not k.startswith("__")
    }
    partial = type("Partial", (), body)
    assert not isinstance(partial(), WorkspaceProvider)


def test_the_bundled_providers_satisfy_the_protocol(tmp_path):
    from nontainer.providers.dir import DirProvider
    from nontainer.providers.kvgit import KvgitProvider

    kv = KvgitProvider.open(None, session="s")
    try:
        assert isinstance(kv, WorkspaceProvider)
    finally:
        kv.close()
    d = DirProvider(tmp_path / "dir", session="s")
    try:
        assert isinstance(d, WorkspaceProvider)
    finally:
        d.close()


def test_an_unversioned_provider_is_honestly_not_frozen(tmp_path):
    """``frozen`` is a real attribute on every bundled provider, so a
    workspace reads it rather than defaulting it — and a substrate that
    has no tags to freeze at answers False rather than raising."""
    from nontainer.providers.dir import DirProvider

    provider = DirProvider(tmp_path / "dir", session="s")
    try:
        assert provider.frozen is False
        assert provider.frozen_at is None
        ws = Workspace(provider)
        try:
            assert ws.frozen is False
            ws.files.write("a.txt", "hello")
            assert ws.files.read("a.txt") == b"hello"
        finally:
            ws.close()
    finally:
        provider.close()


# -- the error hierarchy -------------------------------------------------------


def test_every_error_the_package_raises_is_a_workspace_error():
    """``except WorkspaceError`` is the one clause an embedder writes,
    so nothing nontainer raises may sit outside it."""
    import nontainer

    assert issubclass(nontainer.CacheError, nontainer.WorkspaceError)
    assert issubclass(nontainer.HarvestLost, nontainer.WorkspaceError)


def test_the_errors_keep_the_builtin_they_were_caught_as():
    """Code written against the old bases still catches them."""
    import nontainer

    assert issubclass(nontainer.CacheError, ValueError)
    assert issubclass(nontainer.HarvestLost, RuntimeError)


def test_the_executor_vocabulary_is_exported():
    """``Executor`` is a public name, so the types its methods speak
    are reachable without importing a module path."""
    import nontainer

    for name in ("ExecutionContext", "StagedDiff", "ViewSpec", "HarvestLost"):
        assert name in nontainer.__all__
        assert getattr(nontainer, name) is not None


def test_the_status_vocabularies_are_exported_and_annotated():
    """A caller branching on a status wants the set spelled out, and a
    delegate's answer can hold fewer of them than a job can: a job may
    be running, cancelled or expired, none of which an answer is."""
    import typing

    import nontainer

    assert set(typing.get_args(nontainer.AnswerStatus)) < set(
        typing.get_args(nontainer.JobStatus)
    )
    assert typing.get_type_hints(Job)["status"] is nontainer.JobStatus
    assert typing.get_type_hints(Answer)["status"] is nontainer.AnswerStatus


def test_inherit_is_spelled_as_the_two_words_it_takes():
    import typing

    from nontainer import Workspace as WS
    from nontainer.sessions import Sessions

    for fn in (WS.fork, Sessions.ask):
        assert typing.get_args(typing.get_type_hints(fn)["inherit"]) == (
            "full",
            "fresh",
        ), fn
