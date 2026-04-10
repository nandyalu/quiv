<a id="v0.3.3"></a>
## [v0.3.3 - fixed interval option for tasks](https://github.com/nandyalu/quiv/releases/tag/v0.3.3) - 2026-04-09

### Breaking changes

- Default interval scheduling changed to fixed intervals: `fixed_interval` defaults to `True`, meaning next run is now scheduled from the
job **start time** rather than completion time. Set `fixed_interval=False` to
restore the previous wait-between-runs behavior.

### What's new

- `fixed_interval` per-task scheduling mode: `add_task()` accepts a new `fixed_interval` parameter:

    - **`True`** (default) — next run at fixed intervals from job start time.
      If a run exceeds the interval, missed intervals are skipped.
    - **`False`** — next run `interval` seconds after job completion (old behavior).

### Other changes

- `finalize_task_after_job()` accepts `job_started_at` for fixed-interval
  scheduling.

**Full Changelog**: https://github.com/nandyalu/quiv/compare/v0.3.2...v0.3.3

[Changes][v0.3.3]


<a id="v0.3.2"></a>
## [v0.3.2 - Removed unique task name constraint](https://github.com/nandyalu/quiv/releases/tag/v0.3.2) - 2026-04-09

### Breaking changes

- Task operations now use `task_id` instead of `task_name`:
    All public methods that previously accepted a `task_name` string now accept the `task_id` (UUID string) returned by `add_task()`. This removes the uniqueness constraint on task names — multiple tasks can now share the same `task_name`.

    **Affected methods:**
    - `remove_task(task_id)` — previously `remove_task(task_name)`
    - `pause_task(task_id)` — previously `pause_task(task_name)`
    - `resume_task(task_id)` — previously `resume_task(task_name)`
    - `run_task_immediately(task_id)` — previously `run_task_immediately(task_name)`
    - `get_task(task_id)` — previously `get_task(task_name)` (by-name lookup)

    **Removed methods:**
    - `get_task_by_id()` — merged into `get_task(task_id)`

    **Migration:** Store the return value of `add_task()` and pass it to all task operations:

    ```python
    # Before
    scheduler.add_task("my-task", handler, interval=60)
    scheduler.pause_task("my-task")

    # After
    task_id = scheduler.add_task("my-task", handler, interval=60)
    scheduler.pause_task(task_id)
    ```

- Event listeners receive typed model objects instead of dicts

    Event listener callbacks now receive `Task` and `Job` model objects directly instead of untyped `dict[str, Any]`.

    - **`TASK_*` events**: `callback(event: Event, task: Task)`
    - **`JOB_*` events**: `callback(event: Event, task: Task, job: Job)`

    **Migration:**

    ```python
    # Before
    def on_completed(event, data):
        print(data["task_name"], data["duration"])

    # After
    from quiv.models import Task, Job

    def on_completed(event: Event, task: Task, job: Job):
        print(task.task_name, job.duration_seconds)
    ```

### What's new

- Duplicate task names allowed

    `add_task()` no longer raises `ConfigurationError` on duplicate `task_name`. Each call returns a unique `task_id` (UUID), so multiple tasks can share a display name. This is especially useful for one-shot tasks that may be scheduled repeatedly with the same name.

- `duration_seconds` and `error_message` on the Job model

    The `Job` model now includes two new fields:

    - `duration_seconds: float | None` — job runtime in seconds, set when the job finishes
    - `error_message: str | None` — error description, set when a job fails

    Both fields are persisted in the database and available via `get_job()` and `get_all_jobs()`, making it easy to inspect job history without parsing logs.

- Typed event listener callbacks

    Event listeners now receive real `Task` and `Job` model objects with full IDE autocomplete and type checking. `JOB_*` events include the parent `Task` alongside the `Job`, so listeners have full context without extra lookups.

### Documentation

- Updated all docs to reflect `task_id`-based API across getting-started, API reference, architecture, event listeners, bigger applications, progress callbacks, and exceptions pages.
- Updated all code examples to use `task_id` for runtime operations.
- Added admonitions and footnotes throughout docs for better readability.
- Added `_job_id` tracing section to the "Why quiv?" page, describing how Trailarr uses injected job IDs as trace context for log correlation.
- Rewrote event listeners documentation with typed callback signatures, updated examples, and new FastAPI WebSocket example using model objects.

### Other changes

