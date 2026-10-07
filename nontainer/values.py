"""Typed values: checked against a declared type, and carried across a
boundary in a form that only ever decodes into that type.

A harness that hands values between the host and agent code (a task's
inputs and its result, a delegate's answer) needs three things, and
this module is all three for one declared type, compiled once with
:meth:`Spec.of`:

- **What it needs carried** (:attr:`Spec.kinds`): plain data, bytes, a
  table, an array, a live object, or anything. Data, bytes, tables and
  arrays can cross a process or machine boundary; a live object (a
  client, a callable) can't.
- **A strict check** (:meth:`Spec.check`) of a value built in this
  process: no coercion (``"42"`` is not an ``int``, ``True`` is not an
  ``int``, an ``int`` passes for a ``float``), and a record is checked
  field by field, not only by class. A container longer than
  :data:`SAMPLE_ABOVE` is checked on a sample of its items (the first,
  the last, and some between, chosen the same way on every run); pass
  ``full=True`` to check every one. A table or an array is checked by
  its class, never row by row.
- **A decoding** (:meth:`Spec.decode`) of what :func:`encode` made,
  which builds the declared type and nothing else. Pickle rebuilds
  whatever the bytes ask for; this never does. Every check above holds
  for a decoded value too, on every item: a decoder builds every item
  anyway.

:func:`encode` needs no type: it turns a value into a JSON tree, with
the values JSON lacks (bytes, tables as Arrow IPC streams, arrays as
``.npy``) as numbered binary parts its leaves point to, tagged with
``"$nt"``. The format is :data:`FORMAT`; :meth:`Encoded.to_bytes` puts
a value in one blob.

The types a spec understands: ``None``, ``bool``, ``int``, ``float``,
``str``, ``bytes``, the ``datetime`` family, ``Decimal``, ``UUID``,
paths, enums, ``Literal``, unions, lists, tuples, sets, dicts and their
abstract counterparts (``Sequence``, ``Mapping``, ...), dataclasses,
NamedTuples, TypedDicts and pydantic models (by their own
``model_validate``), pandas, pyarrow and polars tables, numpy arrays,
and as live objects, callables, iterators and any other class.
Anything else is refused with :class:`Unsupported` when the spec is
compiled, rather than passed unchecked.

This module imports only the standard library at module level, so a
sandbox without nontainer installed (a VM guest) can run it from its
source. numpy, pandas and pyarrow are imported only when a value of
theirs is encoded or decoded, and pydantic is never imported at all.
"""

from __future__ import annotations

import collections.abc as abc
import dataclasses
import datetime
import decimal
import enum
import inspect
import io
import json
import math
import pathlib
import random
import struct
import sys
import types
import typing
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import Any, Literal, Union

__all__ = [
    "FORMAT",
    "SAMPLE",
    "SAMPLE_ABOVE",
    "Encoded",
    "Kind",
    "Malformed",
    "Mismatch",
    "Spec",
    "TooLarge",
    "Unencodable",
    "Unsupported",
    "check",
    "decode",
    "encode",
    "fmt",
    "kinds_of",
]

FORMAT = "nt-value/1"
"""The encoding's name and version, at the head of every blob."""

TAG = "$nt"
"""The key marking a JSON object in an encoded tree as one of the
encoding's own values rather than a dict."""

SAMPLE_ABOVE = 10_000
"""Past this many items, a container is checked on a sample."""

SAMPLE = 10
"""How many items a sample takes from each of a container's start, its
end, and between."""

Kind = Literal["data", "bytes", "table", "array", "live", "any"]
"""What a type needs carried."""

Names = Union[Mapping[str, Any], None]
"""Names to resolve annotations with, beside the module a type was
defined in: those of a function that defined it, say."""


# -- errors -------------------------------------------------------------------------


class Mismatch(TypeError):
    """A value that doesn't fit its declared type. ``where`` is the path
    to the part that doesn't (``scores[3].total``), empty at the top."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail
        self._path: list[str] = []

    def at(self, segment: str) -> Mismatch:
        """Record that the mismatch is inside ``segment``; for the
        container a mismatch passes through on its way out."""
        self._path.insert(0, segment)
        return self

    @property
    def where(self) -> str:
        return "".join(self._path).lstrip(".")

    def __str__(self) -> str:
        return f"at {self.where}: {self.detail}" if self._path else self.detail


class Unsupported(TypeError):
    """An annotation no spec can be built for, or one naming something
    that can't be resolved."""


class Unencodable(TypeError):
    """A value with no encoding: a live object, an object array, a
    cycle."""


class TooLarge(ValueError):
    """An encoding over the size it was allowed."""


class Malformed(ValueError):
    """A blob that is not an encoded value."""


def _got(value: Any) -> str:
    name = type(value).__name__
    if isinstance(value, (bool, int, float, str)) and not isinstance(value, enum.Enum):
        shown = repr(value)
        return f"{name} {shown if len(shown) <= 40 else shown[:37] + '...'}"
    if type(value) is dict and TAG in value:
        return f"an encoded {value[TAG]}"
    return name


def _expected(label: str, value: Any) -> Mismatch:
    return Mismatch(f"expected {label}, got {_got(value)}")


# -- formatting a type --------------------------------------------------------------


def fmt(tp: Any) -> str:
    """``tp`` written the way code would write it: ``list[Score]``,
    ``int | None``."""
    if tp is Any or tp is inspect.Parameter.empty:
        return "Any"
    if tp is None or tp is type(None):
        return "None"
    origin = typing.get_origin(tp)
    if origin is not None:
        args = typing.get_args(tp)
        if origin is typing.Annotated:
            return fmt(args[0])
        if origin is Union or origin is types.UnionType:
            return " | ".join(fmt(a) for a in args)
        if origin is Literal:
            return f"Literal[{', '.join(map(repr, args))}]"
        name = getattr(origin, "__name__", None) or repr(origin).replace("typing.", "")
        parts = []
        for arg in args:
            if isinstance(arg, list):
                parts.append("[" + ", ".join(fmt(a) for a in arg) + "]")
            else:
                parts.append("..." if arg is Ellipsis else fmt(arg))
        return f"{name}[{', '.join(parts)}]" if parts else name
    if isinstance(tp, type):
        return tp.__name__
    return repr(tp).replace("typing.", "")


# -- resolving names ----------------------------------------------------------------


