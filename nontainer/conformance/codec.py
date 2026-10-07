"""Frozen dataclasses to JSON and back, and their JSON Schema.

The conformance corpus and the turn events are written in Python as
frozen dataclasses, and read in other languages as JSON. This module is
the bridge, with no dependencies: :func:`dump` is ``dataclasses.asdict``
made JSON-shaped, :func:`load` walks the type hints to rebuild the
dataclass, and :func:`json_schema` walks the same hints to describe it.

The types it understands are the ones the corpus uses: dataclasses,
unions of dataclasses told apart by a ``kind`` field whose type is a
one-value ``Literal``, ``tuple[X, ...]``, ``dict[str, X]``,
``Literal``, ``X | None``, ``str``, ``int``, ``float``, ``bool`` and
``Any``. Anything else is refused with a ``TypeError`` naming it.
"""

from __future__ import annotations

import dataclasses
import json
import types
import typing
from collections.abc import Mapping
from typing import Any, Literal, Union

__all__ = ["dump", "dumps", "json_schema", "load"]


def dump(obj: Any) -> Any:
    """``obj`` as plain JSON data: dataclasses become objects and tuples
    become lists."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: dump(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, (list, tuple)):
        return [dump(v) for v in obj]
    if isinstance(obj, Mapping):
        return {str(k): dump(v) for k, v in obj.items()}
    return obj


def dumps(obj: Any) -> str:
    """:func:`dump` as text, the way the corpus files are written: two
    space indents, sorted keys, a trailing newline."""
    return json.dumps(dump(obj), indent=2, sort_keys=True) + "\n"


def _hints(cls: type) -> dict[str, Any]:
    return typing.get_type_hints(cls)


def _is_union(tp: Any) -> bool:
    return typing.get_origin(tp) is Union or isinstance(tp, types.UnionType)


def _kind_of(cls: type) -> str:
    """The one value a dataclass's ``kind`` field can take: what tells
    it apart from the other members of a union."""
    tp = _hints(cls).get("kind")
    if typing.get_origin(tp) is Literal and len(typing.get_args(tp)) == 1:
        return typing.get_args(tp)[0]
    raise TypeError(f"{cls.__name__} has no one-value Literal 'kind' field")


def load(tp: Any, data: Any) -> Any:
    """Rebuild a value of type ``tp`` from :func:`dump`'s output.

    Raises ``ValueError`` where the data does not fit the type: a field
    missing, a field the dataclass does not have, a ``kind`` no member
    of the union has, a value outside a ``Literal``.
    """
    if tp is Any:
        return data
    if dataclasses.is_dataclass(tp):
        if not isinstance(data, Mapping):
            raise ValueError(f"{tp.__name__}: expected an object, got {data!r}")
        hints = _hints(tp)
        names = {f.name for f in dataclasses.fields(tp) if f.init}
        unknown = set(data) - names
        if unknown:
            raise ValueError(f"{tp.__name__}: unknown field(s) {sorted(unknown)}")
        values = {k: load(hints[k], v) for k, v in data.items()}
        try:
            return tp(**values)
        except TypeError as e:
            raise ValueError(f"{tp.__name__}: {e}") from None
    origin = typing.get_origin(tp)
    args = typing.get_args(tp)
    if _is_union(tp):
        if data is None and type(None) in args:
            return None
        members = [a for a in args if a is not type(None)]
        if len(members) == 1:
            return load(members[0], data)
        if isinstance(data, Mapping) and all(
            dataclasses.is_dataclass(m) for m in members
        ):
            by_kind = {_kind_of(m): m for m in members}
            member = by_kind.get(data.get("kind"))
            if member is None:
                raise ValueError(
                    f"kind {data.get('kind')!r} is none of {sorted(by_kind)}"
                )
            return load(member, data)
        for member in members:
            try:
                return load(member, data)
            except ValueError:
                continue
        raise ValueError(f"{data!r} fits none of {members}")
    if origin is Literal:
        if data not in args:
            raise ValueError(f"{data!r} is not one of {list(args)}")
        return data
    if origin is tuple:
        if not isinstance(data, (list, tuple)):
            raise ValueError(f"expected a list, got {data!r}")
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(load(args[0], v) for v in data)
        return tuple(load(a, v) for a, v in zip(args, data, strict=True))
    if origin is dict:
        if not isinstance(data, Mapping):
            raise ValueError(f"expected an object, got {data!r}")
        return {str(k): load(args[1], v) for k, v in data.items()}
    if tp is float and isinstance(data, int) and not isinstance(data, bool):
        return float(data)
    if tp in (str, int, float, bool):
        if not isinstance(data, tp) or (tp is int and isinstance(data, bool)):
            raise ValueError(f"expected {tp.__name__}, got {data!r}")
        return data
    if tp is type(None):
        if data is not None:
            raise ValueError(f"expected null, got {data!r}")
        return None
    raise TypeError(f"the codec does not handle {tp!r}")


def json_schema(root: Any, *, title: str | None = None) -> dict[str, Any]:
    """A JSON Schema (draft 2020-12) for values of type ``root``, with
    every dataclass it reaches under ``$defs``.

    A field with a default may be left out; the ``kind`` of a union
    member is a ``const``. Objects accept no other properties.
    """
    defs: dict[str, Any] = {}

    def of(tp: Any) -> dict[str, Any]:
        if tp is Any:
            return {}
        if dataclasses.is_dataclass(tp):
            name = tp.__name__
            if name not in defs:
                defs[name] = {}  # reserve: a type may reach itself
                hints = _hints(tp)
                props: dict[str, Any] = {}
                required: list[str] = []
                for f in dataclasses.fields(tp):
                    if not f.init:
                        continue
                    props[f.name] = of(hints[f.name])
                    no_default = (
                        f.default is dataclasses.MISSING
                        and f.default_factory is dataclasses.MISSING
                    )
                    if no_default or f.name == "kind":
                        required.append(f.name)
                entry: dict[str, Any] = {
                    "type": "object",
                    "properties": props,
                    "required": required,
                    "additionalProperties": False,
                }
                doc = (tp.__doc__ or "").strip()
                if doc and not doc.startswith(f"{name}("):
                    entry["description"] = " ".join(doc.split())
                defs[name] = entry
            return {"$ref": f"#/$defs/{name}"}
        origin = typing.get_origin(tp)
        args = typing.get_args(tp)
        if _is_union(tp):
            return {"anyOf": [of(a) for a in args]}
        if origin is Literal:
            if len(args) == 1:
                return {"const": args[0]}
            return {"enum": list(args)}
        if origin is tuple:
            if len(args) == 2 and args[1] is Ellipsis:
                return {"type": "array", "items": of(args[0])}
            return {
                "type": "array",
                "prefixItems": [of(a) for a in args],
                "minItems": len(args),
                "maxItems": len(args),
            }
        if origin is dict:
            return {"type": "object", "additionalProperties": of(args[1])}
        simple = {str: "string", int: "integer", float: "number", bool: "boolean"}
        if tp in simple:
            return {"type": simple[tp]}
        if tp is type(None):
            return {"type": "null"}
        raise TypeError(f"the codec does not handle {tp!r}")

    top = of(root)
    schema: dict[str, Any] = {"$schema": "https://json-schema.org/draft/2020-12/schema"}
    if title:
        schema["title"] = title
    schema.update(top)
    schema["$defs"] = dict(sorted(defs.items()))
    return schema
