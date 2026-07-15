"""The apps extra: agent-authored backends + frontends, testable
without a server. Design: docs/apps.md. Dispatch + curl are pure
Python; test_app needs Playwright; the live router needs Starlette.

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

from .browser import configure_browser, shutdown_browser
from .contract import (
    HttpError,
    Request,
    Response,
    WireResponse,
    make_request,
    normalize,
)
from .dispatch import (
    DEFAULT_SCRIPT_HOSTS,
    AppRuntime,
    AppsConfig,
    enable_apps,
    request,
)
from .testapp import ActionResult, TestAppResult, arun_test_app, render_test_app


def __getattr__(name):
    # Lazy: serving needs the optional starlette dependency.
    if name in ("build_router", "mint_token"):
        from . import serve

        return getattr(serve, name)
    raise AttributeError(name)


__all__ = [
    "AppRuntime",
    "AppsConfig",
    "DEFAULT_SCRIPT_HOSTS",
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
    "arun_test_app",
    "configure_browser",
    "shutdown_browser",
    "build_router",
    "mint_token",
]
