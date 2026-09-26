"""The handler contract: Request, Response, HttpError, liberal returns.

Handlers are agent-authored files under ``/app/api/``; see
docs/apps.md. These classes cross the sandbox boundary: ``Request``
rides in via the ``inputs=`` channel (it is plain picklable data),
``Response``/``HttpError`` are registered in the handler sandbox's
policy so agent code can construct/raise them.

The encoding of a handler's return runs inside the sandbox too (see
``nt__Encoder``), so this module is loaded wherever handlers run,
including sandboxes with no nontainer installed. It imports only the
standard library at module level, and must keep to that.
"""

from __future__ import annotations

import datetime as _dt
import json as _json
import math as _math
import operator as _operator
import sys as _sys
from dataclasses import dataclass, field
from decimal import Decimal as _Decimal
from itertools import islice as _islice
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
        params; raise ``HttpError(400)`` when missing or mistyped.

        Coercion is liberal-in and symmetric across both sources:
        strings coerce through ``typ`` (so a query param and a JSON
        string behave alike; bool accepts ``true/1/false/0``), and
        numerics follow JSON's single number type — an int passes for
        ``float``, an integral float for ``int``. bools are never
        numbers (JSON ``true`` is not a valid ``int``)."""
        if isinstance(self.json, dict) and name in self.json:
            value = self.json[name]
        elif name in self.params:
            value = self.params[name]
        else:
            raise HttpError(400, f"missing required field: {name!r}")
        if value is None:  # JSON null ≙ absent
            raise HttpError(400, f"missing required field: {name!r}")
        ok, coerced = _coerce(value, typ)
        if not ok:
            raise HttpError(400, f"field {name!r} must be {typ.__name__}")
        return coerced


_BOOL_STRINGS = {"true": True, "1": True, "false": False, "0": False}


def _coerce(value: Any, typ: type) -> tuple[bool, Any]:
    """``(ok, coerced)`` for :meth:`Request.require` — see its
    docstring for the rules."""
    if isinstance(value, str) and typ is not str:
        if typ is bool:  # bool("false") is True; map words instead
            b = _BOOL_STRINGS.get(value.lower())
            return b is not None, b
        try:
            return True, typ(value)
        except (TypeError, ValueError):
            return False, None
    if typ in (int, float) and isinstance(value, bool):
        return False, None
    if isinstance(value, typ):
        return True, value
    if typ is float and isinstance(value, int):
        return True, float(value)
    if typ is int and isinstance(value, float) and value.is_integer():
        return True, int(value)
    return False, None


_HEADER_ALLOW = frozenset({"content-type", "accept", "authorization", "user-agent"})


def filter_headers(raw: Any) -> dict[str, str]:
    """The allowlisted-subset rule from the Request contract: standard
    content/auth headers plus any ``x-*`` custom header, lowercased.
    Hop-by-hop and ambient-credential headers (host, cookie, ...) never
    reach handlers."""
    out: dict[str, str] = {}
    for k, v in dict(raw or {}).items():
        lk = str(k).lower()
        if lk in _HEADER_ALLOW or lk.startswith("x-"):
            out[lk] = str(v)
    return out


_RESPONSE_HEADER_ALLOW = frozenset(
    {
        "content-type",
        "cache-control",
        "vary",
        "etag",
        "last-modified",
        "content-disposition",
        "location",
    }
)
"""``vary`` belongs with ``cache-control`` and cannot be split from it.
A handler may vary its output on an allowlisted REQUEST header —
``authorization`` or an ``x-*`` custom such as a tenant id — and serving
a cacheable response without naming what it varied on lets a shared
cache key on the URL alone and hand one caller's variant to the next."""


_RESPONSE_HEADER_DENY = frozenset(
    {
        "x-frame-options",
        "x-sendfile",
        "x-lighttpd-send-file",
    }
)
_RESPONSE_HEADER_DENY_PREFIXES = ("x-accel-",)
"""The ``x-`` namespace is not only custom app metadata, so allowing it
wholesale would readmit two classes of header the allowlist exists to
keep out: security directives (``x-frame-options``), and commands a
reverse proxy EXECUTES on the response's behalf. nginx acts on
``x-accel-redirect`` from an upstream unless configured not to, serving
an internal location the caller could not request directly;
``x-sendfile`` is the same mechanism in Apache and lighttpd.

This cannot be complete — it covers the conventions that exist, and a
proxy with its own is beyond what this library can see. An embedder
fronting the router with one is the only party who can tell it to
ignore them."""


