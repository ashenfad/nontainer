"""``ws-pytest``: pytest's shape over the workspace's own executor.

The wire tier (``ws-curl``) and the E2E tier (``test_app``) both start
from a URL, so the smallest thing either can ask about is a whole
request. This is the tier below: run one function and read the
assertion that failed.

Not pytest — pytest's **shape**. The discovery rule, the report, the
exit codes and a named flag subset; anything else is refused with the
idiom that replaces it. Test code runs through
``runtime.exec_python(view=...)``, which is where the app's handlers
run, so a test sees the session's python config, its workspace modules
and ``from host import db`` exactly as shipped code does.

Three things the sandbox cannot do shape the implementation, all
measured:

- ``globals()``, ``exec`` and ``compile`` are absent, so nothing
  inside can discover or construct what it did not receive: discovery
  and enumeration happen host-side by ``ast.parse``, and the program
  names every test it runs.
- ``traceback.extract_tb`` and ``BaseException.__traceback__`` are
  both refused, so the one structured seam into a failure is
  ``traceback.format_exc()``. The rendered text comes home and is
  parsed back into frames here, where the workspace files are
  readable and the report is written.
- The composed program is one compilation unit whose frames are
  interleaved with the sandbox's own gate frames. Frames are mapped
  back to the file and line the agent wrote, and frames belonging to
  neither the tests nor the workspace are dropped.

The record is the product; the terminal text is a rendering of it
(``render_report``). A caller that acts on a run reads
``TestReport.ok`` and the outcomes, not ``3 passed, 1 failed``.
"""

from __future__ import annotations

import ast
import re
import time
from dataclasses import dataclass
from typing import Any

from .apps.testing import CallError
from .wsverb import FerrySpec, abspath, tag

#: Where python tests live. ``app/`` is what publishes, so a test file
#: under it would ship in every deployment and be fetchable from the
#: served app; ``tests/`` sits outside the published subtree, which is
#: the whole reason to put it there.
TESTS_DIR = "tests"

#: Never collected from: the published subtree.
APP_DIR = "app"

_SKIP_DIRS = {"__pycache__", ".git", "node_modules"}


class UsageError(Exception):
    """A malformed or refused argv. The message is what the agent
    reads, so it names what to do instead."""


@dataclass(frozen=True)
class TestFrame:
    """One frame of a failure, in workspace coordinates. ``source`` is
    the enclosing function's lines as ``(number, text)`` pairs, ending
    at ``line`` — carried in the record so a report renders anywhere,
    including where the workspace that produced it is gone."""

    path: str
    line: int
    function: str = ""
    source: tuple[tuple[int, str], ...] = ()
    column: int = 0
    """The column within ``line``, 1-based, or 0 where the language's
    frames carry none. A Python traceback names a line; a JavaScript
    stack names a line and a column, and a report in vitest's shape
    prints both."""


@dataclass(frozen=True)
class TestOutcome:
    """One test's result. ``name`` is the pytest id
    (``tests/test_scores.py::test_limit``); a collection failure has no
    ``::`` half, because no test was reached."""

    name: str
    file: str
    line: int | None
    status: str
    """``passed`` | ``failed`` | ``error``. A test that raises is a
    failure, as in pytest; ``error`` is a file that could not be
    collected or a test that could not be called."""
    duration: float = 0.0
    message: str | None = None
    """The exception line, as pytest's ``E`` line carries it."""
    traceback: str | None = None
    """The failure as pytest's short traceback — what a card or a log
    line shows without rendering the whole report."""
    frames: tuple[TestFrame, ...] = ()
    """The same failure as data: every frame the agent wrote, with the
    sandbox's own frames dropped."""


@dataclass(frozen=True)
class TestReport:
    """A whole run, as data. ``render_report`` is the only thing that
    turns it into text: a UI or a script reads the counts and the
    outcomes instead of parsing a summary line."""

    tool: str
    ok: bool
    collected: int
    passed: int
    failed: int
    errors: int
    skipped: int
    duration: float
    exit_code: int
    outcomes: tuple[TestOutcome, ...] = ()
    stdout: str = ""
    collection_error: str | None = None
    notes: tuple[str, ...] = ()
    """Things the run refused to do silently — a test file under
    ``app/``, a ``conftest.py`` nobody will read."""

    def __bool__(self) -> bool:
        return self.ok

    def __str__(self) -> str:
        """The count line: ``2 failed, 8 passed in 0.31s``.

        The same text the terminal centres at the foot of a run, with
        no separator around it, so a caller that has to quote the
        outcome in one line — a log line, a message saying why a branch
        was not taken — has it without rendering the whole report and
        slicing the last line off.
        """
        return _summary_text(self)


@dataclass(frozen=True)
class Options:
    """A parsed ``ws-pytest`` argv."""

    select: tuple[tuple[str, str | None], ...] = ()
    """``(path, test name or None)`` pairs from the positional
    arguments."""
    keyword: str | None = None
    maxfail: int = 0
    """Failures allowed before the run stops; 0 is no limit."""
    verbosity: int = 0
    tb: str = "short"


# --------------------------------------------------------------------
# discovery and enumeration
# --------------------------------------------------------------------


def _rel(ws: Any, path: str) -> str:
    """A workspace-absolute path as the agent spells it."""
    root = "" if ws.root == "/" else ws.root.rstrip("/")
    if root and path.startswith(root + "/"):
        return path[len(root) + 1 :]
    return path.lstrip("/")


