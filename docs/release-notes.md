## `v0.2.0` - Bug fixes and minor updates - 2026-03-08

### Breaking Changes

- **`timezone_name` renamed to `timezone`** — Both `Quiv()` and `QuivConfig()` now use `timezone` for the display timezone parameter. Update any `timezone_name=` keyword arguments.
- **`register_handler` / `register_progress_callback` are now private** — Renamed to `_register_handler` / `_register_progress_callback`. Use `add_task()` instead, which handles registration internally.
- **`add_task()` raises on duplicate task names** — Call `remove_task()` first if you need to replace a task.

### New Features

- **`remove_task(task_name)`** — Remove a task and its handler/callback registrations.
- **Cooperative cancellation** — `_stop_event` injection with per-job `threading.Event`. See [Cancellation docs](https://nandyalu.github.io/quiv/cancellation/).
- **Progress callbacks with four dispatch paths** — Async/sync callbacks work with or without an event loop. See [Progress Callbacks docs](https://nandyalu.github.io/quiv/progress-callbacks/).
- **Lazy event loop resolution** — `Quiv()` can be instantiated at module level before any asyncio loop exists. The event loop is resolved on first progress callback dispatch.
- **Backpressure** — Scheduler defers dispatch when the thread pool is full. Late-starting jobs log a warning suggesting to increase `pool_size`.
- **`TaskStatus.RUNNING`** — Tasks are marked `RUNNING` during execution, preventing concurrent runs of the same task.

### Bug Fixes

- Removed `logger.setLevel(logging.DEBUG)` — the library no longer forces a log level
- Removed `asyncio.get_event_loop()` at init — fixes deprecation warnings and module-level instantiation
- `shutdown()` now cleans up SQLite WAL (`-wal`, `-shm`) sidecar files
- Cancellation detection no longer depends on handler accepting `_stop_event`

### Improvements

- `TaskStatus` / `JobStatus` are now proper `(str, Enum)` classes
- History cleanup uses SQL-level filtering with `col()` wrapper (runs every 60s, not every tick)
- Next run scheduled from job completion time, not dispatch time
- All log timestamps use the configured display timezone consistently

### Documentation

- New pages: **Bigger Applications**, **Progress Callbacks**, **Cancellation** (with mermaid diagrams)
- **Architecture** page now has a sequence diagram
- **API** page: added pool size guidance, logger/timezone note blocks
- Tabbed uv/pip install commands across all pages
- GitHub repo link in docs header with edit/view source buttons

### Tests

- 8 new tests covering backpressure, late start warnings, concurrent run prevention, remove_task, progress callbacks without event loop, and task lifecycle
- All existing tests updated for API changes

### Repository

- Added CODEOWNERS, issue templates, CONTRIBUTING.md, CODE_OF_CONDUCT.md
- CI workflows now include `pyproject.toml` in path filters
- Removed `master` branch reference from docs deploy workflow


## `v0.1.0` - Initial Release - 2026-03-09

- Initial Release