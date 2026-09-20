"""One filesystem protocol, checked where both halves are installed.

termish and monkeyfs each declare a structural ``FileSystem``, and they
declare the same one: sixteen methods with the same signatures, a
``FileMetadata`` with the same fields, and a ``read`` that takes a byte
range. Neither library imports the other — a shared package would cost
both of them their zero-dependency line for twenty lines of
``Protocol``, and there is no natural "below" between a shell over a
filesystem and stdlib routing over a filesystem — so the agreement is a
convention, and no assertion inside either library can enforce it.

nontainer is the first place both are installed at once, which makes it
the only place that can. The rule this repository has for a convention
is that it should not be one: ``test_layering.py`` walks the package
with ``ast`` rather than asking anyone to remember, and this does the
same for the protocol. What drift costs is not a type error anywhere:
a ``read`` that quietly loses its range is a workspace that returns the
whole file where a caller asked for twenty bytes of it, and a backend
that never grew one is a ``TypeError`` the first time anything opens a
file in binary mode.

Two halves, and they catch different things:

- **The signatures**, read off both protocols with ``inspect``. Where
  monkeyfs declares a method by name only — ``read``, ``write``,
  ``rmdir``, ``glob`` and ``list_detailed`` are probed rather than
  called, so they are named in ``monkeyfs.base``'s frozensets instead
  of in the ``Protocol`` body — the signature is taken from
  ``VirtualFS``, monkeyfs's own in-memory backend and the one nontainer
  runs on. A name that is in neither place is a name monkeyfs does not
  declare, and then the only thing to check is that it declares nothing
  conflicting under it.
- **Both conformance kits**, run against every filesystem a workspace
  can sit on. Each kit ships inside its own library, so this asks each
  library what it means by the protocol instead of restating it here,
  and a backend that accepts ``offset`` and ``size`` and then discards
  them is exactly what the kits exist to catch — it answers every
  whole-file test there is.

Annotations are compared as text. Both libraries are written under
``from __future__ import annotations``, so every annotation is already
a string; ``list[str]`` from one and ``list[str]`` from the other are
the same string without either being imported or resolved, and
``FileMetadata`` matches ``FileMetadata`` across two modules that each
mean their own.
"""

from __future__ import annotations

import contextlib
import dataclasses
import inspect
import shutil
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import monkeyfs
import pytest
import termish
import termish.fs
from monkeyfs import base as monkeyfs_base

from nontainer.providers import DirProvider, KvgitProvider

#: The protocol, as termish's ``FileSystem`` declares it. Spelled out
#: rather than derived so that a method vanishing from termish is a
#: failure here instead of a shorter loop nobody notices.
PROTOCOL_METHODS = (
    "getcwd",
    "chdir",
    "read",
    "write",
    "exists",
    "isfile",
    "isdir",
    "stat",
    "mkdir",
    "makedirs",
    "remove",
    "rmdir",
    "rename",
    "list",
    "list_detailed",
    "glob",
)

#: Every method name monkeyfs declares for a backend, across the four
#: frozensets it splits them into: required (called unconditionally),
#: optional (probed), and the direct-use ones no stdlib shim dispatches
#: to but callers and wrappers reach for.
MONKEYFS_DECLARED = (
    monkeyfs_base.REQUIRED_METHODS
    | monkeyfs_base.OPTIONAL_METHODS
    | monkeyfs_base.DIRECT_READ_METHODS
    | monkeyfs_base.DIRECT_WRITE_METHODS
)


def _signature(fn: Any) -> tuple[tuple[tuple[str, Any, Any, str], ...], str]:
    """One method's signature as comparable text: every parameter's
    name, kind and default in order, and the return annotation."""
    sig = inspect.signature(fn)
    params = tuple(
        (p.name, p.kind, p.default, str(p.annotation)) for p in sig.parameters.values()
    )
    return params, str(sig.return_annotation)


def _monkeyfs_declaration(name: str) -> tuple[Any, str] | None:
    """monkeyfs's declaration of one method, and where it was found.

    The ``Protocol`` body holds only the methods the patch layer calls
    unconditionally. The rest are probed, so their signature lives in
    the backends; ``VirtualFS`` is the one to read it off, because it
    is the backend a kvgit-backed workspace runs on.
    """
    declared = getattr(monkeyfs.FileSystem, name, None)
    if declared is not None:
        return declared, "monkeyfs.FileSystem"
    if name in MONKEYFS_DECLARED:
        return getattr(monkeyfs.VirtualFS, name), "monkeyfs.VirtualFS"
    return None


