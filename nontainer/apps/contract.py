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


def normalize(value: Any) -> WireResponse:
    """Liberal returns: dict/list → JSON, str → text, bytes → blob,
    Response → as specified, None → 204. Raises ``TypeError`` for a
    return it refuses, naming what was wrong."""
    status, content_type, headers, content = _response_parts(value)
    return WireResponse(status, content, content_type, headers)


def _response_parts(value: Any) -> tuple[int, str, dict[str, str], bytes]:
    """``(status, content_type, headers, content)`` for one handler
    return, by the liberal-return rules. Raises ``TypeError`` for a
    return it refuses."""
    if isinstance(value, Response):
        if value.body is None:
            content_type, content = "text/plain", b""
        else:
            _, content_type, _, content = _response_parts(value.body)
        # Lowercase the agent-supplied header keys: HTTP headers are
        # case-insensitive, and agents type the idiomatic Content-Type —
        # a cased lookup would silently ignore it. Tolerate an explicit
        # headers=None (agents write it; the field default is {}).
        headers = {str(k).lower(): str(v) for k, v in (value.headers or {}).items()}
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
    hint = _REFUSAL_HINTS.get(type(value).__name__)
    raise TypeError(
        f"handler returned {type(value).__name__}; "
        + (hint or "return dict/list/str/bytes/Response or raise HttpError")
    )


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
# back to the host is one of two tuples of primitives:
#
#   (WIRE_RESPONSE, status: int, content_type: str,
#    headers: dict[str, str], body: bytes)
#   (WIRE_REFUSED, message: str)
#
# The first element names the shape and its version. A refusal is a
# return the liberal-return rules reject (a set, a DataFrame, a status
# out of range); its message is the one ``normalize`` raises with. The
# host reads nothing else from the execution's namespace, and checks
# every element's exact type before using it.
#
# Everything the encoding needs lives in this module, and this module
# imports only the standard library at module level: an executor whose
# sandbox has no nontainer installed (a VM guest) builds it from this
# file's source. numpy and pandas are imported only when a value of
# theirs is being encoded, so a sandbox holding neither never needs
# them.

WIRE_RESPONSE = "nt-response/1"
WIRE_REFUSED = "nt-refused/1"


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
    tuple the trailer does, which the host validates like any other."""

    @staticmethod
    def respond(value: Any) -> tuple:
        """The wire tuple for one handler return."""
        try:
            status, content_type, headers, content = _response_parts(value)
        except TypeError as e:
            return (WIRE_REFUSED, str(e))
        return (WIRE_RESPONSE, status, content_type, headers, content)

    @staticmethod
    def error(status: Any, message: Any) -> tuple:
        """The wire tuple for an ``HttpError``: its status, with the
        message in the JSON error body every error response carries.

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
        return (WIRE_RESPONSE, code, "application/json", {}, error_body(str(message)))


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
    "DataFrame": 'return .to_dict("records"), or bytes with a content-type',
    "Series": "return .tolist() or .to_dict()",
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
