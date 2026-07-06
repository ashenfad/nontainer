# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Pre-1.0; the API is still moving. Notable changes since the initial cut:

### Added
- **Safe stdlib by default** — `PythonConfig(stdlib=True)` grants a
  curated stdlib set (see `nontainer.presets.STDLIB`), so a plain
  workspace's Python can `import math`/`json`/`csv`/... out of the box.
- **Module-grant presets** — `nontainer.presets.dataframes()` (numpy +
  pandas) and `plotting()` (matplotlib Agg-pinned + font cache warmed;
  plotly optional). `ModuleGrant` gains `include`/`exclude`/`recursive`/
  `name`; `PythonConfig.modules` flattens preset lists one level.
- **Results pin their commit** — `TerminalResult`/`PythonResult`/
  `EditOutcome` carry `checkpoint` (the commit the call produced, or
  `None`); `write_file`/`put` return a `WriteOutcome`; `ws.head` /
  `ws.dirty` pin the state a read-only call observed.
- **Async host facades** — `ws.aterminal` / `ws.arun_python` run the
  sync execution in a thread so event-loop hosts (FastAPI, etc.) stay
  responsive; the agent surface is unchanged.
- **Shared browser for `test_app`** — one Chromium across all calls
  (async Playwright on a dedicated loop-thread), a context per
  concurrent test bounded by a semaphore (`configure_browser`), plus
  `arun_test_app` and `shutdown_browser`. Memory scales with
  concurrency, not sessions.
- **`py.typed`** — the package now ships its PEP 561 marker.

### Fixed
- **App static serving path traversal** — `.`/`..` segments can no
  longer escape `/app/`, and backend source under `/app/api/` is never
  served as a static file.

### Changed
- Requires **sandtrap ≥ 0.2.2** (recursive-registration filter
  propagation + dotted patterns) and **monkeyfs ≥ 0.1.5**
  (`VirtualFS.invalidate()`).
