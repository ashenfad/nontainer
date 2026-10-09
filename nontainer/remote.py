"""Calls from code in a sandbox to a host object, typed by the host
object's own annotations: what a :class:`~nontainer.HostObject` with a
``stub`` is made of.

Agent code holds the stub, a class the embedder writes, which runs in
the sandbox and is built there as ``stub(remote)``. ``remote.<method>``
calls the host object's method of that name. Its arguments are checked
against the method's annotations and reach the host by value, and its
result comes back by value; an argument that doesn't fit raises
``TypeError`` at the call, on every rung:

- each argument is encoded (:func:`values.encode`, never pickle) and
  decoded by its declared type, so the host only ever gets values built
  afresh as the types it declared, whatever the sandbox passed: a
  subclass's hooks, a ``__deepcopy__`` say, never reach it. In-process
  that is all; an argument whose declared type has a live part, or a
  live object under ``Any``, crosses there as itself;
- elsewhere, the sandbox half encodes and the host half decodes. A
  refusal comes back as data, and the sandbox half raises it where the
  call was made. The result crosses host to sandbox, the safe
  direction, pickled.

A stub that can't be built is one that says why on every use, on every
rung, rather than a run that fails before its code starts.

Imports nothing from nontainer but :mod:`values`, and that absolutely:
a dud guest that has neither gets both as source.
"""

from __future__ import annotations

import contextvars
import inspect
import json
import pickle
from collections.abc import Callable, Iterator, Mapping
from typing import Any

from nontainer import values

REFUSED = b"nt-refused/1\n"
"""How a reply refusing a call starts; JSON naming the error follows."""

RESULT = b"nt-result/1\n"
"""How a reply carrying a call's result starts; the pickled result
follows."""

CALL_LIMIT = 6 << 20
"""The most one call's arguments carry, encoded, off in-process: what
fits dud's 8 MiB value cap once it is base64 text. The same on every
isolated rung, so code that works on one works on the others."""

_ERRORS: Mapping[str, type[Exception]] = {
    "TypeError": TypeError,
    "AttributeError": AttributeError,
    "ValueError": ValueError,
}


class Refused(Exception):
    """A call turned down before it reached the host object: the error
    to raise for it in the sandbox, and why."""

    def __init__(self, error: type[Exception], message: str) -> None:
        super().__init__(message)
        self.error = error
        self.message = message

    def reply(self) -> bytes:
        return (
            REFUSED
            + json.dumps(
                {"error": self.error.__name__, "message": self.message}
            ).encode()
        )


# -- the contract, read off the host object (host side) ------------------------------