def _scope(tp: Any, names: Names) -> dict[str, Any]:
    """The names a type's own annotations resolve with: its module's,
    then ``names``."""
    module = sys.modules.get(getattr(tp, "__module__", "") or "")
    scope = dict(vars(module)) if module is not None else {}
    scope.update(names or {})
    return scope


def _evaluate(ref: str | typing.ForwardRef, names: Names) -> Any:
    """What an annotation left as text names. Python 3.10's
    ``get_type_hints`` leaves the text inside a built-in generic
    (``list["Tree"]``) as it is."""
    text = ref.__forward_arg__ if isinstance(ref, typing.ForwardRef) else ref
    try:
        return eval(text, dict(names or {}))  # noqa: S307 - an annotation's own text
    except Exception as error:
        raise Unsupported(
            f"annotation {text!r} can't be resolved ({type(error).__name__}: {error})"
        ) from None


def _hints(cls: type, names: Names) -> dict[str, Any]:
    try:
        return typing.get_type_hints(cls, localns=dict(names) if names else None)
    except Exception as error:
        raise Unsupported(
            f"{cls.__qualname__}'s annotations can't be resolved "
            f"({type(error).__name__}: {error})"
        ) from None


# -- what kind of thing a type or a value is ------------------------------------------


def _root(tp: type) -> str:
    return (getattr(tp, "__module__", "") or "").split(".")[0]


def _table_name(tp: type) -> str | None:
    """``pandas.DataFrame`` for a table class, ``None`` for any other."""
    root = _root(tp)
    if root == "pandas" and tp.__name__ in ("DataFrame", "Series"):
        return f"pandas.{tp.__name__}"
    if root == "pyarrow" and tp.__name__ in ("Table", "RecordBatch"):
        return f"pyarrow.{tp.__name__}"
    if root == "polars" and tp.__name__ == "DataFrame":
        return "polars.DataFrame"
    if hasattr(tp, "__arrow_c_stream__"):
        return f"{tp.__module__}.{tp.__qualname__}"
    return None


def _is_array(tp: type) -> bool:
    return _root(tp) == "numpy" and tp.__name__ == "ndarray"


def _is_model(tp: Any) -> bool:
    """A pydantic model, known by its interface rather than imported."""
    return (
        isinstance(tp, type)
        and hasattr(tp, "model_fields")
        and hasattr(tp, "model_validate")
    )


def _is_namedtuple(tp: Any) -> bool:
    return isinstance(tp, type) and issubclass(tp, tuple) and hasattr(tp, "_fields")


def _is_record(tp: Any) -> bool:
    return isinstance(tp, type) and (
        dataclasses.is_dataclass(tp)
        or _is_model(tp)
        or _is_namedtuple(tp)
        or typing.is_typeddict(tp)
    )


# -- sampling -------------------------------------------------------------------------


def _indices(n: int, full: bool) -> Sequence[int]:
    """Which of ``n`` items a check looks at."""
    if full or n <= SAMPLE_ABOVE:
        return range(n)
    between = random.Random(n).sample(range(SAMPLE, n - SAMPLE), SAMPLE)
    return [*range(SAMPLE), *sorted(between), *range(n - SAMPLE, n)]


# -- the nodes a spec is compiled into ------------------------------------------------


class _Node:
    """One declared type: how to check a value of it, and how to build
    one from an encoded tree."""

    kinds: frozenset[str] = frozenset({"data"})

    @property
    def label(self) -> str:
        raise NotImplementedError

    def children(self) -> Iterator[_Node]:
        return iter(())

    def check(self, value: Any, full: bool) -> None:
        raise NotImplementedError

    def build(self, tree: Any, parts: Sequence[bytes]) -> Any:
        raise NotImplementedError


class _Any(_Node):
    kinds = frozenset({"any"})
    label = "Any"

    def check(self, value: Any, full: bool) -> None:
        pass

    def build(self, tree: Any, parts: Sequence[bytes]) -> Any:
        """Plain data, as JSON and the encoding's own data tags hold it:
        a table or an array needs its type declared to come back."""
        kind = type(tree)
        if kind is list:
            out = []
            for i, item in enumerate(tree):
                try:
                    out.append(self.build(item, parts))
                except Mismatch as m:
                    raise m.at(f"[{i}]") from None
            return out
        if kind is dict:
            tag = tree.get(TAG)
            if tag is None:
                return {k: self._item(k, v, parts) for k, v in tree.items()}
            if tag == "float":
                return _special_float(tree)
            if tag == "bytes":
                return _part(tree, parts)
            if tag == "timedelta":
                return _timedelta(tree)
            if tag == "number":
                return self.build(tree.get("v"), parts)
            if tag == "map":
                return _pairs(tree, parts, self, self)
            if tag in ("table", "array"):
                raise Mismatch(
                    f"an encoded {tag} where Any is declared: declare its type"
                )
            raise Mismatch(f"unknown encoded value {tag!r}")
        if tree is None or kind in (bool, int, float, str):
            return tree
        raise _expected("JSON", tree)

    def _item(self, key: str, value: Any, parts: Sequence[bytes]) -> Any:
        try:
            return self.build(value, parts)
        except Mismatch as m:
            raise m.at(f"[{key!r}]") from None


_ANY = _Any()


class _None(_Node):
    label = "None"

    def check(self, value: Any, full: bool) -> None:
        if value is not None:
            raise _expected("None", value)

    def build(self, tree: Any, parts: Sequence[bytes]) -> Any:
        self.check(tree, True)
        return None


class _Bool(_Node):
    label = "bool"

    def check(self, value: Any, full: bool) -> None:
        if type(value) is not bool:
            raise _expected("bool", value)

    def build(self, tree: Any, parts: Sequence[bytes]) -> Any:
        self.check(tree, True)
        return tree


class _Int(_Node):
    """``int``, or a subclass of it other than ``bool``: an ``IntEnum``
    member is an ``int``, and is encoded as one."""

    def __init__(self, cls: type = int) -> None:
        self.cls = cls

    @property
    def label(self) -> str:
        return self.cls.__name__

    def check(self, value: Any, full: bool) -> None:
        if not isinstance(value, self.cls) or isinstance(value, bool):
            raise _expected(self.label, value)

    def build(self, tree: Any, parts: Sequence[bytes]) -> Any:
        if type(tree) is not int:
            raise _expected(self.label, tree)
        return tree if self.cls is int else self.cls(tree)