def _abs(ws: Any, rel: str) -> str:
    """A workspace-relative path (a region's, a frame's) as an absolute
    one. Not for an argument the agent typed: that is a path argument,
    and :func:`nontainer.wsverb.abspath` is how every ws-* verb reads
    one."""
    root = "" if ws.root == "/" else ws.root.rstrip("/")
    return f"{root}/{rel.lstrip('/')}"


def _selection(ws: Any, options: Options, cwd: str) -> list[tuple[str, str | None]]:
    """The positional selectors as ``(workspace-relative path, test
    name or None)``: absolute kept, relative resolved against the
    terminal's cwd, the way every ws-* verb reads a path argument."""
    return [(_rel(ws, abspath(cwd, path)), name) for path, name in options.select]


def _walk(fs: Any, base: str, skip: str) -> list[str]:
    """Every ``test_*.py`` under ``base``, sorted, with the published
    subtree and the caches left alone."""
    base = base.rstrip("/")
    found = set(fs.glob(f"{base}/test_*.py")) | set(fs.glob(f"{base}/**/test_*.py"))
    out = []
    for path in found:
        rest = path[len(base) + 1 :].split("/")[:-1]
        if any(part in _SKIP_DIRS or part.startswith(".") for part in rest):
            continue
        if skip and (path == skip or path.startswith(skip.rstrip("/") + "/")):
            continue
        out.append(path)
    return sorted(out)


def _discover(
    ws: Any, selection: list[tuple[str, str | None]]
) -> tuple[list[str], list[str]]:
    """``(test files, notes)`` for this run, workspace-relative.

    With no positional selector the whole tree is walked except
    ``app/``; with one, only what it names is collected. A selector
    that names nothing is a usage error, the way pytest's is.
    """
    fs = ws.files.fs
    root = "" if ws.root == "/" else ws.root.rstrip("/")
    notes: list[str] = []
    if selection:
        files: list[str] = []
        for path, _ in selection:
            target = _abs(ws, path)
            if fs.exists(target) and fs.isdir(target):
                files.extend(_walk(fs, target, f"{root}/{APP_DIR}"))
            elif fs.exists(target):
                files.append(target)
            else:
                raise UsageError(f"file or directory not found: {path}")
        seen = list(dict.fromkeys(files))
        return [_rel(ws, p) for p in seen], notes

    files = _walk(fs, root or "/", f"{root}/{APP_DIR}")
    stray = _walk(fs, f"{root}/{APP_DIR}", "")
    if stray:
        notes.append(
            f"{len(stray)} test file(s) under {APP_DIR}/ were not collected: "
            f"{APP_DIR}/ is what publishes, so a test there ships with the "
            f"app and is fetchable from it. Move them to {TESTS_DIR}/."
        )
    conftest = f"{root}/{TESTS_DIR}/conftest.py"
    if fs.exists(conftest):
        notes.append(
            f"{TESTS_DIR}/conftest.py is not read: there are no fixtures and "
            "no plugins here. Build what a test needs in the test itself."
        )
    return [_rel(ws, p) for p in files], notes


def _enumerate(source: str) -> tuple[list[tuple[str, int]], list[tuple[str, int, str]]]:
    """``(runnable, refused)`` from a test file's source.

    Runnable is ``(name, line)`` per top-level ``def test_*()`` taking
    no arguments. Refused is ``(name, line, why)``: a test taking
    arguments is asking for a fixture, and an ``async def`` test needs
    a plugin. Both are reported rather than skipped — a test that
    silently never runs is worse than one that says why.
    """
    tree = ast.parse(source)
    runnable: list[tuple[str, int]] = []
    refused: list[tuple[str, int, str]] = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("test_"):
            continue
        if isinstance(node, ast.AsyncFunctionDef):
            refused.append(
                (
                    node.name,
                    node.lineno,
                    "async test functions need a plugin to run them, and "
                    "there are no plugins here — make the test synchronous.",
                )
            )
            continue
        args = node.args
        if (
            args.args
            or args.posonlyargs
            or args.kwonlyargs
            or args.vararg
            or args.kwarg
        ):
            names = ", ".join(
                a.arg for a in [*args.posonlyargs, *args.args, *args.kwonlyargs]
            )
            refused.append(
                (
                    node.name,
                    node.lineno,
                    f"fixture(s) not found: {names or '*args'}. There are no "
                    "fixtures and no conftest here: build what the test needs "
                    "in the test body, and pass a handler's dependencies to "
                    "call(..., db=fake).",
                )
            )
            continue
        runnable.append((node.name, node.lineno))
    return runnable, refused


# --------------------------------------------------------------------
# -k expressions
# --------------------------------------------------------------------

_KEYWORD_TOKENS = re.compile(r"\(|\)|\s+")


def _match_keyword(expr: str, name: str) -> bool:
    """pytest's ``-k``: substrings joined by ``and`` / ``or`` / ``not``
    with parentheses. A bare word matches when it appears anywhere in
    the test id."""
    tokens = _KEYWORD_TOKENS.sub(lambda m: f" {m.group(0)} ", expr).split()
    pos = 0

    def peek() -> str | None:
        return tokens[pos] if pos < len(tokens) else None

    def take() -> str:
        nonlocal pos
        tok = tokens[pos]
        pos += 1
        return tok

    def unary() -> bool:
        tok = peek()
        if tok is None:
            raise UsageError(f"-k expression is incomplete: {expr!r}")
        if tok == "not":
            take()
            return not unary()
        if tok == "(":
            take()
            value = expression()
            if peek() != ")":
                raise UsageError(f"-k expression has an unclosed '(': {expr!r}")
            take()
            return value
        return take() in name

    def conjunction() -> bool:
        value = unary()
        while peek() == "and":
            take()
            value = unary() and value
        return value

    def expression() -> bool:
        value = conjunction()
        while peek() == "or":
            take()
            value = conjunction() or value
        return value

    result = expression()
    if pos != len(tokens):
        raise UsageError(f"-k expression is malformed: {expr!r}")
    return result


