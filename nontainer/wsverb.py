"""One ferry for every ``ws-*`` verb on a guest rung.

A ``ws-`` name operates on the workspace through nontainer, so it is
implemented once on the host. On a rung that runs real bash the name
has to reach that host implementation anyway: this module is the relay
that takes it there — one guest shell function per registered verb,
one host object they all call, one argv mapper, one tag.

What a verb supplies is a :class:`FerrySpec`: which of its flags carry
filesystem paths, which carry free text, and whether its bare arguments
are paths at all. Everything else — the guest function, the hostcall
object, the live registry lookup, the sync-on-verb, the answer triple —
is the same for every verb and lives here.

Lives in core rather than apps/ on purpose, for two reasons that point
the same way. The handler reads workspace internals (live command
mapping, guest path math, provider staging for oversized captures,
executor staleness) that the apps↔workspace extension surface
deliberately withholds — see tests/test_apps_surface.py. And the
executor relays through it: a guest executor imports this to carry a
``ws-`` name home, so it has to sit BELOW everything that registers a
verb, never above it. The commands themselves stay in their own
modules; this one only ferries them.
"""

from __future__ import annotations

import json
import posixpath
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from termish.context import PipeStream

#: The dud host-object name fronting every ``ws-*`` verb on guest
#: rungs. A user host object under this name refuses at executor open
#: (fail closed, like RESERVED_COMMANDS) rather than shadowing either
#: way.
DUD_OBJECT = "ws_verb"

#: The attribute that marks a command as a framework ``ws-*`` verb and
#: carries its ferry spec. The dud handler fronts only tagged commands,
#: so a custom command under a framework name stays a local-rung
#: creature instead of being dispatched in the guest.
TAG = "_nontainer_ws_verb"


@dataclass(frozen=True)
class FerrySpec:
    """How one verb's argv crosses the rung boundary.

    A guest names paths in guest coordinates (``/work/...``); the host
    command reads them in workspace coordinates (``/workspace/...``).
    Which words are paths is the one thing the relay cannot guess: a
    ws-curl URL and a ws-git pathspec both start with ``/`` and mean
    opposite things.
    """

    verb: str
    """The command name, ``ws-`` prefix included (``"ws-git"``)."""

    map_bare_paths: bool = True
    """Whether a bare argument starting with ``/`` is a filesystem path
    to rewrite. False for a verb whose bare arguments are URLs."""

    path_flags: tuple[str, ...] = ()
    """Flags whose VALUE is a filesystem path to rewrite
    (``-o FILE``)."""

    opaque_flags: tuple[str, ...] = ()
    """Flags whose VALUE is free text that must cross verbatim
    (``-m MESSAGE``): rewriting it would corrupt the argument."""

    stderr_prefix: bool = False
    """Whether to prefix stderr with ``"<verb>: "`` when it lacks one.
    True for a verb whose local rung gets that prefix from termish's
    shell layer rather than from the command itself — without it the
    two rungs read differently."""

    budget: Callable[[], int] | None = None
    """Answer-size budget in bytes, read per call, or ``None`` for no
    budget. dud's hostcall response frame caps at ~1 MiB guest-side;
    a verb that can answer with more declares what to do about it."""

    over_budget: Callable[..., dict] | None = field(default=None, compare=False)
    """``(ws, stdout, landed, result) -> triple`` for an answer past
    the budget. Required when ``budget`` is set."""


def abspath(cwd: str, arg: str) -> str:
    """A path argument as a workspace-absolute path: an absolute one
    normalized, a relative one resolved against the cwd. Every ws-*
    verb reads its path arguments this way, so ``ws-git stage f.txt``
    and ``ws-pytest f.py`` mean the same directory."""
    if arg.startswith("/"):
        return posixpath.normpath(arg)
    return posixpath.normpath(posixpath.join(cwd or "/", arg))


def tag(fn: Any, spec: FerrySpec) -> Any:
    """Mark a command as the framework's ``ws-*`` verb and attach its
    ferry spec. Returns the function, so registration reads
    ``register_command(name, tag(fn, SPEC), rebind=...)``."""
    setattr(fn, TAG, spec)
    return fn


def spec_of(cmd: Any) -> FerrySpec | None:
    """The ferry spec of a registered command, or ``None`` when the
    command is not a framework ``ws-*`` verb."""
    spec = getattr(cmd, TAG, None)
    return spec if isinstance(spec, FerrySpec) else None


