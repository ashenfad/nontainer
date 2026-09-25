"""Branches written before monkeyfs 0.1.10: refused live, migrated once.

That layout kept every file's metadata in one ``__vfs_metadata__`` table
and nontainer's cwd under ``__cwd__``. Nothing writes either any more, so
the fixtures here build them by hand, the way an old store left them.
"""

import json
import shlex

import pytest
from monkeyfs import VirtualFS

from nontainer import LayoutMigration, LegacyLayoutError, Store, workspace
from nontainer.migrate import (
    LEGACY_CWD_KEY,
    LEGACY_TABLE_KEY,
    MIGRATE_TOOL,
    main,
    migrate_provider,
)
from nontainer.providers.kvgit import KvgitProvider
from nontainer.store import _published_rows, _under

# -- building the old layout ---------------------------------------------------


def provider(tmp_path, session):
    return KvgitProvider.open(tmp_path / "kvgit", session=session)


def to_legacy(tmp_path, session, *, table=True, cwd=True, keep_slot=False):
    """Rewrite a branch head the way the old layout left it; returns the
    commit. The rows fold into one table; the cwd moves to the old key,
    leaving the filesystem's slot empty unless ``keep_slot``."""
    p = provider(tmp_path, session)
    try:
        kv = p.kv
        if table:
            entries = {}
            for key in list(kv.keys()):
                if VirtualFS.is_metadata_key(key):
                    entries[VirtualFS.path_for_metadata_key(key)] = json.loads(kv[key])
                    del kv[key]
            kv[LEGACY_TABLE_KEY] = json.dumps(entries).encode()
        if cwd:
            kv[LEGACY_CWD_KEY] = kv.get(VirtualFS.CWD_KEY) or "/workspace"
            if not keep_slot:
                del kv[VirtualFS.CWD_KEY]
        return p.commit(info={"tool": "legacy"})
    finally:
        p.close()


def legacy_write(tmp_path, session, path, data):
    """One write as the old layout made it: the blob, and its entry in
    the table. Returns the commit."""
    p = provider(tmp_path, session)
    try:
        kv = p.kv
        table = json.loads(kv[LEGACY_TABLE_KEY])
        name = path.lstrip("/")
        stamp = "2026-01-02T03:04:05+00:00"
        created = table.get(name, {}).get("created_at", stamp)
        kv[p.fs._encode_path(path)] = data
        table[name] = {
            "size": len(data),
            "created_at": created,
            "modified_at": stamp,
            "is_dir": False,
        }
        kv[LEGACY_TABLE_KEY] = json.dumps(table).encode()
        return p.commit(info={"tool": "legacy"})
    finally:
        p.close()


def metadata(tmp_path, session, commit=None):
    """Every path's metadata as a filesystem over that head reads it."""
    p = provider(tmp_path, session)
    try:
        handle = p.staged if commit is None else p.staged.checkout(commit)
        return VirtualFS(handle).get_metadata_snapshot()
    finally:
        p.close()


def keys_at(tmp_path, session):
    p = provider(tmp_path, session)
    try:
        return set(p.kv.keys())
    finally:
        p.close()


def head_of(tmp_path, session):
    p = provider(tmp_path, session)
    try:
        return p.head
    finally:
        p.close()


def seeded(tmp_path, session="old"):
    """A store with one session: files, an explicit directory, a cwd."""
    store = Store(tmp_path)
    with store.open(session) as ws:
        ws.files.write("a.txt", "alpha\n")
        ws.files.write("sub/b.txt", "beta beta\n")
        ws.terminal("mkdir -p deep; cd deep")
    return store


# -- the migration ---------------------------------------------------------------


