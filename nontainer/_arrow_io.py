"""pyarrow's file I/O, kept in the workspace.

sandtrap keeps sandboxed code's files in the workspace by patching
Python's file functions (``open``, ``os.*``). pyarrow opens a path in
C++, beneath those patches, so a path handed to pyarrow — by agent code,
or by pandas, which since 3.1 hands parquet paths straight to it — read
and wrote the host's disk instead. Importing this module routes it back.

The rule is the one Python's own ``open()`` follows: pyarrow's I/O goes
where ``open()`` would go at that moment. While the VFS is active
(``monkeyfs.current_fs`` holds a filesystem), a local path given to
pyarrow is opened through a pyarrow filesystem over it; otherwise
nothing changes. So host code, and a module granted the host's
filesystem (sandtrap's ``host_fs_access``, which clears
``current_fs`` for its calls), keep pyarrow's native I/O.

What is routed:

- every path pyarrow resolves to a filesystem
  (``pyarrow.fs._resolve_filesystem_and_path``, ``_ensure_filesystem``):
  parquet, datasets, orc's ``read_table`` and ``fs.copy_files``;
- ``pyarrow.fs.LocalFileSystem()``, which returns the workspace's
  filesystem (and ``isinstance`` still answers for the real class);
- the readers and writers that open a path natively: csv, json,
  feather, ipc, orc's ``ORCFile`` and ``ORCWriter``, ``input_stream``,
  ``output_stream``, ``OSFile`` and ``memory_map`` (a read; a writable
  map has no workspace equivalent and is refused).

A URI with a scheme (``s3://``, ``gs://``) is not a local path and is
left alone. The wrappers are installed when this module is imported,
which the ``dataframes`` preset does in the host and, by granting it,
in every worker that re-imports the preset's modules.
"""

from __future__ import annotations

import contextlib
import functools
import os
import stat
import sys
import threading
from typing import Any, Iterator

import monkeyfs

__all__: list[str] = []

try:  # pragma: no cover - exercised wherever pyarrow is installed
    import pyarrow as pa
    import pyarrow.fs as pafs
except ImportError:  # pragma: no cover
    pa = None  # type: ignore[assignment]
    pafs = None  # type: ignore[assignment]


def _active() -> Any:
    """The filesystem Python's ``open()`` would use now, or ``None``."""
    return monkeyfs.current_fs.get()


#: One operation on the workspace at a time. pyarrow calls a filesystem
#: from a pool of threads, and the workspace's filesystem may not take
#: that: in a worker it is a remote one over a single pipe to the host.
#: The thread that called pyarrow is waiting in it meanwhile, so nothing
#: else is using the filesystem.
_LOCK = threading.RLock()


@contextlib.contextmanager
def _routed(fs: Any) -> Iterator[None]:
    """Route Python's file functions to ``fs`` for this block, whichever
    thread runs it (pyarrow's own threads don't carry the sandbox's
    context), one block at a time."""
    with _LOCK:
        token = monkeyfs.current_fs.set(fs)
        try:
            yield
        finally:
            monkeyfs.current_fs.reset(token)


class _Serialized:
    """A file opened in the workspace whose every call is routed and
    serialized like the filesystem's own operations."""

    def __init__(self, handle: Any, fs: Any) -> None:
        self._handle = handle
        self._fs = fs

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._handle, name)
        if not callable(value):
            return value

        def call(*args: Any, **kwargs: Any) -> Any:
            with _routed(self._fs):
                return value(*args, **kwargs)

        return call

    @property
    def closed(self) -> bool:
        return self._handle.closed


def _local(path: Any) -> str | None:
    """``path`` as a local path string, or ``None`` when it is not one
    (a file object, a buffer, a URI with a scheme other than ``file``)."""
    if isinstance(path, os.PathLike):
        path = os.fspath(path)
    if not isinstance(path, str):
        return None
    if path.startswith("file://"):
        return path[len("file://") :] or "/"
    if "://" in path:
        return None
    return path


#: The compression pyarrow infers from a path's extension when it opens
#: the path itself, so a stream opened here decompresses the same way.
_CODECS = {
    ".bz2": "bz2",
    ".br": "brotli",
    ".gz": "gzip",
    ".lz4": "lz4_frame",
    ".zst": "zstd",
}


def _codec(path: str) -> str | None:
    return _CODECS.get(os.path.splitext(path)[1].lower())


