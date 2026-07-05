import pytest

from nontainer import SessionIdError, validate_session_id


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