def test_migrate_turns_the_table_into_rows_and_keeps_the_metadata(tmp_path):
    store = seeded(tmp_path)
    before = metadata(tmp_path, "old")
    legacy = to_legacy(tmp_path, "old")
    assert metadata(tmp_path, "old") == before  # the old layout reads the same

    reports = store.migrate_layout()

    report = reports["old"]
    assert isinstance(report, LayoutMigration)
    assert report.found == (LEGACY_TABLE_KEY, LEGACY_CWD_KEY)
    assert report.rows == len(before)
    assert (report.kept, report.dropped) == (0, 0)
    assert report.cwd == "/workspace/deep"
    assert not report.dry_run and not report.clean

    # one commit, on top of the legacy head, holding the whole change
    p = provider(tmp_path, "old")
    try:
        assert p.head == report.commit
        assert tuple(p.staged.versioned.parents(report.commit)) == (legacy,)
        info = next(iter(p.history(limit=1))).info
        assert info["tool"] == MIGRATE_TOOL
        for path in before:
            assert p.fs.metadata_key("/" + path) in p.kv
    finally:
        p.close()
    keys = keys_at(tmp_path, "old")
    assert LEGACY_TABLE_KEY not in keys and LEGACY_CWD_KEY not in keys
    assert metadata(tmp_path, "old") == before

    with store.open("old") as ws:
        assert ws.terminal("pwd").stdout.strip() == "/workspace/deep"
        assert ws.files.read("/workspace/sub/b.txt") == b"beta beta\n"
        assert ws.files.fs.stat("/workspace/sub/b.txt").size == len(b"beta beta\n")
        assert ws.files.fs.isdir("/workspace/deep")


def test_an_existing_row_wins_over_the_table_entry(tmp_path):
    """A head the old layout was being drained from holds both: a row
    written since, and the table entry it replaced. The row is the newer
    record, and it stands."""
    store = seeded(tmp_path)
    before = metadata(tmp_path, "old")
    to_legacy(tmp_path, "old", cwd=False)
    p = provider(tmp_path, "old")
    try:
        row = p.fs.metadata_key("/workspace/a.txt")
        written = json.dumps(
            {
                "size": 6,
                "created_at": "2026-05-05T00:00:00+00:00",
                "modified_at": "2026-05-06T00:00:00+00:00",
                "is_dir": False,
            }
        ).encode()
        p.kv[row] = written
        table = json.loads(p.kv[LEGACY_TABLE_KEY])
        table["workspace/a.txt"]["modified_at"] = "1999-01-01T00:00:00+00:00"
        p.kv[LEGACY_TABLE_KEY] = json.dumps(table).encode()
        p.commit(info={"tool": "legacy"})
    finally:
        p.close()

    report = store.migrate_layout("old")["old"]

    assert report.kept == 1
    assert report.rows == len(before) - 1
    p = provider(tmp_path, "old")
    try:
        assert p.kv[row] == written
        assert LEGACY_TABLE_KEY not in p.kv
    finally:
        p.close()


def test_the_cwd_slot_keeps_its_own_value_when_it_has_one(tmp_path):
    """A head written under both keys: the filesystem's slot is the one
    that resolved paths, so the old value is dropped, not moved."""
    store = seeded(tmp_path)
    to_legacy(tmp_path, "old", table=False, keep_slot=True)
    p = provider(tmp_path, "old")
    try:
        p.kv[LEGACY_CWD_KEY] = "/workspace/stale"
        p.commit(info={"tool": "legacy"})
    finally:
        p.close()

    report = store.migrate_layout("old")["old"]

    assert report.found == (LEGACY_CWD_KEY,)
    assert report.cwd is None
    assert LEGACY_CWD_KEY not in keys_at(tmp_path, "old")
    with store.open("old") as ws:
        assert ws.terminal("pwd").stdout.strip() == "/workspace/deep"


def test_a_second_run_is_a_no_op(tmp_path):
    store = seeded(tmp_path)
    to_legacy(tmp_path, "old")
    store.migrate_layout()
    head = head_of(tmp_path, "old")

    again = store.migrate_layout()["old"]

    assert again.clean and again.commit is None
    assert head_of(tmp_path, "old") == head


def test_a_clean_session_is_reported_and_left_alone(tmp_path):
    store = seeded(tmp_path, "fresh")
    head = head_of(tmp_path, "fresh")

    report = store.migrate_layout()["fresh"]

    assert report == LayoutMigration(session="fresh")
    assert head_of(tmp_path, "fresh") == head


def test_a_dry_run_reports_and_writes_nothing(tmp_path):
    store = seeded(tmp_path)
    before = metadata(tmp_path, "old")
    legacy = to_legacy(tmp_path, "old")

    report = store.migrate_layout(dry_run=True)["old"]

    assert report.dry_run and report.commit is None
    assert report.found == (LEGACY_TABLE_KEY, LEGACY_CWD_KEY)
    assert report.rows == len(before)
    assert report.cwd == "/workspace/deep"
    assert head_of(tmp_path, "old") == legacy
    keys = keys_at(tmp_path, "old")
    assert LEGACY_TABLE_KEY in keys and LEGACY_CWD_KEY in keys
    # and the real run reports the same numbers
    real = store.migrate_layout()["old"]
    assert (real.found, real.rows, real.kept, real.dropped, real.cwd) == (
        report.found,
        report.rows,
        report.kept,
        report.dropped,
        report.cwd,
    )