class _Float(_Node):
    """``float``; an ``int`` passes for one, and decodes as a ``float``."""

    label = "float"

    def check(self, value: Any, full: bool) -> None:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise _expected("float", value)

    def build(self, tree: Any, parts: Sequence[bytes]) -> Any:
        kind = type(tree)
        if kind is float or kind is int:
            return float(tree)
        if kind is dict and tree.get(TAG) == "float":
            return _special_float(tree)
        raise _expected("float", tree)


class _Str(_Node):
    def __init__(self, cls: type = str) -> None:
        self.cls = cls

    @property
    def label(self) -> str:
        return self.cls.__name__

    def check(self, value: Any, full: bool) -> None:
        if not isinstance(value, self.cls):
            raise _expected(self.label, value)

    def build(self, tree: Any, parts: Sequence[bytes]) -> Any:
        if type(tree) is not str:
            raise _expected(self.label, tree)
        return tree if self.cls is str else self.cls(tree)


class _Bytes(_Node):
    kinds = frozenset({"bytes"})

    def __init__(self, cls: type = bytes) -> None:
        self.cls = cls

    @property
    def label(self) -> str:
        return self.cls.__name__

    def check(self, value: Any, full: bool) -> None:
        if not isinstance(value, self.cls):
            raise _expected(self.label, value)

    def build(self, tree: Any, parts: Sequence[bytes]) -> Any:
        if type(tree) is not dict or tree.get(TAG) != "bytes":
            raise _expected(self.label, tree)
        data = _part(tree, parts)
        return data if self.cls is bytes else self.cls(data)


class _Parsed(_Node):
    """A scalar the encoding writes as a string: a datetime, a date, a
    time, a ``Decimal``, a ``UUID``, a path."""

    def __init__(self, cls: type, parse: Callable[[str], Any]) -> None:
        self.cls = cls
        self.parse = parse

    @property
    def label(self) -> str:
        return self.cls.__name__

    def check(self, value: Any, full: bool) -> None:
        if not isinstance(value, self.cls) or (
            # a datetime is a date, but not where a date is declared
            self.cls is datetime.date and isinstance(value, datetime.datetime)
        ):
            raise _expected(self.label, value)

    def build(self, tree: Any, parts: Sequence[bytes]) -> Any:
        if type(tree) is not str:
            raise _expected(self.label, tree)
        try:
            return self.parse(tree)
        except (ValueError, decimal.InvalidOperation) as error:
            raise Mismatch(f"expected {self.label}, got {tree!r} ({error})") from None


class _Timedelta(_Node):
    label = "timedelta"

    def check(self, value: Any, full: bool) -> None:
        if not isinstance(value, datetime.timedelta):
            raise _expected("timedelta", value)

    def build(self, tree: Any, parts: Sequence[bytes]) -> Any:
        if type(tree) is not dict or tree.get(TAG) != "timedelta":
            raise _expected("timedelta", tree)
        return _timedelta(tree)


class _Enum(_Node):
    def __init__(self, cls: type[enum.Enum]) -> None:
        self.cls = cls

    @property
    def label(self) -> str:
        return self.cls.__name__

    def check(self, value: Any, full: bool) -> None:
        if not isinstance(value, self.cls):
            raise _expected(self.label, value)

    def build(self, tree: Any, parts: Sequence[bytes]) -> Any:
        try:
            return self.cls(_ANY.build(tree, parts))
        except (ValueError, TypeError):
            raise Mismatch(
                f"expected {self.label}, got {_got(tree)}: not one of its values"
            ) from None


class _Literal(_Node):
    def __init__(self, values: tuple[Any, ...]) -> None:
        self.values = values

    @property
    def label(self) -> str:
        return f"Literal[{', '.join(map(repr, self.values))}]"

    def _match(self, value: Any) -> Any:
        for allowed in self.values:
            if value is allowed or (type(value) is type(allowed) and value == allowed):
                return allowed
        return _NO

    def check(self, value: Any, full: bool) -> None:
        if self._match(value) is _NO:
            raise _expected(self.label, value)

    def build(self, tree: Any, parts: Sequence[bytes]) -> Any:
        try:
            # the encoding's own scalars (bytes, a non-finite float) first
            value = _ANY.build(tree, parts)
        except Mismatch:
            raise _expected(self.label, tree) from None
        for allowed in self.values:
            if isinstance(allowed, enum.Enum):
                if type(value) is type(allowed.value) and value == allowed.value:
                    return allowed
            elif type(value) is type(allowed) and value == allowed:
                return allowed
        raise _expected(self.label, tree)


_NO = object()


class _Union(_Node):
    def __init__(self, options: list[_Node]) -> None:
        self.options = options

    @property
    def kinds(self) -> frozenset[str]:  # type: ignore[override]
        return frozenset()

    @property
    def label(self) -> str:
        return " | ".join(o.label for o in self.options)

    def children(self) -> Iterator[_Node]:
        return iter(self.options)

    def check(self, value: Any, full: bool) -> None:
        for option in self.options:
            try:
                option.check(value, full)
                return
            except Mismatch:
                continue
        raise _expected(self.label, value)

    def build(self, tree: Any, parts: Sequence[bytes]) -> Any:
        for option in self.options:
            try:
                return option.build(tree, parts)
            except Mismatch:
                continue
        raise _expected(self.label, tree)


class _Seq(_Node):
    """A list, or an abstract sequence (decoded as a list)."""

    def __init__(self, cls: type, item: _Node) -> None:
        self.cls = cls
        self.item = item

    @property
    def label(self) -> str:
        return f"{self.cls.__name__}[{self.item.label}]"

    def children(self) -> Iterator[_Node]:
        return iter((self.item,))

    def check(self, value: Any, full: bool) -> None:
        if not isinstance(value, self.cls) or isinstance(
            value, (str, bytes, bytearray)
        ):
            raise _expected(self.label, value)
        check = self.item.check
        for i in _indices(len(value), full):
            try:
                check(value[i], full)
            except Mismatch as m:
                raise m.at(f"[{i}]") from None

    def build(self, tree: Any, parts: Sequence[bytes]) -> Any:
        if type(tree) is not list:
            raise _expected(self.label, tree)
        return _items(tree, parts, self.item)