# --------------------------------------------------------------------
# the composed program
# --------------------------------------------------------------------

#: Runs after the test file's own source, so the file's line numbers
#: are the program's. Every name is ``nt__``-prefixed: a test's own
#: names are the file's, and a collision would rebind the runner.
_EPILOGUE = """\
import time as nt__time
import traceback as nt__tb
try:
    raise ValueError("locate")
except Exception:
    nt__where = nt__tb.format_exc()
nt__results = []
nt__budget = nt__maxfail
for nt__name, nt__fn in ({pairs}):
    nt__t0 = nt__time.perf_counter()
    try:
        nt__fn()
        nt__err = None
    except Exception:
        nt__err = nt__tb.format_exc()
    nt__results.append(
        {{
            "name": nt__name,
            "duration": nt__time.perf_counter() - nt__t0,
            "traceback": nt__err,
        }}
    )
    if nt__err is not None and nt__budget:
        nt__budget -= 1
        if not nt__budget:
            break
"""

#: The epilogue line whose reported number tells the runner how far the
#: rung shifted the program (a guest rung prepends a prelude of its
#: own). One-based, counted in the epilogue's own text.
_LOCATOR_LINE = 4


@dataclass(frozen=True)
class _Region:
    """A run of composed lines that came from one workspace file.
    ``shift`` is what to add to a composed line to get the file's."""

    start: int
    end: int
    path: str
    shift: int = 0


@dataclass(frozen=True)
class _Program:
    source: str
    regions: tuple[_Region, ...] = ()
    locator: int = 0
    """Composed line of the epilogue's locator raise."""


def _compose(ws: Any, rel: str, source: str, names: list[str]) -> _Program:
    """The program one test file runs as: the handlers it calls, the
    contract seeded into the handlers it imports, then the file itself,
    then the calls.

    Every line of workspace source keeps its own line count, and each
    run of them is recorded as a region — so a frame anywhere in the
    program names the file and line the agent wrote.
    """
    from .apps.dispatch import app_root
    from .apps.testing import (
        called_modules,
        compose,
        compose_modules,
        substitute_call,
        uses_call,
    )

    if not source.endswith("\n"):
        source += "\n"
    root = app_root(ws)
    if uses_call(source):
        preamble, spans = compose(ws, called_modules(source), root)
    else:
        preamble, spans = "", []
    # The modules the file imports, after the handlers it calls: their
    # regions are measured in their own preamble, so they shift by
    # whatever the first one came to.
    modules, module_spans, source = compose_modules(ws, source, root)
    shift = preamble.count("\n")
    spans.extend((path, start + shift, length) for path, start, length in module_spans)
    preamble += modules
    # `from host import call` is how a test asks for the helper, and
    # the preamble is what defines it — so the import is rewritten to
    # that name rather than run. After the reading above, which
    # answers for the file the agent wrote.
    source = substitute_call(source)
    # A `from __future__` import must be the first statement of the
    # module, and a docstring may precede it. Whatever the file puts
    # there stays first; the composed handlers go in under it, so the
    # program parses whenever the test file does.
    lines = source.splitlines(keepends=True)
    head = _future_header(source)
    body_lines = len(lines)
    shift = preamble.count("\n")
    regions = []
    if head:
        regions.append(_Region(1, head, rel, 0))
    regions.extend(
        _Region(start + head, start + head + length - 1, path, 1 - start - head)
        for path, start, length in spans
    )
    regions.append(_Region(head + shift + 1, shift + body_lines, rel, -shift))
    pairs = ", ".join(f'("{n}", {n})' for n in names)
    if pairs:
        pairs += ","
    epilogue = _EPILOGUE.format(pairs=pairs)
    return _Program(
        source="".join(lines[:head]) + preamble + "".join(lines[head:]) + epilogue,
        regions=tuple(regions),
        locator=shift + body_lines + _LOCATOR_LINE,
    )