def test_an_entry_keyed_before_paths_resolved_moves_under_the_cwd(tmp_path):
    """monkeyfs 0.1.8 and earlier keyed a relative write's entry by the
    path as written, not as resolved. Such an entry describes the file
    under the cwd, and that is where its row lands; an entry that
    describes no file at all is dropped."""
    store = seeded(tmp_path)
    to_legacy(tmp_path, "old")
    p = provider(tmp_path, "old")
    try:
        p.kv[p.fs._encode_path("/workspace/deep/rel.txt")] = b"relative\n"
        table = json.loads(p.kv[LEGACY_TABLE_KEY])
        entry = {
            "size": 9,
            "created_at": "2026-01-01T00:00:00+00:00",
            "modified_at": "2026-01-01T00:00:00+00:00",
            "is_dir": False,
        }
        table["rel.txt"] = entry
        table["nowhere.txt"] = entry
        p.kv[LEGACY_TABLE_KEY] = json.dumps(table).encode()
        p.commit(info={"tool": "legacy"})
    finally:
        p.close()

    report = store.migrate_layout("old")["old"]

    assert report.dropped == 1
    p = provider(tmp_path, "old")
    try:
        row = p.kv.get(p.fs.metadata_key("/workspace/deep/rel.txt"))
        assert json.loads(row)["created_at"] == "2026-01-01T00:00:00+00:00"
        assert p.fs.metadata_key("/rel.txt") not in p.kv
        assert p.fs.metadata_key("/nowhere.txt") not in p.kv
    finally:
        p.close()


def test_migrate_layout_names_only_sessions_that_exist(tmp_path):
    store = seeded(tmp_path)
    with pytest.raises(ValueError, match="no session 'ghost'"):
        store.migrate_layout("ghost")
    assert "ghost" not in store.sessions()
    with pytest.raises(Exception):
        store.migrate_layout("@store/pub/app/v1")


def test_migrate_provider_refuses_uncommitted_writes(tmp_path):
    seeded(tmp_path)
    to_legacy(tmp_path, "old")
    p = provider(tmp_path, "old")
    try:
        p.kv["__cache__/x"] = b"pending"
        with pytest.raises(Exception, match="uncommitted"):
            migrate_provider(p)
        p.discard()
        assert migrate_provider(p).commit == p.head
    finally:
        p.close()


# -- refusing rather than misreading ---------------------------------------------


def test_a_writable_open_of_an_unmigrated_session_is_refused(tmp_path):
    store = seeded(tmp_path)
    legacy = to_legacy(tmp_path, "old")

    with pytest.raises(LegacyLayoutError) as excinfo:
        store.open("old")
    error = excinfo.value
    assert error.session == "old"
    assert error.keys == (LEGACY_TABLE_KEY, LEGACY_CWD_KEY)
    message = str(error)
    assert "'old'" in message
    assert LEGACY_TABLE_KEY in message and LEGACY_CWD_KEY in message
    assert f"python -m nontainer.migrate --store={tmp_path} --session=old" in message
    assert f"Store({str(tmp_path)!r}).migrate_layout(['old'])" in message

    # the one-liner refuses the same way, and nothing was written
    with pytest.raises(LegacyLayoutError):
        workspace("old", store=tmp_path)
    assert head_of(tmp_path, "old") == legacy

    store.migrate_layout("old")
    with store.open("old") as ws:
        assert ws.files.read("/workspace/a.txt") == b"alpha\n"


def test_a_head_with_only_the_table_is_refused_too(tmp_path):
    store = seeded(tmp_path)
    to_legacy(tmp_path, "old", cwd=False)
    with pytest.raises(LegacyLayoutError) as excinfo:
        store.open("old")
    assert excinfo.value.keys == (LEGACY_TABLE_KEY,)


