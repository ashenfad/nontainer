"""Host objects: what ``PythonConfig.host_objects`` holds, and what
each kind of entry is."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from types import ModuleType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .workspace import PythonConfig, Workspace


@dataclass(frozen=True)
class HostObject:
    """A ``PythonConfig.host_objects`` entry with more said about it than
    the object alone.

    ``type`` declares ``obj`` data of that type, sent into the sandbox
    by value on every rung: in-process the object itself; under process
    isolation and on dud a copy, pickled in (the safe direction: the
    sandbox only unpickles what the host wrote), with the record and
    enum classes it holds imported there or rebuilt from their module's
    source. It is checked against the type here, strictly
    (:mod:`nontainer.values`), and a type with a live part is refused:
    a live object needs no type, and reaches the sandbox as a proxy
    under isolation, as a bare entry does.

    ``stub`` puts a class of the embedder's in front of the live
    ``obj``: code in the sandbox holds ``stub(remote)``, built there on
    every rung, and ``remote.<method>(...)`` calls ``obj``'s method of
    that name, typed by its annotations (:mod:`nontainer.remote`), each
    a type or a compiled :class:`~nontainer.values.Spec`. An
    argument that doesn't fit its parameter's type raises ``TypeError``
    at the call; one that does reaches ``obj`` built afresh as its
    declared type on every rung (encoded and decoded, never unpickled),
    and the result comes back by value. A parameter without an
    annotation is ``Any``, which off in-process means plain data. The
    stub's own code runs in the sandbox, so it can do what no call to
    the host can: end the run by raising, say. A stub that can't be
    built is one that says why when code uses it. Under process
    isolation the worker imports
    the stub by its qualified name, and on dud the guest imports it or
    rebuilds its module from source, so it is refused there if defined
    inside a function or in ``__main__``, as is a type with a live part
    in any of ``obj``'s signatures.

    An entry with neither is the object itself, exactly as if it had
    been put in ``host_objects`` bare.

    ``factory`` takes the place of ``obj`` for an object each world
    needs its own of: it is called with each :class:`Workspace` as it
    opens, forks included, and what it returns is that world's object,
    under the same ``type`` or ``stub``. The world is still opening when
    it's called, so the object should keep it and use it from its own
    calls. A fork, and :meth:`Profile.of`, carry the factory, not the
    object it made.
    """

    obj: Any = field(default_factory=lambda: _NO_OBJECT)

    type: Any = None
    """The type ``obj`` is data of, or ``None`` for an object passed as
    it is. A compiled :class:`nontainer.values.Spec` serves too, for a
    type whose annotations name what its own module can't resolve (the
    locals of the function that defined it)."""

    stub: Any = None
    """The class code in the sandbox holds in front of ``obj``, built as
    ``stub(remote)``, or ``None`` for none."""

    factory: Callable[[Workspace], Any] | None = None
    """Called with each world as it opens, for that world's object, in
    place of ``obj``."""

    spec: Any = field(default=None, init=False, repr=False, compare=False)
    """The compiled type (:class:`nontainer.values.Spec`), when there is
    one."""

    methods: Any = field(default=None, init=False, repr=False, compare=False)
    """``obj``'s public methods with their contracts
    (:class:`nontainer.remote.Methods`), when there is a stub."""

    def __post_init__(self) -> None:
        if self.factory is not None:
            if self.obj is not _NO_OBJECT:
                raise TypeError("HostObject takes obj or factory=, not both")
            if not callable(self.factory):
                raise TypeError(
                    f"HostObject factory is a callable, not {type(self.factory).__name__}"
                )
        elif self.obj is _NO_OBJECT:
            raise TypeError("HostObject needs an obj, or a factory= to make one")
        if self.stub is not None:
            self._stubbed()
        if self.type is None:
            return
        from .values import Mismatch, Spec, Unsupported, find_live

        try:
            spec = Spec.of(self.type)
        except Unsupported as error:
            raise TypeError(f"HostObject type: {error}") from None
        if not spec.travels:
            raise TypeError(
                f"HostObject type {spec!r} has a live part, so its values can't "
                "be sent by value; pass a live object without type= and it "
                "reaches the sandbox as a proxy"
            )
        object.__setattr__(self, "spec", spec)
        if self.factory is not None:
            return  # checked against the type when made
        try:
            spec.check(self.obj)
        except Mismatch as error:
            raise TypeError(f"HostObject value doesn't fit its type: {error}") from None
        if "any" in spec.kinds:
            # Any accepts a live object too, and one can't be sent by value
            live = find_live(self.obj)
            if live is not None:
                raise TypeError(
                    f"HostObject value holds a live object where its type "
                    f"allows anything ({live}); a live object can't be sent by "
                    "value: give it an entry of its own, without type="
                )
        object.__setattr__(self, "spec", spec)

    def _stubbed(self) -> None:
        from .remote import Methods
        from .values import Unsupported

        if self.type is not None:
            raise TypeError(
                "HostObject takes type= for data sent by value or stub= for a "
                "live object called through a stub, not both"
            )
        if not isinstance(self.stub, type):
            raise TypeError(
                f"HostObject stub is a class, not {type(self.stub).__name__}"
            )
        if self.factory is not None:
            return  # the contract is read off each object made
        try:
            methods = Methods(self.obj)
        except Unsupported as error:
            raise TypeError(f"HostObject with a stub: {error}") from None
        object.__setattr__(self, "methods", methods)

    @property
    def by_value(self) -> bool:
        """Whether the entry is sent into the sandbox as a copy of its
        data, rather than as a proxy to a live object."""
        return self.type is not None

    def made_for(self, ws: Workspace) -> HostObject:
        """This entry for world ``ws``: itself, or, with a factory, the
        object it makes for ``ws`` under the same ``type`` or ``stub``,
        checked as any entry is."""
        if self.factory is None:
            return self
        obj = self.factory(ws)
        try:
            return HostObject(obj, type=self.type, stub=self.stub)
        except TypeError as error:
            raise TypeError(f"what HostObject's factory made: {error}") from None


class _NoObject:
    """``HostObject.obj`` when a factory makes it."""

    def __repr__(self) -> str:
        return "<made by factory>"


_NO_OBJECT = _NoObject()


def _made_for(python: PythonConfig, ws: Workspace) -> PythonConfig:
    """``python`` with each host object a factory makes made for ``ws``:
    the config its executor opens with."""
    entries = python.host_objects
    if not any(
        isinstance(e, HostObject) and e.factory is not None for e in entries.values()
    ):
        return python
    made = {
        name: entry.made_for(ws) if isinstance(entry, HostObject) else entry
        for name, entry in entries.items()
    }
    return replace(python, host_objects=made)


def _is_plain_data(obj: Any) -> bool:
    """Builtin-typed values need no policy registration."""
    return type(obj).__module__ == "builtins" and not isinstance(obj, ModuleType)


def _value(entry: Any) -> Any:
    """A host object entry's object: a :class:`HostObject`'s, or the
    entry itself."""
    return entry.obj if isinstance(entry, HostObject) else entry


def _by_value(entry: Any) -> bool:
    """Whether a host object entry is sent into the sandbox as a copy of
    its data rather than as a proxy: plain built-in data, or a
    :class:`HostObject` with a type."""
    if isinstance(entry, HostObject):
        if entry.stub is not None:
            return False
        return entry.by_value or _is_plain_data(entry.obj)
    return _is_plain_data(entry)


def _typed(entry: Any) -> bool:
    """Whether an entry is data of a declared type: sent by value, with
    the classes it holds made available wherever it lands."""
    return isinstance(entry, HostObject) and entry.by_value


def _stubbed(entry: Any) -> bool:
    """Whether an entry is a live object called through a stub, which
    runs in the sandbox."""
    return isinstance(entry, HostObject) and entry.stub is not None
