"""A worktree's trace: ``ws.files.attach(..., trace=True)`` and
``ws-git worktree add --trace``.

The conversation stored at the attached commit is mounted at
``<at>/.trace``, rendered by ``nontainer.traces.Trace`` from what the
store's trace source reads. What the verb prints is pinned by the ws-git
corpus (``a-worktree-with-its-trace``); this checks the host side, the
laziness, and an agno session read back through it."""

import pytest

from nontainer import conversation
from nontainer.errors import NotSupportedError
from nontainer.store import Store
from nontainer.traces import NeutralTraces, Trace, TraceEvent, TraceRun
from nontainer.views import TraceFS


def _run(rid, text="hi", *, status="completed", error=False):
    return TraceRun(
        id=rid,
        status=status,
        started=1760529600.0,
        ended=1760529605.0,
        events=(
            TraceEvent("user", text),
            TraceEvent(
                "tool", "out", tool="terminal", args={"command": "ls"}, error=error
            ),
            TraceEvent("assistant", "done"),
        ),
    )


def _converse(ws, run, harness="neutral"):
    return ws.turns.begin(run.id, harness=harness).end(run.status, body=run.to_dict())


class _Counting(NeutralTraces):
    """The neutral source, counting what it is asked to read."""

    def __init__(self):
        self.reads = []

    def read(self, body):
        self.reads.append(body.get("id"))
        return super().read(body)


@pytest.fixture
def source():
    return _Counting()


@pytest.fixture
def store(source):
    s = Store(memory=True, traces={"neutral": source})
    yield s
    s.close()


@pytest.fixture
def worker(store):
    ws = store.open("worker")
    ws.files.write("a.txt", b"A\n")
    _converse(ws, _run("r-one", "first"))
    yield ws
    ws.close()


@pytest.fixture
def ws(store):
    w = store.open("main")
    yield w
    w.close()


class TestAttach:
    def test_the_files_and_the_conversation_land_together(self, ws, worker):
        ref = ws.files.attach("worker", "w", trace=True)
        assert ref == str(worker.ref)
        assert ws.files.attachments() == {"/workspace/w": ref}
        assert ws.files.read("w/a.txt") == b"A\n"
        index = ws.files.read("w/.trace/index.md").decode()
        assert "001-r-one" in index and "first" in index
        assert sorted(ws.files.fs.list("/workspace/w")) == [".trace", "a.txt"]
        assert ws.terminal("cat w/.trace/runs/001-r-one/events/001-user.md").stdout
        assert (
            ws.run_python(
                "print(open('w/.trace/README.md').read().splitlines()[0])"
            ).stdout
            == "# Trace of worker at " + worker.head[:7] + "\n"
        )

    def test_one_detach_takes_both_down(self, ws, worker):
        ws.files.attach("worker", "w", trace=True)
        ws.files.detach("w")
        assert ws.files.attachments() == {}
        assert not ws.files.fs.exists("/workspace/w/.trace/index.md")

    def test_it_is_read_only_and_outside_versioning(self, ws, worker):
        ws.files.attach("worker", "w", trace=True)
        with pytest.raises(PermissionError):
            ws.files.fs.write("/workspace/w/.trace/x.md", b"no")
        assert ws.terminal("echo x > w/.trace/index.md").exit_code != 0
        assert not any(".trace" in p for p in ws._provider.working_files())

    def test_it_is_pinned_at_the_commit(self, ws, worker):
        first = worker.head
        _converse(worker, _run("r-two", "second"))
        ws.files.attach(f"worker@{first}", "old", trace=True)
        ws.files.attach("worker", "new", trace=True)
        assert "r-two" not in ws.files.read("old/.trace/index.md").decode()
        assert "002-r-two" in ws.files.read("new/.trace/index.md").decode()

    def test_a_store_tag_names_the_session_that_wrote_it(self, ws, worker, store):
        store.tags.add(worker, "good")
        ws.files.attach("good", "g", trace=True)
        assert ws.files.read("g/.trace/README.md").startswith(b"# Trace of worker at ")


class TestLaziness:
    def test_nothing_is_read_until_the_trace_is(self, ws, worker, source):
        ws.files.attach("worker", "w", trace=True)
        ws.files.read("w/a.txt")
        assert source.reads == []
        ws.files.read("w/.trace/index.md")
        assert source.reads == ["r-one"]
        ws.terminal("cat w/.trace/index.md; ls -R w/.trace")
        assert source.reads == ["r-one"]  # read once, kept

    def test_a_file_is_rendered_once(self):
        calls = []

        def load():
            calls.append(1)
            return Trace([_run("r")], session="s", commit="abc1234", harness="h"), 0

        fs = TraceFS(load)
        assert calls == []
        assert fs.read("index.md") == fs.read("/index.md")
        assert calls == [1]


class TestRefusals:
    def test_a_state_with_no_conversation(self, ws, store):
        plain = store.open("plain")
        plain.files.write("a.txt", b"A\n")
        plain.close()
        with pytest.raises(ValueError, match="holds no conversation"):
            ws.files.attach("plain", "p", trace=True)
        assert ws.files.attachments() == {}
        ws.files.attach("plain", "p")  # without the trace it mounts

    def test_a_harness_the_store_cannot_read(self, ws, store):
        other = store.open("other")
        _converse(other, _run("r"), harness="elsewhere")
        other.close()
        with pytest.raises(NotSupportedError, match="'elsewhere'"):
            ws.files.attach("other", "o", trace=True)
        assert ws.files.attachments() == {}

    def test_a_store_without_sources_traces_nothing(self):
        store = Store(memory=True)
        try:
            other = store.open("other")
            _converse(other, _run("r"))
            other.close()
            ws = store.open("main")
            with pytest.raises(NotSupportedError):
                ws.files.attach("other", "o", trace=True)
            ws.close()
        finally:
            store.close()

    def test_a_source_must_read(self):
        with pytest.raises(TypeError, match="traces\\['x'\\]"):
            Store(memory=True, traces={"x": object()})

    def test_the_sources_are_read_only(self, store, source):
        assert dict(store.traces) == {"neutral": source}
        with pytest.raises(TypeError):
            store.traces["agno"] = source  # type: ignore[index]


