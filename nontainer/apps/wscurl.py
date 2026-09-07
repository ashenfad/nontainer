"""The ``ws-curl`` terminal builtin: the agent's fast inner loop.

``ws-curl [-X METHOD] [-d BODY] [-H 'K: V']... URL`` hits the dispatch
directly — no server, no browser. Body → stdout (composes in
pipelines: ``ws-curl $APP_ORIGIN/api/scores | jq .``); status >= 400 reads as a
response unless ``-f/--fail`` turns it into exit code 22 with
``HTTP <status>`` on stderr (real curl's convention).

Real-curl reflexes are absorbed rather than rejected: ``-i`` prints
the status line + headers, ``-o FILE`` writes the body to the
workspace fs, ``-w`` substitutes ``%{http_code}``/``%{size_download}``,
``-L`` follows redirects within the app, and network-only spellings
that change nothing observable (``-s``, ``-k``, ...) are silent
no-ops. What WOULD change the response (``-v``, ``--max-time``) is
refused outright — an agent debugging its app shouldn't discover our
flag surface by silent failure.
"""

from __future__ import annotations

import posixpath
from typing import TYPE_CHECKING, Any

from .contract import make_request

if TYPE_CHECKING:
    from .dispatch import AppRuntime

# Silent no-ops: no network, no TLS, no globbing, no IPv6 here, so
# these change nothing observable and are swallowed.
_SILENT_NOOP = {
    "-s",
    "--silent",
    "-S",
    "--show-error",
    "-k",
    "--insecure",
    "-g",
    "--globoff",
    "--compressed",
    "-4",
    "-6",
}
# Refused: honoring these WOULD change the response the agent sees
# (timeouts, verbose chatter), and this shell cannot honor them — use
# real curl where the shell provides it.
_REFUSED = {
    "-v",
    "--verbose",
}
_REFUSED_VALUED = {
    "--max-time",
    "--connect-timeout",
    "-m",
    "--retry",
}

_SUPPORTED = (
    "supported: -X -d/--data --json -H -A -i -o -w -f -L (plus silent "
    "no-ops: -s -S -k -g --compressed -4 -6) — this command dispatches "
    "straight into the workspace app; there is no network"
)


def _strip_origin(url: str, extra_host: str | None = None) -> str | None:
    """localhost http(s) URLs to their path; ``None`` for anything
    genuinely external. Any localhost port matches — no listener
    exists, so the port is fictional; the host is what matters. The
    configured origin's host is accepted too, so the taught
    ``$APP_ORIGIN`` form works for embedders that set one."""
    if url.startswith("//"):
        url = "http:" + url
    if url.startswith(("http://", "https://")):
        from urllib.parse import urlsplit

        parts = urlsplit(url)
        allowed = {"localhost", "127.0.0.1"}
        if extra_host:
            allowed.add(extra_host)
        if (parts.hostname or "") not in allowed:
            return None
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        return path
    if not url.startswith("/"):
        url = "/" + url
    return url