- Internal handler and progress callback registries are now keyed by `task_id` instead of `task_name`.
- `prepare_invocation()` in the execution layer uses `task_id` for progress callback dispatch.
- Removed `get_task_by_name()`, `get_task_id_by_name()` from the persistence layer.
- Renamed `get_task_by_id()` to `get_task()` in the persistence layer.
- `delete_task()` and `queue_task_for_immediate_run()` in the persistence layer now accept `task_id` instead of `task_name`.
- `finalize_job()` now accepts optional `duration_seconds` and `error_message` parameters.
- Removed unused `timezone` import from persistence module.



**Full Changelog**: https://github.com/nandyalu/quiv/compare/v0.3.1...v0.3.2

[Changes][v0.3.2]


<a id="v0.3.1"></a>
## [v0.3.1 - Better task pickling](https://github.com/nandyalu/quiv/releases/tag/v0.3.1) - 2026-04-07

### Breaking Changes
* The public methods `get_task()`, `get_task_by_id()`, and `get_all_tasks()` return `Task` objects with unpickled `args` (tuple) and `kwargs` (dict) — ready for JSON serialization in FastAPI endpoints.
* Internal model renamed: The SQLModel database model is now `TaskDB` (internal only, not exported). External model `Task` is still the same - but is now only used for Public API responses and has correct types.

### What's New
* Async callbacks without event loop: Async progress callbacks and event listeners now run in a temporary event loop when no main loop is available, instead of being skipped with a warning.
* Eager main loop resolution: The main event loop is now resolved at `start()` time (in addition to lazy resolution on first callback), improving reliability in FastAPI apps.