def filter_response_headers(raw: Any) -> dict[str, str]:
    """The headers a handler may put on the wire: representation and
    caching metadata plus any ``x-*`` custom header, lowercased.

    Handler code is agent-authored and runs on the embedder's serving
    origin, so the headers that grant privileges on that origin are not
    the app's to set. Dropped, with the concrete abuse each one is:
    ``set-cookie`` (planting a cookie on the serving origin),
    ``access-control-allow-*`` (granting other origins read access to
    responses the embedder isolated), ``content-security-policy`` (an
    app relaxing its own containment), framing controls such as
    ``x-frame-options`` (opting into or out of embedding the embedder
    decided), and proxy commands such as ``x-accel-redirect`` (reaching
    an internal location through the server in front). An embedder who
    needs one of these wraps the mountable router in middleware of their
    own; that keeps the app unable to reach them at all."""
    out: dict[str, str] = {}
    for k, v in dict(raw or {}).items():
        lk = str(k).lower()
        if lk in _RESPONSE_HEADER_DENY or lk.startswith(_RESPONSE_HEADER_DENY_PREFIXES):
            continue
        if lk in _RESPONSE_HEADER_ALLOW or lk.startswith("x-"):
            out[lk] = str(v)
    return out


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
        # Lowercased: header names are case-insensitive, and a handler
        # (or the encoder reading ``accept``) looks them up by one
        # spelling whichever caller built the request.
        headers={str(k).lower(): v for k, v in dict(headers or {}).items()},
        body=body,
        json=parsed,
    )


@dataclass(frozen=True)
class Response:
    """Full-control return type; most handlers return dict/str/bytes."""

    status: int = 200
    body: Any = None
    headers: dict[str, str] = field(default_factory=dict)
    """HTTP headers are case-insensitive: keys here may be any casing
    (``Content-Type`` and ``content-type`` alike); :func:`normalize`
    lowercases them on the way to the wire, and
    :func:`filter_response_headers` drops the ones an app may not set
    on the serving origin."""


#: What a handler may name without importing it. Dispatch binds these
#: into the handler's globals for the length of a request, so a handler
#: reads them the way it reads a builtin; every path that runs handler
#: code outside a request has to bind the same three, or the handler
#: means something different depending on who called it.
HANDLER_CONTRACT = (Request, Response, HttpError)


@dataclass(frozen=True)
class WireResponse:
    """A normalized response: status + content bytes + content type.
    What dispatch hands to its consumers (curl / test_app / router)."""

    status: int
    content: bytes
    content_type: str
    headers: dict[str, str] = field(default_factory=dict)
    """Lowercased keys (canonicalized by :func:`normalize`), so
    consumers can look up / merge headers without case games."""

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 400


def normalize(value: Any, accept: str | None = None) -> WireResponse:
    """Liberal returns: dict/list → JSON, str → text, bytes → blob, a
    table → JSON rows or Arrow as ``accept`` negotiates, Response → as
    specified, None → 204. Raises ``TypeError`` for a return it refuses,
    naming what was wrong. A table asked for as Arrow where pyarrow
    cannot be imported answers 406, or JSON rows when the request
    accepts JSON too, as dispatch does."""
    try:
        status, content_type, headers, content = _response_parts(value, accept)
    except _NotAcceptable as e:
        return WireResponse(
            406, error_body(str(e)), "application/json", {"vary": "Accept"}
        )
    return WireResponse(status, content, content_type, headers)


def _response_parts(
    value: Any, accept: str | None = None, notes: list[str] | None = None
) -> tuple[int, str, dict[str, str], bytes]:
    """``(status, content_type, headers, content)`` for one handler
    return, by the liberal-return rules; ``accept`` is the request's
    ``Accept`` header, which only a table return consults. Raises
    ``TypeError`` for a return it refuses, and :class:`_NotAcceptable`
    for a table asked for in a format this sandbox cannot produce.
    Anything the encoding did that the handler's author should hear
    about, without the response failing, is appended to ``notes``."""
    if isinstance(value, Response):
        vary = None
        if value.body is None:
            content_type, content = "text/plain", b""
        else:
            _, content_type, body_headers, content = _response_parts(
                value.body, accept, notes
            )
            vary = body_headers.get("vary")
        # Lowercase the agent-supplied header keys: HTTP headers are
        # case-insensitive, and agents type the idiomatic Content-Type —
        # a cased lookup would silently ignore it. Tolerate an explicit
        # headers=None (agents write it; the field default is {}).
        headers = {str(k).lower(): str(v) for k, v in (value.headers or {}).items()}
        if vary is not None:
            headers["vary"] = _add_vary(headers.get("vary"), vary)
        return (
            _status(value.status, "Response"),
            headers.get("content-type", content_type),
            headers,
            content,
        )
    if value is None:
        return 204, "text/plain", {}, b""
    if isinstance(value, (dict, list)):
        return 200, "application/json", {}, encode_json(value)
    if isinstance(value, str):
        return 200, "text/plain; charset=utf-8", {}, value.encode()
    if isinstance(value, bytes):
        return 200, "application/octet-stream", {}, bytes(value)
    kind = _table_kind(value)
    if kind is not None:
        return _table_parts(value, kind, accept, notes)
    hint = _REFUSAL_HINTS.get(type(value).__name__)
    raise TypeError(
        f"handler returned {type(value).__name__}; "
        + (
            hint
            or "return dict/list/str/bytes, a table (DataFrame, Series, Arrow), "
            "a Response, or raise HttpError"
        )
    )