class _Tuple(_Node):
    """A tuple of fixed shape, or of any length (``tuple[int, ...]``)."""

    def __init__(self, items: list[_Node] | None, rest: _Node | None) -> None:
        self.items = items
        self.rest = rest

    @property
    def label(self) -> str:
        if self.rest is not None:
            return f"tuple[{self.rest.label}, ...]"
        return f"tuple[{', '.join(i.label for i in self.items or ())}]"

    def children(self) -> Iterator[_Node]:
        return iter(self.items if self.rest is None else (self.rest,))

    def check(self, value: Any, full: bool) -> None:
        if not isinstance(value, tuple):
            raise _expected(self.label, value)
        if self.rest is not None:
            for i in _indices(len(value), full):
                try:
                    self.rest.check(value[i], full)
                except Mismatch as m:
                    raise m.at(f"[{i}]") from None
            return
        items = self.items or []
        if len(value) != len(items):
            raise Mismatch(f"expected {self.label}, got a tuple of {len(value)}")
        for i, (item, node) in enumerate(zip(value, items)):
            try:
                node.check(item, full)
            except Mismatch as m:
                raise m.at(f"[{i}]") from None

    def build(self, tree: Any, parts: Sequence[bytes]) -> Any:
        if type(tree) is not list:
            raise _expected(self.label, tree)
        if self.rest is not None:
            return tuple(_items(tree, parts, self.rest))
        items = self.items or []
        if len(tree) != len(items):
            raise Mismatch(f"expected {self.label}, got {len(tree)} items")
        out = []
        for i, (item, node) in enumerate(zip(tree, items)):
            try:
                out.append(node.build(item, parts))
            except Mismatch as m:
                raise m.at(f"[{i}]") from None
        return tuple(out)


class _Set(_Node):
    def __init__(self, cls: type, item: _Node, make: Callable[[Any], Any]) -> None:
        self.cls = cls
        self.item = item
        self.make = make

    @property
    def label(self) -> str:
        return f"{self.cls.__name__}[{self.item.label}]"

    def children(self) -> Iterator[_Node]:
        return iter((self.item,))

    def check(self, value: Any, full: bool) -> None:
        if not isinstance(value, self.cls):
            raise _expected(self.label, value)
        items = value if full or len(value) <= SAMPLE_ABOVE else list(value)
        if items is value:
            for item in value:
                try:
                    self.item.check(item, full)
                except Mismatch as m:
                    raise m.at(f"[{item!r}]") from None
            return
        for i in _indices(len(items), full):
            try:
                self.item.check(items[i], full)
            except Mismatch as m:
                raise m.at(f"[{items[i]!r}]") from None

    def build(self, tree: Any, parts: Sequence[bytes]) -> Any:
        if type(tree) is not list:
            raise _expected(self.label, tree)
        try:
            return self.make(_items(tree, parts, self.item))
        except TypeError as error:
            raise Mismatch(f"expected {self.label}: {error}") from None


class _Map(_Node):
    """A dict, or an abstract mapping (decoded as a dict)."""

    def __init__(self, cls: type, key: _Node, value: _Node) -> None:
        self.cls = cls
        self.key = key
        self.value = value

    @property
    def label(self) -> str:
        return f"{self.cls.__name__}[{self.key.label}, {self.value.label}]"

    def children(self) -> Iterator[_Node]:
        return iter((self.key, self.value))

    def check(self, value: Any, full: bool) -> None:
        if not isinstance(value, self.cls):
            raise _expected(self.label, value)
        if full or len(value) <= SAMPLE_ABOVE:
            pairs: Sequence[tuple[Any, Any]] | Any = value.items()
        else:
            items = list(value.items())
            pairs = [items[i] for i in _indices(len(items), False)]
        for key, item in pairs:
            try:
                self.key.check(key, full)
                self.value.check(item, full)
            except Mismatch as m:
                raise m.at(f"[{key!r}]") from None

    def build(self, tree: Any, parts: Sequence[bytes]) -> Any:
        if type(tree) is not dict:
            raise _expected(self.label, tree)
        tag = tree.get(TAG)
        if tag == "map":
            return _pairs(tree, parts, self.key, self.value)
        if tag is not None:
            raise _expected(self.label, tree)
        out = {}
        for key, item in tree.items():
            try:
                out[self.key.build(key, parts)] = self.value.build(item, parts)
            except Mismatch as m:
                raise m.at(f"[{key!r}]") from None
        return out


@dataclasses.dataclass
class _Field:
    name: str
    node: _Node
    required: bool
    init: bool = True


class _Record(_Node):
    """A dataclass, a NamedTuple or a TypedDict: checked by its class (a
    TypedDict by its keys) and every field; built from a JSON object
    holding each field by name (a NamedTuple from an array too, which is
    how it is encoded)."""

    def __init__(self, cls: type) -> None:
        self.cls = cls
        self.alias: Any = None
        self.fields: list[_Field] = []
        self.typed_dict = typing.is_typeddict(cls)

    @property
    def label(self) -> str:
        return self.cls.__name__ if self.alias is None else fmt(self.alias)

    def children(self) -> Iterator[_Node]:
        return (f.node for f in self.fields)

    def check(self, value: Any, full: bool) -> None:
        if self.typed_dict:
            if not isinstance(value, dict):
                raise _expected(self.label, value)
            known = {f.name for f in self.fields}
            extra = [k for k in value if k not in known]
            if extra:
                raise Mismatch(f"{self.label} has no key {extra[0]!r}")
            for f in self.fields:
                if f.name not in value:
                    if f.required:
                        raise Mismatch(f"{self.label} is missing key {f.name!r}")
                    continue
                try:
                    f.node.check(value[f.name], full)
                except Mismatch as m:
                    raise m.at(f"[{f.name!r}]") from None
            return
        if not isinstance(value, self.cls):
            raise _expected(self.label, value)
        for f in self.fields:
            try:
                f.node.check(getattr(value, f.name), full)
            except Mismatch as m:
                raise m.at(f".{f.name}") from None

    def build(self, tree: Any, parts: Sequence[bytes]) -> Any:
        if type(tree) is list and _is_namedtuple(self.cls):
            if len(tree) > len(self.fields):
                raise Mismatch(f"expected {self.label}, got {len(tree)} items")
            tree = {f.name: item for f, item in zip(self.fields, tree)}
        if type(tree) is not dict or TAG in tree:
            raise _expected(self.label, tree)
        known = {f.name: f for f in self.fields}
        extra = [k for k in tree if k not in known]
        if extra:
            raise Mismatch(f"{self.label} has no field {extra[0]!r}")
        values: dict[str, Any] = {}
        for f in self.fields:
            if f.name not in tree:
                if f.required:
                    raise Mismatch(f"{self.label} is missing field {f.name!r}")
                continue
            if not f.init:
                continue  # set by the class itself, not passed in
            try:
                values[f.name] = f.node.build(tree[f.name], parts)
            except Mismatch as m:
                raise m.at(f".{f.name}") from None
        if self.typed_dict:
            return values
        try:
            return self.cls(**values)
        except Exception as error:
            raise Mismatch(
                f"{self.label} refused its fields ({type(error).__name__}: {error})"
            ) from None