###  Documentation
* Added return types to all method signatures in API docs (e.g., `get_task(task_name: str) -> Task`.
* Updated architecture, progress-callbacks, and event-listeners docs to reflect new async callback behavior 

Changes by [@nandyalu](https://github.com/nandyalu) in [#20](https://github.com/nandyalu/quiv/pull/20)


**Full Changelog**: https://github.com/nandyalu/quiv/compare/v0.3.0...v0.3.1

[Changes][v0.3.1]


<a id="v0.3.0"></a>
## [v0.3.0 - Better pickling and Event listeners](https://github.com/nandyalu/quiv/releases/tag/v0.3.0) - 2026-04-07

### What's Changed
* Changed argument serialization for task persistence from JSON to pickle, allowing most Python objects (except lambdas and inner functions) to be scheduled as arguments by [@nandyalu](https://github.com/nandyalu) in [#19](https://github.com/nandyalu/quiv/pull/19)
* Added support for global event listeners via `add_listener(event, callback)` and `remove_listener(event, callback)`, including both sync and async callbacks, with robust dispatch and error handling. Events include all major task and job lifecycle transitions. by [@nandyalu](https://github.com/nandyalu) in [#19](https://github.com/nandyalu/quiv/pull/19)
* Introduced `startup()` as an alias for `start()`, and `stop()` as an alias for `shutdown()`, making `start/stop` pairs more natural in user code by [@nandyalu](https://github.com/nandyalu) in [#19](https://github.com/nandyalu/quiv/pull/19)
* `Job.id` now uses `UUID` and can be injected into task function (as `_job_id`) if function accepts it - can be used for task tracing / logging by [@nandyalu](https://github.com/nandyalu) in [#19](https://github.com/nandyalu/quiv/pull/19)
* Updated all relevant documentation by [@nandyalu](https://github.com/nandyalu) in [#19](https://github.com/nandyalu/quiv/issues/19)


**Full Changelog**: https://github.com/nandyalu/quiv/compare/v0.2.4...v0.3.0

[Changes][v0.3.0]


<a id="v0.2.4"></a>
## [v0.2.4 - Preserve task args order](https://github.com/nandyalu/quiv/releases/tag/v0.2.4) - 2026-04-07

### What's Changed
* `add_task` args as tuple to preserve order by [@nandyalu](https://github.com/nandyalu) in [#18](https://github.com/nandyalu/quiv/pull/18)
* `logger` accepts `logging.Logger` as well as `logging.LoggerAdapter` by [@nandyalu](https://github.com/nandyalu) in [#18](https://github.com/nandyalu/quiv/pull/18)


**Full Changelog**: https://github.com/nandyalu/quiv/compare/v0.2.3...v0.2.4

[Changes][v0.2.4]


<a id="v0.2.3"></a>
## [v0.2.3 - Exception logging improvements](https://github.com/nandyalu/quiv/releases/tag/v0.2.3) - 2026-04-06

### What's Changed
* Bump zensical from 0.0.24 to 0.0.27 by [@dependabot](https://github.com/dependabot)[bot] in [#8](https://github.com/nandyalu/quiv/pull/8)
* Bump zensical from 0.0.27 to 0.0.28 by [@dependabot](https://github.com/dependabot)[bot] in [#9](https://github.com/nandyalu/quiv/pull/9)
* Bump pytest-cov from 7.0.0 to 7.1.0 by [@dependabot](https://github.com/dependabot)[bot] in [#10](https://github.com/nandyalu/quiv/pull/10)
* Bump actions/configure-pages from 5 to 6 by [@dependabot](https://github.com/dependabot)[bot] in [#11](https://github.com/nandyalu/quiv/pull/11)
* Bump actions/deploy-pages from 4 to 5 by [@dependabot](https://github.com/dependabot)[bot] in [#12](https://github.com/nandyalu/quiv/pull/12)
* Bump zensical from 0.0.28 to 0.0.30 by [@dependabot](https://github.com/dependabot)[bot] in [#13](https://github.com/nandyalu/quiv/pull/13)
* Bump sqlmodel from 0.0.37 to 0.0.38 by [@dependabot](https://github.com/dependabot)[bot] in [#14](https://github.com/nandyalu/quiv/pull/14)
* Bump tzdata from 2025.3 to 2026.1 by [@dependabot](https://github.com/dependabot)[bot] in [#15](https://github.com/nandyalu/quiv/pull/15)
* Bump mypy from 1.19.1 to 1.20.0 by [@dependabot](https://github.com/dependabot)[bot] in [#16](https://github.com/nandyalu/quiv/pull/16)
* Improve job logging with task names and error details by [@nandyalu](https://github.com/nandyalu) in [#17](https://github.com/nandyalu/quiv/pull/17)


**Full Changelog**: https://github.com/nandyalu/quiv/compare/v0.2.2...v0.2.3

[Changes][v0.2.3]


<a id="v0.2.2"></a>
## [v0.2.2 - SQLModel Registry Fix](https://github.com/nandyalu/quiv/releases/tag/v0.2.2) - 2026-03-13

### What's Changed
* fix: quiv registry to not include user models. Updated the private `registry` usage for `SQLModel` models of `Quiv` to the method from https://github.com/fastapi/sqlmodel/discussions/1539#discussioncomment-14229572 by [@nandyalu](https://github.com/nandyalu) in [#7](https://github.com/nandyalu/quiv/pull/7)

* Added a test to ensure user's `SQLModel` with `table=True` does not get created in Quiv database by [@nandyalu](https://github.com/nandyalu) in [#7](https://github.com/nandyalu/quiv/pull/7)

**Full Changelog**: https://github.com/nandyalu/quiv/compare/v0.2.0...v0.2.2

[Changes][v0.2.2]


<a id="v0.2.1"></a>
## [v0.2.1 - Fix Release Build](https://github.com/nandyalu/quiv/releases/tag/v0.2.1) - 2026-03-11

### What's Changed
* Fix release build and auto update release-notes in docs by [@nandyalu](https://github.com/nandyalu) in [#6](https://github.com/nandyalu/quiv/pull/6)


**Full Changelog**: https://github.com/nandyalu/quiv/compare/v0.2.0...v0.2.1

[Changes][v0.2.1]


<a id="v0.2.0"></a>
## [v0.2.0-Bug Fixes and minor updates](https://github.com/nandyalu/quiv/releases/tag/v0.2.0) - 2026-03-09

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


[Changes][v0.2.0]


<a id="v0.1.0"></a>
## [Initial Release (v0.1.0)](https://github.com/nandyalu/quiv/releases/tag/v0.1.0) - 2026-03-08

Initial Release

[Changes][v0.1.0]


[v0.3.3]: https://github.com/nandyalu/quiv/compare/v0.3.2...v0.3.3
[v0.3.2]: https://github.com/nandyalu/quiv/compare/v0.3.1...v0.3.2
[v0.3.1]: https://github.com/nandyalu/quiv/compare/v0.3.0...v0.3.1
[v0.3.0]: https://github.com/nandyalu/quiv/compare/v0.2.4...v0.3.0
[v0.2.4]: https://github.com/nandyalu/quiv/compare/v0.2.3...v0.2.4
[v0.2.3]: https://github.com/nandyalu/quiv/compare/v0.2.2...v0.2.3
[v0.2.2]: https://github.com/nandyalu/quiv/compare/v0.2.1...v0.2.2
[v0.2.1]: https://github.com/nandyalu/quiv/compare/v0.2.0...v0.2.1
[v0.2.0]: https://github.com/nandyalu/quiv/compare/v0.1.0...v0.2.0
[v0.1.0]: https://github.com/nandyalu/quiv/tree/v0.1.0

<!-- Generated by https://github.com/rhysd/changelog-from-release v3.9.1 -->