def _future_header(source: str) -> int:
    """How many leading lines of a test file must stay leading: down to
    the last ``from __future__`` import, which Python requires to be the
    first statement (a module docstring, which may precede it, is
    carried along). Zero where the file has none."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return 0
    end = 0
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            end = max(end, getattr(node, "end_lineno", None) or node.lineno)
    return end


# --------------------------------------------------------------------
# traceback rewriting
# --------------------------------------------------------------------

#: A frame line in a rendered traceback.
_FRAME_RE = re.compile(
    r'^  File "(?P<file>.*)", line (?P<line>\d+)(?:, in (?P<fn>.*))?$'
)

#: The compilation unit the composed program becomes: a sandtrap exec
#: unit on the local rung, the guest's own exec filename on a VM rung.
_UNIT_RE = re.compile(r"^<sandtrap:\d+>$|^<session>$")

#: A workspace module imported through the sandbox's VFS loader. The
#: dotted name is the path it was imported from.
_VFS_RE = re.compile(r"^<sandtrap:vfs:(?P<mod>[\w.]+)>$")

_TRACEBACK_HEAD = "Traceback (most recent call last):"


def _offset(locator_text: str | None, locator_line: int) -> int:
    """How far the rung shifted the submitted program, measured rather
    than assumed: the program raises at a line it knows and reads back
    the line the traceback names. A guest rung prepends a prelude; the
    local rung prepends nothing."""
    if not locator_text or not locator_line:
        return 0
    last = None
    for line in locator_text.splitlines():
        m = _FRAME_RE.match(line)
        if m and _UNIT_RE.match(m.group("file")):
            last = int(m.group("line"))
    return (last - locator_line) if last is not None else 0


def _parse_frames(text: str) -> tuple[list[tuple[str, int, str]], str]:
    """``(frames, exception line)`` from a rendered traceback.

    Frames reset at each ``Traceback (most recent call last):``, so a
    chained exception reports the innermost raise — the one the test
    actually hit — rather than both halves interleaved.
    """
    frames: list[tuple[str, int, str]] = []
    exc = ""
    for line in text.splitlines():
        if line == _TRACEBACK_HEAD:
            frames = []
            exc = ""
            continue
        m = _FRAME_RE.match(line)
        if m:
            frames.append((m.group("file"), int(m.group("line")), m.group("fn") or ""))
            continue
        if line and not line[0].isspace():
            exc = line
    return frames, exc


class _Sources:
    """Workspace files, read once each, for the lines a report shows."""

    def __init__(self, ws: Any):
        self._ws = ws
        self._lines: dict[str, list[str]] = {}
        self._trees: dict[str, Any] = {}

    def lines(self, rel: str) -> list[str]:
        if rel not in self._lines:
            try:
                text = self._ws.files.fs.read(_abs(self._ws, rel)).decode("utf-8")
            except Exception:  # noqa: BLE001 — a missing file shows no source
                text = ""
            self._lines[rel] = text.splitlines()
        return self._lines[rel]

    def line(self, rel: str, no: int) -> str:
        lines = self.lines(rel)
        return lines[no - 1].strip() if 0 < no <= len(lines) else ""

    def block(self, rel: str, no: int) -> list[tuple[int, str]]:
        """``(line number, text)`` for the enclosing function, from its
        ``def`` down to ``no`` — what pytest's long traceback shows
        around a failure. A line outside any function stands alone."""
        lines = self.lines(rel)
        if not (0 < no <= len(lines)):
            return []
        if rel not in self._trees:
            try:
                self._trees[rel] = ast.parse("\n".join(lines))
            except SyntaxError:
                self._trees[rel] = None
        tree = self._trees[rel]
        start = no
        if tree is not None:
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                end = getattr(node, "end_lineno", None) or node.lineno
                if node.lineno <= no <= end:
                    start = min(start, node.lineno)
        return [(n, lines[n - 1]) for n in range(start, no + 1)]


def _frame(sources: "_Sources", path: str, line: int, fn: str) -> TestFrame:
    """One frame with the source the report will show."""
    return TestFrame(path, line, fn, tuple(sources.block(path, line)))


def _rewrite(
    text: str,
    program: _Program,
    offset: int,
    ws: Any,
    sources: "_Sources",
) -> tuple[list[TestFrame], str]:
    """A rendered sandbox traceback as workspace frames plus its
    exception line.

    A frame is kept when it names a file the agent wrote: a line of the
    composed program that came from a test file (or a handler composed
    into it), or a workspace module the test imported. Everything else
    — the sandbox's gate wrappers, the runner's own calls — is
    plumbing, and a report that shows it teaches the agent to read past
    it.
    """
    raw, exc = _parse_frames(text)
    to_host = getattr(getattr(ws, "runtime", None), "guest_to_host", None)
    frames: list[TestFrame] = []
    for path, line, fn in raw:
        if _UNIT_RE.match(path):
            composed = line - offset
            for region in program.regions:
                if region.start <= composed <= region.end:
                    frames.append(
                        _frame(sources, region.path, composed + region.shift, fn)
                    )
                    break
            continue
        vfs = _VFS_RE.match(path)
        if vfs:
            mod = vfs.group("mod").replace(".", "/") + ".py"
            frames.append(_frame(sources, mod, line, fn))
            continue
        host = (to_host(path) if to_host else None) or path
        rel = _rel(ws, host)
        if rel != host.lstrip("/") or host.startswith(
            ("" if ws.root == "/" else ws.root.rstrip("/")) + "/"
        ):
            frames.append(_frame(sources, rel, line, fn))
    return frames, exc


# --------------------------------------------------------------------
# the run
# --------------------------------------------------------------------


#: Flags pytest has that this verb refuses, each with the idiom that
#: replaces it. A refusal is a one-line answer naming what to do
#: instead; silence would cost the agent a run it thinks it filtered.
REFUSED = {
    "-s": "there is no capture to disable — a test's stdout is already in the report",
    "--capture": "there is no capture to disable — a test's stdout is in the report",
    "--lf": "nothing here outlives the call to remember a last run — name the "
    "test instead: ws-pytest tests/test_x.py::test_y",
    "--last-failed": "nothing here outlives the call to remember a last run — "
    "name the test instead: ws-pytest tests/test_x.py::test_y",
    "-p": "there are no plugins here",
    "--co": "use -q with a path to see what would run",
    "--collect-only": "use -q with a path to see what would run",
    "-m": "there are no markers here — select with -k EXPR, or name the test",
    "--markers": "there are no markers here — select with -k EXPR, or name the test",
    "--fixtures": "there are no fixtures and no conftest here — build what a "
    "test needs in the test itself, and pass a handler's dependencies to "
    "call(..., db=fake)",
    "--pdb": "there is no interactive debugger in a sandbox — print, or assert "
    "on the value",
    "--cov": "there is no coverage plugin here",
}

_TB_STYLES = ("short", "long", "no")

#: pytest's exit code for a call that was wrong before anything ran: a
#: flag it does not have, a path that is not there, a name no file
#: defines.
USAGE_ERROR = 4

USAGE = (
    "usage: ws-pytest [paths] [-k EXPR] [-x | --maxfail=N] [-q | -v] "
    "[--tb=short|long|no]"
)


def parse_argv(argv: Any = ()) -> Options:
    """A ``ws-pytest`` argv as options.

    Positional arguments select (``tests/test_x.py``, or
    ``tests/test_x.py::test_one``); the flags are the named subset;
    anything else is refused by name with the idiom that replaces it.
    """
    select: list[tuple[str, str | None]] = []
    keyword: str | None = None
    maxfail = 0
    verbosity = 0
    tb = "short"
    words = [str(w) for w in argv]
    i = 0

    def value(flag: str) -> str:
        nonlocal i
        i += 1
        if i >= len(words):
            raise UsageError(f"{flag} needs a value")
        return words[i]

    while i < len(words):
        word = words[i]
        head = word.split("=", 1)[0]
        if head in REFUSED:
            raise UsageError(f"{head} is not supported here: {REFUSED[head]}")
        if not word.startswith("-"):
            path, sep, name = word.partition("::")
            select.append((path, name if sep else None))
        elif head in ("-k",):
            keyword = word.split("=", 1)[1] if "=" in word else value("-k")
        elif word in ("-x", "--exitfirst"):
            maxfail = 1
        elif head == "--maxfail":
            raw = word.split("=", 1)[1] if "=" in word else value("--maxfail")
            if not raw.isdigit():
                raise UsageError(f"--maxfail takes a number, not {raw!r}")
            maxfail = int(raw)
        elif word in ("-v", "--verbose", "-vv"):
            verbosity = 1
        elif word in ("-q", "--quiet"):
            verbosity = -1
        elif head == "--tb":
            tb = word.split("=", 1)[1] if "=" in word else value("--tb")
            if tb not in _TB_STYLES:
                raise UsageError(f"--tb takes {' | '.join(_TB_STYLES)}, not {tb!r}")
        else:
            raise UsageError(f"unknown flag {word}\n{USAGE}")
        i += 1
    return Options(
        select=tuple(select),
        keyword=keyword,
        maxfail=maxfail,
        verbosity=verbosity,
        tb=tb,
    )


def run_pytest(ws: Any, argv: Any = (), *, cwd: str | None = None) -> TestReport:
    """Run the workspace's tests and report what happened.

    The report is data: ``report.ok`` is what a caller branches on,
    ``report.outcomes`` is what a UI shows, and ``render_report`` is
    what the terminal prints.
    """
    try:
        options = parse_argv(argv)
    except UsageError as e:
        return _usage_report(str(e))
    return run_options(ws, options, cwd)


def run_options(ws: Any, options: Options, cwd: str | None = None) -> TestReport:
    """``run_pytest`` with an argv already parsed. ``cwd`` is what a
    relative selector resolves against — the terminal's, when the verb
    is what called."""
    started = time.perf_counter()
    if cwd is None:
        cwd = ws.files.fs.getcwd()
    selection = _selection(ws, options, cwd)
    try:
        files, notes = _discover(ws, selection)
    except UsageError as e:
        return _usage_report(str(e))

    wanted = {p for p, n in selection if n}
    names_by_file = {p: {n for q, n in selection if q == p and n} for p in wanted}

    sources = _Sources(ws)
    # Before anything runs: a name the selector pins and the file does
    # not define. Left alone it would collect nothing and exit 5, where
    # an empty suite and a typo have to read differently.
    for rel, pinned in names_by_file.items():
        try:
            runnable, refused = _enumerate("\n".join(sources.lines(rel)) + "\n")
        except SyntaxError:
            continue  # the run reports it as the collection error it is
        known = {n for n, _ in runnable} | {n for n, _, _ in refused}
        missing = sorted(pinned - known)
        if missing:
            defines = ", ".join(sorted(known)) or "no tests"
            return _usage_report(
                "not found: "
                + ", ".join(f"{rel}::{name}" for name in missing)
                + f"\n({rel} defines {defines})"
            )

    outcomes: list[TestOutcome] = []
    collection_errors: list[str] = []
    stdout: list[str] = []
    collected = 0
    failures = 0
    stopped = False

    for rel in files:
        if stopped:
            break
        source = "\n".join(sources.lines(rel)) + "\n"
        try:
            runnable, refused = _enumerate(source)
        except SyntaxError as e:
            where = f"{rel}:{e.lineno}" if e.lineno else rel
            message = f"{type(e).__name__}: {e.msg} ({where})"
            collection_errors.append(f"{rel}\n{message}")
            outcomes.append(
                TestOutcome(
                    name=rel,
                    file=rel,
                    line=e.lineno,
                    status="error",
                    message=message,
                    traceback=message,
                )
            )
            continue

        selected = [
            (n, ln) for n, ln in runnable if _wanted(n, rel, names_by_file, options)
        ]
        for name, line, why in refused:
            if not _wanted(name, rel, names_by_file, options):
                continue
            collected += 1
            outcomes.append(
                TestOutcome(
                    name=f"{rel}::{name}",
                    file=rel,
                    line=line,
                    status="error",
                    message=why,
                    traceback=why,
                )
            )
        # A file with nothing selected still runs: importing it IS
        # collecting it, and a file that raises at module level is a
        # collection error whether or not anything in it was wanted.
        collected += len(selected)

        budget = 0 if not options.maxfail else max(options.maxfail - failures, 1)
        try:
            program = _compose(ws, rel, source, [n for n, _ in selected])
        except CallError as e:
            collection_errors.append(f"{rel}\n{e}")
            collected -= len(selected)
            outcomes.append(
                TestOutcome(
                    name=rel,
                    file=rel,
                    line=None,
                    status="error",
                    message=str(e),
                    traceback=str(e),
                )
            )
            continue
        result = ws.runtime.exec_python(
            program.source,
            inputs={"nt__maxfail": budget},
            view=_view(ws),
            echo="none",
        )
        if result.stdout:
            stdout.append(result.stdout)
        if result.error is not None:
            # Nothing in the file ran: the failure is at module level
            # (an import, a name, a call beside the tests), which is a
            # collection error in pytest's vocabulary. The rung has
            # already put this text in submitted-code coordinates.
            frames, exc = _rewrite(result.error, program, 0, ws, sources)
            collection_errors.append(f"{rel}\n{exc}")
            collected -= len(selected)
            outcomes.append(
                TestOutcome(
                    name=rel,
                    file=rel,
                    line=frames[-1].line if frames else None,
                    status="error",
                    message=exc,
                    traceback=_render_frames(frames, exc, "short"),
                    frames=tuple(frames),
                )
            )
            continue

        offset = _offset(result.namespace.get("nt__where"), program.locator)
        lines = dict(selected)
        for record in result.namespace.get("nt__results") or ():
            name = record["name"]
            raw = record.get("traceback")
            if raw is None:
                outcomes.append(
                    TestOutcome(
                        name=f"{rel}::{name}",
                        file=rel,
                        line=lines.get(name),
                        status="passed",
                        duration=float(record.get("duration") or 0.0),
                    )
                )
                continue
            failures += 1
            frames, exc = _rewrite(raw, program, offset, ws, sources)
            outcomes.append(
                TestOutcome(
                    name=f"{rel}::{name}",
                    file=rel,
                    line=frames[-1].line if frames else lines.get(name),
                    status="failed",
                    duration=float(record.get("duration") or 0.0),
                    message=exc,
                    traceback=_render_frames(frames, exc, "short"),
                    frames=tuple(frames),
                )
            )
            if options.maxfail and failures >= options.maxfail:
                stopped = True

    passed = sum(1 for o in outcomes if o.status == "passed")
    failed = sum(1 for o in outcomes if o.status == "failed")
    errors = sum(1 for o in outcomes if o.status == "error")
    duration = time.perf_counter() - started
    if collection_errors:
        code = 2
    elif failed or errors:
        code = 1
    elif not collected:
        code = 5
    else:
        code = 0
    return TestReport(
        tool="pytest",
        ok=code == 0,
        collected=collected,
        passed=passed,
        failed=failed,
        errors=errors,
        skipped=0,
        duration=duration,
        exit_code=code,
        outcomes=tuple(outcomes),
        stdout="".join(stdout),
        collection_error="\n\n".join(collection_errors) or None,
        notes=tuple(notes),
    )


def _wanted(
    name: str,
    rel: str,
    names_by_file: dict[str, set[str]],
    options: Options,
) -> bool:
    """Whether one test survives the selectors: a ``path::name``
    positional pins the name, and ``-k`` filters what is left."""
    pinned = names_by_file.get(rel)
    if pinned and name not in pinned:
        return False
    if options.keyword and not _match_keyword(options.keyword, name):
        return False
    return True


def _view(ws: Any) -> Any:
    """The execution view test code runs under: the contract classes a
    handler sees, so a test can name ``Request`` / ``Response`` /
    ``HttpError``, plus the host half of ``call``."""
    from .apps.contract import HANDLER_CONTRACT
    from .apps.testing import CONTRACT
    from .protocol import ViewSpec

    return ViewSpec(extra_classes=(*HANDLER_CONTRACT, *CONTRACT))


def _usage_report(message: str) -> TestReport:
    """A call that was wrong before anything ran: pytest's exit code 4,
    which is what `pytest tests/test_x.py::test_typo` and an
    unrecognized flag both answer with. 2 is a different thing — a run
    interrupted by a file that would not collect."""
    return TestReport(
        tool="pytest",
        ok=False,
        collected=0,
        passed=0,
        failed=0,
        errors=0,
        skipped=0,
        duration=0.0,
        exit_code=USAGE_ERROR,
        collection_error=message,
    )


# --------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------

_WIDTH = 80


def _sep(title: str = "", char: str = "=") -> str:
    if not title:
        return char * _WIDTH
    text = f" {title} "
    fill = _WIDTH - len(text)
    if fill < 2:
        return text.strip()
    return char * (fill // 2) + text + char * (fill - fill // 2)


def _progress(line: str, done: int, total: int) -> str:
    pct = 100 if not total else int(done * 100 / total)
    mark = f"[{pct:3d}%]"
    pad = _WIDTH - len(mark) - len(line)
    return line + (" " * pad if pad > 0 else " ") + mark


def _render_frames(frames: list[TestFrame], exc: str, tb: str) -> str | None:
    """One failure's frames in pytest's traceback styles. The frames
    carry their own source, so this needs no workspace."""
    if tb == "no":
        return None
    out: list[str] = []
    if tb == "long":
        for i, frame in enumerate(frames):
            if i:
                out.append(("_ " * (_WIDTH // 2)).rstrip())
            out.append("")
            for no, text in frame.source or ((frame.line, ""),):
                prefix = ">" if no == frame.line else " "
                out.append(f"{prefix}   {text}".rstrip())
            if i == len(frames) - 1:
                for part in exc.splitlines() or [""]:
                    out.append(f"E       {part}")
                out.append("")
                out.append(f"{frame.path}:{frame.line}: {exc.split(':')[0]}")
            else:
                out.append("")
                out.append(f"{frame.path}:{frame.line}:")
        if not frames:
            out.append(f"E       {exc}")
        return "\n".join(out)
    for frame in frames:
        where = f"{frame.path}:{frame.line}"
        out.append(f"{where}: in {frame.function}" if frame.function else f"{where}:")
        text = frame.source[-1][1].strip() if frame.source else ""
        if text:
            out.append(f"    {text}")
    for part in exc.splitlines() or [""]:
        out.append(f"E   {part}")
    return "\n".join(out)


def _traceback_text(outcome: TestOutcome, tb: str) -> str:
    """An outcome's traceback in the asked-for style: re-rendered from
    its frames, or the message alone where there are none (a refusal
    names a rule rather than a stack)."""
    if not outcome.frames:
        return outcome.traceback or outcome.message or ""
    return _render_frames(list(outcome.frames), outcome.message or "", tb) or ""


def _summary_text(report: TestReport) -> str:
    """The counts and the duration on one line, unadorned."""
    parts = []
    if report.failed:
        parts.append(f"{report.failed} failed")
    if report.passed:
        parts.append(f"{report.passed} passed")
    if report.skipped:
        parts.append(f"{report.skipped} skipped")
    if report.errors:
        parts.append(f"{report.errors} error" + ("s" if report.errors > 1 else ""))
    if not parts:
        return f"no tests ran in {report.duration:.2f}s"
    return ", ".join(parts) + f" in {report.duration:.2f}s"


def _summary(report: TestReport) -> str:
    """The count line as the terminal prints it: centred in a rule."""
    return _sep(_summary_text(report))


def render_report(report: TestReport, *, verbosity: int = 0, tb: str = "short") -> str:
    """The terminal text for a report: pytest's progress line, its
    FAILURES section and its summary line.

    ``verbosity`` is -1 (``-q``), 0 or 1 (``-v``); ``tb`` is ``short``,
    ``long`` or ``no``.
    """
    if report.exit_code == USAGE_ERROR and report.collection_error:
        return f"ERROR: {report.collection_error}\n"

    out: list[str] = []
    if verbosity >= 0:
        out.append(_sep("test session starts"))
        out.append(
            f"collected {report.collected} item"
            + ("s" if report.collected != 1 else "")
        )
        out.append("")

    total = len(report.outcomes)
    marks = {"passed": ".", "failed": "F", "error": "E"}
    if verbosity >= 1:
        for i, o in enumerate(report.outcomes, 1):
            word = {"passed": "PASSED", "failed": "FAILED", "error": "ERROR"}[o.status]
            out.append(_progress(f"{o.name} {word}", i, total))
    else:
        by_file: dict[str, list[str]] = {}
        for o in report.outcomes:
            by_file.setdefault(o.file, []).append(marks[o.status])
        done = 0
        for path, chars in by_file.items():
            done += len(chars)
            head = "" if verbosity < 0 else f"{path} "
            out.append(_progress(head + "".join(chars), done, total))
    if total:
        out.append("")

    errors = [o for o in report.outcomes if o.status == "error"]
    if errors and tb != "no":
        out.append(_sep("ERRORS"))
        for o in errors:
            title = (
                f"ERROR collecting {o.file}"
                if "::" not in o.name
                else f"ERROR at setup of {o.name.split('::')[-1]}"
            )
            out.append(_sep(title, "_"))
            out.append(_traceback_text(o, tb))
        out.append("")

    failures = [o for o in report.outcomes if o.status == "failed"]
    if failures and tb != "no":
        out.append(_sep("FAILURES"))
        for o in failures:
            out.append(_sep(o.name.split("::")[-1], "_"))
            out.append(_traceback_text(o, tb))
        out.append("")

    if report.stdout:
        out.append(_sep("captured stdout", "-"))
        out.append(report.stdout.rstrip("\n"))
        out.append("")

    for note in report.notes:
        out.append(f"note: {note}")
    if report.notes:
        out.append("")

    if report.collection_error and report.outcomes:
        out.append(
            _sep(
                f"Interrupted: {len(errors)} error"
                + ("s" if len(errors) > 1 else "")
                + " during collection",
                "!",
            )
        )
    out.append(_summary(report))
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------
# the terminal verb
# --------------------------------------------------------------------

_HELP = """\
ws-pytest — run this workspace's unit tests

