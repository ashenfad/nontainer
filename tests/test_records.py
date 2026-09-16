"""Frozen records hash whatever their metadata holds."""

from __future__ import annotations

import dataclasses

import nontainer
from nontainer import CommitInfo, TagInfo

MAPPING_WORDS = ("dict", "Mapping", "MutableMapping")


def _frozen_records() -> list[type]:
    records = []
    for name in nontainer.__all__:
        obj = getattr(nontainer, name)
        if (
            isinstance(obj, type)
            and dataclasses.is_dataclass(obj)
            and obj.__dataclass_params__.frozen
        ):
            records.append(obj)
    assert records, "the package exports frozen records"
    return records


def test_a_tag_with_info_hashes():
    tag = TagInfo(
        name="release",
        scope="store",
        id="abc123",
        tree=None,
        time=1.0,
        info={"owner": "me"},
        dangling=False,
    )
    assert hash(tag) == hash(dataclasses.replace(tag, info={"owner": "you"}))
    assert tag != dataclasses.replace(tag, info={"owner": "you"})


def test_a_commit_with_info_hashes():
    commit = CommitInfo(id="abc123", time=1.0, info={"tool": "publish"})
    assert {commit} == {dataclasses.replace(commit, info={"tool": "publish"})}


def test_every_mapping_field_on_a_frozen_record_is_left_out_of_the_hash():
    """A frozen record is hashable by construction, and a mapping field
    would break that for every instance carrying one; the field is
    still compared, so equality keeps its full meaning."""
    offending = [
        f"{record.__name__}.{f.name}"
        for record in _frozen_records()
        for f in dataclasses.fields(record)
        if any(word in str(f.type) for word in MAPPING_WORDS) and f.hash is not False
    ]
    assert offending == []
