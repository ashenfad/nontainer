"""The ``curl`` terminal builtin: the agent's fast inner loop.

``curl [-X METHOD] [-d BODY] [-H 'K: V']... URL`` hits the dispatch
directly — no server, no browser. Body → stdout (composes in
pipelines: ``curl /api/scores | jq .``); status >= 400 → exit code 22
(curl's --fail convention) with ``HTTP <status>`` on stderr.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .contract import make_request

if TYPE_CHECKING:
    from .dispatch import AppRuntime


def make_curl_command(runtime: "AppRuntime") -> Any:
    def curl(ctx: Any) -> Any:
        from termish import CommandResult

        method = None
        body = b""
        headers: dict[str, str] = {}
        url = None

        args = list(ctx.args)
        i = 0
        while i < len(args):
            a = args[i]
            if a == "-X" and i + 1 < len(args):
                method = args[i + 1]
                i += 2
            elif a in ("-d", "--data") and i + 1 < len(args):
                body = args[i + 1].encode()
                i += 2
            elif a == "-H" and i + 1 < len(args):
                if ":" in args[i + 1]:
                    k, v = args[i + 1].split(":", 1)
                    headers[k.strip().lower()] = v.strip()
                i += 2
            elif a in ("-s", "--silent", "-f", "--fail"):
                i += 1  # accepted no-ops (agents type them from habit)
            elif not a.startswith("-"):
                url = a
                i += 1
            else:
                return CommandResult(exit_code=2, stderr=f"curl: unknown flag {a}")

        if url is None:
            return CommandResult(exit_code=2, stderr="curl: no URL")
        if not url.startswith("/"):
            url = "/" + url
        if method is None:
            method = "POST" if body else "GET"

        resp = runtime.dispatch(
            make_request(method, url, body=body, headers=headers)
        )
        ctx.stdout.write(resp.text)
        if resp.text and not resp.text.endswith("\n"):
            ctx.stdout.write("\n")
        if resp.status >= 400:
            return CommandResult(exit_code=22, stderr=f"HTTP {resp.status}")
        return None

    curl.__doc__ = (
        "Test your app's endpoints without a server: "
        "curl [-X METHOD] [-d BODY] URL (e.g. curl /api/scores?limit=3)"
    )
    return curl