def test_a_dir_session_carrying_the_old_cwd_key(tmp_path):
    """Every backend kept nontainer's own cwd key once. A dir session
    holds no table, so only the cwd moves, and there is no commit."""
    store = Store(tmp_path, backend="dir")
    with store.open("plain") as ws:
        ws.terminal("mkdir -p deep")
    from nontainer.providers.dir import DirProvider

    p = DirProvider(tmp_path / "plain", session="plain")
    p.kv.pop(VirtualFS.CWD_KEY, None)
    p.kv[LEGACY_CWD_KEY] = "/workspace/deep"
    p.close()

    with pytest.raises(LegacyLayoutError) as excinfo:
        store.open("plain")
    assert "--backend=dir" in str(excinfo.value)

    report = store.migrate_layout()["plain"]
    assert report.found == (LEGACY_CWD_KEY,)
    assert report.cwd == "/workspace/deep" and report.commit is None
    with store.open("plain") as ws:
        assert ws.terminal("pwd").stdout.strip() == "/workspace/deep"


# -- the paths that make an old tree live ---------------------------------------


def test_merges_between_migrated_branches_are_clean(tmp_path):
    """Two branches that diverged under the old layout, each migrated.
    Their merge base still holds the table and the old cwd key; both
    sides deleted them, and the rows each side wrote agree or merge by
    field — so nothing is contested but the file both sides edited."""
    store = Store(tmp_path)
    with store.open("main") as ws:
        ws.files.write("shared.txt", "one\ntwo\nthree\n")
        ws.files.write("keep.txt", "keep\n")
        ws.fork("worker").close()
    to_legacy(tmp_path, "main")
    to_legacy(tmp_path, "worker")
    legacy_write(tmp_path, "main", "/workspace/main.txt", b"from main\n")
    legacy_write(tmp_path, "worker", "/workspace/worker.txt", b"from the worker\n")
    store.migrate_layout()

    with store.open("main") as ws:
        out = ws.merge("worker")
        assert out.merged
        assert out.conflicts == ()
        assert ws.files.read("/workspace/worker.txt") == b"from the worker\n"
        assert ws.files.read("/workspace/main.txt") == b"from main\n"
        fs = ws.files.fs
        assert fs.stat("/workspace/worker.txt").size == len(b"from the worker\n")
        assert fs.stat("/workspace/shared.txt").size == len(b"one\ntwo\nthree\n")
        kv = ws._provider.kv
        assert LEGACY_TABLE_KEY not in kv and LEGACY_CWD_KEY not in kv


def test_merging_an_unmigrated_branch_is_refused_by_name(tmp_path):
    store = Store(tmp_path)
    with store.open("main") as ws:
        ws.files.write("a.txt", "a\n")
        ws.fork("worker").close()
    to_legacy(tmp_path, "worker")

    with store.open("main") as ws:
        head = ws.head
        with pytest.raises(LegacyLayoutError) as excinfo:
            ws.merge("worker")
        assert excinfo.value.session == "worker"
        assert "--session=worker" in str(excinfo.value)
        assert ws.head == head


def test_an_old_source_commit_merges_as_its_migrated_head(tmp_path):
    """The commit a merge takes can be older than the source's head —
    the source agent's last commit. Where the head is that commit
    migrated, with no file changed since, the head is what merges; where
    a file changed, the old commit is refused."""
    store = Store(tmp_path)
    with store.open("main") as ws:
        ws.files.write("a.txt", "a\n")
        ws.fork("worker").close()
    to_legacy(tmp_path, "worker")
    old = legacy_write(tmp_path, "worker", "/workspace/w.txt", b"worker\n")
    store.migrate_layout("worker")
    migrated = head_of(tmp_path, "worker")

    main = provider(tmp_path, "main")
    try:
        out = main.merge("worker", at=old)
        assert out.merged
        assert main.staged.versioned.parents(out.commit)[1] == migrated
        assert main.fs.stat("/workspace/w.txt").size == len(b"worker\n")
        assert LEGACY_TABLE_KEY not in main.kv
    finally:
        main.close()

    # a source that moved on since the old commit: that commit is refused
    p = provider(tmp_path, "worker")
    try:
        p.fs.write("/workspace/w.txt", b"changed\n")
        p.commit()
    finally:
        p.close()
    with store.open("main") as ws:
        with pytest.raises(LegacyLayoutError):
            ws._provider.merge("worker", at=old)