def _add_vary(existing: str | None, name: str) -> str:
    """``existing`` (a ``Vary`` value) with ``name`` added unless it is
    already there, or the value is ``*``, which varies on everything."""
    if not existing or not existing.strip():
        return name
    present = {part.strip().lower() for part in existing.split(",")}
    if "*" in present or name.lower() in present:
        return existing
    return f"{existing}, {name}"


def _status(value: Any, source: str) -> int:
    """``value`` as an HTTP status, or ``TypeError``. Anything that is
    an integer by ``__index__`` passes (a numpy integer from a lookup
    table is one); a bool never does."""
    status = None
    if not isinstance(value, bool):
        try:
            status = _operator.index(value)
        except TypeError:
            pass
    if status is None or not 100 <= status <= 599:
        raise TypeError(
            f"{source} status must be an integer from 100 to 599, got {value!r}"
        )
    return int(status)


def error_body(message: str, **extra: str) -> bytes:
    """The body of an error response: JSON, because model-written
    frontends call ``res.json()`` unconditionally, and a plain-text
    error cascades into a second, misleading ``SyntaxError`` in the app
    console. JSON keeps their catch blocks working
    (``{"error": ..., ...}``)."""
    return _json.dumps({"error": message, **extra}).encode()


# -- the response wire --------------------------------------------------
#
# A handler's return is encoded where the handler ran, and what crosses
# back to the host is one of four tuples of primitives:
#
#   (WIRE_RESPONSE, status: int, content_type: str,
#    headers: dict[str, str], body: bytes)
#   (WIRE_NOTED_RESPONSE, status: int, content_type: str,
#    headers: dict[str, str], body: bytes, note: str)
#   (WIRE_REFUSED, message: str)
#   (WIRE_NOT_ACCEPTABLE, message: str)
#
# The first element names the shape and its version. A noted response
# is served like any other, and its note, something the handler's
# author should hear about (JSON sent in place of Arrow), goes to the
# handler log; it is flat, like the others, so every executor carries
# it the way it carries a plain response. A refusal is a
# return the liberal-return rules reject (a set, a status out of range,
# a body over the size limits); its message is the one ``normalize``
# raises with, and the host answers it 500. Not-acceptable is a table
# the request asked for as Arrow in a sandbox that cannot import
# pyarrow; the host answers it 406. The host reads nothing else from
# the execution's namespace, and checks every element's exact type
# before using it.
#
# Everything the encoding needs lives in this module, and this module
# imports only the standard library at module level: an executor whose
# sandbox has no nontainer installed (a VM guest) builds it from this
# file's source. numpy, pandas and pyarrow are imported only when a
# value of theirs is being encoded, so a sandbox holding none of them
# never needs them.

WIRE_RESPONSE = "nt-response/1"
WIRE_REFUSED = "nt-refused/1"
WIRE_NOT_ACCEPTABLE = "nt-not-acceptable/1"
WIRE_NOTED_RESPONSE = "nt-noted-response/1"


class nt__Encoder:
    """The response encoder dispatch binds into a handler's sandbox.

    Dispatch appends a trailer to the handler source that calls the
    verb function and passes its return to :meth:`respond` (or, when it
    raises ``HttpError``, the status and message to :meth:`error`), so
    the encoding runs in the same place on every executor and only the
    wire tuple leaves the sandbox. Bound the way the contract classes
    are, under a name in the reserved ``nt__`` space, rather than
    imported: an import from handler code is refused by the sandbox
    policy, and granting it would let every handler import this module.

    Calling it from handler code grants nothing: it produces the same
    tuple the trailer does, which the host validates like any other,
    size limits included."""

    @staticmethod
    def respond(
        value: Any,
        accept: str | None = None,
        *,
        text_limit: int | None = None,
        binary_limit: int | None = None,
        carry_limit: int | None = None,
    ) -> tuple:
        """The wire tuple for one handler return.

        ``accept`` is the request's ``Accept`` header, which decides
        whether a table answers as JSON rows or an Arrow stream. The
        limits are the response-size caps (see :func:`size_refusal`):
        a body over them is refused here, before its bytes leave the
        sandbox. ``None`` is no limit."""
        notes: list[str] = []
        try:
            status, content_type, headers, content = _response_parts(
                value, accept, notes
            )
        except _NotAcceptable as e:
            return (WIRE_NOT_ACCEPTABLE, str(e))
        except TypeError as e:
            return (WIRE_REFUSED, str(e))
        refusal = size_refusal(
            len(content), content_type, text_limit, binary_limit, carry_limit
        )
        if refusal is not None:
            return (WIRE_REFUSED, refusal)
        if notes:
            note = "; ".join(notes)
            return (WIRE_NOTED_RESPONSE, status, content_type, headers, content, note)
        return (WIRE_RESPONSE, status, content_type, headers, content)

    @staticmethod
    def error(
        status: Any,
        message: Any,
        *,
        text_limit: int | None = None,
        binary_limit: int | None = None,
        carry_limit: int | None = None,
    ) -> tuple:
        """The wire tuple for an ``HttpError``: its status, with the
        message in the JSON error body every error response carries.
        The body is held to the same size caps as any response, so an
        oversized message is refused before it leaves the sandbox.

        The status must be an error: 4xx or 5xx. ``HttpError`` coerces
        with ``int()``, which lets ``HttpError("404")`` through but would
        also turn ``HttpError(201.9)`` into a success; a response that
        succeeds is a ``Response``, so anything below 400 is refused."""
        try:
            code = _status(status, "HttpError")
        except TypeError as e:
            return (WIRE_REFUSED, str(e))
        if code < 400:
            return (
                WIRE_REFUSED,
                f"HttpError status must be 400 to 599, got {status!r}; "
                "return Response(status=...) for anything else",
            )
        body = error_body(str(message))
        refusal = size_refusal(
            len(body),
            "application/json",
            text_limit,
            binary_limit,
            carry_limit,
            advise=False,
        )
        if refusal is not None:
            return (WIRE_REFUSED, f"HttpError message too large: {refusal}; shorten it")
        return (WIRE_RESPONSE, code, "application/json", {}, body)


