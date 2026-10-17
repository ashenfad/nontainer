"""The cases of the trace corpus: one per rendering rule worth pinning.
See the package docstring."""

from __future__ import annotations

from datetime import datetime, timezone

from ...traces import TraceEvent, TraceRun, TraceTokens
from . import Case


def _at(stamp: str, fraction: float = 0.0) -> float:
    """Epoch seconds of a UTC ``YYYY-MM-DDTHH:MM:SS``, plus a fraction."""
    t = datetime.fromisoformat(stamp).replace(tzinfo=timezone.utc)
    return t.timestamp() + fraction


_SYSTEM = (
    "You are an analyst. Work in /workspace.\n"
    "Read SKILL.md files before using a skill.\n"
)


def _user(text: str) -> TraceEvent:
    return TraceEvent("user", text)


def _assistant(text: str | None) -> TraceEvent:
    return TraceEvent("assistant", text)


def _tool(name: str, args: dict, result: str | None, *, error: bool = False):
    return TraceEvent("tool", result, tool=name, args=args, error=error)


_CHART = TraceRun(
    id="1f3a9c2e-7d41-4b8e-9a62-0c5e8f1d2b37",
    status="completed",
    started=_at("2026-10-15T12:00:00", 0.75),
    ended=_at("2026-10-15T12:00:12", 0.5),
    model="claude-sonnet-5-5",
    tokens=TraceTokens(
        input=1200, output=300, cache_read=5000, cache_write=0, reasoning=40
    ),
    system=_SYSTEM,
    events=(
        _user("Make a chart of sales by quarter.\nUse sales.csv.\n"),
        TraceEvent("thinking", "The data is in sales.csv; group it by quarter."),
        _tool(
            "run_python",
            {
                "code": (
                    "import pandas as pd\n"
                    "df = pd.read_csv('sales.csv')\n"
                    "ui = {'chart': df.groupby('quarter').sum()}\n"
                )
            },
            "ran; ui: chart",
        ),
        _assistant(None),
        _tool("terminal", {"command": "ls"}, "chart.png\nsales.csv\n"),
        _assistant("The chart is in `ui`, from sales.csv grouped by quarter."),
    ),
)

_FAILED = TraceRun(
    id="77b0e2d1-0a3c-4f2e-8d1b-5e6f7a8b9c0d",
    status="failed",
    started=_at("2026-10-15T12:05:00"),
    ended=_at("2026-10-15T13:07:09"),
    model="claude-sonnet-5-5",
    tokens=TraceTokens(input=900, output=120, cache_read=4800, cache_write=0),
    system=_SYSTEM,
    events=(
        _user("Now per region, please."),
        _tool(
            "run_python",
            {"code": "df.groupby('region')\n"},
            "Traceback (most recent call last):\n  ...\nKeyError: 'region'\n",
            error=True,
        ),
        _tool("terminal", {"command": "head -1 sales.csv"}, "date,quarter,amount\n"),
        _assistant("sales.csv has no region column, so I can't split it by region."),
    ),
)

_IN_FLIGHT = TraceRun(
    id="c0ffee00-1111-4222-8333-444455556666",
    status="running",
    started=_at("2026-10-15T14:00:00"),
    model="claude-sonnet-5-5",
    tokens=TraceTokens(input=300),
    events=(
        _user("Write the summary to notes.md"),
        _tool("write_file", {"path": "notes.md", "content": "Sales rose.\n"}, None),
    ),
)

_ODD = TraceRun(
    id="run/7 ünïcode",
    status="completed",
    started=_at("2026-10-15T15:00:00"),
    ended=_at("2026-10-15T15:00:03"),
    events=(
        _tool(
            "fetch.page",
            {
                "url": "https://example.com/report",
                "retries": 2.0,
                "timeout": 1.5,
                "headers": {"b": 1, "a": "ü"},
                "tags": ["x", None, True],
            },
            "Use ```python fences``` in docs, and ```` too.\n",
        ),
        _assistant("Done."),
    ),
)

_LONG_INPUT = TraceRun(
    id="d4e5f6a7-0000-4000-8000-000000000001",
    status="cancelled",
    started=_at("2026-10-15T16:00:00"),
    ended=_at("2026-10-15T16:01:05"),
    events=(
        _user(
            "\n  \n\tPlease 📊 summarize every quarterly report in reports/ into one"
            " table with a row per quarter and a column per region, then plot it.\n"
            "Second line."
        ),
    ),
)

_NUMBERS = TraceRun(
    id="e1e2e3e4-0000-4000-8000-000000000002",
    status="completed",
    events=(
        _tool(
            "measure",
            {
                "values": [1e-7, 1e-6, 0.1, 1 / 3, 2.0, -0.0, 1e20, 1e21, 1.5e300],
                "ints": [0, -7, 9007199254740991],
                "keys": {"b": 1, "a": 2, "\uff21": 3, "😀": 4, "10": 5, "9": 6},
                "text": 'a "quote", a tab\t, a \u2028 and \x01',
            },
            "ok",
        ),
    ),
)

CASES: tuple[Case, ...] = (
    Case(
        "an-empty-conversation",
        "A conversation with no runs yet: the README and an index that says so.",
        (),
    ),
    Case(
        "one-run",
        "A whole run: its facts, input, reply, tool calls, events and system "
        "prompt. Times are floored to the second; an assistant event with no "
        "text reads (empty), and the reply is the last one with text.",
        (_CHART,),
        harness="agno",
    ),
    Case(
        "runs-share-a-system-prompt",
        "Three runs over one system prompt, which is stored once. A failed "
        "run with an erroring call, a run longer than an hour, and a run "
        "still in flight: no end, no duration, a call with no result, and a "
        "system prompt the harness didn't keep.",
        (_CHART, _FAILED, _IN_FLIGHT),
    ),
    Case(
        "names-and-values-that-need-care",
        "A run id and a tool name with characters a path can't hold, "
        "arguments that are not strings (an integral float prints as an "
        "integer, keys sort), a result holding backtick fences, and a run "
        "woken by a note, with no input.",
        (_ODD,),
    ),
    Case(
        "a-long-input",
        "The index shows the first line with text, cut to 80 code points; "
        "an emoji is one code point. A cancelled run with no reply.",
        (_LONG_INPUT,),
        harness="agex",
    ),
    Case(
        "numbers-and-keys-as-javascript-writes-them",
        "A non-string argument is JSON as JSON.stringify writes it: shortest "
        "round-trip digits, an exponent below 1e-6 and from 1e21 (1e-7, "
        "1e+21), -0 as 0; keys sorted by UTF-16 code units, so an emoji key "
        "comes before U+FF21 and '10' before '9'.",
        (_NUMBERS,),
    ),
)