usage: ws-pytest [paths] [-k EXPR] [-x | --maxfail=N] [-q | -v]
                 [--tb=short|long|no]

Collects tests/test_*.py and runs every top-level test_* function
through the same executor your python code runs in, so a test imports
your workspace modules exactly as your app does.

  <path>            run one file, or every test file under a directory
  <path>::<name>    run one test
  -k EXPR           run tests whose name matches: a substring, joined
                    with and / or / not and parentheses
  -x                stop after the first failure
  --maxfail=N       stop after the Nth failure
  -q                counts only
  -v                one line per test
  --tb=short|long|no
                    how much of a failure to show (short by default)

Exit codes are pytest's: 0 all passed, 1 failures, 2 a file that would
not collect, 4 an argument that makes no sense (a flag, a path, or a
test name nothing defines), 5 nothing collected.

Tests live in tests/, never under app/: app/ is what publishes, so a
test there ships with the app and is fetchable from it.

A test is plain Python — `assert`, and `from unittest.mock import
MagicMock` where it needs a fake — and one test function per behaviour,
so a failure names what broke. Give a shared function its dependencies
as arguments (`def load(db, limit)`) and a test can call it with one. There are no fixtures, no conftest, no plugins and no
markers here: setup is the test's own code, written in the test.

In a test, beyond plain Python:

  from host import call
      call(module, method="GET", path=None, *, params=None,
           body=None, json=None, headers=None, **objects)
      Runs app/api/<module>.py the way a request does (module is a
      string literal) and returns the response: .status, .json,
      .text, .headers, .ok — a raised HttpError arrives as a status,
      not as an exception. Called with no objects it runs against
      what the session binds, the real `db` included: seed, call,
      assert on the response. A keyword substitutes one of the
      handler's dependencies instead, for a test that must not write
      into the session's db or depend on its state — under either
      spelling the handler reads it by, `from host import db` (what a
      handler should write) or the bare `db` dispatch also binds. A
      keyword the handler never reads is an error, because a fake
      nothing reads proves nothing.

  Request, Response, HttpError
      Bare names, no import: in the test, and in every handler module
      it imports, as a request binds them — so `import app.api.summary`
      and then `summary.get(req)` raises HttpError rather than
      NameError. Such an import reads the session's real `db`; `call`
      is the one that substitutes.

  # app/api/summary.py
  from host import db

  def get(req):
      key = req.params.get("key")
      if not key:
          raise HttpError(400, "key is required")
      return {"row": db.get(key)}

  # tests/test_summary.py
  from host import call, db

  def test_summary():
      db.save({"id": 7})
      r = call("summary", params={"key": "7"})
      assert r.status == 200 and r.json["row"] == {"id": 7}
      assert call("summary").status == 400
      assert call("summary", db=MagicMock()).status == 400  # isolated

