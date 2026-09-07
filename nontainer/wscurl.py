"""The ws-curl dud-rung ferry: the guest shell function plus the host
object fronting it — the same ferry pattern as ws-git.

Lives in core rather than apps/ on purpose: the handler reads
workspace internals (live command mapping, guest path math, provider
staging for oversized captures, executor staleness) that the
apps↔workspace extension surface deliberately withholds — see
tests/test_apps_surface.py. The command itself stays in
apps/wscurl.py; this module only ferries it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

#: Framework host-object name fronting ws-curl on dud rungs.
DUD_OBJECT = "ws_curl"

#: Guest round-trip budget for one ws-curl answer. dud's hostcall
#: response frame caps at ~1 MiB guest-side; past it the guest refuses
#: the frame outright. Bodies under the budget ride the answer triple
#: (so same-script `-o FILE; cat FILE` works); over it, `-o` captures
#: land in the provider with a transcript note (visible host-side at
#: once, guest-side after the next sync) while stdout answers refuse
#: honestly — never a truncated body masquerading as complete.
_FRAME_BUDGET = 768 * 1024

#: The shell function every dud exec prepends when ``ws-curl`` is
#: registered. Calls home over ``dud-hostcall`` with the guest cwd
#: first; a tiny python3 split (present in every guest) writes any
#: ``-o`` captures, separates the triple onto the real streams, and
#: exits with the verb's code.
SHELL_FUNCTION = """ws-curl() {
  dud-hostcall ws_curl run "$PWD" "$@" | python3 -c '
import base64, json, os, sys
t = json.loads(sys.stdin.read())
for p, b in t.get("files", {}).items():
    d = os.path.dirname(p)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(p, "wb") as f:
        f.write(base64.b64decode(b))
sys.stdout.write(t["stdout"])
sys.stderr.write(t["stderr"])
sys.exit(t["exit_code"])
'
}
"""


def _guest_ctx(args: list[str], cwd: str, captured: dict[str, bytes]) -> Any:
    """A termish-shaped context for invoking the command off-rung.

    The closure touches ``args``, ``stdout``, and ``fs.getcwd()`` /
    ``fs.write()`` — writes are captured, not applied, so ``-o``
    captures land guest-side (via the answer triple) instead of
    leaving the guest tree stale behind a provider write.
    """
    from io import StringIO

    class _CaptureFS:
        def __init__(self, cwd: str):
            self._cwd = cwd

        def getcwd(self) -> str:
            return self._cwd

        def write(self, path: str, data: bytes) -> None:
            captured[path] = bytes(data)

    class _Ctx:
        def __init__(self):
            self.args = args
            self.stdout = StringIO()
            self.fs = _CaptureFS(cwd)

    return _Ctx()


class WsCurlHostHandler:
    """The dud host object fronting ws-curl on guest rungs.

    One public method, ``run(cwd, *argv)`` — the shape ``dud-hostcall``
    delivers. It invokes the LIVE ``ws-curl`` command from the
    workspace mapping (so post-open ``enable_apps`` and fork-rebound
    runtimes work with no lifecycle coupling), with a guest-shaped
    context. Unlike ws-git no provider sync is needed: fetch reads no
    provider state, and ``-o`` captures ride home in the answer triple
    rather than being written mid-shell.
    """

    def __init__(self, ws: Any, commands: Mapping[str, Any]):
        self._ws = ws
        # The registry of the runtime whose executor ferries this verb
        # (``ExecutionContext.commands``, bound live), not the
        # workspace's: one workspace can carry several runtimes, each
        # with its own registrations. See ``DudHostHandler``.
        self._commands = commands

    def run(self, cwd: str, *argv: str) -> dict:
        import base64

        ws = self._ws
        with ws._lock:
            cmd = self._commands.get("ws-curl")
            if cmd is None:
                return {
                    "stdout": "",
                    "stderr": "ws-curl: not registered on this workspace\n",
                    "exit_code": 1,
                }
            if not getattr(cmd, "_nontainer_wscurl", False):
                return {
                    "stdout": "",
                    "stderr": "ws-curl: framework command unavailable here\n",
                    "exit_code": 1,
                }
            # Sync-on-verb, same as the ws-git ferry: a script such as
            # `write handler > app/api/x.py; ws-curl $APP_ORIGIN/api/x` dispatches
            # before the outer shell's writes are harvested, and the
            # runtime reads handler source host-side — without this the
            # new handler 404s and an edited one runs stale. The nested
            # harvest is safe for the same reason (blocked guest,
            # re-entrant locks), and fetch needs no push-back: it reads
            # no provider state.
            err = ws._absorb_before_verb("ws-curl")
            if err is not None:
                return err
            executor = getattr(getattr(ws, "runtime", None), "executor", None)
            mapper = getattr(executor, "_guest_to_host", None)
            unmapper = getattr(executor, "_host_to_guest", None)
            host_cwd = (mapper(cwd) if mapper else None) or cwd
            captured: dict[str, bytes] = {}
            ctx = _guest_ctx(self._map_argv(mapper, list(argv)), host_cwd, captured)
            try:
                result = cmd(ctx)
            except Exception as e:  # noqa: BLE001 — parity: termish wraps these
                return {
                    "stdout": ctx.stdout.getvalue(),
                    "stderr": f"ws-curl: execution error: {e}\n",
                    "exit_code": 1,
                }
            out = ctx.stdout.getvalue()
            # (host_path, guest_path, data) per capture; None when a
            # capture escapes the workspace.
            landed = self._guest_files(ws, unmapper, captured)
            if landed is None:
                return {
                    "stdout": out,
                    "stderr": "ws-curl: -o path escapes the workspace\n",
                    "exit_code": 1,
                }
            body_len = len(out.encode()) + sum(len(d) for _, _, d in landed)
            if body_len > _FRAME_BUDGET:
                return self._over_budget(ws, out, landed, result)
            encoded = {
                guest: base64.b64encode(data).decode("ascii")
                for _, guest, data in landed
            }
            if result is None:
                return {"stdout": out, "stderr": "", "exit_code": 0, "files": encoded}
            err = result.stderr or ""
            return {
                "stdout": out,
                "stderr": err,
                "exit_code": result.exit_code,
                "files": encoded,
            }

    @staticmethod
    def _map_argv(mapper: Any, argv: list[str]) -> list[str]:
        """Guest-absolute argv entries to host-absolute — but ONLY the
        ``-o``/``--output`` value, which is the sole filesystem path in
        the surface. URLs (``/api/...``), ``-d`` bodies, headers, and
        ``-w`` formats pass through verbatim: mapping them would corrupt
        requests into file lookups."""
        if mapper is None:
            return argv
        out: list[str] = []
        output_next = False
        for word in argv:
            if output_next:
                word = mapper(word) or word
                output_next = False
            elif word in ("-o", "--output"):
                output_next = True
            out.append(word)
        return out

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

    @staticmethod
    def _over_budget(
        ws: Any, out: str, landed: list[tuple[str, str, bytes]], result: Any
    ) -> dict:
        """Over the guest frame budget: ``-o`` captures still land —
        in the provider, with the guest flagged for re-sync — while a
        stdout answer refuses honestly instead of truncating mid-body.

        Either way the command's own failure is preserved: an HTTP
        error with a large body stays an HTTP error (its exit code and
        stderr ride through), so ``&&`` chains stop and callers never
        read a failed endpoint as successful just because its error
        body was big.
        """
        if landed:
            try:
                for host_path, _, data in landed:
                    ws._fs.write(host_path, data)
            except Exception as e:  # noqa: BLE001 — honest triple, below
                return {
                    "stdout": out,
                    "stderr": f"ws-curl: oversized capture unwritable: {e}\n",
                    "exit_code": 1,
                    "files": {},
                }
            ws._mark_executor_stale()
            note = (
                "ws-curl: body exceeded the guest round-trip frame; "
                "captures landed in the workspace (visible to the next call)"
            )
            if result is None:
                return {
                    "stdout": out + note + "\n",
                    "stderr": "",
                    "exit_code": 0,
                    "files": {},
                }
            return {
                "stdout": out + note + "\n",
                "stderr": result.stderr or "",
                "exit_code": result.exit_code,
                "files": {},
            }
        if result is not None:
            # No captures: the body is unobservable either way, so the
            # command's own failure reads exactly as it would under
            # budget — with one clause naming the withheld body.
            err = result.stderr or ""
            withheld = "ws-curl: response body withheld (exceeds guest frame)"
            return {
                "stdout": "",
                "stderr": (f"{err}\n{withheld}" if err else withheld) + "\n",
                "exit_code": result.exit_code,
                "files": {},
            }
        total = len(out.encode()) + sum(len(d) for _, _, d in landed)
        return {
            "stdout": "",
            "stderr": (
                f"ws-curl: response ({total} bytes) exceeds the guest "
                "round-trip frame; re-request a narrower body or capture "
                "it with -o FILE\n"
            ),
            "exit_code": 1,
            "files": {},
        }