#: The guest shell function, one per registered verb, prepended to
#: every dud exec. Calls home over ``dud-hostcall`` with the verb name
#: and the guest cwd first; a tiny python3 split (present in every
#: guest — stdlib can't be withheld) writes any captured files,
#: separates the triple onto the real streams, and exits with the
#: verb's code. ``type ws-git`` shows a function (fine); sudo/env -i
#: drop it (acceptable — the rung, not the verb).
SHELL_TEMPLATE = """{verb}() {{
  dud-hostcall {object} run {verb} "$PWD" "$@" | python3 -c '
import base64, json, os, sys
t = json.loads(sys.stdin.read())
for p, b in t.get("files", {{}}).items():
    d = os.path.dirname(p)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(p, "wb") as f:
        f.write(base64.b64decode(b))
sys.stdout.write(t["stdout"])
sys.stderr.write(t["stderr"])
sys.exit(t["exit_code"])
'
}}
"""


def shell_functions(commands: Mapping[str, Any]) -> str:
    """The guest shell functions for every framework ``ws-*`` verb in a
    live command registry, in name order.

    Read per exec, so a registration made after the executor opened
    (``enable_apps`` on a live workspace, a fork's rebind) takes effect
    with no session rebuild. An untagged command under a framework name
    is not offered: the name is simply absent in the guest, as on any
    rung that does not carry the verb.
    """
    out = []
    for name in sorted(commands):
        if spec_of(commands[name]) is not None:
            out.append(SHELL_TEMPLATE.format(verb=name, object=DUD_OBJECT))
    return "".join(out)


def _guest_ctx(args: list[str], cwd: str, captured: dict[str, bytes]) -> Any:
    """A termish-shaped context for invoking a command off-rung.

    Commands touch ``args``, ``stdout``, and ``fs.getcwd()`` /
    ``fs.write()`` — writes are captured, not applied, so a capture
    lands guest-side (via the answer triple) instead of leaving the
    guest tree stale behind a provider write.

    ``stdout`` is termish's own stream type rather than a text buffer,
    so a command behaves here exactly as it does on the terminal rung:
    text and bytes written through ``stdout`` and ``stdout.buffer``
    land in the order they were written, and a command that emits
    binary reaches a stream that accepts it instead of an
    ``AttributeError``. What crosses to the guest is still the JSON
    triple, whose stdout is text, so bytes that are not valid UTF-8
    are replaced at that boundary — the rung's limit, not the
    command's: a binary payload travels as a captured file.
    """

    class _CaptureFS:
        def __init__(self, cwd: str):
            self._cwd = cwd

        def getcwd(self) -> str:
            return self._cwd

        def write(self, path: str, data: bytes) -> None:
            captured[path] = bytes(data)

    class _Ctx:
        def __init__(self) -> None:
            self.args = args
            self.stdout = PipeStream()
            self.fs = _CaptureFS(cwd)

    return _Ctx()


def _emitted(ctx: Any) -> tuple[bytes, str]:
    """What a command wrote to stdout: the bytes, and the text the
    answer triple carries.

    The triple is JSON, so its stdout is text and a byte that is not
    valid UTF-8 becomes U+FFFD there; the bytes are what a caller
    needs to know what the command actually produced.
    """
    raw = ctx.stdout.getvalue()
    return raw, raw.decode("utf-8", "replace")


def answer_size(out: str, encoded_files: Mapping[str, str]) -> int:
    """The bytes an answer triple puts on the wire, which is what a
    budget guards.

    The frame the guest receives holds ``out`` as a JSON string and
    each captured file as base64, and neither weighs what its source
    did: a byte that was not valid UTF-8 is three bytes of U+FFFD, a
    control character is a six-byte escape, and base64 is four bytes
    for every three. Measuring the source bytes let a payload pass the
    check and then break the transport, where the documented
    over-budget answer never had its chance; this measures what is
    sent. ``json.dumps`` escapes to ASCII by default, so its length is
    its byte count.
    """
    return len(json.dumps(out)) + sum(len(b64) for b64 in encoded_files.values())


def map_argv(mapper: Any, argv: list[str], spec: FerrySpec) -> list[str]:
    """Guest-absolute argv entries to host-absolute, per the verb's
    spec: the value of a path flag is rewritten, the value of an opaque
    flag crosses verbatim, and a bare ``/``-rooted word is rewritten
    where the verb's bare arguments are paths. Unmappable entries pass
    through — the guest asked for exactly that path, and the command
    refuses it by its own rules."""
    if mapper is None:
        return argv
    out: list[str] = []
    pending = ""
    for word in argv:
        if pending == "path":
            word = mapper(word) or word
            pending = ""
        elif pending:
            pending = ""
        elif word in spec.path_flags:
            pending = "path"
        elif word in spec.opaque_flags:
            pending = "opaque"
        elif spec.map_bare_paths and word.startswith("/"):
            word = mapper(word) or word
        out.append(word)
    return out


