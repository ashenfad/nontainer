"""pyarrow's file I/O in the sandbox: a path given to pyarrow, or to
pandas, which hands parquet paths straight to it, reads and writes the
workspace, not the host's disk (``nontainer._arrow_io``)."""

import os
import textwrap

import pytest

pytest.importorskip("pandas")
pa = pytest.importorskip("pyarrow")

from nontainer import PythonConfig, Store, presets  # noqa: E402


@pytest.fixture(params=["none", "process"])
def ws(request, tmp_path, monkeypatch):
    # the host's working directory, where a path that escaped would land
    host = tmp_path / "host"
    host.mkdir()
    monkeypatch.chdir(host)
    store = Store(memory=True)
    w = store.open(
        "s",
        python=PythonConfig(modules=presets.dataframes(), isolation=request.param),
    )
    w.host_dir = host
    yield w
    w.close()
    store.close()


def run(ws, code):
    result = ws.run_python(textwrap.dedent(code))
    assert result.error is None, result.error
    return result.stdout.strip()


def workspace_files(ws, root="/workspace"):
    out = []
    for name in sorted(ws.files.fs.list(root)):
        path = f"{root}/{name}"
        if ws.files.fs.isdir(path):
            out.extend(workspace_files(ws, path))
        else:
            out.append(path[len("/workspace/") :])
    return out


def assert_nothing_on_the_host(ws):
    assert sorted(os.listdir(ws.host_dir)) == []


ROUND_TRIPS = {
    "pandas parquet": """
        import pandas as pd
        pd.DataFrame({"a": [1, 2, 3]}).to_parquet("t.parquet")
        print(pd.read_parquet("t.parquet").a.sum())
    """,
    "pandas parquet, absolute": """
        import pandas as pd
        pd.DataFrame({"a": [1, 2, 3]}).to_parquet("/workspace/t.parquet")
        print(pd.read_parquet("/workspace/t.parquet").a.sum())
    """,
    "pandas partitioned parquet": """
        import pandas as pd
        df = pd.DataFrame({"k": ["x", "y", "x"], "a": [1, 2, 3]})
        df.to_parquet("parts", partition_cols=["k"])
        print(pd.read_parquet("parts").a.sum())
    """,
    "pandas feather": """
        import pandas as pd
        pd.DataFrame({"a": [1, 2, 3]}).to_feather("t.feather")
        print(pd.read_feather("t.feather").a.sum())
    """,
    "parquet": """
        import pyarrow as pa, pyarrow.parquet as pq
        pq.write_table(pa.table({"a": [1, 2, 3]}), "t.parquet")
        print(pq.read_table("t.parquet").column("a").to_pylist() == [1, 2, 3],
              pq.ParquetFile("t.parquet").metadata.num_rows,
              pq.read_metadata("t.parquet").num_rows)
    """,
    "parquet writer": """
        import pyarrow as pa, pyarrow.parquet as pq
        t = pa.table({"a": [1, 2, 3]})
        with pq.ParquetWriter("t.parquet", t.schema) as w:
            w.write_table(t)
            w.write_table(t)
        print(pq.read_table("t.parquet").num_rows)
    """,
    "dataset": """
        import pyarrow as pa, pyarrow.dataset as ds
        t = pa.table({"k": ["x", "y", "x"], "a": [1, 2, 3]})
        ds.write_dataset(t, "out", format="parquet", partitioning=["k"])
        print(ds.dataset("out", format="parquet", partitioning=["k"]).to_table().num_rows)
    """,
    "csv": """
        import pyarrow as pa, pyarrow.csv as pc
        pc.write_csv(pa.table({"a": [1, 2, 3]}), "t.csv")
        print(pc.read_csv("t.csv").num_rows,
              pc.open_csv("t.csv").read_all().num_rows)
        with pc.CSVWriter("w.csv", pa.schema([("a", pa.int64())])) as w:
            w.write_table(pa.table({"a": [4, 5]}))
        print(pc.read_csv("w.csv").column("a").to_pylist())
    """,
    "csv, gzipped": """
        import gzip, pyarrow.csv as pc
        with gzip.open("t.csv.gz", "wt") as f:
            f.write("a\\n1\\n2\\n")
        print(pc.read_csv("t.csv.gz").num_rows)
    """,
    "json": """
        import pyarrow.json as pj
        with open("t.jsonl", "w") as f:
            f.write('{"a": 1}\\n{"a": 2}\\n')
        print(pj.read_json("t.jsonl").num_rows)
    """,
    "feather": """
        import pyarrow as pa, pyarrow.feather as pf
        pf.write_feather(pa.table({"a": [1, 2, 3]}), "t.feather")
        print(pf.read_table("t.feather").num_rows)
    """,
    "ipc": """
        import pyarrow as pa
        t = pa.table({"a": [1, 2, 3]})
        with pa.ipc.new_file("t.arrow", t.schema) as w:
            w.write_table(t)
        with pa.ipc.new_stream("t.arrows", t.schema) as w:
            w.write_table(t)
        print(pa.ipc.open_file("t.arrow").read_all().num_rows,
              pa.ipc.open_stream("t.arrows").read_all().num_rows)
        with pa.memory_map("t.arrow") as source:
            print(pa.ipc.open_file(source).read_all().num_rows)
    """,
    "orc": """
        import pyarrow as pa, pyarrow.orc as orc
        orc.write_table(pa.table({"a": [1, 2, 3]}), "t.orc")
        print(orc.read_table("t.orc").num_rows, orc.ORCFile("t.orc").nrows)
    """,
    "streams": """
        import pyarrow as pa
        with pa.output_stream("t.bin.gz") as out:
            out.write(b"hello")
        with pa.input_stream("t.bin.gz") as source:
            print(source.read())
        with pa.OSFile("raw.bin", "wb") as out:
            out.write(b"raw")
        with pa.OSFile("raw.bin") as source:
            print(source.read())
    """,
    "a local filesystem": """
        import pyarrow.fs as fs
        local = fs.LocalFileSystem()
        local.create_dir("d/e")
        with local.open_output_stream("d/e/x.txt") as out:
            out.write(b"x")
        fs.copy_files("d/e/x.txt", "d/y.txt")
        print(local.get_file_info("d/y.txt").size,
              sorted(i.path.rsplit("/", 1)[1] for i in local.get_file_info(
                  fs.FileSelector("d", recursive=True))))
    """,
}


