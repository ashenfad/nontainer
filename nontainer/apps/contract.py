"""The handler contract: Request, Response, HttpError, liberal returns.

Handlers are agent-authored files under ``/app/api/``; see
docs/apps.md. These classes cross the sandbox boundary: ``Request``
rides in via the ``inputs=`` channel (it is plain picklable data),
``Response``/``HttpError`` are registered in the handler sandbox's
policy so agent code can construct/raise them.
"""

from __future__ import annotations

import json as _json
from dataclasses import dataclass, field
from typing import Any


class HttpError(Exception):
    """Raise inside a handler for a clean error response."""

    def __init__(self, status: int, message: str = "") -> None:
        self.status = int(status)
        self.message = message
        super().__init__(f"{status}: {message}")


@dataclass(frozen=True)
class Request:
    """One HTTP-shaped request. Plain picklable data by design."""

    method: str
    path: str
    params: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    json: Any = None
    """Parsed body when it parses as JSON (populated at construction
    by :func:`make_request`); ``None`` otherwise."""

    def require(self, name: str, typ: type = str) -> Any:
        """Fetch ``name`` from the JSON body (preferred) or query
        params; raise ``HttpError(400)`` when missing or mistyped."""
        value: Any = None
        if isinstance(self.json, dict) and name in self.json:
            value = self.json[name]
        elif name in self.params:
            value = self.params[name]
            if typ is not str:
                try:
                    value = typ(value)
                except (TypeError, ValueError):
                    value = None
        if value is None:
            raise HttpError(400, f"missing required field: {name!r}")
        if not isinstance(value, typ):
            raise HttpError(
                400, f"field {name!r} must be {typ.__name__}"
            )
        return value


def make_request(
    method: str,
    url: str,
    *,
    body: bytes = b"",
    headers: dict[str, str] | None = None,
) -> Request:
    """Build a Request from a method + url-with-query, parsing the
    body as JSON eagerly (host-side — handlers never need a json
    module for it)."""
    from urllib.parse import parse_qsl, urlsplit

    parts = urlsplit(url)
    params = {k: v for k, v in parse_qsl(parts.query)}
    parsed: Any = None
    if body:
        try:
            parsed = _json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            parsed = None
    return Request(
        method=method.upper(),
        path=parts.path,
        params=params,
        headers=dict(headers or {}),
        body=body,
        json=parsed,
    )


@dataclass(frozen=True)
class Response:
    """Full-control return type; most handlers return dict/str/bytes."""

    status: int = 200
    body: Any = None
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class WireResponse:
    """A normalized response: status + content bytes + content type.
    What dispatch hands to its consumers (curl / test_app / router)."""

    status: int
    content: bytes
    content_type: str
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 400


def normalize(value: Any) -> WireResponse:
    """Liberal returns: dict/list → JSON, str → text, bytes → blob,
    Response → as specified, None → 204."""
    if isinstance(value, Response):
        inner = normalize(value.body) if value.body is not None else WireResponse(
            204, b"", "text/plain"
        )
        return WireResponse(
            status=value.status,
            content=inner.content,
            content_type=value.headers.get("content-type", inner.content_type),
            headers=dict(value.headers),
        )
    if value is None:
        return WireResponse(204, b"", "text/plain")
    if isinstance(value, (dict, list)):
        return WireResponse(
            200, _json.dumps(value).encode(), "application/json"
        )
    if isinstance(value, str):
        return WireResponse(200, value.encode(), "text/plain; charset=utf-8")
    if isinstance(value, bytes):
        return WireResponse(200, value, "application/octet-stream")
    raise TypeError(
        f"handler returned {type(value).__name__}; return dict/list/str/"
        "bytes/Response or raise HttpError"
    )
