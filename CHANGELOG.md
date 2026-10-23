# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## 0.11.6 - 2026-10-22

### Added
- **`nontainer.traces`: a stored conversation rendered as files**, the first piece of curation's trace projection. A `TraceSource` reads one stored run, in its harness's format, into a neutral `TraceRun` (status, times, model, `TraceTokens`, system prompt, and `TraceEvent`s: user, assistant, thinking, and one per tool call with its result). `Trace` renders `README.md`, `index.md`, `system/<hash>.md`, and each run's `run.md` and event files from that shape, so a trace reads the same whichever harness wrote it. `NeutralTraces` reads runs stored in the neutral shape itself.
- **`ws-git worktree add --trace` mounts a session's conversation beside its files**, at `<dir>/.trace`, pinned at the same commit and read-only: `README.md`, `index.md` (one line per run), each run's `run.md` and event files, and each distinct system prompt once. The host's spelling is `ws.files.attach(ref, at, trace=True)`; one `detach` takes both down. Nothing is read until the trace is, and each file is rendered on its first read. `worktree list` says how many runs each trace holds. A state with no conversation is refused, and so is one written by a harness the store has no source for.
- **Each run's `changed.md` says what it changed in the files**: the commits after the run before it landed, through its own last landing, that changed a file, each under the tool its commit names (the host's too, a `file_write` or a `put`), with paths marked A, M or D. Nothing from before the session's fork counts, so a forked session's first run isn't given its parent's history. `index.md` and `run.md` count the files. So `grep -l helpers/excel.py */.trace/runs/*/changed.md` finds the run that wrote a file. Read from the branch's history with the runs; where the provider keeps none, or a run never landed on the branch, it says so. `nontainer.traces` gains `TraceCommit` and `run_ranges`, which assigns commits to runs by their turn stamps.
- **`worktree add --trace --since <ref>` marks the runs new since a state** with `+` in `index.md`, and `worktree list` counts them (`trace: 23 runs, 4 new`). The ref is any ref `worktree add` takes, or a bare commit of the session being traced; a run that landed before it and again after is new. The host's spelling is `ws.files.attach(..., trace=True, since=ref)`.
- **`ws-git show <ref>:.trace/<path>` reads one file of another session's trace**, or a store tag's, with nothing mounted, so a citation is one command. A state with no conversation reads its tree's own `.trace`; your own session's `.trace/` is always your tree's.
- **`Store(traces={harness: source})`** says how the store reads a stored conversation, by the harness name its index carries: `Store(..., traces={"agno": AgnoTraces()})`. Nothing by default. `store.traces` is the mapping, read-only.
- **The ws-git corpus seeds conversations.** A `Converse` step stores a run in the neutral shape through a turn, and the corpus store reads the `"neutral"` harness, so `worktree add --trace` is pinned for agex-ts's port without porting a harness's parser.
- **The trace corpus** (`nontainer/conformance/traces/json/`) pins the rendering byte for byte, for agex-ts's port: each case's runs as neutral bodies, with every file they render to.
- **`nontainer.adapters.agno_traces.AgnoTraces`, the trace source for agno's stored runs.** It reads a run `KvgitSessionDb` stored into the neutral `TraceRun`: status in core's words, times, token counts, the system prompt, and the run's own events. History agno replayed into the run is left out, and each call is paired with its result. It parses the stored dict alone, so reading an agno session's trace needs no agno install. A tool call counts as an error when the stored run flags it, which it now does for nontainer's own tools too (see Fixed).

### Changed
- **`ws-git stage` is `ws-git add` under its own name.** Both take paths, a directory (`.` included) or `-A`, so `ws-git stage -A` stages everything modified and `ws-git stage <dir>` what is modified under it.
- **A ws-git verb given arguments it does not take answers in one line.** What was wrong, the verb's synopsis and `see 'ws-git help'` replace the whole usage text, and an unknown verb lists the verbs.
- **Requires `termish>=0.2.4`; `$HOME` is the workspace root.** termish 0.2.4 makes an unset variable an error, so the local terminal sets `HOME` to the workspace root unless the runtime's environment sets it.

### Fixed
- **`ws-git add /` no longer stages the whole workspace.** A path above the root was read as a directory holding every modified file; it is now refused like any path outside the root, and on a frozen snapshot `add -A` with nothing to stage reports the snapshot frozen rather than succeeding.
- **A failed command's stderr appears once, as it was written.** The terminal's `[stderr]` is only what reached stderr: nothing after `2>&1`, `2>/dev/null`, `2>file` or a silent failure such as `grep` with no match (no more `grep: exited with code 1`), and `ls: ...` rather than `ls: ls: ...`.
- **A failed tool call is stored as failed in an agno session.** nontainer's tools return normally and say in their text that a call failed (a non-zero exit, python that raised, an edit that missed), so agno stored every one with `tool_call_error: false`, and a trace counted no errors. `WorkspaceTools` now logs each call's outcome by the call id agno hands it, and `KvgitSessionDb` and `KvgitStoreDb` flag the failed calls' messages and `tools` entries in the run they store. A flag already stored is kept when agno writes the session again. What the model sees is unchanged: agno never sends the flag to a provider. agno 2.x can't leave the call id's parameter out of a tool's schema without logging an error for it; nontainer filters out exactly that error.

## 0.11.5 - 2026-10-16

