"""The apps extra: agent-authored backends + frontends, testable
without a server. Design: docs/apps.md. M1 = dispatch + curl (pure
Python, no extra deps); M2 adds test_app (Playwright); M3 adds the
live router (Starlette).

Usage::

    from nontainer import workspace
    from nontainer.apps import enable_apps

    ws = workspace("user-42")
    runtime = enable_apps(ws)   # registers the `curl` terminal builtin

    # agent writes /app/api/scores.py handlers via its tools, then:
    ws.terminal("curl /api/scores?limit=3 | jq .")

    # embedders can dispatch directly:
    from nontainer.apps import request
    resp = runtime.dispatch(request("GET", "/api/scores?limit=3"))
"""

from .contract import (
    HttpError,
    Request,
    Response,
    WireResponse,
    make_request,
    normalize,
)
from .dispatch import AppRuntime, AppsConfig, enable_apps, request
from .testapp import ActionResult, TestAppResult, render_test_app

__all__ = [
    "AppRuntime",
    "AppsConfig",
    "enable_apps",
    "request",
    "Request",
    "Response",
    "HttpError",
    "WireResponse",
    "make_request",
    "normalize",
    "TestAppResult",
    "ActionResult",
    "render_test_app",
]