class _Model(_Record):
    """A pydantic model: checked by its class (pydantic checked its
    fields when it was built), and built by its own ``model_validate``
    from fields decoded here."""

    def check(self, value: Any, full: bool) -> None:
        if not isinstance(value, self.cls):
            raise _expected(self.label, value)

    def build(self, tree: Any, parts: Sequence[bytes]) -> Any:
        if type(tree) is not dict or TAG in tree:
            raise _expected(self.label, tree)
        known = {f.name: f for f in self.fields}
        extra = [k for k in tree if k not in known]
        if extra:
            raise Mismatch(f"{self.label} has no field {extra[0]!r}")
        values: dict[str, Any] = {}
        for name, item in tree.items():
            try:
                values[name] = known[name].node.build(item, parts)
            except Mismatch as m:
                raise m.at(f".{name}") from None
        validate = self.cls.model_validate  # type: ignore[attr-defined]
        try:
            try:
                return validate(values, by_name=True)
            except TypeError:  # a pydantic without by_name
                return validate(values)
        except ValueError as error:
            raise Mismatch(
                f"{self.label} refused its fields: {_validation_summary(error)}"
            ) from None


def _validation_summary(error: ValueError) -> str:
    """pydantic's ``ValidationError`` on one line, known by its
    interface."""
    errors = getattr(error, "errors", None)
    if not callable(errors):
        return str(error)
    try:
        found = errors()
    except Exception:  # noqa: BLE001 - its own text, then
        return str(error)
    return "; ".join(
        f"{'.'.join(map(str, e.get('loc', ()))) or 'the value'}: {e.get('msg', '')}"
        for e in found[:5]
    )


class _Table(_Node):
    """A table, checked by its class; carried as an Arrow IPC stream."""

    kinds = frozenset({"table"})

    def __init__(self, cls: type, name: str) -> None:
        self.cls = cls
        self.name = name

    @property
    def label(self) -> str:
        return self.cls.__name__

    def check(self, value: Any, full: bool) -> None:
        if not isinstance(value, self.cls):
            raise _expected(self.label, value)

    def build(self, tree: Any, parts: Sequence[bytes]) -> Any:
        if type(tree) is not dict or tree.get(TAG) != "table":
            raise _expected(self.label, tree)
        if tree.get("of") != self.name:
            raise Mismatch(f"expected {self.label}, got an encoded {tree.get('of')}")
        return _read_table(self.name, tree, _part(tree, parts), parts)


class _Array(_Node):
    """A numpy array, checked by its class; carried as ``.npy``."""

    kinds = frozenset({"array"})
    label = "ndarray"

    def __init__(self, cls: type) -> None:
        self.cls = cls

    def check(self, value: Any, full: bool) -> None:
        if not isinstance(value, self.cls):
            raise _expected(self.label, value)

    def build(self, tree: Any, parts: Sequence[bytes]) -> Any:
        if type(tree) is not dict or tree.get(TAG) != "array":
            raise _expected(self.label, tree)
        data = _part(tree, parts)
        numpy = _library("numpy", "an ndarray")
        try:
            return numpy.load(io.BytesIO(data), allow_pickle=False)
        except Exception as error:  # noqa: BLE001 - whatever a bad .npy raises
            raise Mismatch(
                f"expected ndarray, got an unreadable array "
                f"({type(error).__name__}: {error})"
            ) from None


class _Live(_Node):
    """A live object: a callable, an iterator, a client. Checked here;
    it never crosses a boundary."""

    kinds = frozenset({"live"})

    def __init__(self, label: str, test: Callable[[Any], bool]) -> None:
        self._label = label
        self.test = test

    @property
    def label(self) -> str:
        return self._label

    def check(self, value: Any, full: bool) -> None:
        if not self.test(value):
            raise _expected(self.label, value)

    def build(self, tree: Any, parts: Sequence[bytes]) -> Any:
        raise Mismatch(f"a live {self.label} can't cross into this process")


# -- compiling a type into nodes ------------------------------------------------------

_SEQUENCES = {
    list: list,
    abc.Sequence: abc.Sequence,
    abc.MutableSequence: abc.MutableSequence,
}
_SETS: dict[Any, tuple[type, Callable[[Any], Any]]] = {
    set: (set, set),
    frozenset: (frozenset, frozenset),
    abc.Set: (abc.Set, set),
    abc.MutableSet: (abc.MutableSet, set),
}
_MAPS = {
    dict: dict,
    abc.Mapping: abc.Mapping,
    abc.MutableMapping: abc.MutableMapping,
}
_PARSED: dict[type, Callable[[str], Any]] = {
    datetime.datetime: datetime.datetime.fromisoformat,
    datetime.date: datetime.date.fromisoformat,
    datetime.time: datetime.time.fromisoformat,
    decimal.Decimal: decimal.Decimal,
    uuid.UUID: uuid.UUID,
}


def _compile(tp: Any, names: Names, memo: dict[Any, _Node]) -> _Node:
    if tp is Any or tp is object or tp is inspect.Parameter.empty:
        return _ANY
    if tp is None or tp is type(None):
        return _None()
    if isinstance(tp, typing.TypeVar):
        return _ANY
    if isinstance(tp, (str, typing.ForwardRef)):
        return _compile(_evaluate(tp, names), names, memo)
    if type(tp).__name__ == "TypeAliasType":  # ``type X = ...``, 3.12+
        return _compile(tp.__value__, names, memo)
    supertype = getattr(tp, "__supertype__", None)  # a NewType
    if supertype is not None:
        return _compile(supertype, names, memo)
    origin = typing.get_origin(tp)
    if origin is not None:
        return _compile_generic(tp, origin, typing.get_args(tp), names, memo)
    if not isinstance(tp, type):
        raise Unsupported(f"no spec can be built for {fmt(tp)}")
    return _compile_class(tp, names, memo)