### Changed
- **Printed values are rendered as they are printed, not copied** (#263). The sandbox renders each print with reprobate when it is made, so an oversized stdout is rebuilt from text; a printed 6M-row DataFrame no longer costs the host 164MB under process isolation.
- **Over-long output keeps its start and its end.** Output that isn't prints, and stderr, used to keep only their start; captured output is now bounded in the sandbox at four observations.
- **Requires `sandtrap[render]>=0.5.1`, `reprobate>=0.1.6` and `kvgit>=0.4.3`.** kvgit 0.4.3 bounds a worktree's read cache (32MB by default).

## 0.11.4 - 2026-10-16

### Added
- **`keep=` on `run_python`, `arun_python` and `runtime.exec_python`** names the bindings `result.namespace` carries; under process isolation the rest stay in the worker, unpickled (#263). `ui` is materialized either way.

### Changed
- **Breaking: `memory_limit_mb` with `isolation="none"` is refused at construction** (#265). In-process, the limit capped the whole host process and its threads, not the sandboxed code. Use `isolation="process"` or `"kernel"`.
- **The agent's `run_python` tool returns only `ui` from its namespace**, and apps dispatch and ws-pytest only their own result bindings, so a call's scratch values no longer cross from a worker (#263). An executor without `keep` still serves them: the runtime narrows the namespace itself.
- **Requires `monkeyfs>=0.2.8` and `sandtrap>=0.5.0`.** A `run_python` call whose file save fails at an implicit close (`open(p, 'w').write(x)`) now fails with `StLostWrite` naming the file, rather than reporting success.

### Fixed
- **An oversized `ui` value is refused before it is fully serialized** (#266). Text is measured before encoding, and a plotly figure whose arrays alone exceed the 8MB cap is refused before its encoder runs; that note gives the size as "at least".
- **Sandboxed code reads large files from a disk store** (#264). With `isolation="none"`, a binary read of a file of 32KB or more written in an earlier commit raised `FileNotFoundError`.

## 0.11.3 - 2026-10-15

### Added
- **`KvgitStoreDb.get_sessions` and `KvgitSessionDb.get_sessions` honor agno's `include_runs=False`** (#257). agno 3 lets a list view ask for sessions without their run history; the argument was accepted and ignored, so every listed session's runs were still read. Now `runs` comes back `None` and no run is read. The default still returns the whole history.
- **`skills.mounts_from_modules(python_or_modules, root=...)`**, the library-embedded-skill convention as mounts: every granted module whose package ships `<pkg>/skills/<name>/SKILL.md` gets a read-only mount per skill. It takes a `PythonConfig` or its `modules` list (modules, `ModuleGrant`s, names, nested preset lists), looks at each package once, skips a package whose skills have no directory on disk (a zip import), and raises `ValueError` for two skills with one name, as `mounts` does.

### Changed
- **A store listing reads only the heads that moved** (#257). `KvgitStoreDb.get_sessions` read every branch's whole session record, `session_data` and all, to filter, sort and count, on every call. It now does that on a small row per head (session id, user, agent, name, created and updated times), kept by commit hash. A commit never changes, so a kept row is never stale: a turn, a rename, a fork, a checkout or a deletion moves the branch to another commit, which the next listing reads. Whole records are read only for the page returned, at the commit its row came from. Existing stores need nothing done.

### Removed
- **Breaking: `skills.install` and `skills.install_from_modules` are gone** (#256). A skill they copied in landed as workspace files in a framework commit, so ws-git, which measures from the agent's own last commit, listed it in a fresh session as work the agent never did. Nothing called them: embedders mount their skills. Mount instead: `skills.mounts(dir)` for an embedder's set, and `skills.mounts_from_modules(python)` for the skills granted packages ship. An agent's own skills are files it writes, as before. The name `install` is left free for curation's take of a published release, which will land in the agent's graph.

### Fixed
- **Sessions with equal times list in one order** (#257). Ties fell back to the order of the store's branches, so a page boundary between two sessions with the same `created_at` could repeat one and skip the other. They are now ordered by session id, in either direction.
- **A skill named with dots alone is refused.** `name: ..` in a skill's frontmatter put its mount point at `<root>/skills/..`, the workspace root; `mounts` now raises `ValueError`. Leading dots are dropped from a name, so a skill no longer lands as a hidden directory.

## 0.11.2 - 2026-10-13

### Added
- **A `ws-git` script corpus for ports of the verb.** `nontainer.conformance.wsgit` holds short scripts covering every verb and its refusals: terminal commands across sessions, plus the host steps that set a scene. `python -m nontainer.conformance.export` records each script by running it and writes it under `conformance/wsgit/json/` with what every command printed. Commit ids are named by order of appearance, so a port in another language can replay the steps and compare. The drift test fails when the verb's output changes without a re-export.
- **Mount a filesystem object, not only a host directory** (#242). `Mount(fs=obj)` mounts any object with the monkeyfs reads (`exists`, `isdir`, `isfile`, `list`, `read`, `stat`; the composition derives `getsize`, `islink` and `glob` when it has none of its own), so a workspace can expose content that isn't on local disk, such as a document store fetched as the agent reads it. Both tools see it, under `isolation="process"` too, and only the agent's reads reach it: commits, ws-git and forks read none of it. It is mounted read-only, a fork shares it, and closing the workspace leaves it open. An executor that copies the tree into a machine of its own declares `copies_tree`, and refuses such a mount; `DudExecutor` does.

### Fixed
- **ws-git measures a fork's first commit from where the fork began.** `ws-git show` diffed a fork's first commit against nothing, so an inherited file it edited showed as wholly added and one it deleted didn't show; `ws-git log -S` credited it with every match it inherited. Both now measure from the fork's base, as revert and cherry-pick do.
- **A store tag can't be named like a commit id.** ws-git reads seven or more hex characters as a commit id, so such a tag was unreachable by name; `store.tags.add` now refuses the name, as ws-git's own tags already do.
- **The conformance runner catches a harness that stops short** (#241). A turn that ended with replies left in its script passed, so a harness that skipped the reply after its tool calls passed every scenario that doesn't check events, the fork scenarios among them. A turn with steps left now fails `turn<N>.script`, and a delegate `delegate.<name>.script`; a scenario's scripts, its delegates' included, must end on a reply.
- **ws-git reports a conflict in a file that already holds marker text** (#246). A merge counted a file as conflicted only when it held `<<<<<<< ` and neither parent's version did. So in a file with marker text of its own (notes on resolving merges, a test fixture), a real conflict from an overlapping edit was reported as auto-merged: the merge exited 0, recorded no outstanding merge, and `ws-git status` read clean over live markers. Revert and cherry-pick had the same gap. Each file's text merge now reports whether it conflicted (kvgit's `text_merge_result`), and that answer is what counts; the merged bytes are unchanged.
- **A turn whose run fails to store keeps the notes it delivered** (#243). `Turn.end` settled the inbox only after storing the run, so when storing raised, the delivered notes stayed unsettled: the next turn neither delivered them again nor found them in any stored run, and its end settled them. A person's correction was lost with no sign. They now go back to the front of the queue, and the error is raised as before. A failed write also leaves the stored conversation as it was: `conversation.write` puts back the keys it had already written, where it left a run body staged that no index named, for the next commit to land.
- **Notes from failing delivery sources keep their sources' order** (#243). Each failing source's notes were put back at the head of the queue one source at a time, so the second source's notes came out ahead of the first's. They are now put back once, in source order.
- **ws-git across a directory/file swap** (#246). A path that is a file in one state and a directory in another is now handled wherever ws-git writes a tree back:
  - A checkout from a state where `x/` held an empty directory made with `mkdir` back to a commit where `x` is a file left `x` a file with `x/empty` still listed under it. A file written where a directory stands now clears it first when only directories are left in it (as git does), and is refused with `IsADirectoryError` when it still holds a file.
  - A checkout that failed partway, at the commit or during the rewrite, left the tree half restored: the rewrite ran before the block that undoes it. It now runs inside it, and a failure puts back the files and the directories it cleared.
  - Staging one side of a swap (`x/c.txt` but not the deletion of the file `x`, or the reverse) made a commit holding both or neither. A staged path that is in the tree now takes the paths in its way with it, as `git add` takes them; a staged deletion takes nothing along, so staging the removal of `x/a` commits the rest of `x/` and leaves a new file `x` uncommitted. `ws-git add x`, where `x` is now a file and was a directory or the reverse, stages `x` itself as well as what was under it. A swap left out of a commit stays in the tree, empty directories included.
  - On the dud rung, a shell that swapped a file for a directory (`rm x; mkdir x; ...`) failed to land: the harvest wrote before it deleted. Deletes now land first.
- **ws-git checkout leaves ignored paths as they are** (#250). The agent's checkout (`ws-git checkout`, `ws.index.checkout`, and so `ws-git stash`) restored every path that differed from the commit, ignored ones included. Files outside the root, authoring output (`app/logs`, `app/screenshots`) and whatever the embedder's `ignore=` patterns name were rolled back to the commit's copy, and deleted when they were made after it, so an agent lost its own logs and scratch, and could rewind state an embedder parks outside the root. Ignored paths are now left alone, as git's checkout leaves them. The one exception is git's too: an ignored file standing where the commit puts a tracked file is removed to make way, and `ws-git checkout` names it. The host's `ws.checkout` is unchanged: it restores the whole session, ignored paths included.
- **`AgentFSProvider` raises the standard library's errors.** The SDK's own `ErrnoException` escaped `write`, `list`, `mkdir` and `makedirs`, and `stat`, `read`, `remove`, `rmdir` and `rename` turned every failure into `FileNotFoundError` (with no errno, for `stat`) or a bare `OSError`. termish, the monkeyfs patch layer and its conformance kit catch the `OSError` subclass for each errno, so a write over a directory, for one, wasn't recognised as `IsADirectoryError`. Each call now raises the subclass the standard library would for the code AgentFS reports (`IsADirectoryError`, `NotADirectoryError`, `FileNotFoundError`, ENOTEMPTY's `OSError`), with the errno and path set. Reading or removing a directory now says `IsADirectoryError` where it said `FileNotFoundError`.

- **`ws-pytest .` and `ws-pytest /workspace` run the whole tree.** Naming the workspace root as a selector failed with `file or directory not found: workspace`: the root read as a directory of that name beneath it. Both `ws-pytest` and `ws-vitest` now read the root as the root.
### Changed
- **Needs termish 0.2.3.** Its `grep -r` reads a tree in batches through `read_many` rather than one call per file, which matters most over a `Mount(fs=...)` that fetches remotely, and it fixes eight parser, interpreter and `MemoryFS` bugs (termish#31). It also makes termish's `MemoryFS` pass monkeyfs's conformance kit as of its next release.
- **Needs monkeyfs 0.2.7.** A mounted object needs only the six reads in `MOUNT_FS_METHODS`: `os.walk`, `glob`, `os.path.getsize` and the test verbs' discovery work over one without `getsize`, `islink` or `glob`. `ws-pytest` and `ws-vitest` pointed above a mount (`ws-pytest .`) find the tests inside it, where a glob rooted above a mount never searched it, and a directory mount's glob stays inside its directory, where `..` or a symlink could list host paths outside it. The workspace's own glob now matches by path segment, as a real directory does, so test discovery no longer picks up a file like `test_utils/helpers.py`. A write over a directory raises `IsADirectoryError` rather than leaving the path both a file and a directory.

## 0.11.1 - 2026-10-12

### Fixed
- **A read-only mount under the workspace root no longer blocks creating files there** (#236). In a fresh session with a mount at, say, `/workspace/docs`, `run_python` couldn't create `/workspace/a.txt` or anything else in a directory leading to the mount: in-process it raised `FileNotFoundError`, under `isolation="process"` `PermissionError`. The mount showed those directories, but nothing had made them in the store. Needs monkeyfs 0.2.6.

## 0.11.0 - 2026-10-12

### Added
- **Specs as data.** `values.export_specs` writes specs out as plain JSON, records and enums by name, fields and docstring, and `load_specs` reads them back over dataclasses and enums it builds, evaluating nothing.
- **A world's helper, found from the world.** `Sessions.of(ws)` is the helper open over a workspace, so a host object built for that world can delegate through it.
- **Loaded specs cross every rung.** The classes `values.load_specs` builds pickle as the data they were built from, so they serve as a world's classes and typed data's types under process isolation and on dud too; `values.is_shape` tells them apart.
- **Host objects per world.** `HostObject(factory=...)` is called with each world as it opens, forks included, for that world's own object; a profile and a fork carry the factory.
- **An ask can bring its own runner.** `Sessions.ask(..., runner=)` runs that job on a runner of its own, sync or async, on any helper; an async runner is typed `AsyncSessionRunner`.
- **Turn events carry what a transcript shows.** A delivered delegate note carries its `Answer`, and `Compacted` its fold's `summary`, `tokens_before` and `tokens_after`; `DeliveredNote.of(note)` and `Compacted.of(fold)` build them.
- **An empty view.** `fork(paths=[])` gives a child none of the caller's files and forks it from the last commit, landing nothing, so a fork taken mid-call doesn't split that call's commit.
- **`ws.runtime.interrupt()` stops the running call.** It ends as at a timeout, from any thread; `LocalExecutor` cancels the sandbox and `DudExecutor` the guest's exec; needs sandtrap 0.4.4, whose process workers no longer hang when cancelled again and again, and dud 0.4.4 for the dud extra.

### Changed
- **Agent code runs in a module of its own on every rung.** A dataclass with a quoted or postponed annotation works, and a leading `from __future__` import takes effect, on dud too (it goes above the guest's prelude); needs sandtrap 0.4.3.
- **The cache refuses what the code defined, in-process too.** A value holding a class or function of the agent's own is a `CacheError` at the write, as it is under isolation and on dud.
- **Host calls don't count against the timeout.** On every rung, time inside a host object's call is off the run's clock; needs sandtrap 0.4.2, and dud 0.4.2 for the dud extra.
- **A stub's host half runs as host code in-process.** It ran in the sandbox's context, which refused its own network calls and the work it scheduled; it now runs in the embedder's, as it does elsewhere, unless handed a live argument.

### Fixed
- **A kept agno run answers the tool call a stop cut off, and says why.** A provider refuses a call with no result, so every later request in the session failed; the result names the run's note, so a stop doesn't read as a time limit.

## 0.10.1 - 2026-10-08

### Fixed
- **A fork can check out a commit from before it was forked.** The restored conversation named the parent, so the store lost the fork and refused its writes; the checkout now rebinds it to the fork in the same commit, and checking out again repairs a fork already in that state.

## 0.10.0 - 2026-10-08

### Added
- **`static_assets` serves files from memory.** A source may be a mapping of relative path to bytes as well as a host directory, for an asset built at startup; the config keeps a checked copy.
- **Delegation in the harness corpus (tier 5).** Scenarios script delegates (`Scenario.delegates`, `asks`, `delegate_answers`) and check answers delivered once, a delegate waiting on its own, spent wakes naming the unread, and a late note read first; harnesses add `sessions=`, `wake()` and `delegation()` (`nontainer.adapters.corpus_delegates`).
- **`until_settled` / `auntil_settled`.** A delegate's turns run until neither its own delegates nor a note in its inbox is outstanding, woken to read them, within `max_wakes` or until the helper closes (`Sessions.closed`); `Turn.opening()` / `aopening()` is a woken turn's first message, delivered as a tool result's notes are.
- **Async delegate runners.** A `SessionRunner` whose `run` is `async def` runs on the embedder's loop (`Sessions(..., loop=)`), `max_workers` at a time, and `cancel` stops it; `aask`, `await_ready`, `answers()` and `aclose` serve coroutines.
- **Compaction in the harness corpus (tier 4).** Scenarios pin a fold's record and its rules (in force, rewind and fork, never stored), not how a harness folds; scripted steps report `input_tokens`, a scenario sets `budget`, and `clock.next(sent)` sees what a request carried.
- **A compiled `Spec` stands for its type.** Wherever nontainer takes a type (a `HostObject`'s `type`, a stubbed object's annotations, inside another annotation), a `values.Spec` serves, carrying the names it was compiled with.
- **Typed calls through a stub: `HostObject(obj, stub=...)`.** Code in the sandbox holds a class of the embedder's that calls the live object, typed by its annotations; an argument that doesn't fit raises `TypeError` at the call, and every argument reaches the host built afresh as its declared type, never unpickled.
- **Typed host data and classes: `HostObject(obj, type=...)`, `PythonConfig.classes`.** Data of a declared type reaches the sandbox by value on every rung, a fresh copy each run, and classes are bound by name; on dud both cross by pickle and by module source.
- **Typed values: `nontainer.values`.** A strict check of a value against its declared type (deep into records, sampled past 10,000 items), and an encoding that decodes only into that type, with tables as Arrow and arrays as `.npy`; standard library only, so it runs inside a VM guest.
- **`turn.deliver(result)`.** Appends the notes queued for a tool result and returns them; `adeliver` awaits `on_delivered`, `collect` serves results that are not text, and `sources=` adds notes such as delegate answers.
- **`nontainer.sessions.answer_notes(helper, inbox)` and `Inbox.announce`.** Delegate answers as delivered notes, and the `on_delivered` call; the agno toolkit's delivery is built on both, unchanged.

### Changed
- **Tracebacks show the agent's line, not stub plumbing.** A stubbed call's refusal drops `nontainer.remote`'s frames, and a dud run's traceback is trimmed as the other rungs' are, host install paths included.
- **Breaking: `PythonConfig.host_objects` is read-only.** Each executor takes its host objects when it opens, so one added to the mapping later reached in-process code and nowhere else; a different set is a new config (`dataclasses.replace`).

### Fixed
- **Nested `static_assets` prefixes serve from the most specific one.** A path went to the first declared prefix covering it, so `vendor` listed before `vendor/charts` hid the nested directory; order no longer matters, and two spellings of one prefix (`vendor`, `vendor/`) are refused.
- **Code waiting for a delegate no longer hangs.** Landing an answer read the parent through its workspace lock, which a `run_python` call waiting for that answer holds; it now reads committed history through the child's handle.

## 0.9.1 - 2026-10-06

### Added
- **The turn: `ws.turn(run_id)`.** One run of a harness's loop, one at a time (`TurnInProgress`); ending it settles the inbox and lands one commit stamped with how the run ended. Sync or async; `ws.turns.begin` / `turn.end` for hook-based loops.
- **Turn events and `RunStatus`.** `nontainer.turns`: `RunStarted`, `TextDelta`, `ThinkingDelta`, `ToolStarted`, `Delivered`, `ToolEnded`, `Usage`, `Compacted`, `RunEnded`; a run ends `completed`, `cancelled`, `interrupted` or `failed`.
- **The harness corpus: `nontainer.conformance`.** Scenarios that check a harness against the turn contract, a runner, and their JSON and JSON Schemas for other languages; `AgnoHarness` runs the agno adapter under it.
- **`finish_turn` and `status_of_error`.** `nontainer.adapters.agno` helpers that classify an agno run error and end its turn, keeping a cancelled or failed run with its closing note.

### Changed
- **Under a turn, the agno db and `end_turn` leave the commit to it.** A cancelled or failed run lands in one commit stamped `cancelled` or `failed`, not two stamped with agno's status; without a turn nothing changes.

## 0.9.0 - 2026-10-06

### Added
- **`nontainer.adapters.tools`: the workspace tools, defined once.** `Toolset` and a harness-neutral `ToolOutput`; the agno and MCP adapters are built on it, and a golden file pins the tool surface.
- **`Store(memory=True)`.** A store that keeps everything in the process and writes nothing to disk, its registry included.
- **`Profile`: a session's world as one value.** `python`, `mounts`, `commands`, `executor_factory`, `root`, `ignore` and `variables`; `Store.open(profile=)`, read back with `Profile.of(ws)`.

### Changed
- **Storage: the conversation lives under `__conversation__/`.** Read through `nontainer.conversation`; a session moves off `__agno__/` on its next write, and releases before 0.9.0 cannot read it after that.
- **Breaking: `planes.CONVERSATION_SESSION_KEY`, `agno_db.SESSION_KEY` and `RUN_PREFIX` are gone.** Use `nontainer.conversation`.
- **The compaction adapter runs on agno 2.5 and later** (#193).
- **Tool descriptions.** The terminal says there is no `node`; the sessions tool says `paths` is a view, not a share of the work.

### Fixed
- **MCP `run_python` reports saved ui artifacts.** As the agno toolkit always has.
- **`get_run` / `get_runs` work on agno 2.** They raised `ImportError` there.
- **Turn-mode instructions.** The agno toolkit no longer claims every call is committed under `commit="turn"`.

## 0.8.14 - 2026-10-06

### Changed
- **Requires sandtrap 0.4.1** (package imports, module dunders).

### Fixed
- **Workspace packages import and run.** `import tern` no longer fails as "not allowed"; `__init__.py` loads first, as in CPython.
- **Module `__version__`, `__author__`, `__all__` readable,** as plain data.
- **Top-level `await` in `run_python` returns an error result** instead of raising `SyntaxError`.
- **Forked delegates diff as their own work.** A session begun outside this history diffs from its start; new `AgentGit.source_began`.
- **No plain merge offer for outside forks.** Such answers carry `"outside": True` and offer `ws-git checkout <name> -- <paths>`.

## 0.8.13 - 2026-10-06

### Added
- **`fork_from` takes a bare session name,** resolved to its latest commit; a store tag wins a clash.

### Changed
- **Breaking: `inherit` follows `fork_from` when unset.** A child forked from elsewhere gets the conversation (`"full"`); pass `inherit="fresh"` for the old behavior.

### Fixed
- **Empty `ws-git log` points to `ws-git log --all`** when only `--all` would list commits.

## 0.8.12 - 2026-10-05

### Added
- **test_app's `press` sends real key events,** heard by `document` and `window` listeners; supports `"hold"` and `"on"`.

### Fixed
- **test_app names failed requests.** New `[failed requests]` section, `TestAppResult.failed_requests`.
- **A run starting with `goto` opens that page** (`DriveSpec.start`), not the index first.
- **MCP tools no longer block the event loop** (#176). Tools and `workspace://` readers are `async` and run on a worker thread.
- **Host objects with `async` methods refused at open** (#177). Expose sync methods or a sync facade.
- **agno 3 run methods work** (#190). `get_run`, `get_runs`, `delete_run`, `delete_runs` on `KvgitSessionDb` and `KvgitStoreDb`.
- **Smaller fixes:** delegate answers suggest `ws-git diff <name> --stat` first; transient Chromium screenshot errors are retried twice.

## 0.8.11 - 2026-10-05

### Added
- **Bind handlers to another host object.** `test_app(..., bind={"db": "testdb"})` and `ws-curl --bind db=testdb`; also `TestAppResult.bound`, `ViewSpec.bind`, `AppRuntime.dispatch(bind=)`.

### Fixed
- **Terminal scripts run as `__main__`,** so the `if __name__ == "__main__"` guard runs.
- **`sessions` accepts `action="resume"`.**
- **Resumed delegates list only that task's changes,** measured from where the sessions last met.

## 0.8.10 - 2026-10-04

### Added
- **`ws-git` takes git's everyday spellings.** `diff --stat`, `--` paths, `status -s`, `log --oneline`, `log -5`.
- **ws-vitest runs common vitest idioms.** Array `.each`, scoped `beforeAll`/`afterAll`, `.skip`, `.only`, `.todo`, `.concurrent`, `.runIf`, `.skipIf`.

### Fixed
- **`ws-git diff <session>` shows what a merge would bring.** It is git's `A...B` from the merge base; providers expose `merge_base(a, b)`.
- **Mounted sessions stay clean.** A frozen workspace is never `uncommitted`, and opening a branch without its mounts writes nothing.
- **`ws-git status` uses git's letters** (`A`, `D`, `M`); `WorkspaceStatus` gains `added` and `removed`.
- **Delegates answer to their short name** in `resume`, `result`, `keep` and `cancel`.
- **`file_edit` no longer re-indents on a guess;** only a uniform indent shift matches.
- **Smaller fixes:** `req.require` says what value arrived.

## 0.8.9 - 2026-10-04

### Added
- **`ignore=`: gitignore-style non-work paths.** On `workspace()` and `Store.open`; ws-git skips them, forks inherit them, `ws.ignore` reads them.
- **Mounted skills.** `skills.mounts(*sources, root=...)` mounts skill directories read-only and unversioned.

### Changed
- **Requires termish 0.2.2** (bash/GNU-like globs and `ls`).
- **Paths outside `ws.root` are never work** for ws-git status, commit, merge or diff.

### Fixed
- **A fork's first commit takes only its changes;** a fork measures from the tree it started at.

## 0.8.8 - 2026-10-03

### Added
- **Compaction of long conversations** (`nontainer.compaction`; docs/compaction.md). Past a token budget, earlier turns are sent as one summary; stored history is never rewritten.
- **Storage: a new `__compaction__/` plane.** Rewinds and `full` forks carry folds; `fresh` forks and merges don't.
- **`CompactingCompression` agno adapter** (`nontainer.adapters.agno_compaction`, agno 3.0+). With `Policy(budget, window=None)`, `on_fold` and `summary_model`.
- **Compaction never stored or forked fresh.** `KvgitSessionDb` drops the summary pair; `fork_session(..., conversation="fresh")` drops folds.

## 0.8.7 - 2026-10-03

### Added
- **`ws-git add`, git's name for `stage`.** Takes paths, a directory or `-A`/`--all`.

### Changed
- **Requires termish 0.2.1** (`>/dev/null`, POSIX regex, `test`/`[`).
- **Requires monkeyfs 0.2.5** (cwd-independent directory operations).

### Fixed
- **A merge after a `cd` in one call lands.** `ws-git merge`, `revert`, `cherry-pick`, `stash pop` commit pending writes first.
- **A session left mid-merge is refused as a source** (#172), naming the paths with conflict markers.
- **Handlers see the same headers however called** (#152). `make_request` applies the allowlist.

## 0.8.6 - 2026-10-03

### Added
- **Authoring output is never work** (`nontainer.ignore`). `app/logs` and `app/screenshots` stay out of ws-git, diffs and delegate changes.
- **`ws-git show` reads other sessions and files.** `show <session>@<commit>` and `show <ref>:<path>`.
- **Turn commits name their runs** in `info["runs"]`.

### Changed
- **A delegate's answer is everything it wrote.** Writes past its last commit are committed for it; `Answer.uncommitted` is True only mid-merge or if that commit fails.
- **Breaking: provider `merge` and `apply` take `ignore`.** Third-party providers with `caps.merge` must accept it.

### Fixed
- **The session db stores each run once,** not again every turn.

## 0.8.5 - 2026-10-01

### Fixed
- **App audio and video can be scrubbed.** Static files answer a single byte `Range` with `206`; range headers pass both allowlists.
- **More media and image MIME types** (`mp3`, `mp4`, `webm`, `webp`, `pdf`, others) are no longer `application/octet-stream`.

## 0.8.4 - 2026-09-30

### Added
- **`Sessions` reports when an answer lands.** `on_answer(name, answer)`, `wait(timeout=None)` and `outstanding()`.
- **A runner's obligation is documented** in `SessionRunner` and docs/sessions.md.

### Fixed
- **The `test_app` tool description lists `goto`.**

## 0.8.3 - 2026-09-30

### Security
- **Removing the `dir` backend closes a host code-execution hole.** Its pickle cache was agent-writable and loaded on open; only `backend="dir"` stores were affected.

### Removed
- **Breaking: the `dir` backend** (`backend="dir"`, `nontainer.providers.DirProvider`). It now raises `ValueError`; `--backend` drops `dir`.

## 0.8.2 - 2026-09-29

### Added
- **`test_app` viewports of any size.** `"hd"` (1920x1080), `"WIDTHxHEIGHT"`, or `{"width": W, "height": H}`.
- **Screenshot grids in `test_app`.** `{"screenshot": true, "grid": "name"}` tiles up to 12 frames into one image.

### Changed
- **`nontainer.workspace(...)` owns the store it builds;** `KvgitProvider._take_repo()` is gone.
- **An unknown `viewport` is refused,** not run at desktop size.
- **Documented close order** (#164). Closing a served snapshot mid-request is safe; stop serving before `Store.close()`.

## 0.8.1 - 2026-09-28

### Added
- **`ws.runtime.warm()` starts the worker now.** On `DudExecutor` it boots the guest; executors may define `warm()`.
- **`test_app(action_timeout_ms=)`** for element waits (default 5000).

### Changed
- **Breaking: `DudExecutor` boots on first use** (#160). Boot failures move to first execution; call `ws.runtime.warm()` to see them at open.
- **Breaking: opening a workspace starts no worker** (#159). `open()` no longer raises `IsolationUnavailable`; call `ws.runtime.warm()` instead.

## 0.8.0 - 2026-09-28

### Added
- **PostgreSQL backend for kvgit data.** `Store(path, kv="postgresql://...")` or `NONTAINER_KV` / `NONTAINER_KV_TABLE`, with the `postgres` extra.
- **Repository accessors.** `Store.repo`, `KvgitProvider.on(repo, session)` and `KvgitProvider.delete_in(repo, sessions)`.
- **`KvgitStoreDb` takes the embedder's `Store`;** a path still works.

### Changed
- **Requires kvgit 0.4.1 and monkeyfs 0.2.4** (`Repo`/`Worktree`/`Snapshot` API; `Staged` gone).
- **Storage: upgrading a store is one-way; back it up.** The first commit stamps kvgit storage version 4, which nontainer 0.7.x cannot open.
- **Breaking: `KvgitProvider` is built from a `Repo`.** `KvgitProvider(repo, worktree, *, session)`; `provider.worktree` replaces `provider.staged`.
- **Breaking: providers need `working_diff(commit)`,** or must raise `NotSupportedError`.
- **Bulk reads cost the change, not the tree.** ws-git verbs batch-read only changed paths; views gain `get_many(paths)`.
- **A `Store` keeps its repository open until `close()`,** sharing one connection pool.
- **Merging an already-contained branch writes nothing.**

### Fixed
- **A publish that died before committing no longer strands its version.**

## 0.7.11 - 2026-09-26

### Added
- **`ws.runtime.reap_idle(max_age)` drains idle view workers.** Closes warm view workers idle for `max_age` seconds and returns how many, for embedders to schedule beside `sessions.sweep`; `LocalExecutor` implements it.
- **`Store.migrate_layout()` and `python -m nontainer.migrate`.** Convert each session head in one commit (`--dry-run` to preview), returning `LayoutMigration` reports; `migrate_provider(provider)` does a single provider.
- **Old commits convert when they become live.** Restore, revert, cherry-pick and fork from a pre-migration commit migrate the tree on the way in; old commits are never rewritten.
- **Handler JSON takes numpy, pandas and stdlib values.** Scalars, arrays, dates, `Decimal`, `NaT` and tables encode at any depth instead of a 500; a refused value names its path.
- **Handlers can return tables, negotiated by `Accept`.** A `DataFrame`, `Series`, pyarrow `Table` or `__arrow_c_stream__` object answers as Arrow IPC or JSON rows; new `nt-noted-response/1` and `nt-not-acceptable/1` wire values.
- **`AppsConfig.max_binary_response_bytes`** (default 32 MB) caps non-text bodies; `nontainer.apps.contract.is_text_type` decides what is text.
- **`ws.runtime.view_result_limit` and `ViewSpec.result_bytes`.** Executors declare how much one execution carries back and views say what they need; `DudExecutor` sizes dud's caps up to 64 MiB.

### Changed
- **Breaking: old-layout stores need a one-time migration.** For stores written before monkeyfs 0.1.10, run `python -m nontainer.migrate --store <dir>`; a writable open or merge of an unmigrated head raises `LegacyLayoutError`.
- **Storage: frozen reads of old layouts still work.** Tags, publications, `store.resolve` and `ws.files.attach` read old trees unmigrated; such a snapshot opens at the workspace root, not the old `__cwd__`.
- **Breaking: handler responses are encoded in the sandbox.** Custom executors must carry the `(status, content type, headers, body)` tuple back intact; out-of-range statuses are a bad return, and a malformed wire value answers 500, not 204.
- **NaN and Infinity in handler JSON become `null`.** They were bare invalid tokens that broke the browser's `res.json()`.
- **`AppsConfig.max_response_bytes` is 10 MB, text only.** Up from 2 MB; both caps are enforced in the sandbox and on the host, bounded by `view_result_limit`, with a logged message saying what to do.
- **`make_request` lowercases header names.** So `req.headers["accept"]` works however the request was built.

### Fixed
- **`DudExecutor`: dates, numpy values, `Decimal` no longer answer 204.** Handlers now answer what `LocalExecutor` answers.
- **`ws-pytest` with no paths collects `tests/` only.** Test files elsewhere are named in a note and run when passed; `ws-vitest` names stray `.test.js` files instead of skipping them.
- **`ws-vitest` drops its note on where tests run.**

### Removed
- **Breaking: `Store.shared` and `HostObjectFactory` are gone.** Neither did anything; code using them now gets `ImportError` or `AttributeError`.

### Security
- **Apps no longer serve their authoring log or screenshots.** Static serving refuses `logs/` and `screenshots/` like `api/`, case-insensitively and after path normalization, covering published versions; this also stops `/API/h.py` leaking handler source.
- **`store.publish(exclude=)`.** Defaults to `("app/logs/", "app/screenshots/")`, recorded on `Version.exclude`; a `static_assets` prefix at `logs` or `screenshots` is refused.

## 0.7.10 - 2026-09-23

### Added
- **`keep_aborted_run(db, session_id, run_id, note)`.** In `nontainer.adapters.agno`: keeps a failed or stopped run in agent memory, marked completed with an aborted-early note; works on agno 2.x and 3.
- **`tk.begin_turn` warns when agno restarts a run.** Run-level retries forget tool calls whose files remain; use `Agent(retries=0)` and set `Model.retries` for transient provider errors.

## 0.7.9 - 2026-09-22

### Changed
- **Inbox hook follows tool entrypoints, not `run()`/`arun()`.** Docs fix: `tk.deliver` suits `WorkspaceTools` (always sync) under `arun()` too; use `tk.adeliver` only beside your own async tools.

### Fixed
- **Minted tokens never begin with a dash.** `mint_token()` redraws until the first character is alphanumeric, so a token used as a publication name or tag never reads as a CLI flag.

## 0.7.8 - 2026-09-22

### Added
- **Mid-run inbox: `nontainer.inbox.Inbox`.** A thread-safe queue whose notes the agno `tk.deliver` / `tk.adeliver` hooks append to each tool result, framed as `principal` or `mechanism`; `split()` strips the block.
- **`tk.begin_turn` / `tk.end_turn` hooks.** Re-queue notes a retried attempt threw away, then settle them.
- **`Sessions.take()`.** Returns every landed, uncollected delegate answer in landing order, sharing the collected mark with `result`.

## 0.7.7 - 2026-09-20

### Added
- **`test_app` runs through an `AppDriver`.** A driver takes a `DriveSpec` and returns a `DriveReport`, serving through the publication's own dispatch; set via `AppsConfig.driver` or `Runtime.app_driver`, default headless Chromium.
- **Handler-less publications are served as static files.** `Publication.open` builds no executor when `app/api/` has no handler; `ws.runtime.executes` says which tier, and execution raises.
- **`ws.files.read(path, offset, size)`.** Host-side reads take a byte range.

### Changed
- **Requires termish 0.2.0, monkeyfs 0.2.2 and sandtrap 0.4.0** (ranged binary reads).
- **Breaking: `FileSystem.read` takes `(path, offset=0, size=-1)`.** Binary `open()` is a lazy ranged stream; a custom filesystem without the two arguments raises `TypeError`.
- **Pipes and `>` carry bytes.** Binary data no longer turns into U+FFFD; `ctx.stdout.buffer` is the new binary path for commands.
- **Control-flow keywords raise a clear `ParseError`.** `for`, `if` and friends in command position name the keyword and point at `xargs`, `find -exec`, `&&` or `||`.
- **`printf` is a builtin.**
- **Missing worker processes raise `IsolationUnavailable`.** Instead of a bare `OSError` under `isolation="process"`.

### Fixed
- **AgentFS filesystem fixes.** `makedirs`/`mkdir` raise `FileExistsError` for a taken path, `list_detailed` answers in the queried namespace, and `read` accepts the byte range.
- **A directory named like a handler is a 404.** It was a 500.
- **Views place nested listing entries correctly.** `ViewFS` and `SubtreeFS` `list_detailed` now use and translate `FileInfo.path`.
- **Binary output fixes.** `ws-curl` writes binary bodies as bytes (newline only for text); `ws-*` verbs can emit binary on the dud rung.

## 0.7.6 - 2026-09-16

### Added
- **`AppsConfig.handler_example`.** Replaces the example handler in the apps notes: `None` keeps the built-in, `""` omits it, a string replaces it.

### Changed
- **Agent-facing rules stated once.** Tool descriptions defer test details to `ws-pytest --help` / `ws-vitest --help`; frontend tests belong in `tests/`; new `render.test_app_description(ws)`.

## 0.7.5 - 2026-09-16

### Changed
- **Requires kvgit 0.3.9** (public `merge_base`; a lost fast-forward race retries via merge).
- **Requires monkeyfs 0.1.11** (recursive listings walk nested mounts).

### Fixed
- **Nested mounts reach a dud guest.**
- **Guest writes into read-only points are refused, not raised.** Writes into an attachment or read-only `Mount` are dropped and named in an errored result, as in-process; other writes land.
- **Handler writes into read-only points answer 500.** They raised out of `dispatch()` on guest rungs; now the same 500, `api.log` entry and rollback as in-process.
- **A `ui` value whose serializer raises writes no file.** On any rung; the diagnosis comes from a binding of its own, not the value.

## 0.7.4 - 2026-09-16

### Added
- **`ws-pytest --help` states the whole test contract.** `call(...)`, `Request` / `Response` / `HttpError` and an example; the tool description defers to it.
- **`types`, `dataclasses` and narrowed `typing` granted.** `typing.get_type_hints`, `typing.ForwardRef`, `dataclasses.make_dataclass` and interpreter types stay refused, since they evaluate strings host-side.

### Changed
- **Requires sandtrap 0.3.7** (`type(e).__name__` readable).
- **`from host import call` reaches a handler in tests.** A bare `call` still works.

### Fixed
- **Frozen records hash despite metadata.** Mapping fields on `TagInfo`, `CommitInfo`, `Job` and other exported records are left out of the hash but still compared.
- **`host` fixes.** A handler's own global no longer hides the session's object; the test-composed `host` is read-only; `call(db=fake)` substitutes `from host import db` and `import host` too.
- **A handler module a test imports runs as a request does.** Contract names like `HttpError` are in scope; setting its attributes and `from app.api.x import *` are refused.

## 0.7.3 - 2026-09-15

### Fixed
- **A full-inherit fork carries the conversation as the child's.** The record is rebound to the child, with `session_data["forked_from_session_id"]` naming the source, so delegates see their inherited history.

## 0.7.2 - 2026-09-15

### Added
- **A store tag is a ref.** `ws-git` `worktree add`, `checkout`, `diff`, `log`, `show`, plus `ws.expand_ref`, `ws.files.attach`, `ws.checkout` and `store.resolve` take tags, readable after their session is deleted.
- **`inherit="full"` with `fork_from`.** A delegate forked from another point gets the conversation stored there; `resume` still refuses a non-fresh inherit.

### Changed
- **New store tag names may not hold `@` or `:`.** Existing names keep working but cannot be spelled as refs.

## 0.7.1 - 2026-09-15

### Fixed
- **`sessions keep` keeps.** The tool's `keep` action now promotes the job, so the retention sweep honours it.

## 0.7.0 - 2026-09-14

### Added
- **New ws-git verbs.** `worktree add/list/remove`, `revert`, `cherry-pick`, `stash`, `merge --abort`, `tag`, `log -S`, `log --all` and `sparse-checkout list`; see `docs/ws-git.md`.
- **Host half.** `ws.revert(commit)`, `ws.cherry_pick(ref)` (over `provider.apply`) and `ws.index.tags` (`add`/`list`/`info`/`delete`/`at=`).
- **Short commit ids are refs everywhere.** Any unique prefix of 7+ hex characters, host API and ws-git alike.
- **`ws-pytest` and `ws-vitest` unit-test verbs.** pytest's shape on the workspace executor; vitest's on headless Chromium (`[apps]` extra).
- **Hermetic `ws-vitest` runs.** No api routes or other hosts (`connect-src 'self'`); mock with `vi.stubFetch({...})`, keyed by exact path.
- **`TestReport` and `render_report`.** A test run as a record (`TestOutcome`, `TestFrame`); branch on `run_pytest(ws).ok`.
- **`call(...)` in a test's namespace.** Runs a handler as dispatch would, dependencies substituted by keyword (`db=fake`).
- **`from host import db`.** Injected host objects also arrive as a read-only synthetic `host` module, in handlers and imported modules alike.
- **`__future__` and `unittest.mock` in the stdlib preset.**
- **`sessions ask(fork_from=, resume=)`.** `fork_from` starts a delegate at a store tag or `session@commit` (`inherit="fresh"`); `resume` re-tasks an existing delegate. `Job` gains `origin`.
- **`sessions.sweep(idle, *, min_age=3600)`.** Idle-TTL cleanup of delegate branches; adds `Job.touched`, status `expired` and `BranchExpired`.
- **Public seam names.** `ws.store`, `ws.provider`, `ws.expand_ref(ref) -> Ref`, `nontainer.executor.flatten_grants(cfg)` and `ws.runtime.guest_to_host(path)`.
- **Full `WorkspaceProvider` and `Executor` contracts.** Providers declare `frozen`, `frozen_at`, `key_at`, `branch_head`, `expand_commit`, `commit_at`; executors gain `supports_ws_verbs` and public `guest_to_host(path)`.
- **`ws.files.remove(path)`.** Deletes one file under the lock and view rule, returning a `RemoveOutcome`; directories are refused.
- **`JobStatus` and `AnswerStatus` exported.** Typed status literals; `inherit` is `Literal["full", "fresh"]`.

### Changed
- **Requires sandtrap 0.3.6** (per-exec modules, for `host`).
- **Docs re-cut by audience.** New `docs/ws-git.md`, `testing.md`, `sessions.md`, `extending.md` and `quick-start.md`.
- **Breaking: the ui set is closed.** Only plotly, DataFrame, matplotlib, image, card rows or a file name; anything else gets a `ui_problems` note and no file.
- **One relay carries every `ws-*` verb into a guest.** Via a `FerrySpec`; reserved host-object name `ws_verb` replaces `ws_git` and `ws_curl`.
- **Breaking: `host` is a reserved name.** A host object or module grant named `host` is refused; a workspace `host.py` or `host/` errors.
- **`materialize_ui` moved to `nontainer.ui`.** No shim in `nontainer.adapters.render`.
- **Shared backend code takes dependencies as arguments.** Documented: injected host objects are not globals of imported modules.
- **Storage: ws-git blob is layout 3.** Adds `tags`, `pre_merge` and the stash record; layout 2 blobs still read.
- **Every fork lands a `ws-git.fork` commit**, so a delegate's inherited files no longer read as added.
- **`register_wsgit(ws)` returns a bool.** False only where the executor cannot carry a terminal builtin.

### Fixed
- **Breaking: a taken delegate name is refused.** `sessions.ask(name=)` raises `SessionsError` instead of forking `name.2`; use `resume=`.
- **Wiring twice is a no-op.** `enable_apps` and `register_wsgit` no longer raise on a fork or second call; new `nontainer.apps.app_runtime(ws)`.
- **`CacheError` and `HarvestLost` are `WorkspaceError`s.** `HarvestLost`, `ExecutionContext`, `StagedDiff` and `ViewSpec` are exported.
- **Merges block tree moves.** `merge` and `checkout` refuse until markers are resolved or `ws-git merge --abort`.
- **A frozen workspace refuses writes by every door.** `ws.files.fs` on a frozen open raises `PermissionError`; reads still work.
- **Smaller fixes:** `ws.log()` is a list; `ws.diff`/`ws.changed_since` need versioning, not tags; plotly spec dicts encode like figures; merged metadata rows match; clearer bad-ref errors; dud handler tracebacks show the right line.

## 0.6.5 - 2026-09-10

### Added
- **`Publication.meta` and `store.set_meta(name, mapping)`.** Mutable publication metadata (e.g. a display title); `Version.info` stays immutable.

### Fixed
- **Publication records hold their own data.** `set_meta` normalizes through JSON; `meta` and `Version.info` are read-only all the way down.
- **Publication reads cannot straddle `unpublish`.** Resolve, attach, `tags.add` and `Publication.open` check under the registry lock.
- **`store.tags.add` accepts a published version's ref.** It raised `SessionIdError`.

## 0.6.4 - 2026-09-10

### Added
- **`store.tags.list_info()`.** Describes every store-scoped tag on one backend open.
- **`Version.info` and `Version.paths`.** Publish records `info` on the registry row, so listing titles needs no backend open.
- **`publish(..., create_only=True)`.** Refuses a name that already holds a version.

### Fixed
- **A resumed publish uses the adopted commit's `info` and `paths`.** Late tag minting no longer raises `NotSupportedError`.

## 0.6.3 - 2026-09-09

### Added
- **`publish(..., current=False)`.** Records a version without moving the pointer; switch later with `set_current`.

### Changed
- **Breaking: publish caller mistakes raise `ValueError`.** Reused versions, unmatched `paths`, unknown versions; `WorkspaceError` stays for store state.
- **Shared backend code lives under `app/`.** As `app/api/_<name>.py`, not `/workspace/helpers`; `python_description` takes `apps=`.
- **The cache does not travel with a publication.** Precomputed data belongs in published files.
- **The default version is the next `v<N>`.** Other names are not counted.

### Fixed
- **A crashed publish can be resumed or cleared.** A retry adopts its own recordless branch; `unpublish` clears what publish left. `paths` is a reserved `info` key.

## 0.6.2 - 2026-09-09

### Added
- **Store-scoped reads never need a session.** Frozen opens fall back to a publication branch or the reserved `@store/anchor`.
- **`Store.delete` deletes sessions only.** `@store/` branches are unreachable; remove publications with `unpublish`.
- **`SessionRunner.run` gets `forked_at`.** Passed only to runners that accept it; also `Sessions.base(name)`.

## 0.6.1 - 2026-09-09

### Added
- **Delegation is a tool.** `WorkspaceTools(ws, sessions=runner)` and `build_server(ws, sessions=runner)` add a `sessions` tool when given a `SessionRunner`.
- **`nontainer.sessions.Sessions(ws, runner)`.** Forks pet-named children on a worker thread, returning a `Job`; `wait=True` blocks for the `Answer`.
- **A fork starts with fresh ws-git state.** No inherited head, staging or merge context; recorded as a hidden `ws-git.fork` commit.
- **`Job` and `Answer` records.** `SessionRunner.run` may return either or a string; new `SessionsError`, `JobRunning`.
- **Frozen opens take execution settings.** `store.tags.at`, `store.resolve`, `Publication.open` take `Store.open`'s keywords except `autocommit`.

### Fixed
- **Published apps reach their host objects.** Via `pub.open(python=PythonConfig(host_objects=...))`.

## 0.6.0 - 2026-09-09

### Added
- **`nontainer.Store` / `store(...)`.** `open`, `sessions`, `exists`, `delete`, `fork`, `resolve`, `clean`, `tags`, `close`; plus `nontainer.Ref` and `ws.ref`.
- **Publications.** `store.publish(ws, name, *, paths=, version=, info=)`, `publications()`, `publication()`, `set_current()`, `unpublish()`; frozen `Publication`/`Version`.
- **Delegation forks.** `ws.fork(name, *, at=, inherit=, paths=)`; `ws.checkout(ref, paths=)`; `ws.files.attach`/`detach`/`attachments`.
- **`ws.log(kind=)`.** `"work"`, `"agent"` or `"all"`; `CommitInfo.parents`, `WorkspaceDiff.seed`/`.in_seed`/`.elsewhere`.
- **More ws-git verbs.** `branch`, `merge <session>`, `checkout <ref>`, `show <ref>`, `log`/`diff <session>`.
- **`nontainer.BookkeepingLost`** and `examples/tour.py`.

### Changed
- **Breaking: API v2, no compatibility layer.** `Store`, `Workspace` and `ws.runtime` split the seams; `enable_apps(ws, config)` takes the `AppsConfig`.
- **Breaking: git's words.** `checkpoint`->`ws.commit`, `autocheckpoint`->`autocommit`, staged `ws.commit`->`ws.index.commit`, `history`->`log`, `restore`->`checkout`, `dirty`->`uncommitted`.
- **Breaking: namespaces.** File verbs -> `ws.files.*`, staging -> `ws.index.*`, tags -> `ws.tags.*`/`store.tags.*`, execution -> `ws.runtime.*`.
- **Breaking: renames.** `CheckpointInfo`->`CommitInfo`, `CheckpointNotFoundError`->`CommitNotFoundError`, `.checkpoint`->`.commit`, `delete_workspace`->`Store.delete`.
- **Breaking: provider protocol.** `checkpoint/commit/restore` become `commit`/`commit_keys`/`checkout`, plus `files_at`, `working_files`, `key_at`, `branch_head`, `refresh`, `merge`.
- **ws-git is a fiction over store history.** Its state is metadata, so autocommit is never suspended.
- **`ws.checkout` appends.** History is append-only; only `Store.delete` moves a head back.
- **`ws.merge` takes only committed agent work.** Gated by `caps.merge`; files merge three-way.
- **Storage: one metadata row per file** instead of one table per write.
- **Storage: one cwd key.** `__cwd__` is dropped on open.
- **Requires kvgit 0.3.8, monkeyfs 0.1.10, sandtrap 0.3.5** (merge policy, per-file rows, raw default).

### Removed
- **Breaking: `Workspace.rollback(steps)`.** Use `ws.checkout(commit_id)`.
- **Breaking: flat `Workspace` methods and staging suspension.** Including `head_tree`, tag `scope=`, `delete_workspace`, `StageResult`.

## 0.5.2 - 2026-09-04

### Added
- **`AppsConfig.csp_extend`.** `{directive: sources}` appended onto the derived policy, e.g. an intranet `http://` API in `connect-src`; extend-only, and raises `ValueError` if `csp` is also set.

### Changed
- **`blob:` images and media in the default served CSP** (#58). `img-src` gains `blob:` and `media-src 'self' https: data: blob:` is added, so plotly's PNG export works; `script-src` still refuses `blob:`.

### Fixed
- **test_app's interception honours the served policy.** Origins the policy allows (an `http://` API, tile host or framed origin) are no longer aborted during verification; a refusal names the directive.
- **Script origins with an explicit port are honoured.** `https://scripts.internal:8443` was dropped as a scheme-only source.

## 0.5.1 - 2026-09-03

### Added
- **`ws.fork(name, at=<checkpoint>)` and `fork_session(..., at=)`.** Branch from an earlier checkpoint without rewinding the session; the dir backend raises `NotSupportedError` for `at`.
- **Tags.** `ws.tag("v1")` names a state immutably and anchors GC, session-scoped by default or store-scoped to outlive the session; plus `ws.tags()`, `ws.tag_info()`, `ws.delete_tag()`, gated by `caps.tags`.
- **Frozen workspaces: `ws.at_tag("v1")`.** Opens the tagged state read-only; write tools, `checkpoint`, `fork` and `tag` refuse, and the executor gets a read-only filesystem.
- **`ws.changed_since(ref)` and `ws.diff(a, b)`.** Changed file paths between checkpoints or tags; framework keys are excluded and a same-bytes re-save is not a change.
- **`CheckpointInfo.tree` and `ws.head_tree`.** A checkpoint's content hash; equal trees mean identical content.
- **`KvgitSessionDb.seed(session)`.** Imports a whole `AgentSession` into a branch with no runs yet, e.g. to migrate a conversation from another agno db.

### Changed
- **Requires sandtrap 0.3.4** (frozen workspaces under `isolation="process"` refuse a handler's file write at `open()` instead of silently succeeding).

## 0.5.0 - 2026-09-02

### Added
- **`nontainer.adapters.agno_db`: the conversation in the branch.** One commit holds a turn's files, `cache`, cwd and memory; `ws.restore()` and `fork_session()` move all four together.
- **Storage: `KvgitSessionDb`.** An agno `JsonDb` keeping one key per run (`__agno__/runs/<run_id>`) plus `__agno__/session` in the branch; other tables stay at `db_path`, and `upsert_session` fires the turn commit.
- **`KvgitStoreDb`.** One agno db over a whole kvgit store, a branch per session; `get_sessions` lists every branch and agno's `Agent.fork_session` works.
- **`WorkspaceTools(..., session_db=db)`.** Names the db (either kind) that owns the turn commit, making `tk.end_turn` a no-op; a db over another workspace is refused.
- **`fork_session(ws, name, conversation="inherit" | "fresh")`.** Forks the workspace and rewrites the session key; `"fresh"` drops the runs for a clean chat.
- **Fork lineage in `session_data["forked_from_session_id"]`.** Where agno's readers look for it.
- **Leave `Agent.cache_session` at its default.** With it on, an upsert whose prior runs are not a tail of the branch's `run_ids` raises and writes nothing.

### Changed
- **Breaking: `DudExecutor` honours or refuses `PythonConfig`.** On VM guests `network=True` and `stdlib=False` raise `NotSupportedError` instead of being ignored; the subprocess rung refuses `isolation` above `"none"`.
- **Module grants become the guest image's package list.** Pinned to host versions and merged with `vm={"packages": [...]}`; `vm={"packages_from_grants": False}` opts out, and a local module with no distribution raises.

## 0.4.1 - 2026-08-30

### Fixed
- **A lone callout no longer renders as raw JSON.** A bare tagged callout in `ui` is adopted as a one-item row; a bare stat (`{label, value}`) gets a note to wrap it in a list.

## 0.4.0 - 2026-08-30

### Added
- **`ArtifactPath`.** A `str` subclass naming where a rich `ui` value was written (`<root>/ui/<name>.<ext>`); `.kind` is derived from the suffix. Exported from `nontainer`.
- **`ws.read_artifact(path) -> bytes | None`.** `None` when unreadable, matching the `read_bytes` contract of `turn_to_a2ui`.
- **test_app `{"goto": "about.html"}`.** Verifies pages past the entry point; an HTTP error fails the action.

### Changed
- **Breaking: a rich `ui` value reads back as an `ArtifactPath`.** Not the live object, on every rung; read it with `ws.read_artifact(p)`, a rendering (tables are `head(200)`), not a serialization.
- **Breaking: `run_python` writes `/ui/` artifacts itself.** On every rung, in the call's own checkpoint, with or without the agno adapter; also fixes a regenerated artifact going unnoticed (#46).
- **Requires dud 0.4.0** for the `[dud]` extra (rich `ui` flattening moved to the host-named hook `nontainer.dud_outputs:flatten`).
- **dud VM notes.** On `backend="vm"` add the hook module via `vm={"packages": [...]}`; the `state=` tag is a no-op on firecracker unless `$DUD_VM_MAX_AFFINITY` is set.
- **Oversized artifacts fail the same on either rung.** Both write the same `.txt` note and report the same `ui_problems`.
- **agno 3.0 is supported, with no upper bound.**
- **Python 3.14 joins the test matrix.**
- **Breaking: an absolute url now fails verification.** It only warned before, so a currently-green app can turn red.
- **test_app diagnostics.** `eval` settles before observing like `read`; a failed action captures the page; a missed selector lists the ids and `data-key`s present; early console errors stay visible.

### Fixed
- **Requires agno 2.1** for the `agno` extra (the old `agno>=1.0` floor could not import the adapter); verified on 2.1.0, 2.7.2 and 3.0.1.

## 0.3.7 - 2026-08-28

### Changed
- **The default frontend choice moved into `frontend_notes`.** "Plain HTML + DOM + fetch" is now the first line of `DEFAULT_FRONTEND_NOTES`, so an embedder replacing `frontend_notes` replaces it too.
- **Docs realigned with 0.3.3-0.3.6.** Stale text on HTM+Preact, `@babel/standalone`, test_app and the CSP, `assert`, and `TestAppResult.ok` corrected.

## 0.3.6 - 2026-08-28

### Fixed
- **`assert` retries again under the enforced CSP.** A 0.3.5 regression failed async apps; asserts now poll via `page.evaluate`, and an expression that raises is retried too.

## 0.3.5 - 2026-08-28

### Added
- **`AppsConfig.csp`.** The policy served HTML carries and `test_app` enforces: `None` derives it from `script_hosts`, `""` disables, a string is verbatim; `build_router(csp=...)` still wins but is unverified.
- **Custom `script-src` origins join test_app's allowlist.** Quoted keywords, scheme-only sources and wildcards are skipped; list those in `script_hosts`.

### Changed
- **Breaking: `test_app` sends the served Content-Security-Policy.** A violation that stops code (`eval`, `new Function`, blob scripts) fails the run; refused images, fonts and stylesheets stay warnings.
- **CSP violations are reported as fixes in `[rejected requests]`.** A disallowed external script keeps the allowlist wording.

## 0.3.4 - 2026-08-28

### Added
- **`AppsConfig.frontend_notes`.** The embedder owns the notes on which frontend libraries exist and where from: `None` keeps the built-in, `""` omits, a string replaces; extend `nontainer.adapters.render.DEFAULT_FRONTEND_NOTES`.

### Changed
- **An empty `script_hosts` reads as a rule.** `()` now says scripts may load only from the app itself, not a dangling colon.

## 0.3.3 - 2026-08-27

### Added
- **`AppsConfig.static_assets`.** Maps a URL prefix to a host directory of vendored files (`{"vendor": "/srv/assets"}`), outside the workspace; pass one config to `enable_apps` and `build_router`.
- **Static assets skip `max_response_bytes`.** They also win over a workspace file at the same path, noted in `api.log`.
- **More static types.** `.wasm`, `.woff2`, `.woff`, `.ttf`, `.map`.

### Changed
- **`test_app` page errors name the agent's own frame and quote the line.** Frames are classed as workspace, library or generated code; an inline `<script>` resolves to `index.html`.
- **The served CSP allows WebAssembly** (`'wasm-unsafe-eval'` in `script-src`). Compilation only; `eval` stays refused.
- **`Mount` docs clarified.** A fork inherits the mount point; the data behind it stays a live view of the host directory that neither parent nor fork can roll back.

### Fixed
- **`fork()` keeps the workspace's mounts.** Forks, and so published snapshots, lost every mount, 404ing mounted data and letting writes bypass a read-only mount.
- **Mount sources resolve once, at construction.** A relative `Mount.path` or retargeted symlink no longer sends parent and fork to different directories.

## 0.3.2 - 2026-08-24

### Changed
- **Requires sandtrap 0.3.3** (policy-gated `__import__`). `__import__("numpy")` now returns a granted module.
- **`run_python` no longer reports the namespace.** The `[namespace kept for host: ...]` line is gone; silent calls show `(no output; success)`.

### Removed
- **The `__import__` intent hint.** A blocked `__import__` now gets the same error and `blocked_import_hint` redirect as an import statement.

## 0.3.1 - 2026-08-21

### Added
- **`PythonConfig.preload_grants`.** Imports grants once in sandtrap's forkserver broker for copy-on-write workers (~176 ms to ~14 ms start with `dataframes()`); process/kernel only, off by default.
- **`preload_grants` is process-wide.** The first workspace to start a worker decides; unsafe if a grant starts threads on import.

### Changed
- **Breaking: `PythonConfig.view_workers` is now `warm_view_workers`.** It sizes a warm cache, not a limit; default 1 (was 8), raise it for concurrent app traffic.
- **Requires sandtrap 0.3.2** (for `preload_grants`).

## 0.3.0 - 2026-08-20

### Added
- **Bridged host objects declare their surface.** Data attributes and unknown methods raise a clear `AttributeError` instead of reading `None` or losing writes.

### Changed
- **Requires sandtrap 0.3.0** (forkserver workers). Fixes "Worker process became unresponsive" hangs in multi-threaded hosts.
- **Breaking: a supplied `PythonConfig.policy` must be serializable.** No lambdas, closures, bound methods or function-local classes; `Workspace(...)` raises `StPolicyNotPortable`.
- **Breaking: your entry point must be importable.** Workers re-import `__main__`, so guard module-level work with `if __name__ == "__main__":`; `python -c`, heredoc and bare-REPL hosts break.
- **Granted modules import once per worker.** Heavy grants slow worker start (~126 ms for `pandas`).
- **Requires dud 0.3.0 for `[dud]`.** Host objects need an explicit grant (else `PolicyError`); nontainer grants their public methods. Guests get their image's environment.
- **Host objects are registered by class only in-process.** Under process/kernel isolation the registration never matched the `RpcProxy`.
- **App handlers reuse resident sandbox workers.** No more `fork()` per request (sandtrap#38); `PythonConfig.view_workers` caps them (default 8, `0` for per-call). Process state persists between handler calls.

### Fixed
- **A2UI fallback drops the invalid `link` key** (#31). The link is now markdown in the `Text`.

## 0.2.4 - 2026-08-04

### Changed
- **Requires monkeyfs 0.1.6 and sandtrap 0.2.14.** monkeyfs 0.1.5 let `bytes`/`os.PathLike` paths and `dir_fd` bypass filesystem interception; sandtrap adds `StForkUnsafe`.

## 0.2.3 - 2026-07-30

### Added
- **More safe stdlib by default.** `heapq`, `bisect`, `difflib`, `struct`, `binascii`, plus narrow `functools`, `shlex` and `pprint` grants.

### Fixed
- **Process workers drop unrelated host descriptors.** Requires sandtrap 0.2.13; idle workers no longer orphan after an abrupt host exit.
- **Fresh versioned workspaces commit an init baseline.** A one-time `{"tool": "init"}` checkpoint, also the floor for `Workspace.rollback()`; a pre-seeded provider stays staged.
- **The MCP extra is capped below 2.0.** MCP 2 removed `mcp.server.fastmcp`.

### Security
- **`pickle` left the default safe stdlib.** Its reducers could reach blocked builtins like `eval`; opt in with `ModuleGrant(pickle)`.
- **`numbers` and `collections.abc` stay excluded.** `ABCMeta.register` could alter host-wide `isinstance`.

## 0.2.2 - 2026-07-27

### Added
- **A `Table` a2ui extension component** (#18). Under `NONTAINER_CATALOG` a dataframe is one `Table` node: `{columns, rows, total, columnTypes}`.
- **`.table.json` artifacts carry `columnTypes`.** `number`/`string`/`datetime`/`boolean` per column; additive.
- **`test_app` `select` action.** `{"select": [selector, value]}` matches option value, then label.

### Changed
- **Breaking: `a2ui.component_for(extension_cards=)` is now `extensions=`.** `turn_to_a2ui` callers are unaffected.
- **`api.log` records every `/api` request** (`METHOD path -> status`) under a format header; read-only lines buffer until `AppRuntime.flush_log()`.
- **Repeated `test_app` console lines collapse** to one entry with an `(xN)` count.
- **Executor syncs are lazy.** Writes and `restore`/`rollback`/`discard` mark the view stale and sync once before the next execution.

### Fixed
- **Direct `ws.fs` writes reach a remote executor.** Under dud, host writes (handler tracebacks, `skills.install`) were invisible to the guest.

## 0.2.1 - 2026-07-20

### Added
- **`delete_workspace(sessions, *, store=, backend=)`.** Idempotent session teardown per backend; providers gain `delete(path, sessions)`, kvgit via `kvgit.delete_branches` (kvgit 0.3.2).
- **Storage: the hidden `__void__` anchor branch is retired.** It defeated orphan GC; the next delete purges it from legacy stores.

## 0.2.0 - 2026-07-20

### Added
- **The workspace root contract.** Agent files live under one path, `/workspace` by default, set with `workspace(..., root=)`, read as `ws.root`; dud VMs match.
- **`[dud]` extra documented**, with a README Executors section.
- **`Executor.supports_commands`** (also `ws.supports_commands`). False for `DudExecutor`.

### Changed
- **Breaking: agent-visible paths moved under the root.** Skills, app, ui and `api.log` now live under `<root>/`; repath prompts, seeded files and stored sessions.
- **Breaking: `DudExecutor()` defaults to a real VM** (`backend="vm"`). No hypervisor raises `IsolationUnavailable`; `backend="subprocess"` is an unsandboxed opt-in.
- **Requires sandtrap 0.2.12 and dud 0.2.1** (`Policy.module_root`; guest root mount).

### Fixed
- **Smaller fixes:** the apps primer drops `curl` where absent; `DudExecutor` goes through `dud.session()`, so firecracker works; `root=` normalizes by segment; absolute guest writes land in the diff.

## 0.1.2 - 2026-07-19

### Added
- **Tracebacks in error results and `api.log`.** Also under process isolation, with sandbox frames trimmed.
- **Request context in api.log tags.** Entries include the query string.
- **More intent hints via `error_hint`.** Replaces `blocked_import_hint` as entry point; covers `shutil`, `__import__`, kaleido and the tick limit.
- **Wider `os.path` grant.** `getsize`, `abspath`, `split`, `normpath`, `relpath`.
- **Bare final expressions display in `run_python`** (`PythonConfig.echo = "last"`).

### Changed
- **Tick limits raised.** `PythonConfig.tick_limit` 1M to 50M, `AppsConfig.request_tick_limit` 200k to 10M.
- **Requires sandtrap 0.2.11** (worker tracebacks, per-exec echo).

### Fixed
- **`dataframes()` pins a fork-safe arrow allocator** (`ARROW_DEFAULT_MEMORY_POOL=system`). Stops `libarrow` segfaults in forked workers; set it yourself if you import pandas earlier.

## 0.1.1 - 2026-07-15

### Added
- **The nontainer a2ui catalog** (`NONTAINER_CATALOG`). Declares `Stat`, `Callout`, `Chart`; opt in via `turn_to_a2ui(catalog_id=...)`.

### Fixed
- **a2ui cards render on basic consumers** (#16). Content rides a `Column` behind `child`.
- **Card builders harden direct `/ui` writes.** Unknown `tone` clamps to `info`; nulls read as absent.

## 0.1.0 - 2026-07-15

### Added
- **`AppsConfig.script_hosts` and `apps_primer`.** One script allowlist feeds `test_app`, the CSP (`build_csp`) and agent notes; `apps_primer` adds embedder guidance.
- **`run_python` reports files written to `/ui`.**
- **Redirect hints.** A `/api/<name>.py` 404 explains endpoint names; blocked network imports point at terminal `curl`.
- **Safe stdlib by default.** `PythonConfig(stdlib=True)` grants `nontainer.presets.STDLIB`, including `urllib.parse` and `warnings`.
- **The `ui` artifact cap explains itself.** `materialize_ui` now returns `(artifacts, problems)`.
- **Artifact channels.** A `view_image` tool, MCP `workspace://{path}` resources and a `--mount POINT=DIR[:rw]` MCP CLI flag.
- **Module-grant presets.** `presets.dataframes()`, `plotting()`; `ModuleGrant` gains `include`/`exclude`/`recursive`/`name`.
- **Results pin their commit.** `checkpoint` on results, `WriteOutcome`, `ws.head`/`ws.dirty`.
- **Host conveniences.** `ws.aterminal`, `ws.arun_python`, a `python3` alias, `py.typed`.
- **Shared `test_app` browser.** `configure_browser`, `arun_test_app`, `shutdown_browser`.
- **Tool primers.** `terminal_primer` and `python_primer` on `WorkspaceTools`/`build_server`.
- **Faithful `sys` in terminal `python`.** Piped stdin, `sys.argv`, `input()`.
- **Extension surface: `exec_python`, `build_sandbox`, `lock`.** The apps extra uses only this.
- **`--apps` flag on the MCP CLI.**

### Changed
- **Workspace enforces single-writer internally.** Mutating calls hold an `RLock`, so harnesses no longer need their own lock.
- **stderr capture is per-execution** (`ExecResult.stderr`).
- **Breaking: live app serving uses frozen snapshots.** Read-only and concurrent; keep mutable state in `host_objects`. Adds `on_log`, `AppRuntime(frozen=, log_sink=)`.
- **Breaking: removed router and `test_app` knobs.** `queue_depth`, `quiesce_seconds`, `rate_limit_per_min`, `max_snapshots`, `cdn_allowlist`.
- **Requires sandtrap 0.2.4 and monkeyfs 0.1.5** (per-execution stderr, synthetic `sys`; `VirtualFS.invalidate()`).

### Fixed
- **`test_app` fixes:** stringified actions accepted; screenshot cap is a soft skip; fewer false PASSes (`settle_cap`).
- **Response headers match case-insensitively.**
- **`Request.require()` coerces consistently.**
- **Handler-log failures warn.**
- **Faster browser shutdown at exit.**
- **App static serving path traversal.** `.`/`..` can't escape `/app/`; `/app/api/` is never served.
