"""The ws-curl ferry policy: what its argv means across the rung
boundary, and what to do with an answer too big for the guest frame.

The relay itself is generic (:mod:`nontainer.wsverb`); what is
ws-curl's own is that its bare arguments are URLs rather than paths,
that only ``-o`` names a file, and that a response body can be larger
than a guest round trip holds. The command itself stays in
apps/wscurl.py.
"""

from __future__ import annotations

from typing import Any

from .wsverb import FerrySpec

#: Guest round-trip budget for one ws-curl answer. dud's hostcall
#: response frame caps at ~1 MiB guest-side; past it the guest refuses
#: the frame outright. Bodies under the budget ride the answer triple
#: (so same-script `-o FILE; cat FILE` works); over it, `-o` captures
#: land in the provider with a transcript note (visible host-side at
#: once, guest-side after the next sync) while stdout answers refuse
#: honestly — never a truncated body masquerading as complete.
_FRAME_BUDGET = 768 * 1024


def _budget() -> int:
    """The live budget, read per call (a test narrows it)."""
    return _FRAME_BUDGET


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


#: The ferry spec: bare arguments are URLs (mapping them would corrupt
#: requests into file lookups), and the only filesystem path in the
#: surface is the ``-o``/``--output`` value.
FERRY = FerrySpec(
    verb="ws-curl",
    map_bare_paths=False,
    path_flags=("-o", "--output"),
    budget=_budget,
    over_budget=_over_budget,
)
