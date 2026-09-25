"""``ws-vitest``: vitest's shape over the workspace's JavaScript.

The tier below ``ws-curl`` and ``test_app``, for the other language.
Where ``ws-pytest`` asks a question of a Python function, this asks one
of a module under ``app/``: run it, and read the assertion that failed.

Not vitest — vitest's **shape**, which is jest's surface. The discovery
rule, the matchers worth having, the report, the exit codes and a named
flag subset; anything else is refused with the idiom that replaces it.
Each test file runs as page JavaScript in a headless Chromium, on a
harness page served by a driver of nontainer's own
(:mod:`nontainer.apps.jsharness`), with ``app/`` and ``tests/`` as
siblings under one synthetic root so the import an agent writes
(``import { add } from '../app/util.js'``) resolves.

**The browser is on the host on every rung.** ``ws-pytest`` runs test
code through the executor, so on a VM rung it runs in the guest; this
verb reads the workspace's files on the host and drives Chromium there.
That is the asymmetry ``test_app`` already has, and it is said out loud
in the help, in the report's own notes and in the docs rather than left
to be discovered.

The run is hermetic: no api routes come up, nothing off the synthetic
origin is reachable, and the policy on the wire is stricter than the
app's. A forgotten fetch stub fails here instead of passing for the
wrong reason.

The record is the product; the terminal text is a rendering of it
(:func:`render_report`). It is the same ``TestReport`` ``ws-pytest``
produces, with ``tool`` telling the two apart, so one gate on a merge
and one UI consume either.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from .wspytest import TestOutcome, TestReport, UsageError
from .wsverb import FerrySpec, abspath, tag

#: Where JavaScript tests live, recursively. The only home: it sits
#: outside the subtree a publication carries.
TESTS_DIR = "tests"

#: The published subtree, which is also where the modules under test
#: live — so a misplaced test lands here, and ships when the app does.
#: Collected anyway, and named in the run, because a test that silently
#: never ran is worse than one that runs where it should not be.
APP_DIR = "app"

#: Backend source. A ``.test.js`` here is not runnable: the harness
#: refuses that subtree the way the app's own static dispatch does, so
#: the page could not load the file even if it were collected — and
#: handlers are Python anyway.
API_DIR = "app/api"

#: What a test file is called. The ecosystem's convention, and what an
#: agent writes without being asked.
SUFFIX = ".test.js"

_SKIP_DIRS = {"__pycache__", ".git", "node_modules", "dist", "build"}


# --------------------------------------------------------------------
# discovery
# --------------------------------------------------------------------


def _rel(ws: Any, path: str) -> str:
    """A workspace-absolute path as the agent spells it."""
    root = "" if ws.root == "/" else ws.root.rstrip("/")
    if root and path.startswith(root + "/"):
        return path[len(root) + 1 :]
    return path.lstrip("/")


def _abs(ws: Any, rel: str) -> str:
    root = "" if ws.root == "/" else ws.root.rstrip("/")
    return f"{root}/{rel.lstrip('/')}"


def _walk(fs: Any, base: str) -> list[str]:
    """Every ``*.test.js`` under ``base``, sorted, with the caches and
    vendored trees left alone."""
    base = base.rstrip("/")
    found = set(fs.glob(f"{base}/*{SUFFIX}")) | set(fs.glob(f"{base}/**/*{SUFFIX}"))
    out = []
    for path in found:
        rest = path[len(base) + 1 :].split("/")[:-1]
        if any(part in _SKIP_DIRS or part.startswith(".") for part in rest):
            continue
        out.append(path)
    return sorted(out)


def _some(paths: list[str], shown: int = 3) -> str:
    """The first few of ``paths``, and how many more: a vendored tree
    can hold hundreds, and a note is one line."""
    head = ", ".join(paths[:shown])
    rest = len(paths) - shown
    return f"{head}, and {rest} more" if rest > 0 else head


def discover(ws: Any) -> tuple[list[str], list[str]]:
    """``(test files, notes)`` for this workspace, workspace-relative.

    Tests belong in ``tests/``, which is collected first. A
    ``.test.js`` under ``app/`` is collected too, and reported: an
    agent writes one there without being asked, and a test that
    silently never ran is worse than one that runs with a note saying
    it ships with a publication. ``app/api/`` is neither home: the
    harness refuses that subtree, so a test there is reported as not
    runnable rather than silently skipped. A test file anywhere else is
    named in a note and not run, for the same reason.
    """
    fs = ws.files.fs
    root = "" if ws.root == "/" else ws.root.rstrip("/")
    api_root = f"{root}/{API_DIR}"
    tests = [_rel(ws, p) for p in _walk(fs, f"{root}/{TESTS_DIR}")]
    beside: list[str] = []
    unreachable: list[str] = []
    for path in _walk(fs, f"{root}/{APP_DIR}"):
        target = (
            unreachable
            if path == api_root or path.startswith(api_root + "/")
            else beside
        )
        target.append(_rel(ws, path))

    notes: list[str] = []
    if beside:
        notes.append(
            f"{_files(beside)} beside a module under {APP_DIR}/ "
            f"({', '.join(beside)}). {APP_DIR}/ is what publishes, so these "
            "ship with the app and are fetchable from it. Move a test to "
            f"{TESTS_DIR}/ if that is not what you want."
        )
    if unreachable:
        notes.append(
            f"{_files(unreachable)} under {API_DIR}/ not runnable "
            f"({', '.join(unreachable)}): the harness refuses that subtree the "
            "way the served app does, so the page could not load them — and "
            f"handlers are Python, which ws-pytest runs from {TESTS_DIR}/."
        )
    tests_root = f"{root}/{TESTS_DIR}"
    app_root = f"{root}/{APP_DIR}"
    elsewhere = [
        _rel(ws, p)
        for p in _walk(fs, root or "/")
        if not any(p.startswith(d + "/") for d in (tests_root, app_root))
    ]
    if elsewhere:
        notes.append(
            f"{_files(elsewhere)} outside {TESTS_DIR}/ and {APP_DIR}/ not "
            f"collected ({_some(elsewhere)}). Move a test to {TESTS_DIR}/ to "
            "run it."
        )
    return [*tests, *beside], notes


def _files(paths: list[str]) -> str:
    return f"{len(paths)} test file" + ("" if len(paths) == 1 else "s")


def _filter_key(ws: Any, cwd: str, word: str) -> str:
    """One positional filter as it will be matched.

    vitest's positionals are substrings of a file's path, and that is
    what a bare word stays. A word that names an absolute path is
    resolved and relativized first — the rule every ws-* verb reads its
    path arguments by, and what makes the guest's own ``/work/...``
    spelling mean the same file after the ferry rewrites it.
    """
    if word.startswith("/"):
        return _rel(ws, abspath(cwd, word))
    return word


# --------------------------------------------------------------------
# the flag surface
# --------------------------------------------------------------------

#: Flags vitest has that this verb refuses, each with the idiom that
#: replaces it. A refusal is a one-line answer naming what to do
#: instead; silence would cost the agent a run it thinks it filtered.
REFUSED = {
    "--coverage": "there is no coverage instrumentation here — the report "
    "names every test that ran",
    "--ui": "there is no UI here — the report is the answer, and "
    "--reporter=verbose is one line per test",
    "--config": "there is no config file — the layout IS the configuration: "
    "tests/*.test.js, never under app/",
    "-c": "there is no config file — the layout IS the configuration: "
    "tests/*.test.js, never under app/",
    "--watch": "nothing here watches for edits — re-run the verb",
    "-w": "nothing here watches for edits — re-run the verb",
    "--browser": "there is one browser and it is already headless Chromium",
    "--environment": "there is one environment: a browser page",
}

REPORTERS = ("default", "verbose")

USAGE = "usage: ws-vitest [run] [paths] [-t NAME] [--reporter=default|verbose] [--bail]"


@dataclass(frozen=True)
class Options:
    """A parsed ``ws-vitest`` argv."""

    filters: tuple[str, ...] = ()
    """Positional path filters: a file runs when its workspace-relative
    path contains one of them."""
    name: str | None = None
    reporter: str = "default"
    bail: int = 0
    """Failing files allowed before the run stops; 0 is no limit."""


def parse_argv(argv: Any = ()) -> Options:
    """A ``ws-vitest`` argv as options.

    ``run`` is accepted and dropped (there is no watch mode to opt out
    of); positional words filter by path; the flags are the named
    subset; anything else is refused by name.
    """
    filters: list[str] = []
    name: str | None = None
    reporter = "default"
    bail = 0
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
            if word != "run":
                filters.append(word)
        elif head in ("-t", "--testNamePattern"):
            name = word.split("=", 1)[1] if "=" in word else value(head)
        elif head == "--reporter":
            reporter = word.split("=", 1)[1] if "=" in word else value("--reporter")
            if reporter not in REPORTERS:
                raise UsageError(
                    f"--reporter takes {' | '.join(REPORTERS)}, not {reporter!r}"
                )
        elif head == "--bail":
            raw = word.split("=", 1)[1] if "=" in word else "1"
            if not raw.isdigit() or int(raw) < 1:
                raise UsageError(f"--bail takes a count of 1 or more, not {raw!r}")
            bail = int(raw)
        else:
            raise UsageError(f"unknown flag {word}\n{USAGE}")
        i += 1
    return Options(
        filters=tuple(filters), name=name, reporter=reporter, bail=max(bail, 0)
    )


# --------------------------------------------------------------------
# the run
# --------------------------------------------------------------------


def run_vitest(ws: Any, argv: Any = (), *, cwd: str | None = None) -> TestReport:
    """Run the workspace's JavaScript tests and report what happened.

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
    """``run_vitest`` with an argv already parsed. ``cwd`` is what an
    absolute path filter resolves against — the terminal's, when the
    verb is what called."""
    from .apps.jsharness import run_js_tests

    started = time.perf_counter()
    if cwd is None:
        cwd = ws.files.fs.getcwd()
    files, notes = discover(ws)
    if not files:
        # The notes ride along: a workspace whose only test files sit
        # where they cannot run must read the reason, not "none found".
        return _usage_report(
            "\n".join(
                [
                    f"no test files found. JavaScript tests are "
                    f"{TESTS_DIR}/*{SUFFIX}, never under {APP_DIR}/.",
                    *notes,
                ]
            )
        )
    if options.filters:
        keys = [_filter_key(ws, cwd, word) for word in options.filters]
        files = [rel for rel in files if any(key in rel for key in keys)]
        if not files:
            return _usage_report("no test files matched: " + ", ".join(options.filters))

    results = run_js_tests(ws, files, name=options.name, bail=options.bail)

    outcomes: list[TestOutcome] = []
    errors: list[str] = []
    collected = 0
    for result in results:
        collected += len(result.outcomes)
        if result.load_error is not None:
            errors.append(f"{result.file}\n{result.load_error}")
            outcomes.append(
                TestOutcome(
                    name=result.file,
                    file=result.file,
                    line=None,
                    status="error",
                    duration=result.duration,
                    message=result.load_error,
                    traceback=result.load_error,
                )
            )
            continue
        if result.collection_error is not None:
            errors.append(f"{result.file}\n{result.collection_error}")
            outcomes.append(
                TestOutcome(
                    name=result.file,
                    file=result.file,
                    line=result.frames[0].line if result.frames else None,
                    status="error",
                    duration=result.duration,
                    message=result.collection_error,
                    traceback=_frames_text(result.frames),
                    frames=result.frames,
                )
            )
            continue
        outcomes.extend(result.outcomes)
        notes.extend(result.violations)

    console = "\n".join(line for r in results for line in r.console)
    passed = sum(1 for o in outcomes if o.status == "passed")
    failed = sum(1 for o in outcomes if o.status == "failed")
    error_count = sum(1 for o in outcomes if o.status == "error")
    if options.name and not collected:
        return _usage_report(f"no test matched -t {options.name!r}")
    code = 1 if (failed or error_count or not collected) else 0
    return TestReport(
        tool="vitest",
        ok=code == 0,
        collected=collected,
        passed=passed,
        failed=failed,
        errors=error_count,
        skipped=0,
        duration=time.perf_counter() - started,
        exit_code=code,
        outcomes=tuple(outcomes),
        stdout=console,
        collection_error="\n\n".join(errors) or None,
        notes=tuple(notes),
    )


