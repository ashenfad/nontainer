import pytest

from nontainer import (
    Answer,
    Job,
    SessionIdError,
    SessionRunner,
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