def _resolve_redirect(
    path: str, location: str, extra_host: str | None = None
) -> str | None:
    """A redirect Location to the next request path; ``None`` when it
    leaves the app (refused like any external URL). Location resolves
    as a URL reference against the request path — so ``?page=2`` keeps
    it and ``../sibling`` walks it — while absolute URLs strip to
    their path when same-origin, any port."""
    if location.startswith(("http://", "https://", "//")):
        return _strip_origin(location, extra_host)
    from urllib.parse import urljoin, urlsplit

    parts = urlsplit(urljoin(f"http://localhost{path}", location))
    ref = parts.path or "/"
    if parts.query:
        ref += "?" + parts.query
    return ref


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
        fail = False
        follow = False

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
                # repeated -d concatenates with '&', like real curl
                chunk = args[i + 1].encode()
                body = body + b"&" + chunk if body else chunk
                i += 2
            elif a == "--json" and i + 1 < len(args):
                body = args[i + 1].encode()
                headers["content-type"] = "application/json"
                headers.setdefault("accept", "application/json")
                i += 2
            elif a in ("-A", "--user-agent") and i + 1 < len(args):
                headers["user-agent"] = args[i + 1]
                i += 2
            elif a in ("-f", "--fail"):
                fail = True
                i += 1
            elif a in ("-L", "--location"):
                follow = True
                i += 1
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
            elif a in _SILENT_NOOP:
                i += 1
            elif a in _REFUSED:
                return CommandResult(
                    exit_code=2,
                    stderr=(
                        f"ws-curl: {a} is not supported here; use real curl "
                        f"where the shell provides it ({_SUPPORTED})\n"
                    ),
                )
            elif a in _REFUSED_VALUED and i + 1 < len(args):
                return CommandResult(
                    exit_code=2,
                    stderr=(
                        f"ws-curl: {a} is not supported here; use real curl "
                        f"where the shell provides it ({_SUPPORTED})\n"
                    ),
                )
            elif not a.startswith("-"):
                url = a
                i += 1
            else:
                return CommandResult(
                    exit_code=2,
                    stderr=f"ws-curl: unknown flag {a} ({_SUPPORTED})\n",
                )

        if url is None:
            return CommandResult(
                exit_code=2, stderr=f"ws-curl: no URL ({_SUPPORTED})\n"
            )
        origin = runtime.config.origin
        from urllib.parse import urlsplit as _urlsplit

        origin_host = (
            _urlsplit(origin).hostname
            if origin.startswith(("http://", "https://"))
            else None
        )
        was_origin = url.startswith(("http://", "https://"))
        url = _strip_origin(url, origin_host)
        if url is None:
            # The single most expensive discovery an agent can make by
            # trial and error — say it outright instead (curl exit 6:
            # could not resolve host).
            return CommandResult(
                exit_code=6,
                stderr="ws-curl: external URLs are unreachable — this command "
                "dispatches only into the workspace app (try: ws-curl "
                "$APP_ORIGIN/api/...). The workspace has no internet access; "
                "BROWSER-side code may load scripts from the CDN "
                f"allowlist ({', '.join(runtime.config.script_hosts)}).\n",
            )
        notes: list[str] = []
        if not was_origin:
            # Bare paths keep working for one release, noisily: the
            # canonical form names the origin.
            notes.append(
                f"ws-curl: prefer {origin}/api/... over bare paths (deprecated)"
            )
        explicit_method = method is not None
        if method is None:
            method = "POST" if body else "GET"

        hops = 0
        while True:
            resp = runtime.dispatch(
                make_request(method, url, body=body, headers=headers)
            )
            # The agent's next move after curl is `tail api.log`, and this
            # call is already inside a tool call that will checkpoint — so
            # buffered request lines can go out now (see _flush_if_free).
            runtime.flush_log()
            if not follow or resp.status not in (301, 302, 303, 307, 308) or hops >= 5:
                break
            location = resp.headers.get("location")
            if not location:
                break
            nxt = _resolve_redirect(url, location, origin_host)
            if nxt is None:
                return CommandResult(
                    exit_code=6,
                    stderr="ws-curl: redirect outside the workspace app is "
                    "unreachable — the app has no internet access.\n",
                )
            url = nxt
            if (
                resp.status in (301, 302, 303)
                and method == "POST"
                and not explicit_method
            ):
                # curl's convention: an inferred POST (from -d) becomes
                # a GET on these statuses; only explicit -X POST — and
                # always 307/308 — resend the body.
                method = "GET"
                body = b""
            hops += 1

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

        if resp.status >= 400 and fail:
            # --fail honored (real curl's convention): without it the
            # response reads as a response, status line and all.
            err = f"HTTP {resp.status}"
            stderr = "\n".join([err, *notes]) if notes else err
            return CommandResult(exit_code=22, stderr=stderr + "\n")
        if notes:
            # Exit-0 stderr rides the transcript on both rungs (termish
            # merges it; the dud guest re-emits it), so the deprecation
            # teaches exactly where the old habit runs. Terminated: on
            # the dud rung the transcript merges both streams, and an
            # unterminated line would glue onto whatever prints next.
            return CommandResult(exit_code=0, stderr="\n".join(notes) + "\n")
        return None

    curl.__doc__ = (
        "Test your app's endpoints without a server: "
        "ws-curl [-X METHOD] [-d BODY] [-i] [-o FILE] [-w '%{http_code}'] URL "
        "(e.g. ws-curl $APP_ORIGIN/api/scores?limit=3)"
    )
    # Tags OUR registrations: like ws-git's tag, the dud ferry only
    # fronts the framework command under this name.
    curl._nontainer_wscurl = True
    return curl