def test_a_restore_to_an_old_commit_lands_in_the_current_layout(tmp_path):
    store = seeded(tmp_path)
    before = metadata(tmp_path, "old")
    legacy = to_legacy(tmp_path, "old")
    store.migrate_layout()

    with store.open("old") as ws:
        ws.files.write("/workspace/a.txt", "rewritten\n")
        ws.files.remove("/workspace/sub/b.txt")
        landed = ws.checkout(legacy)
        kv = ws._provider.kv
        assert LEGACY_TABLE_KEY not in kv and LEGACY_CWD_KEY not in kv
        assert ws.files.read("/workspace/a.txt") == b"alpha\n"
        assert ws.files.read("/workspace/sub/b.txt") == b"beta beta\n"
        assert ws.terminal("pwd").stdout.strip() == "/workspace/deep"
        # the same state again writes nothing
        assert ws.checkout(legacy) == landed
    assert metadata(tmp_path, "old") == before


def test_a_fork_from_an_old_commit_is_migrated_on_its_own_branch(tmp_path):
    store = seeded(tmp_path)
    before = metadata(tmp_path, "old")
    legacy = to_legacy(tmp_path, "old")
    store.migrate_layout()

    with store.open("old") as ws:
        child = ws.fork("child", at=legacy)
        try:
            kv = child._provider.kv
            assert LEGACY_TABLE_KEY not in kv and LEGACY_CWD_KEY not in kv
            assert child.files.read("/workspace/sub/b.txt") == b"beta beta\n"
            tools = [c.info.get("tool") for c in child.log(kind="all")]
            assert MIGRATE_TOOL in tools
        finally:
            child.close()
    with store.open("child") as ws:
        assert ws.files.read("/workspace/a.txt") == b"alpha\n"
    # the fork point itself is untouched
    p = provider(tmp_path, "old")
    try:
        assert LEGACY_TABLE_KEY in p.staged.checkout(legacy)
    finally:
        p.close()
    assert metadata(tmp_path, "child") == before


def test_applying_an_old_commit_writes_rows_not_the_table(tmp_path):
    """A revert or cherry-pick of a commit made under the old layout:
    the change it made to the table arrives as the row of the file it
    wrote."""
    store = seeded(tmp_path)
    to_legacy(tmp_path, "old")
    base = head_of(tmp_path, "old")
    theirs = legacy_write(tmp_path, "old", "/workspace/a.txt", b"changed later\n")
    store.migrate_layout()

    p = provider(tmp_path, "old")
    try:
        undone = p.apply(theirs, base)
        assert undone.merged
        assert p.fs.read("/workspace/a.txt") == b"alpha\n"
        assert p.fs.stat("/workspace/a.txt").size == len(b"alpha\n")
        redone = p.apply(base, theirs)
        assert redone.merged
        assert p.fs.read("/workspace/a.txt") == b"changed later\n"
        assert p.fs.stat("/workspace/a.txt").size == len(b"changed later\n")
        assert LEGACY_TABLE_KEY not in p.kv and LEGACY_CWD_KEY not in p.kv
    finally:
        p.close()


# -- frozen state -----------------------------------------------------------------


def _legacy_write_publication(
    self, ws, head, *, branch, paths, exclude, tag, commit_info
):
    """``Store._write_publication`` as the old layout would have
    written it: the blobs, and one table describing them."""
    import kvgit

    src = ws._provider
    handle = src.staged.checkout(head)
    wanted = {
        key: path
        for key, path in src._file_keys(handle.keys()).items()
        if _under(path, paths, ws.root) and not _under(path, exclude, ws.root)
    }
    rows = _published_rows(
        VirtualFS(handle).get_metadata_snapshot(), set(wanted.values())
    )
    pub = kvgit.store(kind="disk", path=str(self._kvgit_path()), branch=branch)
    try:
        for key in wanted:
            pub[key] = handle.get(key)
        pub[LEGACY_TABLE_KEY] = json.dumps(rows).encode()
        pub.commit(info=commit_info)
        commit = pub.current_commit
        KvgitProvider(pub, session=branch).tag(
            tag, at=commit, info=commit_info, scope="store"
        )
        return commit
    finally:
        self._close_backend(getattr(pub.versioned, "store", None))