@pytest.mark.parametrize("case", sorted(ROUND_TRIPS))
def test_a_round_trip_stays_in_the_workspace(ws, case):
    if case == "orc":
        pytest.importorskip("pyarrow.orc")
    assert run(ws, ROUND_TRIPS[case])
    assert workspace_files(ws)
    assert_nothing_on_the_host(ws)


def test_what_a_round_trip_reads_back(ws):
    assert run(ws, ROUND_TRIPS["pandas parquet"]) == "6"
    assert run(ws, ROUND_TRIPS["streams"]) == "b'hello'\nb'raw'"
    assert run(ws, ROUND_TRIPS["a local filesystem"]) == "1 ['e', 'x.txt', 'y.txt']"


READS = {
    "pandas parquet": "import pandas as pd; pd.read_parquet({p!r})",
    "parquet": "import pyarrow.parquet as pq; pq.read_table({p!r})",
    "csv": "import pyarrow.csv as pc; pc.read_csv({p!r})",
    "json": "import pyarrow.json as pj; pj.read_json({p!r})",
    "feather": "import pyarrow.feather as pf; pf.read_table({p!r})",
    "ipc": "import pyarrow as pa; pa.ipc.open_file({p!r})",
    "stream": "import pyarrow as pa; pa.input_stream({p!r}).read()",
    "OSFile": "import pyarrow as pa; pa.OSFile({p!r}).read()",
    "memory map": "import pyarrow as pa; pa.memory_map({p!r}).read()",
    "a local filesystem": (
        "import pyarrow.fs as fs; fs.LocalFileSystem().open_input_stream({p!r})"
    ),
    "a file URI": "import pyarrow.parquet as pq; pq.read_table('file://' + {p!r})",
}


@pytest.mark.parametrize("case", sorted(READS))
def test_a_host_file_is_out_of_reach(ws, tmp_path, case):
    secret = tmp_path / "secret.parquet"
    pa.parquet.write_table(pa.table({"s": ["host"]}), secret)
    result = ws.run_python(READS[case].format(p=str(secret)))
    # not found: the path is the workspace's, which has no such file
    assert "FileNotFoundError" in (result.error or ""), result.error
    assert "host" not in result.stdout


def test_a_writable_memory_map_is_refused_with_a_reason(ws):
    result = ws.run_python("import pyarrow as pa; pa.memory_map('m.bin', 'r+')")
    assert "not available in the workspace" in result.error
    result = ws.run_python("import pyarrow as pa; pa.create_memory_map('m.bin', 8)")
    assert "not available in the workspace" in result.error


def test_a_failed_write_leaves_no_file(ws):
    result = ws.run_python(
        "import pyarrow.csv as pc\npc.write_csv(object(), 'bad.csv')"
    )
    assert result.error is not None
    assert "bad.csv" not in workspace_files(ws)
    assert_nothing_on_the_host(ws)


def test_the_host_keeps_native_io(tmp_path):
    """Outside the sandbox nothing is routed: pyarrow on the host reads
    and writes the host's disk, and its own classes are what it builds."""
    import pyarrow.fs as fs
    import pyarrow.parquet as pq

    presets.dataframes()  # installs the routing
    path = tmp_path / "host.parquet"
    pq.write_table(pa.table({"a": [1]}), str(path))
    assert path.exists() and pq.read_table(str(path)).num_rows == 1
    local = fs.LocalFileSystem()
    assert type(local).__module__ == "pyarrow._fs"
    assert isinstance(local, fs.LocalFileSystem)
    with pa.OSFile(str(path)) as f:
        assert type(f).__name__ == "OSFile" and f.read(4) == b"PAR1"


def test_a_host_fs_grant_keeps_native_io(tmp_path, monkeypatch):
    """A module granted the host's filesystem keeps pyarrow's native I/O
    for its calls, as it keeps Python's: the routing follows the VFS's own
    switch."""
    import monkeyfs
    import pyarrow.parquet as pq

    import nontainer._arrow_io  # noqa: F401

    path = tmp_path / "granted.parquet"
    vfs = monkeyfs.VirtualFS({})
    with monkeyfs.patch(vfs):
        with monkeyfs.suspend():
            pq.write_table(pa.table({"a": [1]}), str(path))
        assert not os.path.exists(str(path))  # the VFS's view: not there
    assert path.exists()


def test_an_import_meanwhile_does_not_break_the_install(monkeypatch):
    """Another thread may import while the routing is installed: the
    walk over loaded modules reads a copy, not the live dict."""
    import sys
    import types

    from nontainer import _arrow_io

    class Importing(types.ModuleType):
        @property
        def __dict__(self):  # what another thread's import does meanwhile
            sys.modules[f"pyarrow._imported_meanwhile_{len(sys.modules)}"] = None
            return {}

    monkeypatch.setitem(
        sys.modules, "pyarrow._importing", Importing("pyarrow._importing")
    )
    before = set(sys.modules)
    _arrow_io._patch({})
    for name in set(sys.modules) - before:
        del sys.modules[name]
