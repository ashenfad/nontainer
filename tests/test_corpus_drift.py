"""The committed corpus JSON and JSON Schemas match the Python sources.

Harnesses in other languages read the files under
``nontainer/conformance/harness/json/`` and ``nontainer/conformance/
schema/``; the scenarios and the turn events are written in Python. This
fails when the two drift apart. Regenerate with
``python -m nontainer.conformance.export``, or run this test with
``NONTAINER_UPDATE_GOLDEN=1``.
"""

import os

from nontainer.conformance import export


def test_the_committed_corpus_matches_its_sources():
    if os.environ.get("NONTAINER_UPDATE_GOLDEN"):
        export.write()
    wanted = export.files()
    for path, text in wanted.items():
        assert path.exists(), f"{path.name} is missing: run the export"
        assert path.read_text() == text, f"{path.name} drifted: run the export"
    stale = [p.name for p in export.JSON_DIR.glob("*.json") if p not in wanted]
    assert not stale, f"no scenario makes {stale}: run the export"