def _frames_text(frames: Any) -> str | None:
    from .apps.jsharness import render_frames

    return render_frames(frames) or None


def _usage_report(message: str) -> TestReport:
    """A call that was wrong before anything ran. vitest has no separate
    exit code for one — everything that is not a green run is 1 — so the
    message is what carries the difference."""
    return TestReport(
        tool="vitest",
        ok=False,
        collected=0,
        passed=0,
        failed=0,
        errors=0,
        skipped=0,
        duration=0.0,
        exit_code=1,
        collection_error=message,
    )


# --------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------

_WIDTH = 68


def _sep(title: str, char: str = "⎯") -> str:
    text = f" {title} " if title else ""
    fill = _WIDTH - len(text)
    if fill < 2:
        return text.strip()
    return char * (fill // 2) + text + char * (fill - fill // 2)


def _ms(seconds: float) -> str:
    return f"{seconds * 1000:.0f}ms"


def _count(label: str, failed: int, passed: int, total: int) -> str:
    parts = []
    if failed:
        parts.append(f"{failed} failed")
    if passed:
        parts.append(f"{passed} passed")
    body = " | ".join(parts) if parts else "no tests"
    return f"{label:>11}  {body} ({total})" if parts else f"{label:>11}  {body}"


def _by_file(report: TestReport) -> list[tuple[str, list[TestOutcome]]]:
    """The outcomes grouped by the file they came from, in run order."""
    groups: dict[str, list[TestOutcome]] = {}
    for o in report.outcomes:
        groups.setdefault(o.file, []).append(o)
    return list(groups.items())


def render_report(report: TestReport, *, verbose: bool = False) -> str:
    """The terminal text for a ``ws-vitest`` report: vitest's per-file
    lines, its failure sections and its three summary lines.

    ``verbose`` is ``--reporter=verbose``: a check or a cross per test
    under each file, rather than a count.
    """
    if report.collection_error is not None and not report.outcomes:
        return f"ws-vitest: {report.collection_error}\n"

    out: list[str] = []
    for path, group in _by_file(report):
        broken = [o for o in group if o.status == "error"]
        tests = [o for o in group if o.status != "error"]
        bad = [o for o in tests if o.status == "failed"]
        duration = _ms(sum(o.duration for o in group))
        mark = "✓" if not bad and not broken else "❯"
        # A file that ran tests AND carried an error reports both: an
        # unhandled rejection does not un-run what already passed, and a
        # "(0 tests)" line there would hide work that was done.
        counts = f"{len(tests)} test{'' if len(tests) == 1 else 's'}"
        if bad:
            counts += f" | {len(bad)} failed"
        out.append(f" {mark} {path} ({counts}) {duration}")
        for o in tests:
            if verbose:
                out.append(
                    f"   {'✓' if o.status == 'passed' else '×'} "
                    f"{_short(o)} {_ms(o.duration)}"
                )
            elif o.status == "failed":
                out.append(f"   × {_short(o)}")

    # A file that would not load at all, and an error no test was
    # running to be charged with, are different repairs — the first is
    # the file's own code, the second is work a test started and left
    # behind — so they are not one list.
    suites = [o for o in report.outcomes if o.status == "error" and o.name == o.file]
    unhandled = [o for o in report.outcomes if o.status == "error" and o.name != o.file]
    for title, group in (("Failed Suites", suites), ("Unhandled Errors", unhandled)):
        if not group:
            continue
        out.append("")
        out.append(_sep(f"{title} {len(group)}"))
        for o in group:
            out.append("")
            out.append(f" FAIL  {o.file}")
            out.append(o.message or "")
            if o.traceback and o.traceback != o.message:
                out.append(o.traceback)
        out.append("")
        out.append(_sep(""))

    failures = [o for o in report.outcomes if o.status == "failed"]
    if failures:
        out.append("")
        out.append(_sep(f"Failed Tests {len(failures)}"))
        for o in failures:
            out.append("")
            out.append(f" FAIL  {o.name}")
            out.append(o.message or "")
            if o.traceback:
                out.append(o.traceback)
        out.append("")
        out.append(_sep(""))

    if report.stdout:
        out.append("")
        out.append(_sep("Console"))
        out.append(report.stdout.rstrip("\n"))

    out.append("")
    files = _by_file(report)
    bad_files = sum(1 for _, group in files if any(o.status != "passed" for o in group))
    out.append(_count("Test Files", bad_files, len(files) - bad_files, len(files)))
    # A suite that would not load contributes no tests: vitest counts
    # files and tests separately, and folding an unloadable file into
    # the test count would invent a test nobody wrote.
    out.append(
        _count("Tests", report.failed, report.passed, report.failed + report.passed)
    )
    out.append(f"{'Duration':>11}  {report.duration:.2f}s")
    for note in report.notes:
        out.append(f"note: {note}")
    return "\n".join(out) + "\n"


def _short(outcome: TestOutcome) -> str:
    """A test's name without the file it came from — the file heads its
    own block, so repeating it costs a line's worth of width."""
    head = outcome.file + " > "
    return outcome.name[len(head) :] if outcome.name.startswith(head) else outcome.name


# --------------------------------------------------------------------
# the terminal verb
# --------------------------------------------------------------------

_HELP = """\
ws-vitest — run this workspace's JavaScript unit tests

usage: ws-vitest [run] [paths] [-t NAME] [--reporter=default|verbose]
                 [--bail]

Collects tests/*.test.js and runs each file as page JavaScript in a
headless browser, with app/ and tests/ served as siblings — so
`import { add } from '../app/util.js'` resolves from a test file.

  run               accepted and ignored (nothing here watches for edits)
  <path>            run only files whose path contains this word
  -t NAME           run only tests whose name contains NAME
  --reporter=default|verbose
                    counts per file / one line per test
  --bail[=N]        stop after the first (or Nth) failing file

Exit codes are 0 for a green run and 1 for anything else:
vitest has no separate exit code for a usage error, so a refused flag, a
filter that matched nothing and a failing test all exit 1, and the
message is what tells them apart.

Tests live in tests/, never under app/: app/ is what publishes, so a
test there ships with the app and is fetchable from it. One such test
still runs — silently skipping it would be worse — and the run names it
so you can move it.

The harness supplies describe, it/test, beforeEach/afterEach, expect and
vi as globals, and the same names resolve from 'vitest' and
'@jest/globals'. expect covers toBe, toEqual, toStrictEqual, toContain,
toHaveLength, toBeTruthy/toBeFalsy, toBeNull/toBeUndefined/toBeDefined,
toThrow, toMatch, the toBeGreaterThan family, toBeCloseTo,
toHaveBeenCalled(Times|With), .not, and resolves/rejects. vi supplies
fn, spyOn, stubGlobal and stubFetch; jest is an alias of vi.

The run is hermetic: no api routes are up and no other host is
reachable, so mock at the fetch boundary —
vi.stubFetch({'api/scores': {scores: []}}) — and a forgotten stub fails
rather than passing for the wrong reason.

Refused, each with what to do instead: vi.mock (a browser has no loader
hook — pass the dependency in, or spy on an object your code owns),
snapshot matchers, expect.extend, --coverage, --ui, --config, --watch.

JavaScript tests run in a browser on the host on every rung, against the
files this workspace holds. Python tests (ws-pytest) run where your code
runs."""

#: The ferry spec: a positional argument is a path filter, and the only
#: free-text value in the surface is the ``-t`` name. stderr carries the
#: ``"ws-vitest: "`` prefix termish's shell layer adds on the local
#: rung, so both rungs read identically.
FERRY = FerrySpec(
    verb="ws-vitest",
    map_bare_paths=True,
    opaque_flags=("-t", "--testNamePattern"),
    stderr_prefix=True,
)


def make_wsvitest_command(ws: Any) -> Any:
    """Build the ``ws-vitest`` command closure over a workspace."""

    def wsvitest(ctx: Any) -> Any:
        from termish import CommandResult

        args = list(ctx.args)
        if args and args[0] in ("-h", "--help", "help"):
            ctx.stdout.write(_HELP + "\n")
            return None
        try:
            options = parse_argv(args)
        except UsageError as e:
            return CommandResult(exit_code=1, stderr=str(e))
        report = run_options(ws, options, ctx.fs.getcwd())
        if report.collection_error is not None and not report.outcomes:
            # Nothing ran and nothing was collected: the answer is the
            # refusal itself, on stderr, not an empty report.
            return CommandResult(exit_code=1, stderr=report.collection_error)
        ctx.stdout.write(render_report(report, verbose=options.reporter == "verbose"))
        if report.exit_code:
            return CommandResult(exit_code=report.exit_code)
        return None

    wsvitest.__doc__ = (
        "Run this workspace's JavaScript unit tests: ws-vitest [paths] "
        "[-t NAME] [--reporter=verbose] [--bail] (ws-vitest --help for the rest)"
    )
    return wsvitest


def register_wsvitest(ws: Any) -> None:
    """Register the ``ws-vitest`` terminal verb on a workspace.

    Two doors lead here — ``enable_apps`` wires it in with the rest of
    the app loop, and a workspace with no app registers it directly — so
    a second call is a no-op rather than a duplicate-name error. No-op
    as well where the executor neither runs injected commands nor
    ferries ws-* verbs into a guest: the gate doubles as the primer
    gate, since an agent on such an executor is never told the verb
    exists.
    """
    rt = ws.runtime
    if not rt.supports_commands and not rt.supports_ws_verbs:
        return
    if "ws-vitest" in rt.commands:
        return
    # Tags OUR registration and carries the ferry spec; framework-owned,
    # so a fork/snapshot rebuilds the command bound to itself instead of
    # inheriting the parent-bound closure.
    rt.register_command(
        "ws-vitest", tag(make_wsvitest_command(ws), FERRY), rebind=register_wsvitest
    )