class WsVerbHostHandler:
    """The dud host object fronting every ``ws-*`` verb on guest rungs.

    One public method, ``run(verb, cwd, *argv)`` — the shape
    ``dud-hostcall`` delivers (every word past METHOD arrives
    verbatim). It invokes the LIVE command from the registry of the
    runtime whose executor ferried it (so post-open registration and
    fork-rebound runtimes work with no lifecycle coupling), with a
    guest-shaped context, so both rungs share behavior byte for byte.

    The triple comes back JSON-safe
    (``{"stdout","stderr","exit_code"}``, plus base64 ``"files"`` for
    captures); the guest function splits it onto the real streams.
    """

    def __init__(self, ws: Any, commands: Mapping[str, Any]):
        self._ws = ws
        # The registry of the runtime whose executor ferries these
        # verbs (``ExecutionContext.commands``, bound live), not the
        # workspace's: one workspace can carry several runtimes (an app
        # served from a snapshot runs on its own), each with its own
        # registrations. A verb must dispatch the one registered where
        # it was invoked, or answer that it is not registered there.
        self._commands = commands

    def run(self, verb: str, cwd: str, *argv: str) -> dict:
        import base64

        ws = self._ws
        with ws._lock:
            cmd = self._commands.get(verb)
            if cmd is None:
                return _fail(verb, "not registered on this workspace")
            spec = spec_of(cmd)
            if spec is None:
                return _fail(
                    verb,
                    "a custom command owns that name here — "
                    "the dud rung only fronts the framework one",
                )
            # Sync-on-verb: absorb first so every verb dispatches
            # against fresh state — `printf x > new.txt; ws-git stage
            # new.txt` stages instead of rejecting an unknown path, and
            # `cat > app/api/x.py; ws-curl $APP_ORIGIN/api/x` dispatches
            # the handler just written. Reads need it as much as
            # mutations. The verbs that DO rewrite worktree files push
            # back the other way: the guest shell is blocked on this
            # hostcall, so its tree is quiescent, and the executor is
            # marked stale so the next call re-materializes it. A
            # nested harvest is safe here: both locks are re-entrant on
            # this thread.
            err = ws._absorb_before_verb(verb)
            if err is not None:
                return err
            executor = getattr(getattr(ws, "runtime", None), "executor", None)
            mapper = getattr(executor, "guest_to_host", None) or getattr(
                executor, "_guest_to_host", None
            )
            unmapper = getattr(executor, "_host_to_guest", None)
            host_cwd = (mapper(cwd) if mapper else None) or cwd
            captured: dict[str, bytes] = {}
            ctx = _guest_ctx(map_argv(mapper, list(argv), spec), host_cwd, captured)
            try:
                result = cmd(ctx)
            except Exception as e:  # noqa: BLE001 — parity: termish wraps these
                return {
                    "stdout": _emitted(ctx)[1],
                    "stderr": f"{verb}: execution error: {e}",
                    "exit_code": 1,
                }
            out = _emitted(ctx)[1]
            # (host_path, guest_path, data) per capture; None when a
            # capture escapes the workspace.
            landed = self._guest_files(ws, unmapper, captured)
            if landed is None:
                return _fail(verb, "output path escapes the workspace")
            encoded = {
                guest: base64.b64encode(data).decode("ascii")
                for _, guest, data in landed
            }
            if spec.budget is not None and spec.over_budget is not None:
                if answer_size(out, encoded) > spec.budget():
                    return spec.over_budget(ws, out, landed, result)
            if result is None:
                return {"stdout": out, "stderr": "", "exit_code": 0, "files": encoded}
            err_text = result.stderr or ""
            if spec.stderr_prefix and err_text and not err_text.startswith(f"{verb}:"):
                err_text = f"{verb}: {err_text}"
            return {
                "stdout": out,
                "stderr": err_text,
                "exit_code": result.exit_code,
                "files": encoded,
            }

    @staticmethod
    def _guest_files(
        ws: Any, unmapper: Any, captured: dict[str, bytes]
    ) -> list[tuple[str, str, bytes]] | None:
        """Captured writes to ``(host_path, guest_path, data)`` triples.

        ``None`` when a capture escapes the workspace (refused, like
        the local rung's isolated-fs refusal, instead of writing the
        guest somewhere its tree doesn't cover). Unmappable absolute
        paths pass through as guest paths — the guest writes exactly
        what was asked, and the provider is never consulted for them.
        """
        executor = getattr(getattr(ws, "runtime", None), "executor", None)
        work = getattr(executor, "_work", "")
        landed: list[tuple[str, str, bytes]] = []
        for host_path, data in captured.items():
            guest_path = (unmapper(host_path) if unmapper else None) or host_path
            if work and not (
                guest_path == work or guest_path.startswith(work.rstrip("/") + "/")
            ):
                return None
            landed.append((host_path, guest_path, data))
        return landed


def _fail(verb: str, message: str) -> dict:
    """A refusal triple in the verb's own voice."""
    return {"stdout": "", "stderr": f"{verb}: {message}", "exit_code": 1}