if pa is not None:

    class _WorkspaceHandler(pafs.FileSystemHandler):
        """A pyarrow filesystem over the workspace: each operation is
        Python's own, routed to the filesystem it was made with."""

        def __init__(self, fs: Any) -> None:
            self._fs = fs

        def __eq__(self, other: object) -> bool:
            return isinstance(other, _WorkspaceHandler) and other._fs is self._fs

        def __ne__(self, other: object) -> bool:
            return not self == other

        def get_type_name(self) -> str:
            return "workspace"

        def normalize_path(self, path: str) -> str:
            with _routed(self._fs):
                return os.path.abspath(path)

        def _info(self, path: str) -> Any:
            try:
                st = os.stat(path)
            except (FileNotFoundError, NotADirectoryError):
                return pafs.FileInfo(path, pafs.FileType.NotFound)
            if stat.S_ISDIR(st.st_mode):
                return pafs.FileInfo(path, pafs.FileType.Directory, mtime=st.st_mtime)
            return pafs.FileInfo(
                path, pafs.FileType.File, size=st.st_size, mtime=st.st_mtime
            )

        def get_file_info(self, paths: list[str]) -> list[Any]:
            with _routed(self._fs):
                return [self._info(p) for p in paths]

        def get_file_info_selector(self, selector: Any) -> list[Any]:
            base = selector.base_dir
            with _routed(self._fs):
                if not os.path.isdir(base):
                    if os.path.exists(base):
                        raise NotADirectoryError(base)
                    if selector.allow_not_found:
                        return []
                    raise FileNotFoundError(base)
                infos = []
                pending = [base]
                while pending:
                    current = pending.pop()
                    for name in sorted(os.listdir(current)):
                        info = self._info(os.path.join(current, name))
                        infos.append(info)
                        if selector.recursive and info.type == pafs.FileType.Directory:
                            pending.append(info.path)
                return infos

        def create_dir(self, path: str, recursive: bool) -> None:
            with _routed(self._fs):
                if recursive:
                    os.makedirs(path, exist_ok=True)
                elif not os.path.isdir(path):
                    os.mkdir(path)

        def _clear(self, path: str) -> None:
            for name in os.listdir(path):
                child = os.path.join(path, name)
                if os.path.isdir(child):
                    self._clear(child)
                    os.rmdir(child)
                else:
                    os.remove(child)

        def delete_dir(self, path: str) -> None:
            with _routed(self._fs):
                self._clear(path)
                os.rmdir(path)

        def delete_dir_contents(self, path: str, missing_dir_ok: bool = False) -> None:
            with _routed(self._fs):
                if not os.path.isdir(path):
                    if missing_dir_ok and not os.path.exists(path):
                        return
                    raise FileNotFoundError(path)
                self._clear(path)

        def delete_root_dir_contents(self) -> None:
            raise PermissionError("deleting everything under / is refused")

        def delete_file(self, path: str) -> None:
            with _routed(self._fs):
                if os.path.isdir(path):
                    raise IsADirectoryError(path)
                os.remove(path)

        def move(self, src: str, dest: str) -> None:
            with _routed(self._fs):
                os.replace(src, dest)

        def copy_file(self, src: str, dest: str) -> None:
            with _routed(self._fs):
                with open(src, "rb") as reader, open(dest, "wb") as writer:
                    while chunk := reader.read(1 << 20):
                        writer.write(chunk)

        def _open(self, path: str, mode: str) -> Any:
            with _routed(self._fs):
                handle = open(path, mode)
            return pa.PythonFile(
                _Serialized(handle, self._fs), mode="r" if "r" in mode else "w"
            )

        def open_input_stream(self, path: str) -> Any:
            return self._open(path, "rb")

        def open_input_file(self, path: str) -> Any:
            return self._open(path, "rb")

        def open_output_stream(self, path: str, metadata: Any = None) -> Any:
            return self._open(path, "wb")

        def open_append_stream(self, path: str, metadata: Any = None) -> Any:
            return self._open(path, "ab")

    def _workspace(fs: Any) -> Any:
        """A pyarrow filesystem over ``fs``."""
        return pafs.PyFileSystem(_WorkspaceHandler(fs))

    def _target(path: Any) -> tuple[Any, str] | None:
        """The workspace's pyarrow filesystem and ``path`` made absolute in
        it, when the VFS is active and ``path`` is local; else ``None``."""
        fs = _active()
        if fs is None:
            return None
        local = _local(path)
        if local is None:
            return None
        with _routed(fs):
            local = os.path.abspath(local)
        return _workspace(fs), local

    def _source(path: Any, *, decompress: bool = False) -> Any:
        """``path`` opened for reading in the workspace (decompressed by
        its extension where pyarrow would); ``path`` itself otherwise."""
        found = _target(path)
        if found is None:
            return path
        wfs, local = found
        stream = wfs.open_input_file(local)
        codec = _codec(local) if decompress else None
        return pa.CompressedInputStream(stream, codec) if codec else stream

    # -- stand-ins for classes pyarrow constructs by name ---------------------

    def _stand_in(real: type) -> type:
        """A metaclass whose ``isinstance`` and ``issubclass`` answer for
        ``real``, so code that checks against the replaced name still
        recognizes real instances."""

        class _Meta(type):
            def __instancecheck__(cls, obj: Any) -> bool:
                return isinstance(obj, real)

            def __subclasscheck__(cls, sub: type) -> bool:
                return issubclass(sub, real)

        return _Meta

    _RealLocal = pafs.LocalFileSystem

    class LocalFileSystem(metaclass=_stand_in(_RealLocal)):  # type: ignore[misc]
        __doc__ = _RealLocal.__doc__

        def __new__(cls, *args: Any, **kwargs: Any) -> Any:
            fs = _active()
            if fs is None:
                return _RealLocal(*args, **kwargs)
            return _workspace(fs)

    _RealFileSystem = pafs.FileSystem
    _RealSubTree = pafs.SubTreeFileSystem

    def _reroute(filesystem: Any, fs: Any) -> Any:
        """``filesystem`` with a native local filesystem under it — itself,
        or the base of a subtree (what a ``file://`` URI resolves to) —
        replaced by the workspace's."""
        if isinstance(filesystem, _RealLocal):
            return _workspace(fs)
        if isinstance(filesystem, _RealSubTree):
            base = filesystem.base_fs
            rerouted = _reroute(base, fs)
            if rerouted is not base:
                return _RealSubTree(filesystem.base_path, rerouted)
        return filesystem

    _real_from_uri = _RealFileSystem.from_uri

    def from_uri(uri: Any) -> Any:
        filesystem, path = _real_from_uri(uri)
        fs = _active()
        if fs is not None:
            filesystem = _reroute(filesystem, fs)
        return filesystem, path

    from_uri.__doc__ = _real_from_uri.__doc__

    class _FileSystemMeta(_stand_in(_RealFileSystem)):  # type: ignore[misc]
        def __getattr__(cls, name: str) -> Any:
            return getattr(_RealFileSystem, name)

    class FileSystem(metaclass=_FileSystemMeta):  # type: ignore[misc]
        """pyarrow's ``FileSystem``, whose ``from_uri`` gives a ``file://``
        URI the workspace's filesystem while the VFS is active."""

        from_uri = staticmethod(from_uri)

        def __new__(cls, *args: Any, **kwargs: Any) -> Any:
            return _RealFileSystem(*args, **kwargs)

    _RealOSFile = pa.OSFile

    class OSFile(metaclass=_stand_in(_RealOSFile)):  # type: ignore[misc]
        __doc__ = _RealOSFile.__doc__

        def __new__(cls, path: Any, mode: str = "r", memory_pool: Any = None) -> Any:
            found = _target(path)
            if found is None:
                return _RealOSFile(path, mode, memory_pool)
            wfs, local = found
            kind = mode.replace("b", "")
            if kind == "r":
                return wfs.open_input_file(local)
            if kind == "w":
                return wfs.open_output_stream(local)
            if kind == "a":
                return wfs.open_append_stream(local)
            raise ValueError(f"Invalid file mode: {mode}")

    # -- functions --------------------------------------------------------------

    # private, so looked up rather than assumed: a pyarrow without one
    # leaves that route as it was
    _real_resolve = getattr(pafs, "_resolve_filesystem_and_path", None)
    _real_ensure = getattr(pafs, "_ensure_filesystem", None)

    def _wraps(real: Any) -> Any:
        return functools.wraps(real) if real is not None else (lambda f: f)

    @_wraps(_real_resolve)
    def _resolve_filesystem_and_path(
        path: Any, filesystem: Any = None, **kwargs: Any
    ) -> Any:
        fs = _active()
        if fs is not None and (
            filesystem is None or isinstance(filesystem, _RealLocal)
        ):
            found = _target(path)
            if found is not None:
                return found
        filesystem, path = _real_resolve(path, filesystem, **kwargs)
        if fs is not None:
            filesystem = _reroute(filesystem, fs)
        return filesystem, path

    @_wraps(_real_ensure)
    def _ensure_filesystem(filesystem: Any, **kwargs: Any) -> Any:
        out = _real_ensure(filesystem, **kwargs)
        fs = _active()
        return out if fs is None else _reroute(out, fs)

    _real_input_stream = pa.input_stream
    _real_output_stream = pa.output_stream

    @functools.wraps(_real_input_stream)
    def input_stream(
        source: Any, compression: Any = "detect", buffer_size: Any = None
    ) -> Any:
        found = _target(source)
        if found is None:
            return _real_input_stream(
                source, compression=compression, buffer_size=buffer_size
            )
        wfs, local = found
        if compression == "detect":
            compression = _codec(local)
        return _real_input_stream(
            wfs.open_input_stream(local),
            compression=compression,
            buffer_size=buffer_size,
        )

    @functools.wraps(_real_output_stream)
    def output_stream(
        source: Any, compression: Any = "detect", buffer_size: Any = None
    ) -> Any:
        found = _target(source)
        if found is None:
            return _real_output_stream(
                source, compression=compression, buffer_size=buffer_size
            )
        wfs, local = found
        if compression == "detect":
            compression = _codec(local)
        return _real_output_stream(
            wfs.open_output_stream(local),
            compression=compression,
            buffer_size=buffer_size,
        )

    _real_memory_map = pa.memory_map
    _real_create_memory_map = pa.create_memory_map

    _NO_MAP = (
        "a writable memory map is not available in the workspace: its files "
        "are not on a disk to map. Open the file instead (pa.OSFile(path, 'w'))."
    )

    @functools.wraps(_real_memory_map)
    def memory_map(path: Any, mode: str = "r") -> Any:
        found = _target(path)
        if found is None:
            return _real_memory_map(path, mode)
        if mode.replace("b", "") != "r":
            raise OSError(_NO_MAP)
        wfs, local = found
        return wfs.open_input_file(local)

    @functools.wraps(_real_create_memory_map)
    def create_memory_map(path: Any, size: int) -> Any:
        if _target(path) is None:
            return _real_create_memory_map(path, size)
        raise OSError(_NO_MAP)

    def _arg(args: tuple, kwargs: dict, at: int, name: str) -> Any:
        """The argument at position ``at`` or named ``name``."""
        return args[at] if len(args) > at else kwargs.get(name)

    def _with(args: tuple, kwargs: dict, at: int, name: str, value: Any) -> Any:
        """``args`` and ``kwargs`` with that argument replaced."""
        if len(args) > at:
            return (*args[:at], value, *args[at + 1 :]), kwargs
        return args, {**kwargs, name: value}

    def _reader(real: Any, name: str, *, decompress: bool = False) -> Any:
        """A reader that takes a path first: opened in the workspace,
        read, and closed."""

        @functools.wraps(real)
        def read(*args: Any, **kwargs: Any) -> Any:
            source = _arg(args, kwargs, 0, name)
            stream = _source(source, decompress=decompress)
            if stream is source:
                return real(*args, **kwargs)
            args, kwargs = _with(args, kwargs, 0, name, stream)
            try:
                return real(*args, **kwargs)
            finally:
                stream.close()

        return read

    def _streaming(real: Any, name: str) -> Any:
        """A reader that keeps reading after it returns: its stream stays
        open with it."""

        @functools.wraps(real)
        def open_(*args: Any, **kwargs: Any) -> Any:
            source = _arg(args, kwargs, 0, name)
            stream = _source(source, decompress=True)
            if stream is not source:
                args, kwargs = _with(args, kwargs, 0, name, stream)
            return real(*args, **kwargs)

        return open_

    def _writer(real: Any, name: str, at: int = 1) -> Any:
        """A function that writes a whole file to a path: opened in the
        workspace, written, closed, and removed if the write failed, as
        pyarrow removes a path it failed to write."""

        @functools.wraps(real)
        def write(*args: Any, **kwargs: Any) -> Any:
            found = _target(_arg(args, kwargs, at, name))
            if found is None:
                return real(*args, **kwargs)
            wfs, local = found
            stream = wfs.open_output_stream(local)
            args, kwargs = _with(args, kwargs, at, name, stream)
            try:
                result = real(*args, **kwargs)
            except BaseException:
                stream.close()
                with contextlib.suppress(OSError):
                    wfs.delete_file(local)
                raise
            stream.close()
            return result

        return write

    def _closing_writer(real: type) -> type:
        """A writer class whose sink may be a path: opened in the
        workspace, and closed when the writer is."""

        class Writer(real):  # type: ignore[valid-type, misc]
            def __init__(self, sink: Any, *args: Any, **kwargs: Any) -> None:
                found = _target(sink)
                self._nontainer_stream = None
                if found is not None:
                    wfs, local = found
                    sink = self._nontainer_stream = wfs.open_output_stream(local)
                super().__init__(sink, *args, **kwargs)

            def close(self) -> None:
                try:
                    super().close()
                finally:
                    stream, self._nontainer_stream = self._nontainer_stream, None
                    if stream is not None:
                        stream.close()

        Writer.__name__ = real.__name__
        Writer.__qualname__ = real.__qualname__
        Writer.__module__ = real.__module__
        Writer.__doc__ = real.__doc__
        return Writer

    def _opening_reader(real: type) -> type:
        """A reader class whose source may be a path, opened in the
        workspace for as long as the reader lives."""

        class Reader(real):  # type: ignore[valid-type, misc]
            def __init__(self, source: Any, *args: Any, **kwargs: Any) -> None:
                found = _target(source)
                if found is not None:
                    wfs, local = found
                    source = wfs.open_input_file(local)
                super().__init__(source, *args, **kwargs)

        Reader.__name__ = real.__name__
        Reader.__qualname__ = real.__qualname__
        Reader.__module__ = real.__module__
        Reader.__doc__ = real.__doc__
        return Reader


