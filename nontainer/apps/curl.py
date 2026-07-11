"""The ``curl`` terminal builtin: the agent's fast inner loop.

``curl [-X METHOD] [-d BODY] [-H 'K: V']... URL`` hits the dispatch
directly — no server, no browser. Body → stdout (composes in
pipelines: ``curl /api/scores | jq .``); status >= 400 → exit code 22
(curl's --fail convention) with ``HTTP <status>`` on stderr.

Real-curl reflexes are absorbed rather than rejected: ``-i`` prints
the status line + headers, ``-o FILE`` writes the body to the
workspace fs, ``-w`` substitutes ``%{http_code}``/``%{size_download}``,
and network-only flags (``-v``, ``-L``, ``--max-time``, ...) are
accepted no-ops — an agent debugging its app shouldn't discover our
flag surface by silent failure.
"""

from __future__ import annotations

import posixpath
from typing import TYPE_CHECKING, Any

from .contract import make_request

if TYPE_CHECKING:
    from .dispatch import AppRuntime

# accepted-but-meaningless here (no network, no TLS, no redirects —
# dispatch is a direct call); consuming them beats a cryptic rejection
_NOOP_FLAGS = {
    "-s", "--silent", "-f", "--fail", "-S", "--show-error",
    "-v", "--verbose", "-L", "--location", "-k", "--insecure",
    "-g", "--globoff", "--compressed", "-4", "-6",
}
_NOOP_VALUED = {"--max-time", "--connect-timeout", "-m", "--retry", "-A", "--user-agent"}

_SUPPORTED = (
    "supported: -X -d/--data --json -H -i -o -w (plus accepted no-ops: "
    "-s -f -v -L --max-time ...) — this curl dispatches straight into "
    "the workspace app; there is no network"
)


def make_curl_command(runtime: "AppRuntime") -> Any:
    def curl(ctx: Any) -> Any:
        from termish import CommandResult

        method = None
        body = b""
        headers: dict[str, str] = {}
        url = None
        include_headers = False
        out_file = None
        write_fmt = None

        args = list(ctx.args)
        i = 0
        while i < len(args):
            a = args[i]
            if a == "-X" and i + 1 < len(args):
                method = args[i + 1]
                i += 2
            elif a in ("-d", "--data", "--data-raw", "--data-binary") and i + 1 < len(
                args
            ):
                body = args[i + 1].encode()
                i += 2
            elif a == "--json" and i + 1 < len(args):
                body = args[i + 1].encode()
                headers["content-type"] = "application/json"
                i += 2
            elif a == "-H" and i + 1 < len(args):
                if ":" in args[i + 1]:
                    k, v = args[i + 1].split(":", 1)
                    headers[k.strip().lower()] = v.strip()
                i += 2
            elif a in ("-i", "--include"):
                include_headers = True
                i += 1
            elif a in ("-o", "--output") and i + 1 < len(args):
                out_file = args[i + 1]
                i += 2
            elif a in ("-w", "--write-out") and i + 1 < len(args):
                write_fmt = args[i + 1]
                i += 2
            elif a in _NOOP_FLAGS:
                i += 1
            elif a in _NOOP_VALUED and i + 1 < len(args):
                i += 2
            elif not a.startswith("-"):
                url = a
                i += 1
            else:
                return CommandResult(
                    exit_code=2, stderr=f"curl: unknown flag {a} ({_SUPPORTED})"
                )

        if url is None:
            return CommandResult(exit_code=2, stderr=f"curl: no URL ({_SUPPORTED})")
        if url.startswith(("http://", "https://")):
            # The single most expensive discovery an agent can make by
            # trial and error — say it outright instead (curl exit 6:
            # could not resolve host).
            return CommandResult(
                exit_code=6,
                stderr="curl: external URLs are unreachable — this curl "
                "dispatches only into the workspace app (try: curl "
                "/api/...). The workspace has no internet access; "
                "BROWSER-side code may load scripts from the CDN "
                "allowlist (esm.sh, unpkg.com, cdn.jsdelivr.net, "
                "cdn.plot.ly).",
            )
        if not url.startswith("/"):
            url = "/" + url
        if method is None:
            method = "POST" if body else "GET"

        resp = runtime.dispatch(make_request(method, url, body=body, headers=headers))

        if include_headers:
            ctx.stdout.write(f"HTTP/1.1 {resp.status}\n")
            shown = dict(resp.headers)
            shown.setdefault("content-type", resp.content_type)
            for k, v in shown.items():
                ctx.stdout.write(f"{k}: {v}\n")
            ctx.stdout.write("\n")

        if out_file is not None:
            path = out_file
            if not path.startswith("/"):
                path = posixpath.normpath(posixpath.join(ctx.fs.getcwd(), path))
            ctx.fs.write(path, resp.content)
        else:
            ctx.stdout.write(resp.text)
            if resp.text and not resp.text.endswith("\n"):
                ctx.stdout.write("\n")

        if write_fmt is not None:
            out = (
                write_fmt.replace("%{http_code}", str(resp.status))
                .replace("%{size_download}", str(len(resp.content)))
                .replace("\\n", "\n")
                .replace("\\t", "\t")
            )
            ctx.stdout.write(out)
            if out and not out.endswith("\n"):
                ctx.stdout.write("\n")

        if resp.status >= 400:
            return CommandResult(exit_code=22, stderr=f"HTTP {resp.status}")
        return None

    curl.__doc__ = (
        "Test your app's endpoints without a server: "
        "curl [-X METHOD] [-d BODY] [-i] [-o FILE] [-w '%{http_code}'] URL "
        "(e.g. curl /api/scores?limit=3)"
    )
    return curl