def _compile_generic(
    tp: Any, origin: Any, args: tuple[Any, ...], names: Names, memo: dict[Any, _Node]
) -> _Node:
    if origin is typing.Annotated:
        return _compile(args[0], names, memo)
    if origin is Literal:
        return _Literal(args)
    if origin is Union or origin is types.UnionType:
        return _Union([_compile(a, names, memo) for a in args])
    if origin in _SEQUENCES:
        item = _compile(args[0], names, memo) if args else _ANY
        return _Seq(_SEQUENCES[origin], item)
    if origin is tuple:
        if len(args) == 2 and args[1] is Ellipsis:
            return _Tuple(None, _compile(args[0], names, memo))
        if args == ((),):  # tuple[()], on Pythons that spell it so
            args = ()
        return _Tuple([_compile(a, names, memo) for a in args], None)
    if origin in _SETS:
        cls, make = _SETS[origin]
        return _Set(cls, _compile(args[0], names, memo) if args else _ANY, make)
    if origin in _MAPS:
        key = _compile(args[0], names, memo) if args else _ANY
        value = _compile(args[1], names, memo) if len(args) > 1 else _ANY
        return _Map(_MAPS[origin], key, value)
    if origin is type:
        bound = args[0] if args and isinstance(args[0], type) else object
        return _Live(fmt(tp), lambda v: isinstance(v, type) and issubclass(v, bound))
    if origin is abc.Callable:
        return _Live(fmt(tp), callable)
    if _is_record(origin):
        return _compile_record(origin, names, memo, alias=tp, args=args)
    if isinstance(origin, type):
        return _Live(fmt(tp), lambda v: isinstance(v, origin))
    raise Unsupported(f"no spec can be built for {fmt(tp)}")


def _compile_class(tp: type, names: Names, memo: dict[Any, _Node]) -> _Node:
    if issubclass(tp, enum.Enum):
        return _Enum(tp)
    if tp is bool:
        return _Bool()
    if issubclass(tp, int):
        return _Int(tp)
    if tp is float:
        return _Float()
    if issubclass(tp, str):
        return _Str(tp)
    if issubclass(tp, (bytes, bytearray)):
        return _Bytes(tp)
    if tp is datetime.timedelta:
        return _Timedelta()
    if tp in _PARSED:
        return _Parsed(tp, _PARSED[tp])
    if issubclass(tp, pathlib.PurePath):
        return _Parsed(tp, tp)
    if tp in (list, tuple, set, frozenset, dict):
        return _compile_generic(tp, tp, (), names, memo)
    if _is_record(tp):
        return _compile_record(tp, names, memo)
    table = _table_name(tp)
    if table is not None:
        return _Table(tp, table)
    if _is_array(tp):
        return _Array(tp)
    if getattr(tp, "_is_protocol", False) and not getattr(
        tp, "_is_runtime_protocol", False
    ):
        raise Unsupported(
            f"{tp.__name__} is a Protocol without @runtime_checkable, so no "
            "value can be checked against it"
        )
    return _Live(tp.__name__, lambda v: isinstance(v, tp))


def _substitute(tp: Any, bound: Mapping[Any, Any]) -> Any:
    """``tp`` with the type variables ``bound`` names replaced, through
    typing's own substitution (``list[T]`` with ``T`` bound to ``int`` is
    ``list[int]``)."""
    if not bound:
        return tp
    if isinstance(tp, typing.TypeVar):
        return bound.get(tp, tp)
    params = getattr(tp, "__parameters__", None)
    if params and not isinstance(tp, type):
        try:
            return tp[tuple(bound.get(p, p) for p in params)]
        except TypeError:
            return tp
    return tp


def _compile_record(
    tp: type,
    names: Names,
    memo: dict[Any, _Node],
    *,
    alias: Any = None,
    args: tuple[Any, ...] = (),
) -> _Node:
    """A record's node; for a generic one given its arguments
    (``Box[int]``), with them bound in its fields' annotations."""
    key: Any = tp if alias is None else alias
    try:
        known = memo.get(key)
    except TypeError:  # an alias that can't be hashed
        key = id(key)
        known = memo.get(key)
    if known is not None:
        return known  # a record that holds itself
    node: _Record = _Model(tp) if _is_model(tp) else _Record(tp)
    node.alias = alias
    memo[key] = node
    bound = dict(zip(getattr(tp, "__parameters__", ()), args))
    scope = _scope(tp, names)
    if isinstance(node, _Model):
        for name, info in tp.model_fields.items():  # type: ignore[attr-defined]
            node.fields.append(
                _Field(
                    name,
                    _compile(_substitute(info.annotation, bound), scope, memo),
                    info.is_required(),
                )
            )
        return node
    hints = _hints(tp, names)
    if dataclasses.is_dataclass(tp):
        for f in dataclasses.fields(tp):
            required = (
                f.init
                and f.default is dataclasses.MISSING
                and f.default_factory is dataclasses.MISSING
            )
            node.fields.append(
                _Field(
                    f.name,
                    _compile(_substitute(hints[f.name], bound), scope, memo),
                    required,
                    f.init,
                )
            )
    elif _is_namedtuple(tp):
        defaults = getattr(tp, "_field_defaults", {})
        for name in tp._fields:  # type: ignore[attr-defined]
            node.fields.append(
                _Field(
                    name,
                    _compile(_substitute(hints.get(name, Any), bound), scope, memo),
                    name not in defaults,
                )
            )
    else:  # a TypedDict
        required = getattr(tp, "__required_keys__", frozenset(hints))
        for name, hint in hints.items():
            node.fields.append(
                _Field(
                    name,
                    _compile(_substitute(hint, bound), scope, memo),
                    name in required,
                )
            )
    return node


# -- the spec ------------------------------------------------------------------------


class Spec:
    """One declared type, compiled: what it needs carried, a strict check
    of a value against it, and a decoding into it."""

    def __init__(self, annotation: Any, root: _Node) -> None:
        self.annotation = annotation
        self._root = root
        kinds: set[str] = set()
        found: list[type] = []
        seen: set[int] = set()
        stack = [root]
        while stack:
            node = stack.pop()
            if id(node) in seen:
                continue
            seen.add(id(node))
            kinds |= node.kinds
            if isinstance(node, (_Record, _Enum)) and node.cls not in found:
                found.append(node.cls)
            stack.extend(node.children())
        self.kinds: frozenset[str] = frozenset(kinds)
        """What values of the type need carried."""
        self.types: tuple[type, ...] = tuple(found)
        """The record and enum classes the type names: what code needs at
        hand to build a value of it."""

    @classmethod
    def of(cls, annotation: Any, *, names: Names = None) -> Spec:
        """Compile ``annotation``. ``names`` resolve what it names beside
        its own module (the locals of a function that defined a type it
        uses). An annotation no spec can be built for raises
        :class:`Unsupported`."""
        return cls(annotation, _compile(annotation, names, {}))

    def __repr__(self) -> str:
        return f"<Spec {fmt(self.annotation)}>"

    @property
    def travels(self) -> bool:
        """Whether the type declares no live part, so its values can cross
        a process boundary. Under ``Any`` only plain data crosses: a value
        there that isn't data is refused when it is encoded (a live
        object) or decoded (a table or an array, which need their type
        declared), never mishandled."""
        return "live" not in self.kinds

    def check(self, value: Any, *, full: bool = False) -> None:
        """Refuse ``value`` with :class:`Mismatch` unless it fits. A
        container longer than :data:`SAMPLE_ABOVE` is checked on a sample
        unless ``full``."""
        self._root.check(value, full)

    def decode(self, encoded: Encoded | bytes) -> Any:
        """Build a value of the type from what :func:`encode` made, or
        refuse it with :class:`Mismatch`: a part of it the type doesn't
        allow, a field it doesn't have, a scalar of the wrong type."""
        if isinstance(encoded, (bytes, bytearray, memoryview)):
            encoded = Encoded.from_bytes(bytes(encoded))
        return self._root.build(encoded.tree, encoded.parts)