class Method:
    """One method's contract: its signature, and a spec for each
    parameter and for its result. A parameter declared ``*args`` or
    ``**kwargs`` declares each item's type; an unannotated one is
    ``Any``, which off in-process means plain data."""

    def __init__(self, fn: Callable[..., Any]) -> None:
        try:
            self.signature = inspect.signature(fn)
        except (TypeError, ValueError) as error:
            raise values.Unsupported(f"its signature can't be read ({error})") from None
        names = getattr(getattr(fn, "__func__", fn), "__globals__", None)
        self.params: dict[str, values.Spec] = {}
        for param in self.signature.parameters.values():
            try:
                self.params[param.name] = values.Spec.of(param.annotation, names=names)
            except values.Unsupported as error:
                raise values.Unsupported(f"its {param.name!r}: {error}") from None
        try:
            self.returns = values.Spec.of(self.signature.return_annotation, names=names)
        except values.Unsupported as error:
            raise values.Unsupported(f"its result: {error}") from None

    def specs(self) -> Iterator[tuple[str, values.Spec]]:
        """Each spec with what it is the type of."""
        for name, spec in self.params.items():
            yield repr(name), spec
        yield "result", self.returns

    def _bind(self, where: str, args: Any, kwargs: Any) -> inspect.BoundArguments:
        try:
            return self.signature.bind(*args, **kwargs)
        except TypeError as error:
            raise Refused(TypeError, f"{where}: {error}") from None

    def _each(
        self, bound: inspect.BoundArguments
    ) -> Iterator[tuple[str, values.Spec, Callable[[Any], Any], Any]]:
        """``(path, spec, put back, value)`` for each argument bound: an
        item of ``*args`` or ``**kwargs`` on its own."""
        for name, value in list(bound.arguments.items()):
            spec = self.params[name]
            kind = self.signature.parameters[name].kind
            if kind is inspect.Parameter.VAR_POSITIONAL:
                items = list(value)
                for i, item in enumerate(items):
                    yield f"{name}[{i}]", spec, _setter(items, i), item
                bound.arguments[name] = items
            elif kind is inspect.Parameter.VAR_KEYWORD:
                found = dict(value)
                for key, item in found.items():
                    yield f"{name}[{key!r}]", spec, _setter(found, key), item
                bound.arguments[name] = found
            else:
                yield name, spec, _setter(bound.arguments, name), value

    def accept(
        self, where: str, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> tuple[tuple[Any, ...], dict[str, Any], bool]:
        """In-process: the arguments as they would arrive from elsewhere,
        each encoded and decoded by its declared type, except one whose
        type has a live part, or a live object under ``Any``, which is
        checked and passed as itself. The flag says whether any was."""
        bound = self._bind(where, args, kwargs)
        live = False
        for path, spec, put, value in self._each(bound):
            accepted, as_itself = _accept(where, path, spec, value)
            put(accepted)
            live = live or as_itself
        return bound.args, bound.kwargs, live

    def decode(self, where: str, blob: bytes) -> tuple[tuple[Any, ...], dict[str, Any]]:
        """On the host: the arguments a sandbox half encoded, each built
        as its declared type. Whatever arrives, only those types are
        built, and anything else is refused."""
        try:
            encoded = values.Encoded.from_bytes(blob)
        except values.Malformed as error:
            raise Refused(TypeError, f"{where}: a malformed call ({error})") from None
        tree = encoded.tree
        if not (
            isinstance(tree, list)
            and len(tree) == 2
            and isinstance(tree[0], list)
            and isinstance(tree[1], dict)
        ):
            raise Refused(TypeError, f"{where}: a malformed call")
        bound = self._bind(where, tree[0], tree[1])
        for path, spec, put, item in self._each(bound):
            put(_decode(where, path, spec, values.Encoded(item, encoded.parts)))
        return bound.args, bound.kwargs

    def check_result(self, where: str, result: Any, *, sent: bool) -> None:
        """Refuse a result that doesn't fit the declared one, or, when it
        is ``sent`` out of the process, one holding a live object."""
        try:
            self.returns.check(result)
        except values.Mismatch as mismatch:
            raise Refused(
                TypeError,
                f"{where} returned a value that doesn't fit "
                f"{values.fmt(self.returns.annotation)}: {mismatch}",
            ) from None
        if sent and "any" in self.returns.kinds:
            live = values.find_live(result)
            if live is not None:
                raise Refused(
                    TypeError,
                    f"{where} returned {live}, which can't be sent into the sandbox",
                )


def _setter(target: Any, key: Any) -> Callable[[Any], None]:
    def put(value: Any) -> None:
        target[key] = value

    return put


def _decode(where: str, path: str, spec: values.Spec, encoded: Any) -> Any:
    """One argument built as its declared type, or refused."""
    try:
        return spec.decode(encoded)
    except values.Mismatch as mismatch:
        raise Refused(TypeError, f"{where}: {mismatch.at(path)}") from None
    except Exception as error:  # noqa: BLE001 - what a sandbox sent, refused
        raise Refused(
            TypeError,
            f"{where}: {path} can't be decoded ({type(error).__name__}: {error})",
        ) from None


def _accept(where: str, path: str, spec: values.Spec, value: Any) -> tuple[Any, bool]:
    """One argument in-process: through bytes and back, as it would
    cross elsewhere, so a subclass of the declared type arrives as the
    type itself; or, where a live object may cross, checked and passed
    as itself. The flag says which."""
    if spec.travels:
        try:
            blob = values.encode(value).to_bytes()
        except values.Unencodable as error:
            unsent = error
        else:
            return _decode(where, path, spec, blob), False
    try:
        spec.check(value)
    except values.Mismatch as mismatch:
        raise Refused(TypeError, f"{where}: {mismatch.at(path)}") from None
    if spec.travels and not (
        "any" in spec.kinds and values.find_live(value) is not None
    ):
        raise Refused(TypeError, f"{where}: {path} can't be sent by value: {unsent}")
    return value, True


def _result(spec: values.Spec, value: Any) -> Any:
    """A result in-process: a copy, as a result sent elsewhere is, unless
    the type, or the value under ``Any``, has a live part."""
    if not spec.travels:
        return value
    if "any" in spec.kinds and values.find_live(value) is not None:
        return value
    return values.copy(value)


class Methods(Mapping[str, Method]):
    """A host object's public methods, each with its contract."""

    def __init__(self, obj: Any) -> None:
        found: dict[str, Method] = {}
        for name in dir(obj):
            if name.startswith("_"):
                continue
            try:
                raw = inspect.getattr_static(obj, name)
            except AttributeError:
                continue
            if isinstance(raw, (property, type)) or not (
                callable(raw) or isinstance(raw, (staticmethod, classmethod))
            ):
                continue  # a data attribute, or a class, which isn't a call
            try:
                found[name] = Method(getattr(obj, name))
            except values.Unsupported as error:
                raise values.Unsupported(f"{name}(): {error}") from None
        self._methods = found

    def __getitem__(self, name: str) -> Method:
        return self._methods[name]

    def __iter__(self) -> Iterator[str]:
        return iter(self._methods)

    def __len__(self) -> int:
        return len(self._methods)

    def live_parts(self) -> list[str]:
        """Where a declared type has a live part, which only in-process
        can carry: ``"success()'s 'value' (Client)"``."""
        return [
            f"{method}()'s {what} ({values.fmt(spec.annotation)})"
            for method, contract in self._methods.items()
            for what, spec in contract.specs()
            if not spec.travels
        ]

    def result_types(self) -> list[type]:
        """The record and enum classes the results name: what the
        sandbox needs at hand to unpickle one."""
        found: list[type] = []
        for contract in self._methods.values():
            found.extend(t for t in contract.returns.types if t not in found)
        return found


def _lookup(name: str, methods: Methods, method: Any) -> Method:
    if not isinstance(method, str) or method.startswith("_") or method not in methods:
        raise Refused(AttributeError, f"host object {name!r} has no method {method!r}")
    return methods[method]


# -- the host half -------------------------------------------------------------------


class Host:
    """A stubbed host object's host half, off in-process: one method,
    ``call``, that its sandbox half reaches, and that answers every call
    with a reply rather than an exception for a call it refuses. What the
    host object itself raises is the transport's to carry, as it is for
    any host object."""

    def __init__(self, name: str, obj: Any, methods: Methods) -> None:
        self._name = name
        self._obj = obj
        self._methods = methods

    def call(self, method: str, blob: bytes) -> bytes:
        where = f"{self._name}.{method}()"
        try:
            contract = _lookup(self._name, self._methods, method)
            if not isinstance(blob, (bytes, bytearray)):
                raise Refused(TypeError, f"{where}: a malformed call")
            args, kwargs = contract.decode(where, bytes(blob))
        except Refused as refused:
            return refused.reply()
        result = getattr(self._obj, method)(*args, **kwargs)
        try:
            contract.check_result(where, result, sent=True)
        except Refused as refused:
            return refused.reply()
        return RESULT + pickle.dumps(result, protocol=pickle.HIGHEST_PROTOCOL)


# -- the sandbox half ----------------------------------------------------------------


class Local:
    """``remote`` in-process: each call checked against the host object's
    contract, then made on the object itself.

    The call runs as host code, as it does in the host half elsewhere: in
    the context this was built in, which is the embedder's, not the
    sandbox's. In the sandbox's, the host object's own network calls are
    refused, its files are the sandbox's, and work it schedules (a task
    on the embedder's loop, say) inherits all of that. Except when an
    argument crossed as itself: code of the sandbox's it holds (a
    generator, whose body runs where it is iterated) would run unconfined
    there, so that call stays in the sandbox's context.

    The call is host time too, off the run's clock, as a call through
    the host half is elsewhere; but not one an argument crossed as
    itself into, where the sandbox's own code may be what runs.
    """

    def __init__(self, name: str, obj: Any, methods: Methods) -> None:
        self._name = name
        self._obj = obj
        self._methods = methods
        self._context = contextvars.copy_context()

    def __getattr__(self, method: str) -> Callable[..., Any]:
        if method.startswith("_"):
            raise AttributeError(method)
        try:
            contract = _lookup(self._name, self._methods, method)
        except Refused as refused:
            raise refused.error(refused.message) from None
        target = getattr(self._obj, method)
        where = f"{self._name}.{method}()"

        def call(*args: Any, **kwargs: Any) -> Any:
            try:
                args, kwargs, live = contract.accept(where, args, kwargs)
            except Refused as refused:
                raise refused.error(refused.message) from None
            if live:
                result = target(*args, **kwargs)
            else:
                from sandtrap import host_time

                # A copy per call: a context can't be entered twice at
                # once, and calls nest and run from threads.
                with host_time():
                    result = self._context.copy().run(target, *args, **kwargs)
            try:
                contract.check_result(where, result, sent=False)
            except Refused as refused:
                raise refused.error(refused.message) from None
            return _result(contract.returns, result)

        call.__name__ = method
        return call


class Remote:
    """``remote`` off in-process: each call's arguments encoded and sent
    to the host half by ``send(method, blob)``, and its reply read, a
    refusal raised here, where the call was made."""

    def __init__(self, name: str, send: Callable[[str, bytes], Any]) -> None:
        self._name = name
        self._send = send

    def __getattr__(self, method: str) -> Callable[..., Any]:
        if method.startswith("_"):
            raise AttributeError(method)
        name, send = self._name, self._send
        where = f"{name}.{method}()"

        def call(*args: Any, **kwargs: Any) -> Any:
            return _read_reply(where, send(method, _encode_call(where, args, kwargs)))

        call.__name__ = method
        return call


def _encode_call(where: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> bytes:
    try:
        return values.encode([list(args), kwargs], limit=CALL_LIMIT).to_bytes()
    except values.TooLarge as error:
        raise ValueError(
            f"{where}: too large to send to the host ({error}); write the data "
            "to a file in the workspace and pass its path instead"
        ) from None
    except values.Unencodable:
        pass
    # say which argument
    named = [(f"argument {i + 1}", a) for i, a in enumerate(args)]
    named += [(f"argument {k!r}", a) for k, a in kwargs.items()]
    for label, arg in named:
        try:
            values.encode(arg)
        except values.Unencodable as error:
            raise TypeError(
                f"{where}: {label} can't be sent to the host: {error}"
            ) from None
    raise TypeError(f"{where}: the arguments can't be sent to the host")


def _read_reply(where: str, reply: Any) -> Any:
    if isinstance(reply, (bytes, bytearray)):
        reply = bytes(reply)
        if reply.startswith(RESULT):
            return pickle.loads(reply[len(RESULT) :])
        if reply.startswith(REFUSED):
            try:
                said = json.loads(reply[len(REFUSED) :])
                error, message = said["error"], str(said["message"])
            except (ValueError, KeyError, TypeError):
                pass
            else:
                raise _ERRORS.get(error, TypeError)(message)
    raise RuntimeError(f"{where}: the host's reply can't be read")


def build(name: str, cls: Any, remote: Any) -> Any:
    """The stub for host object ``name``, ``cls(remote)``. Never raises:
    a stub that can't be built is one that says why on every use, the
    same on every rung."""
    try:
        return cls(remote)
    except Exception as error:  # noqa: BLE001 - said on every use instead
        return _Unavailable(name, f"{type(error).__name__}: {error}")


def stub(
    name: str, module: str, qualname: str, send: Callable[[str, bytes], Any]
) -> Any:
    """The stub for host object ``name``, built in the sandbox around a
    wire to its host half: the class at ``module:qualname``, imported.
    Never raises, as :func:`build` doesn't."""
    import importlib

    try:
        found: Any = importlib.import_module(module)
        for part in qualname.split("."):
            found = getattr(found, part)
    except Exception as error:  # noqa: BLE001 - said on every use instead
        return _Unavailable(name, f"{type(error).__name__}: {error}")
    return build(name, found, Remote(name, send))


def proxied(proxy: Any, name: str, module: str, qualname: str) -> Any:
    """sandtrap's wrapper for a stubbed host object's proxy, in a worker:
    the stub, built around it. Never raising matters here, since sandtrap
    would hand code the bare proxy in its place."""
    return stub(name, module, qualname, proxy.call)


class _Unavailable:
    """A stubbed host object whose stub couldn't be built, saying so on
    every use."""

    def __init__(self, name: str, why: str) -> None:
        self._name = name
        self._why = why

    def __getattr__(self, attr: str) -> Any:
        if attr.startswith("_"):
            raise AttributeError(attr)
        raise RuntimeError(
            f"host object {self._name!r} is unavailable: its stub couldn't be "
            f"built ({self._why})"
        )