# -- response size ------------------------------------------------------
#
# Two caps, chosen by whether the body is text: a JSON (or other text)
# response and a binary one. They exist to catch a handler returning
# something runaway, and are applied where the body is encoded, so an
# oversized one never crosses the sandbox boundary. An executor that
# can carry only so much back from one execution adds a third, lower
# bound of its own (the carry limit); the effective cap is the smallest
# that applies, and the refusal names the one that bound.

_TEXT_TYPE_MARKERS = ("json", "xml", "javascript", "ecmascript")


def is_text_type(content_type: str) -> bool:
    """Whether a response's media type says its body is text.

    The media type is the server's own statement about the body, and
    it is the only honest one: sniffing the bytes classifies a binary
    payload that happens to be valid UTF-8 -- one holding NUL bytes,
    or any ASCII-armored blob -- as text. ``text/*`` is text, so are
    the structured types that are text on the wire (JSON, XML,
    JavaScript, including the ``+json`` / ``+xml`` suffix forms), and
    nothing else is -- an absent or ``application/octet-stream`` type
    is binary.
    """
    kind = content_type.split(";", 1)[0].strip().lower()
    if not kind:
        return False
    if kind.startswith("text/"):
        return True
    subtype = kind.partition("/")[2]
    return any(marker in subtype for marker in _TEXT_TYPE_MARKERS)