def test_an_old_publication_still_serves_and_is_never_rewritten(tmp_path, monkeypatch):
    store = Store(tmp_path)
    with store.open("author") as ws:
        ws.files.write("app/index.html", "<h1>old</h1>")
        with monkeypatch.context() as patch:
            patch.setattr(Store, "_write_publication", _legacy_write_publication)
            pub = store.publish(ws, "site")
    branch = pub.current_version.ref.session
    assert LEGACY_TABLE_KEY in keys_at(tmp_path, branch)
    head = head_of(tmp_path, branch)

    snapshot = pub.open()
    try:
        assert snapshot.files.list("app") == ["app/index.html"]
        body = snapshot.files.read("/workspace/app/index.html")
        assert body == b"<h1>old</h1>"
        assert snapshot.files.fs.stat("/workspace/app/index.html").size == len(body)
    finally:
        snapshot.close()

    assert store.migrate_layout() == {"author": LayoutMigration(session="author")}
    assert head_of(tmp_path, branch) == head


def test_old_commits_read_frozen_by_ref_and_by_tag(tmp_path):
    store = seeded(tmp_path)
    legacy = to_legacy(tmp_path, "old")
    store.migrate_layout()

    with store.resolve(f"old@{legacy}") as snap:
        assert snap.files.read("/workspace/sub/b.txt") == b"beta beta\n"
        assert snap.files.fs.stat("/workspace/sub/b.txt").size == len(b"beta beta\n")
        assert "/workspace/sub" in snap.files.list("/workspace")
    store.tags.add(f"old@{legacy}", "before")
    with store.tags.at("before") as snap:
        assert snap.files.read("/workspace/a.txt") == b"alpha\n"
    assert store.tags.list()["before"] == legacy


# -- reserved names -----------------------------------------------------------------


def test_the_legacy_keys_are_never_files(tmp_path):
    """A stray table key is not a file in any listing or diff; and a
    file NAMED like either key is a file, stored under its own encoded
    key, which trips no refusal."""
    store = seeded(tmp_path)
    legacy = to_legacy(tmp_path, "old")
    with store.resolve(f"old@{legacy}") as snap:
        listed = snap.files.list("/", recursive=True)
        assert not [p for p in listed if "metadata" in p or "cwd" in p]
        assert LEGACY_TABLE_KEY not in snap._provider._file_keys([LEGACY_TABLE_KEY])
    store.migrate_layout()

    with store.open("named") as ws:
        ws.files.write(LEGACY_TABLE_KEY, "a file")
        ws.files.write(LEGACY_CWD_KEY, "another")
        kv = ws._provider.kv
        assert LEGACY_TABLE_KEY not in kv and LEGACY_CWD_KEY not in kv
    with store.open("named") as ws:
        assert ws.files.read(LEGACY_TABLE_KEY) == b"a file"
        diff = ws.diff(legacy, ws.head)
        assert all("__vfs_metadata__" != p.rsplit("/", 1)[-1] for p in diff.removed)


# -- the command line -----------------------------------------------------------------


def test_the_command_line_dry_runs_then_migrates(tmp_path, capsys):
    seeded(tmp_path)
    seeded(tmp_path, "fresh")
    legacy = to_legacy(tmp_path, "old")

    assert main(["--store", str(tmp_path), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "old: would migrate __vfs_metadata__, __cwd__" in out
    assert "fresh" not in out.splitlines()[0]
    assert "would migrate 1 of 2 sessions" in out
    assert head_of(tmp_path, "old") == legacy

    assert main(["--store", str(tmp_path), "--session", "old"]) == 0
    out = capsys.readouterr().out
    assert "old: migrated __vfs_metadata__, __cwd__" in out
    assert "migrated 1 of 1 session" in out
    assert head_of(tmp_path, "old") != legacy

    assert main(["--store", str(tmp_path)]) == 0
    assert "migrated 0 of 2 sessions" in capsys.readouterr().out


def test_the_refusal_prints_a_command_that_runs_as_written(tmp_path, capsys):
    store_dir = tmp_path / "my store"
    store = seeded(store_dir, "-old")
    legacy = to_legacy(store_dir, "-old")

    with pytest.raises(LegacyLayoutError) as excinfo:
        store.open("-old")
    message = str(excinfo.value)
    command = message.split("retry: ", 1)[1].split(", or Store(", 1)[0]
    argv = shlex.split(command)
    assert argv[:3] == ["python", "-m", "nontainer.migrate"]

    assert main(argv[3:]) == 0
    assert "-old: migrated" in capsys.readouterr().out
    assert head_of(store_dir, "-old") != legacy


def test_the_command_line_names_a_session_it_cannot_migrate(tmp_path, capsys):
    seeded(tmp_path)
    assert main(["--store", str(tmp_path), "--session", "ghost"]) == 2
    assert "no session 'ghost'" in capsys.readouterr().err
