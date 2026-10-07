"""The tool surface each adapter offers a model, pinned byte for byte.

What a model is told is the tools' names, descriptions and parameter
schemas, plus the agno toolkit's instructions. Those are the adapters'
real contract with the model, so a change to any of them should be a
decision someone makes, never a side effect of a refactor. This test
renders the surface for a spread of configurations and compares it with
``tests/golden/tool_surface.json``.

When a change to the surface IS intended, regenerate the file and read
its diff::

    NONTAINER_UPDATE_GOLDEN=1 uv run pytest tests/test_tool_surface.py
"""

import asyncio
import json
import os
from pathlib import Path

import pytest

pytest.importorskip("agno")
pytest.importorskip("mcp")

from nontainer import Store  # noqa: E402

GOLDEN = Path(__file__).parent / "golden" / "tool_surface.json"


class _Runner:
    def run(self, session, task, *, budget=None):  # pragma: no cover - never run
        return "unused"


def _workspace(*, cache=True, name="s"):
    return Store(memory=True).open(name, cache=cache)


def _apps(ws):
    from nontainer.apps import enable_apps

    return enable_apps(ws)


def _agno(ws, **kwargs) -> dict:
    from nontainer.adapters.agno import WorkspaceTools

    tk = WorkspaceTools(ws, **kwargs)
    tools = []
    for name, fn in tk.functions.items():
        fn.process_entrypoint()
        data = fn.to_dict()
        tools.append(
            {
                "name": name,
                "description": data.get("description"),
                "parameters": data.get("parameters"),
            }
        )
    if tk.sessions is not None:
        tk.sessions.close()
    return {"instructions": tk.instructions, "tools": tools}


def _mcp(ws, **kwargs) -> dict:
    from nontainer.adapters.mcp import build_server

    server = build_server(ws, **kwargs)
    listed = asyncio.run(server.list_tools())
    return {
        "tools": [
            {
                "name": t.name,
                "description": t.description,
                "inputSchema": t.inputSchema,
                "outputSchema": t.outputSchema,
            }
            for t in listed
        ]
    }


PRIMERS = {"terminal_primer": "TERMINAL PRIMER.", "python_primer": "PYTHON PRIMER."}


def capture() -> dict:
    """Render every configuration's surface."""
    out: dict = {}

    def each(key, build, *, cache=True, apps=False, sessions=False, **kwargs):
        ws = _workspace(cache=cache)
        try:
            if apps:
                kwargs["apps"] = _apps(ws)
            if sessions:
                kwargs["sessions"] = _Runner()
            out[key] = build(ws, **kwargs)
        finally:
            ws.close()

    each("agno/split", _agno)
    each("agno/terminal-only", _agno, cache=False)
    each("agno/turn-commits-no-vision", _agno, commit="turn", vision=False)
    each("agno/everything", _agno, apps=True, sessions=True, **PRIMERS)
    each("agno/terminal-only-everything", _agno, cache=False, apps=True, **PRIMERS)
    each("mcp/split", _mcp)
    each("mcp/terminal-only", _mcp, cache=False)
    each("mcp/everything", _mcp, apps=True, sessions=True, **PRIMERS)
    return out


@pytest.mark.filterwarnings("ignore:python_primer set")
def test_the_tool_surface_matches_the_golden_file():
    got = capture()
    if os.environ.get("NONTAINER_UPDATE_GOLDEN"):
        GOLDEN.parent.mkdir(exist_ok=True)
        GOLDEN.write_text(json.dumps(got, indent=2, sort_keys=True) + "\n")
    want = json.loads(GOLDEN.read_text())
    assert sorted(got) == sorted(want)
    for key in want:
        assert got[key] == want[key], f"tool surface changed: {key}"