Whatever your python code may import, a test may import. In the code
a test exercises, an exception check is spelled `except HttpError` or
`isinstance(e, HttpError)`."""

#: The ferry spec: a positional argument is a path in the tree, and the
#: only free-text value in the surface is the ``-k`` expression.
#: stderr carries the ``"ws-pytest: "`` prefix termish's shell layer
#: adds on the local rung, so both rungs read identically.
FERRY = FerrySpec(
    verb="ws-pytest",
    map_bare_paths=True,
    opaque_flags=("-k",),
    stderr_prefix=True,
)


def make_wspytest_command(ws: Any) -> Any:
    """Build the ``ws-pytest`` command closure over a workspace."""

    def wspytest(ctx: Any) -> Any:
        from termish import CommandResult

        args = list(ctx.args)
        if args and args[0] in ("-h", "--help", "help"):
            ctx.stdout.write(_HELP + "\n")
            return None
        try:
            options = parse_argv(args)
        except UsageError as e:
            return CommandResult(exit_code=USAGE_ERROR, stderr=str(e))
        report = run_options(ws, options, ctx.fs.getcwd())
        if report.collection_error is not None and not report.outcomes:
            # Nothing ran and nothing was collected: the answer is the
            # refusal itself, on stderr, not an empty report.
            return CommandResult(
                exit_code=report.exit_code, stderr=report.collection_error
            )
        ctx.stdout.write(
            render_report(report, verbosity=options.verbosity, tb=options.tb)
        )
        if report.exit_code:
            return CommandResult(exit_code=report.exit_code)
        return None

    wspytest.__doc__ = (
        "Run this workspace's unit tests: ws-pytest [paths] [-k EXPR] "
        "[-x] [-q|-v] [--tb=short|long|no] (ws-pytest --help for the rest)"
    )
    return wspytest


def register_wspytest(ws: Any) -> None:
    """Register the ``ws-pytest`` terminal verb on a workspace.

    Two doors lead here — ``enable_apps`` wires it in with the rest of
    the app loop, and a workspace with no app registers it directly —
    so a second call is a no-op rather than a duplicate-name error.
    No-op as well where the executor neither runs injected commands nor
    ferries ws-* verbs into a guest: the gate doubles as the primer
    gate, since an agent on such an executor is never told the verb
    exists.
    """
    rt = ws.runtime
    if not rt.supports_commands and not rt.supports_ws_verbs:
        return
    if "ws-pytest" in rt.commands:
        return
    # Tags OUR registration and carries the ferry spec; framework-owned,
    # so a fork/snapshot rebuilds the command bound to itself instead of
    # inheriting the parent-bound closure.
    rt.register_command(
        "ws-pytest", tag(make_wspytest_command(ws), FERRY), rebind=register_wspytest
    )