def _size(n: int) -> str:
    """A byte count for a message, in decimal units."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}".rstrip("0").rstrip(".") + " MB"
    if n >= 1_000:
        return f"{n / 1_000:.1f}".rstrip("0").rstrip(".") + " kB"
    return f"{n} bytes"


def _smaller(a: int | None, b: int | None) -> int | None:
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


def size_refusal(
    size: int,
    content_type: str,
    text_limit: int | None = None,
    binary_limit: int | None = None,
    carry_limit: int | None = None,
    *,
    advise: bool = True,
) -> str | None:
    """The message refusing a body of ``size`` bytes, or ``None`` when
    it is within the cap for its kind.

    ``text_limit`` applies to a text body (:func:`is_text_type`) and
    ``binary_limit`` to any other; ``carry_limit`` is how much the
    executor can carry back, which lowers either. ``None`` is no limit,
    and a body exactly at its cap passes. With ``advise``, the message
    says what a handler can do instead."""
    text = is_text_type(content_type)
    configured = text_limit if text else binary_limit
    limit = _smaller(configured, carry_limit)
    if limit is None or size <= limit:
        return None
    media = content_type.split(";", 1)[0].strip().lower()
    if "json" in media.partition("/")[2]:
        subject = "JSON response"
    elif media == ARROW_STREAM:
        subject = "Arrow response"
    elif media:
        subject = f"{media} response"
    else:
        subject = "Response"
    shown = _size(size)
    if shown == _size(limit):
        shown = f"{size:,} bytes"
    if carry_limit is not None and carry_limit == limit and carry_limit != configured:
        over = f"over the {_size(limit)} this executor can carry"
    elif text:
        over = f"over the {_size(limit)} limit for text"
    else:
        over = f"over the {_size(limit)} limit for binary responses"
    message = f"{subject} is {shown}, {over}"
    if not advise:
        return message
    if text:
        arrow = _smaller(binary_limit, carry_limit)
        cap = f" (limit {_size(arrow)})" if arrow is not None else ""
        return (
            f"{message}: aggregate or paginate on the server, or return a "
            f"table and request Arrow{cap}"
        )
    return f"{message}: filter or paginate on the server"


# -- table returns ------------------------------------------------------
#
# A table is a pandas DataFrame, a pandas Series (one column), a
# pyarrow Table, or any object exposing ``__arrow_c_stream__`` (polars,
# a DuckDB relation, ...). Returned as the whole response, it answers
# as an Arrow IPC stream when the request's Accept asks for one, and as
# JSON rows (``[{column: value, ...}, ...]``) otherwise; the response
# carries ``Vary: Accept`` either way. Nested anywhere inside a dict or
# list return, it encodes as its JSON rows.
#
# The index follows one rule on both paths, decided by
# ``_keeps_index``: an index that only numbers the rows is dropped, and
# any other becomes columns the way ``reset_index()`` names them (the
# index's name, or ``index``; one column per level of a MultiIndex).
# Row numbers are any RangeIndex (whatever its start, step or name) and
# an unnamed index of integer dtype. The rule is applied with pandas,
# so JSON rows never need pyarrow, and the Arrow path converts the same
# frame with ``preserve_index=False``, so both carry the same columns.
#
# pyarrow is needed for Arrow output, and for reading an
# ``__arrow_c_stream__`` object at all. Where it cannot be imported, an
# Arrow request that also accepts JSON gets JSON rows, with a note for
# the handler log saying why; one that accepts only Arrow answers 406.
# A stream object asked for as JSON is refused. Each says so.

ARROW_STREAM = "application/vnd.apache.arrow.stream"

ARROW_UNAVAILABLE = (
    "Arrow was requested, but pyarrow is not available to this app's "
    "handlers: install pyarrow where they run, or request JSON"
)

ARROW_FELL_BACK = (
    "Arrow was requested, but pyarrow is not available to this app's "
    "handlers, so JSON was sent (the request accepts JSON too): install "
    "pyarrow where they run to serve Arrow"
)


class _NotAcceptable(Exception):
    """The request asked for a format this sandbox cannot produce."""


def _import_pyarrow() -> Any:
    """The pyarrow module, or ``None`` where it cannot be imported."""
    try:
        import pyarrow
        import pyarrow.ipc  # noqa: F401
    except ImportError:
        return None
    return pyarrow


def _table_kind(value: Any) -> str | None:
    """``"pandas"``, ``"arrow"`` or ``"stream"`` for a table, else
    ``None``. A pandas or pyarrow value can exist only once its library
    is imported, so the check reads ``sys.modules`` instead of
    importing either."""
    pd = _sys.modules.get("pandas")
    if pd is not None and isinstance(value, (pd.DataFrame, pd.Series)):
        return "pandas"
    pa = _sys.modules.get("pyarrow")
    if pa is not None and isinstance(value, (pa.Table, pa.RecordBatch)):
        return "arrow"
    if hasattr(type(value), "__arrow_c_stream__"):
        return "stream"
    return None


def _keeps_index(index: Any) -> bool:
    """Whether a frame's index carries data, and so becomes columns.

    The name decides. A named index is data and becomes a column:
    ``groupby("year")`` keeps ``year``, and ``set_index("id")`` keeps
    ``id`` even where pandas stores consecutive ids as a named
    RangeIndex. An unnamed index is data unless it is row positions:
    an unnamed RangeIndex or integer index (what slicing and boolean
    filters leave behind) is dropped, and any other unnamed index
    (dates, strings, floats) becomes a column called ``index``. A
    MultiIndex keeps every level.

    pyarrow's default (``preserve_index=None``) differs in two places:
    it drops a named RangeIndex, and keeps an unnamed integer index as
    ``__index_level_0__``. The first loses a column the author named;
    the second ships row positions no page wants."""
    import pandas as pd

    if isinstance(index, pd.MultiIndex):
        return True
    if index.name is not None:
        return True
    return not (
        isinstance(index, pd.RangeIndex) or pd.api.types.is_integer_dtype(index.dtype)
    )
    if isinstance(index, pd.RangeIndex):
        return False
    if index.name is None and pd.api.types.is_integer_dtype(index.dtype):
        return False
    return True


def _pandas_frame(value: Any) -> Any:
    """A DataFrame or Series as the frame both output paths encode:
    a Series as one column (named after it, or ``0`` if unnamed, as
    ``to_frame()`` names it), its index kept as columns or dropped by
    :func:`_keeps_index`. Either way the result has a default index,
    which neither path encodes."""
    import pandas as pd

    name = type(value).__name__
    frame = value.to_frame() if isinstance(value, pd.Series) else value
    if not _keeps_index(frame.index):
        frame = frame.reset_index(drop=True)
    else:
        try:
            frame = frame.reset_index()
        except ValueError as e:
            raise _Unencodable(
                f"{name} index cannot become a column ({e}); rename the index "
                "or the column it collides with"
            ) from None
    if isinstance(frame.columns, pd.MultiIndex):
        raise _Unencodable(
            f"{name} has MultiIndex columns, which have no row form; flatten "
            "them to single names first"
        )
    if not frame.columns.is_unique:
        dupes = sorted({str(c) for c in frame.columns[frame.columns.duplicated()]})
        raise _Unencodable(
            f"{name} has duplicate column names ({', '.join(dupes)}); rename them"
        )
    # Distinct labels can still meet once converted: 1 and "1" are one
    # JSON key, and a row would silently keep only one of the two values.
    # Arrow names fields by str(), which can collide differently (a
    # Timestamp label is ISO as a JSON key but not as a str).
    for kind, key_of in (("JSON key", _json_key), ("Arrow field name", str)):
        seen: dict[str, Any] = {}
        for label in frame.columns:
            key = key_of(label)
            if key is None:
                continue
            if key in seen:
                raise _Unencodable(
                    f"{name} columns {seen[key]!r} and {label!r} both become the "
                    f"{kind} {key!r}; rename one"
                )
            seen[key] = label
    return frame


def _json_key(label: Any) -> str | None:
    """The object key ``label`` becomes in JSON, or ``None`` when it has
    none (encoding the rows refuses that label with its own message)."""
    try:
        key = _clean_key(label)
    except _Unencodable:
        return None
    return next(iter(_json.loads(_FINAL.encode({key: 0}))))


def _arrow_table(value: Any, kind: str, pa: Any) -> Any:
    """A pyarrow value or Arrow stream object as a ``pa.Table``."""
    name = type(value).__name__
    try:
        table = value if kind == "arrow" else pa.table(value)
    except (pa.ArrowException, TypeError, ValueError) as e:
        raise _Unencodable(
            f"{name} exposes __arrow_c_stream__ but does not read as a table: {e}"
        ) from None
    if isinstance(table, pa.RecordBatch):
        table = pa.Table.from_batches([table])
    names = table.column_names
    if len(set(names)) != len(names):
        dupes = sorted({n for n in names if names.count(n) > 1})
        raise _Unencodable(
            f"{name} has duplicate column names ({', '.join(dupes)}); rename them"
        )
    return table


def _needs_pyarrow(value: Any) -> str:
    return (
        f"{type(value).__name__} is an Arrow stream, and reading one needs "
        "pyarrow, which is not available to this app's handlers: install "
        "pyarrow where they run, or return a pandas DataFrame"
    )


def _table_rows(value: Any, kind: str) -> list:
    """A table as JSON rows: one dict per row, column name to value.
    The values are left for the JSON encoder to convert."""
    if kind == "pandas":
        return _pandas_frame(value).to_dict("records")
    pa = _import_pyarrow()
    if pa is None:
        raise _Unencodable(_needs_pyarrow(value))
    return _arrow_table(value, kind, pa).to_pylist()


def _table_ipc(value: Any, kind: str, pa: Any) -> bytes:
    """A table as an Arrow IPC stream."""
    if kind == "pandas":
        frame = _pandas_frame(value)
        try:
            table = pa.Table.from_pandas(frame, preserve_index=False)
        except (pa.ArrowException, TypeError, ValueError) as e:
            raise _Unencodable(
                f"{type(value).__name__} could not be converted to Arrow: {e}"
            ) from None
    else:
        table = _arrow_table(value, kind, pa)
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


def _table_parts(
    value: Any, kind: str, accept: str | None, notes: list[str] | None = None
) -> tuple[int, str, dict[str, str], bytes]:
    """``_response_parts`` for a table returned as the whole response."""
    headers = {"vary": "Accept"}
    try:
        if accepts_arrow(accept):
            pa = _import_pyarrow()
            if pa is not None:
                return 200, ARROW_STREAM, headers, _table_ipc(value, kind, pa)
            if not accepts_json(accept):
                raise _NotAcceptable(ARROW_UNAVAILABLE)
            if notes is not None:
                notes.append(ARROW_FELL_BACK)
        rows = _table_rows(value, kind)
    except _Unencodable as e:
        raise TypeError(e.message) from None
    return 200, "application/json", headers, encode_json(rows)


def _media_ranges(accept: str) -> list[tuple[str, str, float]]:
    """``(type, subtype, q)`` for each well-formed media range in an
    ``Accept`` value, lowercased. A range whose q is not a number from
    0 to 1 is left out."""
    out = []
    for part in accept.split(","):
        fields = part.split(";")
        media = fields[0].strip().lower()
        mtype, slash, subtype = media.partition("/")
        mtype, subtype = mtype.strip(), subtype.strip()
        if not slash or not mtype or not subtype:
            continue
        q: float | None = 1.0
        for param in fields[1:]:
            key, _, raw = param.partition("=")
            if key.strip().lower() != "q":
                continue
            try:
                q = float(raw.strip().strip('"'))
            except ValueError:
                q = None
            if q is not None and not 0.0 <= q <= 1.0:
                q = None
            break
        if q is not None:
            out.append((mtype, subtype, q))
    return out


def _quality(ranges: list[tuple[str, str, float]], mtype: str, subtype: str):
    """``(q, specificity)`` the most specific matching range gives a
    media type: 2 for an exact match, 1 for ``type/*``, 0 for ``*/*``,
    ``(None, -1)`` when nothing matches."""
    best: tuple[float | None, int] = (None, -1)
    for t, st, q in ranges:
        if t == mtype and st == subtype:
            level = 2
        elif t == mtype and st == "*":
            level = 1
        elif t == "*" and st == "*":
            level = 0
        else:
            continue
        if level > best[1] or (level == best[1] and q > (best[0] or 0.0)):
            best = (q, level)
    return best


def accepts_arrow(accept: str | None) -> bool:
    """Whether an ``Accept`` header asks for an Arrow IPC stream.

    Only by name: the Arrow type must be listed explicitly with a q
    above 0 (``*/*`` and ``application/*`` do not ask for it), and not
    ranked below JSON, whose q comes from its most specific matching
    range. An absent header asks for JSON."""
    if not accept:
        return False
    ranges = _media_ranges(accept)
    q_arrow, level = _quality(ranges, "application", ARROW_STREAM.partition("/")[2])
    if level != 2 or not q_arrow:
        return False
    q_json, _ = _quality(ranges, "application", "json")
    return q_json is None or q_arrow >= q_json


def accepts_json(accept: str | None) -> bool:
    """Whether an ``Accept`` header explicitly accepts JSON: its most
    specific range covering ``application/json`` (the type itself,
    ``application/*`` or ``*/*``) has a q above 0. An absent header
    accepts anything, but says nothing explicitly, so it is False."""
    if not accept:
        return False
    q_json, _ = _quality(_media_ranges(accept), "application", "json")
    return bool(q_json)


# -- JSON encoding of handler returns -----------------------------------
#
# Handler returns are built by data code, so they carry numpy scalars,
# pandas timestamps and NaN as often as plain Python values. Encoding
# rules, applied at any depth:
#
#   numpy bool/integer/floating scalar  -> the native Python value
#   numpy array                         -> nested lists
#   datetime / date / time / Timestamp  -> ISO 8601 string (offset kept)
#   numpy datetime64                    -> ISO 8601 string
#   timedelta / Timedelta / timedelta64 -> total seconds (float)
#   Decimal                             -> number (float)
#   NaN / +Inf / -Inf, NaT, pd.NA, None -> null
#   tuple                               -> list
#   a table (DataFrame, Series, Arrow)  -> its JSON rows (see above)
#   set, anything else                  -> refused, naming the path
#
# Plain data takes the stdlib C encoder in one pass. A value it cannot
# take (a NaN, which ``json.dumps`` writes as the non-JSON token
# ``NaN`` unless ``allow_nan=False`` makes it raise; a non-str dict
# key; an unknown type) sends the whole value through a Python walk
# that rebuilds only the containers whose contents change, then
# encodes the result. Both passes are linear, so the slow case costs
# at most a constant factor over the fast one: 100k five-field records
# encode in ~40ms (the same as bare ``json.dumps``), and ~160ms with a
# NaN in the last record.

_REFUSAL_HINTS = {
    "set": "its order is unstable; return sorted(...)",
    "frozenset": "its order is unstable; return sorted(...)",
    "bytes": "decode it, or return the bytes themselves with a content-type",
}


class _Unencodable(Exception):
    """A value with no JSON encoding. ``path`` collects the keys and
    indexes leading to it, innermost first, as the exception unwinds
    through the containers holding it."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message
        self.path: list[str] = []