def kinds_of(tp: Any, *, names: Names = None) -> frozenset[str]:
    """What values of ``tp`` need carried: ``list[Score]`` is data,
    ``dict[str, DataFrame]`` data and a table, ``Callable[[int], int]``
    live."""
    return Spec.of(tp, names=names).kinds


def check(value: Any, tp: Any, *, names: Names = None, full: bool = False) -> None:
    """:meth:`Spec.check` against ``tp``, compiled for the one call."""
    Spec.of(tp, names=names).check(value, full=full)


def decode(encoded: Encoded | bytes, tp: Any, *, names: Names = None) -> Any:
    """:meth:`Spec.decode` into ``tp``, compiled for the one call."""
    return Spec.of(tp, names=names).decode(encoded)


# -- encoding ------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Encoded:
    """An encoded value: a JSON tree, and the binary parts its leaves
    point to."""

    tree: Any
    parts: tuple[bytes, ...] = ()

    def _header(self) -> bytes:
        return json.dumps(
            {"tree": self.tree, "parts": [len(p) for p in self.parts]},
            allow_nan=False,
            separators=(",", ":"),
        ).encode()

    @property
    def size(self) -> int:
        """Its size as one blob, in bytes."""
        return len(_MAGIC) + 4 + len(self._header()) + sum(len(p) for p in self.parts)

    def to_bytes(self) -> bytes:
        """The value as one blob: :data:`FORMAT`, the JSON tree with the
        parts' sizes, then the parts."""
        header = self._header()
        return b"".join((_MAGIC, struct.pack(">I", len(header)), header, *self.parts))

    @classmethod
    def from_bytes(cls, blob: bytes) -> Encoded:
        """Read a blob :meth:`to_bytes` made; :class:`Malformed` for
        anything else."""
        if not blob.startswith(_MAGIC) or len(blob) < len(_MAGIC) + 4:
            raise Malformed(f"not an encoded value (expected {FORMAT})")
        start = len(_MAGIC) + 4
        (length,) = struct.unpack(">I", blob[len(_MAGIC) : start])
        try:
            header = json.loads(blob[start : start + length])
            tree, sizes = header["tree"], header["parts"]
        except (ValueError, KeyError, TypeError) as error:
            raise Malformed(f"an encoded value with a broken header: {error}") from None
        if not isinstance(sizes, list) or not all(
            type(s) is int and s >= 0 for s in sizes
        ):
            raise Malformed("an encoded value with broken part sizes")
        parts = []
        at = start + length
        for size in sizes:
            parts.append(blob[at : at + size])
            at += size
        if at != len(blob):
            raise Malformed("an encoded value whose parts don't add up to its size")
        return cls(tree, tuple(parts))


_MAGIC = FORMAT.encode() + b"\n"


def encode(value: Any, *, limit: int | None = None) -> Encoded:
    """Encode ``value`` for :meth:`Spec.decode`. Needs no type: the
    decoder brings that. Refuses a live object, an object array or a
    cycle with :class:`Unencodable`, and an encoding over ``limit``
    bytes (as one blob) with :class:`TooLarge`."""
    parts: list[bytes] = []
    try:
        tree = _encode(value, parts)
    except RecursionError:
        raise Unencodable("a value nested too deeply, or holding itself") from None
    encoded = Encoded(tree, tuple(parts))
    if limit is not None:
        size = encoded.size
        if size > limit:
            raise TooLarge(
                f"the value is {size:,} bytes encoded, over the {limit:,} allowed"
            )
    return encoded


def _encode(v: Any, parts: list[bytes]) -> Any:
    kind = type(v)
    # the exact built-ins first: they are nearly every leaf
    if kind is str or kind is int or kind is bool or v is None:
        return v
    if kind is float:
        return v if math.isfinite(v) else {TAG: "float", "v": repr(v)}
    if kind is list:
        return [_encode(item, parts) for item in v]
    if kind is dict and TAG not in v and all(type(k) is str for k in v):
        return {k: _encode(item, parts) for k, item in v.items()}
    if isinstance(v, enum.Enum):
        return _encode(v.value, parts)
    if isinstance(v, float):
        return v if math.isfinite(v) else {TAG: "float", "v": repr(float(v))}
    if isinstance(v, (int, str)):
        return int(v) if isinstance(v, int) else str(v)
    if isinstance(v, (bytes, bytearray, memoryview)):
        return _add(parts, bytes(v), {TAG: "bytes"})
    if isinstance(v, datetime.timedelta):
        return {TAG: "timedelta", "d": v.days, "s": v.seconds, "us": v.microseconds}
    if isinstance(v, (datetime.date, datetime.time)):
        return v.isoformat()
    if isinstance(v, (decimal.Decimal, uuid.UUID, pathlib.PurePath)):
        return str(v)
    if dataclasses.is_dataclass(v) and not isinstance(v, type):
        return {
            f.name: _encode(getattr(v, f.name), parts) for f in dataclasses.fields(v)
        }
    if _is_namedtuple(type(v)):
        # an array, as the tuple it is: decoded by a declared tuple too
        return [_encode(item, parts) for item in v]
    if _is_model(type(v)):
        return {
            name: _encode(getattr(v, name), parts)
            for name in type(v).model_fields  # type: ignore[attr-defined]
        }
    if isinstance(v, abc.Mapping):
        if all(type(k) is str for k in v) and TAG not in v:
            return {k: _encode(item, parts) for k, item in v.items()}
        return {
            TAG: "map",
            "items": [
                [_encode(k, parts), _encode(item, parts)] for k, item in v.items()
            ],
        }
    if isinstance(v, (list, tuple, abc.Set)) or (
        isinstance(v, abc.Sequence) and not isinstance(v, (str, bytes))
    ):
        return [_encode(item, parts) for item in v]
    root = _root(kind)
    if root == "numpy":
        return _encode_numpy(v, parts)
    table = _table_name(kind)
    if table is not None:
        return _encode_table(v, table, parts)
    raise Unencodable(
        f"a {kind.__name__} can't be encoded: it isn't data, bytes, a table or an array"
    )