class TestWhatIsShown:
    def test_a_trace_hides_the_trees_own(self, ws, store):
        keeper = store.open("keeper")
        keeper.files.write(".trace/notes.md", b"mine\n")
        _converse(keeper, _run("r"))
        keeper.close()
        ws.files.attach("keeper", "k", trace=True)
        assert not ws.files.fs.exists("/workspace/k/.trace/notes.md")
        assert ws.files.fs.exists("/workspace/k/.trace/index.md")

    def test_a_run_its_source_cannot_read_is_left_out(self, ws, store):
        odd = store.open("odd")
        _converse(odd, _run("good"))
        t = odd.turns.begin("bad", harness="neutral")
        t.end("completed", body={"not": "a run"})
        odd.close()
        ws.files.attach("odd", "o", trace=True)
        index = ws.files.read("o/.trace/index.md").decode()
        assert "001-good" in index and "bad" not in index

    def test_the_tree_and_the_trace_list_together(self, ws, worker):
        ws.files.attach("worker", "w", trace=True)
        listed = ws.files.fs.list("/workspace/w", recursive=True)
        assert "a.txt" in listed
        assert ".trace/runs/001-r-one/run.md" in listed
        assert ".trace/runs" in listed


class TestTraceFS:
    @pytest.fixture
    def fs(self):
        trace = Trace(
            [_run("r1", error=True)], session="s", commit="abc1234", harness="h"
        )
        return TraceFS(lambda: (trace, 1760529605.0))

    def test_directories_are_what_the_paths_imply(self, fs):
        assert fs.list("/") == ["README.md", "index.md", "runs"]
        assert fs.list("runs") == ["001-r1"]
        assert fs.list("runs/001-r1/events") == [
            "001-user.md",
            "002-tool-terminal.md",
            "003-assistant.md",
        ]
        assert fs.isdir("/") and fs.isdir("runs/001-r1") and not fs.isfile("runs")
        assert fs.isfile("runs/001-r1/run.md") and not fs.isdir("index.md")
        assert "runs/001-r1/events/001-user.md" in fs.list("/", recursive=True)

    def test_a_file_reads_its_bytes_by_range(self, fs):
        whole = fs.read("index.md")
        assert fs.read("index.md", 2, 4) == whole[2:6]
        assert fs.read("index.md", 2) == whole[2:]
        assert fs.stat("index.md").size == len(whole)
        assert fs.stat("index.md").modified_at.startswith("2025-10-15T12:00:05")
        assert fs.stat("runs").is_dir

    def test_what_is_not_there(self, fs):
        assert not fs.exists("nope.md")
        with pytest.raises(FileNotFoundError):
            fs.read("nope.md")
        with pytest.raises(IsADirectoryError):
            fs.read("runs")
        with pytest.raises(NotADirectoryError):
            fs.list("index.md")
        with pytest.raises(FileNotFoundError):
            fs.list("nope")
        with pytest.raises(FileNotFoundError):
            fs.chdir("nope")

    def test_details_are_named_as_asked(self, fs):
        assert [i.path for i in fs.list_detailed("/runs")] == ["/runs/001-r1"]
        assert [i.path for i in fs.list_detailed("runs")] == ["runs/001-r1"]
        assert [i.path for i in fs.list_detailed(".")][:1] == ["README.md"]

    def test_the_snapshot_covers_every_path(self, fs):
        snap = fs.get_metadata_snapshot()
        assert snap["index.md"].size == len(fs.read("index.md"))
        assert snap["runs"].is_dir


class TestAgno:
    def test_an_agno_session_read_back_through_its_trace(self, tmp_path):
        pytest.importorskip("agno")
        from agno.agent import Agent
        from test_agno_db import ScriptedModel, run_turn

        from nontainer.adapters.agno import WorkspaceTools
        from nontainer.adapters.agno_db import KvgitSessionDb
        from nontainer.adapters.agno_traces import AgnoTraces

        store = Store(memory=True, traces={"agno": AgnoTraces()})
        try:
            chat = store.open("chat")
            db = KvgitSessionDb(chat, db_path=str(tmp_path / "agno"))
            tk = WorkspaceTools(chat, commit="turn", session_db=db)
            agent = Agent(
                model=ScriptedModel(),
                db=db,
                session_id=chat.session,
                tools=[tk],
                post_hooks=[tk.end_turn],
                telemetry=False,
            )
            run_turn(
                agent,
                [
                    ("file_write", {"path": "a.txt", "content": "hi"}),
                    ("terminal", {"command": "exit 3"}),
                    "wrote it",
                ],
                message="Write a.txt",
            )
            [rid] = conversation.index_of(chat).runs

            ws = store.open("main")
            ws.files.attach("chat", "c", trace=True)
            assert ws.files.read("c/a.txt") == b"hi"
            index = ws.files.read("c/.trace/index.md").decode()
            assert f"001-{rid[:8]}" in index
            assert "calls:2 err:1" in index and "Write a.txt" in index
            run = ws.files.read(f"c/.trace/runs/001-{rid[:8]}/run.md").decode()
            assert "- status: completed" in run and "## Reply\n\nwrote it" in run
            ws.close()
            chat.close()
        finally:
            store.close()