def _replacements() -> dict[int, tuple[Any, Any]]:
    """Each original pyarrow object, by id, with what replaces it."""
    import pyarrow.csv as csv
    import pyarrow.dataset  # noqa: F401 - imported so its names are patched
    import pyarrow.feather as feather
    import pyarrow.ipc as ipc
    import pyarrow.json as json_
    import pyarrow.parquet  # noqa: F401

    pairs: list[tuple[Any, Any]] = [
        (_RealLocal, LocalFileSystem),
        (_RealFileSystem, FileSystem),
        (_RealOSFile, OSFile),
        (_real_resolve, _resolve_filesystem_and_path),
        (_real_ensure, _ensure_filesystem),
        (_real_input_stream, input_stream),
        (_real_output_stream, output_stream),
        (_real_memory_map, memory_map),
        (_real_create_memory_map, create_memory_map),
        (csv.read_csv, _reader(csv.read_csv, "input_file", decompress=True)),
        (csv.open_csv, _streaming(csv.open_csv, "input_file")),
        (csv.write_csv, _writer(csv.write_csv, "output_file")),
        (csv.CSVWriter, _closing_writer(csv.CSVWriter)),
        (json_.read_json, _reader(json_.read_json, "input_file", decompress=True)),
        (feather.read_table, _reader(feather.read_table, "source")),
        (feather.write_feather, _writer(feather.write_feather, "dest")),
        (ipc.RecordBatchFileReader, _opening_reader(ipc.RecordBatchFileReader)),
        (ipc.RecordBatchStreamReader, _opening_reader(ipc.RecordBatchStreamReader)),
        (ipc.RecordBatchFileWriter, _closing_writer(ipc.RecordBatchFileWriter)),
        (ipc.RecordBatchStreamWriter, _closing_writer(ipc.RecordBatchStreamWriter)),
    ]
    # newer than some pyarrow this runs with: each is routed where it exists
    if hasattr(json_, "open_json"):
        pairs.append((json_.open_json, _streaming(json_.open_json, "input_file")))
    internal = getattr(feather, "_read_table_internal", None)
    if internal is not None:  # read_feather's own path, from pyarrow 25
        pairs.append((internal, _reader(internal, "source")))
    try:
        import pyarrow.orc as orc
    except ImportError:  # pragma: no cover - a pyarrow built without ORC
        pass
    else:
        pairs.append((orc.ORCFile, _opening_reader(orc.ORCFile)))
        pairs.append((orc.ORCWriter, _closing_writer(orc.ORCWriter)))
    return {id(real): (real, stand_in) for real, stand_in in pairs if real is not None}


_INSTALLING = threading.Lock()


def _install() -> None:
    """Replace each original wherever a loaded pyarrow module binds it:
    pyarrow's modules import these names from one another, so each
    binding is its own lookup."""
    if pa is None:
        return
    with _INSTALLING:
        if getattr(pa, "_nontainer_arrow_io", False):
            return
        _patch(_replacements())
        pa._nontainer_arrow_io = True  # type: ignore[attr-defined]


def _patch(replacements: dict[int, tuple[Any, Any]]) -> None:
    # a copy, not an iteration: another thread may be importing meanwhile
    for name, module in sys.modules.copy().items():
        if module is None or not (name == "pyarrow" or name.startswith("pyarrow.")):
            continue
        namespace = getattr(module, "__dict__", None)
        if namespace is None:
            continue
        for attr, value in namespace.copy().items():
            found = replacements.get(id(value))
            if found is not None and found[0] is value:
                with contextlib.suppress(AttributeError, TypeError):
                    setattr(module, attr, found[1])


_install()