def _add(parts: list[bytes], data: bytes, tag: dict[str, Any]) -> dict[str, Any]:
    tag["part"] = len(parts)
    parts.append(data)
    return tag


def _encode_numpy(v: Any, parts: list[bytes]) -> Any:
    import numpy

    if isinstance(v, numpy.ndarray):
        if v.dtype.hasobject:
            raise Unencodable("an array of Python objects can't be encoded")
        buffer = io.BytesIO()
        numpy.save(buffer, v, allow_pickle=False)
        return _add(parts, buffer.getvalue(), {TAG: "array"})
    if isinstance(v, numpy.generic):
        # a numpy scalar is not the Python one a declared int or float
        # is, so it travels as itself and only Any takes it back
        return {TAG: "number", "dtype": str(v.dtype), "v": _encode(v.item(), parts)}
    raise Unencodable(f"a numpy {type(v).__name__} can't be encoded")


def _encode_table(v: Any, name: str, parts: list[bytes]) -> Any:
    try:
        import pyarrow
    except ImportError:
        raise Unencodable(
            f"a {name} needs pyarrow to be encoded, and it isn't installed here"
        ) from None
    tag: dict[str, Any] = {TAG: "table", "of": name}
    if name == "pandas.DataFrame":
        table = pyarrow.Table.from_pandas(v)
    elif name == "pandas.Series":
        tag["name"] = _encode(v.name, parts)
        table = pyarrow.Table.from_pandas(v.to_frame(name="values"))
    elif name == "pyarrow.Table":
        table = v
    elif name == "pyarrow.RecordBatch":
        table = pyarrow.Table.from_batches([v])
    elif name == "polars.DataFrame":
        table = v.to_arrow()
    else:
        table = pyarrow.table(v)
    sink = io.BytesIO()
    with pyarrow.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return _add(parts, sink.getvalue(), tag)


# -- decoding helpers ----------------------------------------------------------------


def _part(tree: dict[str, Any], parts: Sequence[bytes]) -> bytes:
    index = tree.get("part")
    if type(index) is not int or not 0 <= index < len(parts):
        raise Mismatch("an encoded value pointing at a part it doesn't have")
    return parts[index]


def _special_float(tree: dict[str, Any]) -> float:
    text = tree.get("v")
    if text not in ("nan", "inf", "-inf"):
        raise Mismatch(f"an encoded float that isn't nan or infinite: {text!r}")
    return float(text)


def _timedelta(tree: dict[str, Any]) -> datetime.timedelta:
    fields = (tree.get("d"), tree.get("s"), tree.get("us"))
    if not all(type(f) is int for f in fields):
        raise Mismatch("an encoded timedelta with broken fields")
    days, seconds, micro = fields
    return datetime.timedelta(days=days, seconds=seconds, microseconds=micro)  # type: ignore[arg-type]


def _items(tree: list[Any], parts: Sequence[bytes], node: _Node) -> list[Any]:
    out = []
    build = node.build
    for i, item in enumerate(tree):
        try:
            out.append(build(item, parts))
        except Mismatch as m:
            raise m.at(f"[{i}]") from None
    return out


def _pairs(
    tree: dict[str, Any], parts: Sequence[bytes], key: _Node, value: _Node
) -> dict[Any, Any]:
    items = tree.get("items")
    if type(items) is not list:
        raise Mismatch("an encoded map without its items")
    out: dict[Any, Any] = {}
    for i, pair in enumerate(items):
        if type(pair) is not list or len(pair) != 2:
            raise Mismatch("an encoded map item that isn't a pair").at(f"[{i}]")
        try:
            k = key.build(pair[0], parts)
            out[k] = value.build(pair[1], parts)
        except Mismatch as m:
            raise m.at(f"[{i}]") from None
        except TypeError as error:  # a key that can't be hashed
            raise Mismatch(f"an encoded map key that can't be one: {error}").at(
                f"[{i}]"
            ) from None
    return out


def _library(module: str, what: str) -> Any:
    """``module``, which decoding ``what`` needs: :class:`Unsupported`
    where it isn't installed, since that is this process's lack and not
    the value's fault."""
    import importlib

    try:
        return importlib.import_module(module)
    except ImportError:
        raise Unsupported(
            f"decoding {what} needs {module}, which isn't installed here"
        ) from None


def _read_table(
    name: str, tree: dict[str, Any], data: bytes, parts: Sequence[bytes]
) -> Any:
    pyarrow = _library("pyarrow", f"a {name}")
    readers: dict[str, Callable[[Any], Any]] = {
        "pandas.DataFrame": lambda table: table.to_pandas(),
        "pandas.Series": lambda table: _series(table, tree, parts),
        "pyarrow.Table": lambda table: table,
        "pyarrow.RecordBatch": lambda table: (
            table.combine_chunks().to_batches()[0]
            if table.num_rows
            else pyarrow.RecordBatch.from_pylist([], schema=table.schema)
        ),
        "polars.DataFrame": lambda table: _library("polars", f"a {name}").from_arrow(
            table
        ),
    }
    reader = readers.get(name)
    if reader is None:
        raise Unsupported(f"no decoder for the table type {name}")
    try:
        return reader(pyarrow.ipc.open_stream(data).read_all())
    except (Mismatch, Unsupported):
        raise
    except Exception as error:  # noqa: BLE001 - whatever a bad stream raises
        raise Mismatch(
            f"expected {name}, got an unreadable table "
            f"({type(error).__name__}: {error})"
        ) from None


def _series(table: Any, tree: dict[str, Any], parts: Sequence[bytes]) -> Any:
    frame = table.to_pandas()
    if list(frame.columns) != ["values"]:
        raise Mismatch("an encoded Series that isn't one column")
    series = frame["values"]
    series.name = _ANY.build(tree.get("name"), parts)
    return series