def test_termish_declares_exactly_the_sixteen_methods():
    """The list above is the protocol, not a sample of it."""
    declared = {
        name
        for name in vars(termish.fs.FileSystem)
        if not name.startswith("_") and callable(vars(termish.fs.FileSystem)[name])
    }
    assert declared == set(PROTOCOL_METHODS)


def test_monkeyfs_names_every_method_termish_declares():
    """Both sides answer for all sixteen. A name monkeyfs stopped
    naming would leave the signature check with nothing to compare, so
    the absence is asserted on its own rather than passing quietly."""
    missing = [name for name in PROTOCOL_METHODS if name not in MONKEYFS_DECLARED]
    assert missing == []


@pytest.mark.parametrize("name", PROTOCOL_METHODS)
def test_the_two_protocols_declare_one_signature(name):
    """Parameter names, kinds, defaults and annotations, method by
    method. A method monkeyfs does not declare at all is allowed —
    termish's protocol is the wider one by design — but it may not
    declare a different thing under the same name."""
    found = _monkeyfs_declaration(name)
    if found is None:
        assert not hasattr(monkeyfs.VirtualFS, name), (
            f"monkeyfs does not declare {name!r} for a backend, yet "
            f"VirtualFS defines one — a second meaning for a name "
            f"termish already spends"
        )
        return
    declared, where = found
    assert _signature(getattr(termish.fs.FileSystem, name)) == _signature(declared), (
        f"{name}: termish.fs.FileSystem and {where} have drifted apart"
    )


def test_read_takes_a_byte_range_on_both_sides():
    """The parameter the whole protocol change is about, asserted by
    name and default rather than only by equality with the other side —
    two libraries that both reverted to a whole-file ``read(path)``
    would agree with each other and be wrong."""
    for where, fn in (
        ("termish.fs.FileSystem", termish.fs.FileSystem.read),
        ("monkeyfs.VirtualFS", monkeyfs.VirtualFS.read),
    ):
        params = inspect.signature(fn).parameters
        assert list(params) == ["self", "path", "offset", "size"], where
        assert params["offset"].default == 0, where
        assert params["size"].default == -1, where


def test_the_metadata_types_carry_the_same_fields():
    """``stat()`` returns one library's ``FileMetadata`` to the other's
    caller, so the two have to be the same record. Dataclass fields
    only: monkeyfs's carries ``st_*`` properties on top so that
    ``os.stat()`` can hand it back, and a property is not a field."""

    def fields(cls: type) -> list[tuple[str, str]]:
        return [(f.name, str(f.type)) for f in dataclasses.fields(cls)]

    assert fields(termish.fs.FileMetadata) == fields(monkeyfs.FileMetadata)


# -- the conformance kits ------------------------------------------------


@contextlib.contextmanager
def _memory_fs() -> Iterator[Any]:
    yield termish.MemoryFS()


@contextlib.contextmanager
def _virtual_fs() -> Iterator[Any]:
    yield monkeyfs.VirtualFS({})


@contextlib.contextmanager
def _kvgit_fs() -> Iterator[Any]:
    provider = KvgitProvider.open(None, session="fs_conformance")
    try:
        yield provider.fs
    finally:
        provider.close()


@contextlib.contextmanager
def _dir_fs() -> Iterator[Any]:
    root = tempfile.mkdtemp()
    provider = DirProvider(root, session="fs_conformance")
    try:
        yield provider.fs
    finally:
        provider.close()
        shutil.rmtree(root, ignore_errors=True)


@contextlib.contextmanager
def _agentfs_fs() -> Iterator[Any]:
    from nontainer.providers import AgentFSProvider

    root = tempfile.mkdtemp()
    provider = AgentFSProvider(Path(root) / "fs.db", session="fs_conformance")
    try:
        yield provider.fs
    finally:
        provider.close()
        shutil.rmtree(root, ignore_errors=True)