def _refuse(value: Any) -> _Unencodable:
    name = type(value).__name__
    hint = _REFUSAL_HINTS.get(name)
    suffix = f" ({hint})" if hint else ""
    return _Unencodable(f"{name} is not JSON-encodable{suffix}")


def _convert(value: Any) -> Any:
    """The JSON-native stand-in for one value the stdlib encoder does
    not accept, or :class:`_Unencodable`. The result may itself be a
    container still holding such values (an object array's
    ``tolist()``); callers encode it recursively.

    numpy and pandas are recognized by the module their types come
    from, so a return holding neither never imports them."""
    module = type(value).__module__
    if module == "numpy":
        import numpy as np

        if isinstance(value, np.ndarray):
            if value.dtype.kind == "M":
                text = np.datetime_as_string(value)
                return np.where(np.isnat(value), None, text).tolist()
            if value.dtype.kind == "m":
                return (value / np.timedelta64(1, "s")).tolist()
            return value.tolist()
        if isinstance(value, np.datetime64):
            return None if np.isnat(value) else str(np.datetime_as_string(value))
        if isinstance(value, np.timedelta64):
            if np.isnat(value):
                return None
            return float(value / np.timedelta64(1, "s"))
        if isinstance(value, (np.bool_, np.integer, np.floating)):
            native = value.item()
            # An extended-precision float (longdouble on x86) has no
            # lossless native equivalent, so .item() returns it
            # unchanged; the consumer reads a double either way.
            if isinstance(value, np.floating) and not isinstance(native, float):
                native = float(value)
            if isinstance(native, float) and not _math.isfinite(native):
                return None
            return native
        raise _refuse(value)
    if module == "pandas" or module.startswith("pandas."):
        import pandas as pd

        if value is pd.NaT or value is pd.NA:
            return None
    if isinstance(value, float):
        return value if _math.isfinite(value) else None
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return value.isoformat()
    # Total seconds rather than an ISO 8601 duration: a JavaScript
    # consumer adds seconds to a Date with arithmetic, while parsing
    # "P1DT2H" needs a library.
    if isinstance(value, _dt.timedelta):
        seconds = value.total_seconds()
        return seconds if _math.isfinite(seconds) else None
    # A float rather than a string: the consumer is JavaScript, which
    # parses any JSON number to a double, so a string would only move
    # the same rounding into every caller.
    if isinstance(value, _Decimal):
        number = float(value)
        return number if _math.isfinite(number) else None
    kind = _table_kind(value)
    if kind is not None:
        return _table_rows(value, kind)
    raise _refuse(value)


