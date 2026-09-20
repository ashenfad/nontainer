"""The ferry budget weighs the answer that is sent, not the bytes it came from.

dud's hostcall response frame is capped guest-side, and what fills it
is the JSON answer: stdout as a JSON string, captured files as base64.
Neither weighs what its source did, so a budget measured against the
source bytes could pass a payload that then breaks the transport --
where the over-budget answer, the one that explains what was withheld,
never gets its chance.
"""

import base64

from nontainer.wsverb import answer_size


def test_plain_ascii_weighs_its_length_plus_the_quotes():
    assert answer_size("abc", {}) == len('"abc"')


def test_a_replacement_character_weighs_more_than_the_byte_it_replaced():
    # One invalid byte became one U+FFFD on the way into the answer;
    # json.dumps escapes it to ASCII, six bytes for one.
    out = b"\xff".decode("utf-8", "replace")
    assert len(out) == 1
    assert answer_size(out, {}) > 1 + 2


def test_control_characters_weigh_their_escape():
    assert answer_size("\x00", {}) == len('"\\u0000"')


def test_a_captured_file_weighs_its_base64():
    data = bytes(range(256))
    encoded = {"/workspace/out.bin": base64.b64encode(data).decode("ascii")}
    assert answer_size("", encoded) == len('""') + len(encoded["/workspace/out.bin"])
    assert len(encoded["/workspace/out.bin"]) > len(data)


def test_a_binary_stdout_weighs_far_more_than_its_source():
    """The case the budget exists for: a blob that fit under the
    budget as bytes and would not have fit as the answer."""
    raw = bytes(range(128, 256)) * 1000  # 128 000 invalid bytes
    out = raw.decode("utf-8", "replace")
    assert answer_size(out, {}) > 3 * len(raw)
