"""Write the corpus JSON and the JSON Schemas from the Python sources.

    python -m nontainer.conformance.export

One JSON file per scenario under ``harness/json/``, and under
``schema/`` the JSON Schemas of the scenario format and of
:data:`~nontainer.turns.TurnEvent`. Harnesses in other languages read
these. Under ``wsgit/json/``, one file per ``ws-git`` script, recorded
by running it here (see :mod:`nontainer.conformance.wsgit`), for ports
of the verb. Under ``traces/json/``, one file per trace case, its runs
in the neutral shape with every file they render to (see
:mod:`nontainer.conformance.traces`), for ports of the renderer.
``tests/test_corpus_drift.py`` fails when the committed
files differ from what this would write.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..turns import TurnEvent
from .codec import dumps, json_schema
from .corpus import Scenario
from .harness import SCENARIOS
from .traces import CASES
from .traces import dumps as dump_case
from .traces import record as record_case
from .wsgit import SCRIPTS, record
from .wsgit import dumps as dump_script

__all__ = ["JSON_DIR", "SCHEMA_DIR", "TRACES_DIR", "WSGIT_DIR", "files", "write"]

HERE = Path(__file__).resolve().parent
JSON_DIR = HERE / "harness" / "json"
SCHEMA_DIR = HERE / "schema"
WSGIT_DIR = HERE / "wsgit" / "json"
TRACES_DIR = HERE / "traces" / "json"


def _schema_text(schema: dict) -> str:
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


def files() -> dict[Path, str]:
    """Every file the export writes, by path, with its content."""
    out = {JSON_DIR / f"{s.name}.json": dumps(s) for s in SCENARIOS}
    out[SCHEMA_DIR / "scenario.schema.json"] = _schema_text(
        json_schema(Scenario, title="Scenario")
    )
    out[SCHEMA_DIR / "turn_event.schema.json"] = _schema_text(
        json_schema(TurnEvent, title="TurnEvent")
    )
    for script in SCRIPTS:
        out[WSGIT_DIR / f"{script.name}.json"] = dump_script(record(script))
    for case in CASES:
        out[TRACES_DIR / f"{case.name}.json"] = dump_case(record_case(case))
    return out


def write() -> list[Path]:
    """Write :func:`files`, and remove scenario JSON no scenario makes
    any more; the paths written."""
    wanted = files()
    for directory in (JSON_DIR, SCHEMA_DIR, WSGIT_DIR, TRACES_DIR):
        directory.mkdir(parents=True, exist_ok=True)
    for stale in [
        *JSON_DIR.glob("*.json"),
        *WSGIT_DIR.glob("*.json"),
        *TRACES_DIR.glob("*.json"),
    ]:
        if stale not in wanted:
            stale.unlink()
    for path, text in wanted.items():
        path.write_text(text)
    return sorted(wanted)


if __name__ == "__main__":
    for path in write():
        print(path.relative_to(HERE))