# The fast pass converts through ``default=``; the stdlib encoder
# re-encodes what it returns, so a NaN inside a converted value (an
# array's ``tolist()``) still raises and reaches the walk.
_FAST = _json.JSONEncoder(allow_nan=False, default=_convert)
_FINAL = _json.JSONEncoder(allow_nan=False)


def _clean_key(key: Any) -> Any:
    # A non-finite float key becomes null like a non-finite value, which
    # the encoder writes as the key "null".
    if isinstance(key, float):
        return float(key) if _math.isfinite(key) else None
    if isinstance(key, (str, int)) or key is None:
        return key
    try:
        converted = _convert(key)
    except _Unencodable:
        converted = key
    if isinstance(converted, (str, int, float)) or converted is None:
        return converted
    raise _Unencodable(f"dict key of type {type(key).__name__} is not JSON-encodable")


def _clean(value: Any, active: set[int]) -> Any:
    """``value`` with every non-JSON-native leaf replaced, returning
    the original object wherever nothing under it changed."""
    kind = type(value)
    if kind is str or kind is int or kind is bool or value is None:
        return value
    if kind is float:
        return value if _math.isfinite(value) else None
    if isinstance(value, dict):
        return _clean_dict(value, active)
    if isinstance(value, (list, tuple)):
        return _clean_list(value, active)
    if isinstance(value, (str, int)):
        return value
    return _clean(_convert(value), active)