#: monkeyfs's in-memory backend accepts ``makedirs(exist_ok=False)`` on
#: a directory that is already there. Marked expected-failure rather
#: than worked around: the kit is stating the rule ``os.makedirs``
#: states, termish's ``MemoryFS`` keeps it, and monkeyfs's own
#: ``IsolatedFS`` keeps it by delegating to ``os.makedirs`` — only
#: ``VirtualFS`` is out, and it raises solely when the path exists as a
#: file. A kvgit-backed workspace is a ``VirtualFS``, so it inherits
#: this. Settling it belongs in monkeyfs, where the method is.
_virtualfs_makedirs = pytest.mark.xfail(
    strict=True,
    reason=(
        "monkeyfs VirtualFS.makedirs() returns silently for a directory "
        "that already exists, where exist_ok=False asks for "
        "FileExistsError — the behaviour os.makedirs, termish's MemoryFS "
        "and monkeyfs's own IsolatedFS all have"
    ),
)

#: monkeyfs's real-directory backend spells ``FileInfo.path`` relative
#: to the filesystem root, so listing ``/a/b`` answers ``a/b/one.txt``
#: where termish's convention — the queried directory joined to the
#: entry — asks for ``/a/b/one.txt``. ``MemoryFS`` and ``VirtualFS``
#: both follow the convention, which is what makes this a divergence
#: rather than an undecided question. nontainer itself is unaffected:
#: its own ``list_detailed`` filters on ``FileInfo.name``, never on
#: ``path``. Settling it belongs in monkeyfs.
_isolatedfs_list_detailed_path = pytest.mark.xfail(
    strict=True,
    reason=(
        "monkeyfs IsolatedFS.list_detailed() returns FileInfo.path "
        "relative to the filesystem root rather than to the queried "
        "directory, which is the spelling termish's FileInfo documents "
        "and MemoryFS and VirtualFS produce"
    ),
)


def _agentfs_installed() -> bool:
    from importlib.util import find_spec

    return find_spec("agentfs_sdk") is not None


_needs_agentfs = pytest.mark.skipif(
    not _agentfs_installed(), reason="requires the agentfs extra"
)

#: Every filesystem a workspace can sit on, plus the two reference
#: implementations the protocol is written against. Each entry opens an
#: EMPTY filesystem: both kits write their own scratch tree and remove
#: it, and neither is a test double for one holding real content.
FILESYSTEMS: list[tuple[str, Callable[[], Any], tuple[Any, ...], tuple[Any, ...]]] = [
    # (id, factory, marks for the termish kit, marks for the monkeyfs kit)
    ("termish.MemoryFS", _memory_fs, (), ()),
    ("monkeyfs.VirtualFS", _virtual_fs, (_virtualfs_makedirs,), ()),
    ("KvgitProvider.fs", _kvgit_fs, (_virtualfs_makedirs,), ()),
    ("DirProvider.fs", _dir_fs, (_isolatedfs_list_detailed_path,), ()),
    ("AgentFSProvider.fs", _agentfs_fs, (_needs_agentfs,), (_needs_agentfs,)),
]


def _params(kit: int) -> list[Any]:
    """The filesystem table as pytest params, carrying the marks that
    belong to one kit — a divergence one kit asserts and the other does
    not is expected-failure there and passing here."""
    return [
        pytest.param(factory, id=name, marks=list(row[2 + kit]))
        for row in FILESYSTEMS
        for name, factory in ((row[0], row[1]),)
    ]


@pytest.mark.parametrize("factory", _params(0))
def test_termish_conformance_kit(factory):
    """termish's kit, which works relative to whatever ``getcwd()``
    reports and removes the scratch tree it created. It exercises all
    sixteen methods, the ranged read, append mode on ``write`` and the
    ``FileInfo.path`` convention."""
    with factory() as fs:
        termish.fs.check_filesystem(fs)


@pytest.mark.parametrize("factory", _params(1))
def test_monkeyfs_conformance_kit(factory):
    """monkeyfs's kit, which takes a scratch directory that must not
    already exist. It exercises the required methods, every ranged-read
    case, and ``open()`` — the backend's own where it has one, the
    synthesized one under ``patch()`` where it does not."""
    with factory() as fs:
        monkeyfs.check_filesystem(fs)