def _clean_dict(value: dict, active: set[int]) -> dict:
    ident = id(value)
    if ident in active:
        raise _Unencodable("circular reference")
    active.add(ident)
    out: dict | None = None
    for i, (key, item) in enumerate(value.items()):
        new_key = _clean_key(key)
        try:
            new_item = _clean(item, active)
        except _Unencodable as e:
            e.path.append(_key_segment(key))
            raise
        if out is None and (new_key is not key or new_item is not item):
            out = dict(_islice(value.items(), i))
        if out is not None:
            out[new_key] = new_item
    active.discard(ident)
    return value if out is None else out


def _clean_list(value: list | tuple, active: set[int]) -> list | tuple:
    ident = id(value)
    if ident in active:
        raise _Unencodable("circular reference")
    active.add(ident)
    out: list | None = None
    for i, item in enumerate(value):
        try:
            new_item = _clean(item, active)
        except _Unencodable as e:
            e.path.append(f"[{i}]")
            raise
        if out is None and new_item is not item:
            out = list(value[:i])
        if out is not None:
            out.append(new_item)
    active.discard(ident)
    return value if out is None else out


def _key_segment(key: Any) -> str:
    if isinstance(key, str) and key.isidentifier():
        return f".{key}"
    return f"[{_json.dumps(key) if isinstance(key, str) else repr(key)}]"


def encode_json(value: Any) -> bytes:
    """Encode a handler's dict/list return as JSON bytes by the rules
    above. Raises ``TypeError`` naming the path to the first value it
    refuses, e.g. ``$.rows[3].when: DataFrame is not JSON-encodable``."""
    try:
        return _FAST.encode(value).encode()
    except (TypeError, ValueError, _Unencodable):
        pass
    except RecursionError:
        raise TypeError("return value is nested too deeply to encode as JSON") from None
    try:
        cleaned = _clean(value, set())
    except _Unencodable as e:
        raise TypeError(f"${''.join(reversed(e.path))}: {e.message}") from None
    except RecursionError:
        raise TypeError("return value is nested too deeply to encode as JSON") from None
    try:
        return _FINAL.encode(cleaned).encode()
    except ValueError as e:
        # The walk leaves nothing the strict encoder refuses; if it ever
        # does, it surfaces as the refusal dispatch reports, not a crash.
        raise TypeError(f"return value is not JSON-encodable: {e}") from None
