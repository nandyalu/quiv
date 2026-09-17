<a id="v1.0.0"></a>
## v1.0.0 - Unreleased

The first stable release. `v1.0.0` freezes the public API.

### What changed since v0.3

Six phases of work separate `v0.3` from `v1.0.0`. Each one shipped as its own minor release.

| Release | Theme | Main additions |
| --- | --- | --- |
| `v0.5.0` | Correctness and stability | A guard against a double run in `run_task_immediately()`, locked handler registries, and `shutdown(timeout=...)` |
| `v0.6.0` | Scheduler efficiency | The loop sleeps until the next task is due, instead of polling once a second. Intervals below one second work, and an idle scheduler queries the database zero times |
| `v0.7.0` | Database locking | Reads run without a lock under WAL. Writes serialize on a writer lock |
| `v0.8.0` | Execution features | `timeout`, `max_retries` with exponential backoff, and `jitter` |
| `v0.9.0` | Management and observability | `update_task()`, filters and paging for the job and task queries, and `stats()` |
| `v0.10.0` | Absolute-time scheduling | `run_at` sets the time of the first run |

Read [Migrating from 0.x](#migrating-from-0x) for every change that needs an edit to your code.

### Breaking changes

!!! warning "Injected handler parameters lost the underscore prefix"

    `add_task()` rejects a handler that still declares `_job_id`, `_stop_event`, or `_progress_hook`. Rename the parameter. The behavior of each one is unchanged.

quiv injects three parameters into a handler that declares them. Those names are now public names:

| Before (`0.x`) | Now (`v1.0.0`) |
| --- | --- |
| `_job_id` | `job_id` |
| `_stop_event` | `stop_event` |
| `_progress_hook` | `progress_hook` |

A leading underscore means "private, do not touch" in Python. These three parameters are the opposite of private. You declare them, you read them, and cooperative cancellation is built on them. They are part of the contract for writing a handler, so they now read as public names.

#### How to migrate a handler

Rename the parameter in the signature and in the body. Nothing else changes.

```python
# 0.x
def sync_users(_job_id=None, _stop_event=None, _progress_hook=None):
    if _stop_event and _stop_event.is_set():
        return
    _progress_hook(percent=50)

# v1.0.0
def sync_users(job_id=None, stop_event=None, progress_hook=None):
    if stop_event and stop_event.is_set():
        return
    progress_hook(percent=50)
```

#### quiv reports a missed rename

`add_task()` raises `ConfigurationError` when a handler still declares an old name. The message names the parameter and its new spelling. quiv raises it at registration, before it writes the task row and before any job runs.

This check exists because the failure is otherwise silent. quiv no longer injects `_stop_event`, so the parameter keeps its default value. The handler never sees a cancellation. `cancel_job()` stops working, and every `timeout` stops working, because a timeout sets the same stop event. Nothing raises, and nothing is written to the log.

A handler that declares `**kwargs` is not affected. It asks for no name, so quiv has nothing to check.

#### A new conflict to know about

`job_id`, `stop_event`, and `progress_hook` are ordinary names now, so a key in `kwargs` can collide with one:

```python
scheduler.add_task("sync", sync_users, interval=60, kwargs={"job_id": "external-123"})
```

An injected value overwrites a caller value of the same name. If `sync_users` accepts `job_id`, quiv would replace `"external-123"` with its own job id, and you would never know. `add_task()` and `update_task()` raise `ConfigurationError` instead, because quiv cannot tell which value you wanted. Rename the key, or remove the parameter from the handler signature. A handler that declares `**kwargs` accepts every injected name, so this check covers it too.

### `TaskNotScheduledError` was removed

`v0.9.0` deprecated this name and stopped raising it. `v1.0.0` removes the class and its export. Catch `TaskNotFoundError` instead.

```python
# 0.x
from quiv import TaskNotScheduledError

# v1.0.0
from quiv import TaskNotFoundError
```

### `run_on_main()` raises a quiv exception

`run_on_main()` raised a bare `RuntimeError` when it could not reach a main event loop. It now raises `MainLoopUnavailableError`, which inherits both `QuivError` and `RuntimeError`.

This is not a breaking change. An existing `except RuntimeError` clause still catches it, and `except QuivError` now catches it as well. Every exception that quiv raises is under `QuivError`.

### Migrating from 0.x

Work through this list. Each entry names the release that changed the behavior.

**1. Rename the injected handler parameters.** (`v1.0.0`) Change `_job_id` to `job_id`, `_stop_event` to `stop_event`, and `_progress_hook` to `progress_hook`. `add_task()` raises `ConfigurationError` if you miss one. See [the section above](#breaking-changes).

**2. Catch `TaskNotFoundError` instead of `TaskNotScheduledError`.** (`v0.9.0`, removed in `v1.0.0`) The alias is gone.

**3. Check the exceptions you catch around `run_task_immediately()`.** (`v0.5.0`, `v0.9.0`) Two causes now report different exceptions:

- The task is `running` or `paused` — `TaskNotActiveError`. Earlier versions dispatched a second concurrent run, or un-paused the task without telling you. Call `resume_task()` to un-pause a task.
- The task id is unknown — `TaskNotFoundError`. Earlier versions raised `HandlerNotRegisteredError`, which names a different fault. `HandlerNotRegisteredError` keeps its own meaning: the task exists, but no handler is registered for it.

**4. Review timing-sensitive code.** (`v0.6.0`) The scheduler no longer polls once a second. A due task now dispatches almost at once. Code that relied on the old delay of about one second sees a job start sooner than before.

**5. Bound your shutdown if a handler can hang.** (`v0.5.0`) `shutdown(timeout=...)` limits how long quiv waits for the scheduler thread and for jobs still running. A job that does not exit within the deadline is abandoned on its worker thread, and quiv writes a warning. The default still waits forever.

**6. Drop `interval` from a run-once task, if you want to.** (`v0.9.0`) A run-once task never repeats, so quiv never reads its interval. Passing one with `run_once=True` is accepted and ignored, and `Task.interval_seconds` reads `None`. A recurring task still needs `interval > 0`. Passing `interval=None` for a recurring task now raises `ConfigurationError`, where earlier versions raised `TypeError`.

**7. Note the new default of `delay`.** (`v0.10.0`) `delay` defaults to `None` instead of `0`, so quiv can tell "no delay given" from `delay=0`. Omitting `delay` still means no delay, and `delay=0` still works. Passing both `delay` and `run_at` raises `ConfigurationError`.

Nothing else in the `0.x` API changed. Your calls to `add_task()`, `pause_task()`, `resume_task()`, `remove_task()`, `cancel_job()`, and the query methods keep working.

### Performance

`v1.0.0` adds a [benchmark suite](https://github.com/nandyalu/quiv/tree/main/benchmarks). The numbers below come from an Intel Core i5-11600 at 2.80 GHz, 12 logical CPUs, Linux, Python 3.10.12. Read them as one machine on one day.

| Measurement | Result |
| --- | --- |
| Time from a task becoming due to its handler starting | 14 ms at p50, 18 ms at p95 |
| 100 tasks that share one due time | about 5 ms for each extra task in the batch |
| Jobs completed each second, no-op handler, `pool_size=16` | about 200 |
| `add_task()` calls each second | about 1000 |

The first row is the one to remember. A task starts within about 18 ms of its due time, where quiv `0.5` and earlier polled once a second and could take up to 1000 ms.

**A larger pool does not raise the rate of short jobs.** Between `pool_size=4` and `pool_size=64` the rate stays near 200 jobs per second. The limit is the database, not the pool: each job writes four times, and a write takes the writer lock, so those writes queue behind the writes of every other job. Raise `pool_size` when your handlers wait on something. For work that lasts a few milliseconds, the bookkeeping costs more than the work itself.

### Reliability

`v1.0.0` ran for 24 hours without a pause, under a workload that mixes every execution path: a sync task, an async task, a task that fails and retries, a task that another thread cancels, a task that passes its timeout, and a task on a sub-second interval. A separate thread read `stats()` and `get_all_jobs()` every second throughout.

| Measurement | Result over 24 hours |
| --- | --- |
| Jobs finished | 294,400 — 234,876 completed, 32,229 failed, 27,295 cancelled |
| Retries queued | 21,486 |
| Live threads | 8 at the start, 8 at the end, 8 at every sample between |
| Retained job history | reached 2,264 rows in the first hour, then flat |
| Memory | 53 MB, start to finish |
| Unexpected errors | none |

The thread count is the number to look at. It did not move across roughly 294,000 job lifecycles, each of which created a stop event and a set of injected arguments, and each run of the async task built its own event loop and closed it. The retained history stopped growing after the first hour, so the cleanup keeps pace with the work for as long as the process runs. Memory held flat, which rules out job rows, stop events, and event loops accumulating behind the scenes.

Reproduce it with `uv run python scripts/soak.py --hours 24`. The full log of the release run is in the repository at [`benchmarks/results/soak-24h-2026-09-17.log`](https://github.com/nandyalu/quiv/blob/main/benchmarks/results/soak-24h-2026-09-17.log), and [Testing](testing.md#soak-test) describes the method.

<a id="v0.10.0"></a>
## [v0.10.0 - Absolute-time scheduling](https://github.com/nandyalu/quiv/releases/tag/v0.10.0) - 2026-09-08

`add_task()` can now schedule the first run at an absolute time.

### What's new

- **`run_at`** ([#66](https://github.com/nandyalu/quiv/issues/66)) — sets the absolute time of the first run, as an alternative to `delay`. It is keyword-only.

    ```python
    scheduler.add_task(
        task_name="agent_wakeup_alarm",
        func=agent_wakeup_alarm,
        run_at=monday_open,
        run_once=True,
    )
    ```

    The rules:

    - `run_at` and `delay` are mutually exclusive. Passing both raises `ConfigurationError`.
    - A naive `datetime` is read as UTC. The `timezone` setting only formats log output, and never interprets `run_at`.
    - An aware `datetime` in any zone is converted to UTC.
    - A time already past runs at once. The process may have been down when the time came due, and dropping the task is worse than running it late.
    - A `run_at` that is not a `datetime` raises `ConfigurationError`.

    `run_at` sets one instant for the first run. It is not calendar scheduling: recurrence stays interval-based, and quiv still has no cron expressions.

- `delay` now defaults to `None` rather than `0`, so quiv can tell "no delay given" from `delay=0` and reject `run_at` combined with either. Omitting `delay` still means no delay, and `delay=0` still works.

**Full Changelog**: https://github.com/nandyalu/quiv/compare/v0.9.0...v0.10.0

<a id="v0.9.0"></a>
## [v0.9.0 - Management & Observability API](https://github.com/nandyalu/quiv/releases/tag/v0.9.0) - 2026-09-05

Phase 5 of the [roadmap to v1.0.0](roadmap.md) adds three management features: `update_task()`, rich job/task queries, and `stats()`. See the new [Observability](observability.md) docs page.

### What's new

- **`update_task()`** — changes a scheduled task in place and keeps its `task_id`. You can change the name, interval, scheduling mode, args/kwargs, timeout, retry settings, jitter, and the progress callback. All parameters are keyword-only; omit the parameters you do not want to change. `timeout=None` disables the timeout, and `progress_callback=None` removes the callback. When you change `interval`, quiv reschedules the next run to `now + interval`. When you update a `running` task, the changes apply from its next run. The method emits the new `Event.TASK_UPDATED` with the updated `Task`. You cannot change `run_once`, `delay`, or the handler `func`.
- **Rich job/task queries** — `get_all_jobs()` accepts new filters: `task_id`, and `since`/`until` for a time window on `started_at` (pass timezone-aware UTC values). It also accepts `order_by` (`"started_at"` or `"ended_at"`), `descending`, and `limit`/`offset` for pagination. `get_all_tasks()` accepts `status`, `limit`, and `offset`, and returns tasks ordered by `next_run_at`.
- **`stats()`** — returns a `QuivStats` snapshot (a frozen dataclass, exported from `quiv`). It contains the active job count, the pool size and utilization, task counts by status, the earliest upcoming run, and the retained job-history count.
- The FastAPI example app has three new endpoints: `GET /tasks/stats`, `GET /tasks/{task_id}/jobs?limit=&offset=`, and `PATCH /tasks/{task_id}`.

### Fixes

- **`add_task()` no longer requires an `interval` for a run-once task** ([#65](https://github.com/nandyalu/quiv/issues/65)). A run-once task never repeats, so quiv never reads its interval. Omit `interval` when you pass `run_once=True`. An interval given with `run_once=True` is ignored, and `Task.interval_seconds` reads `None`. A recurring task still requires `interval > 0`.
- **`interval=None` now raises `ConfigurationError`** ([#65](https://github.com/nandyalu/quiv/issues/65)). Earlier versions raised `TypeError` from an unguarded comparison. `None` now reaches the same error as `0` and `-1`.
- **`run_task_immediately()` raises `TaskNotFoundError` for an unknown task id** ([#67](https://github.com/nandyalu/quiv/issues/67)). Earlier versions raised `HandlerNotRegisteredError`, which names a different fault. `get_task()`, `remove_task()` and `run_task_immediately()` now report the same error for the same cause. A run-once task deletes itself when it finishes, so its id stops resolving after it runs. `HandlerNotRegisteredError` keeps its own meaning: the task exists, but no handler is registered for it.

### Behavior changes

- **`TaskNotScheduledError` is deprecated.** quiv no longer raises it. It is now a subclass of `TaskNotFoundError`, and the name stays exported, so imports keep working. An `except TaskNotScheduledError` clause no longer catches these errors. Change it to `except TaskNotFoundError`. quiv removes the alias in 1.0.0.

**Full Changelog**: https://github.com/nandyalu/quiv/compare/v0.8.0...v0.9.0

<a id="v0.8.0"></a>
## [v0.8.0 - Execution Features](https://github.com/nandyalu/quiv/releases/tag/v0.8.0) - 2026-08-06

Phase 4 of the [roadmap to v1.0.0](roadmap.md) adds three failure-handling features: per-task timeout, retry with exponential backoff, and jitter. See the new [Failure Handling](failure-handling.md) docs page.

### What's new

- **Per-task timeout** — `add_task(..., timeout=30)` sets a time limit for each job. When a job runs longer than `timeout` seconds, quiv sets the job's stop event, in the same way as `cancel_job()`. The job then finalizes as `cancelled` with a timeout error message. The timeout is cooperative. If the handler ignores its stop event, it keeps its pool thread until it returns; quiv never kills threads. The scheduler loop wakes for the nearest timeout deadline, so a timeout fires within milliseconds of that deadline.
- **Retry with exponential backoff** — `add_task(..., max_retries=3, retry_backoff=10)` runs a failed job again. A job is failed when an exception escapes the handler. The next attempt starts after `retry_backoff * 2**(failures - 1)` seconds: the first retry waits `retry_backoff` seconds, the second waits twice that, and so on. Cancelled jobs do not retry; this includes timeouts. A successful run resets the failure counter. When retries are exhausted, a recurring task returns to its normal schedule and a run-once task is deleted. Each `Job` records its `attempt` number. The new `Event.JOB_RETRYING` fires after `JOB_FAILED` when quiv schedules a retry.
- **Jitter** — `add_task(..., jitter=5)` adds a random offset between 0 and `jitter` seconds to each next run of a recurring task. Use it when many tasks share the same interval boundaries and would start at the same time. quiv draws a new offset for every run. Jitter does not apply to the initial `delay` or to retry backoff.
- `Task` exposes the new fields `timeout_seconds`, `max_retries`, `retry_backoff_seconds`, `retry_attempt`, and `jitter_seconds`. `Job` exposes `attempt`.
- The four new options are keyword-only. The rest of the `add_task()` signature is unchanged, including positional `args`, `kwargs`, and `progress_callback`. Existing calls continue to work.

### Fixes

- Fixed-interval scheduling could set `next_run_at` to a time that is not in the future. This happened when a job finished within clock resolution of its start time, or when the elapsed time landed exactly on an interval boundary. The task then dispatched again immediately. The next run is now always the next interval boundary that is strictly in the future.
- When a timed-out job's handler also raised an exception, the job's `error_message` showed only the exception text and hid the timeout. The timeout message now comes first, and the handler's exception is appended.

**Full Changelog**: https://github.com/nandyalu/quiv/compare/v0.7.0...v0.8.0

<a id="v0.7.0"></a>
## [v0.7.0 - Database Locking Rework](https://github.com/nandyalu/quiv/releases/tag/v0.7.0) - 2026-08-04

Phase 3 of the [roadmap to v1.0.0](roadmap.md): lock-free reads under SQLite WAL.

### What's new

- **Lock-free reads** — read queries (`get_task`, `get_job`, `get_all_tasks`, `get_all_jobs`, due-task queries) no longer serialize behind the persistence layer's global lock; SQLite WAL mode provides the reader/writer coordination. Read-modify-write operations (task/job lifecycle transitions, pause/resume, cleanup) still serialize on a dedicated write lock, preserving the no-lost-update guarantees.
- **SQLite pragmas** — connections now set `synchronous=NORMAL` (the standard WAL pairing: fsync on checkpoint instead of per-commit; the DB is an ephemeral temp file, so durability-on-crash was never a goal) and an explicit `busy_timeout=10000` matching the existing driver-level timeout.

### Performance

With a writer and a heavy `get_all_jobs` reader running concurrently, p99 latency of a small `get_task` read drops from ~89 ms to ~1.1 ms (~80×) — small reads no longer queue behind large scans or writes on a global lock. Aggregate wall time on a synthetic hammer benchmark (5,000 mixed ops, 70% reads / 30% writes, 16 threads) measured 31.0 s before vs 44.5 s after: with every thread busy-looping CPU-bound ORM deserialization, CPython's GIL makes convoy-free serialized execution faster in aggregate — an artifact of the synthetic saturation workload, not of realistic loads, and one that disappears on free-threaded builds. The `synchronous=NORMAL` pragma alone improves write throughput ~20% (31.0 s → 24.6 s with reads still serialized).

### Housekeeping

- New concurrency stress-test suite (`tests/test_persistence_concurrency.py`): concurrent writers with no lost updates, readers seeing consistent rows during writes, a full scheduler run at `pool_size=32` under constant read load, and a pause/resume race. Docs and version updated to `0.7.0`.

**Full Changelog**: https://github.com/nandyalu/quiv/compare/v0.6.0...v0.7.0

<a id="v0.6.0"></a>
## [v0.6.0 - Scheduler Core Efficiency](https://github.com/nandyalu/quiv/releases/tag/v0.6.0) - 2026-07-26

Phase 2 of the [roadmap to v1.0.0](roadmap.md): smart sleep loop and handler signature caching.

### What's new

- **Sub-second intervals** — the scheduler loop now sleeps until the next due task on an interruptible wait instead of polling every second. `add_task(interval=0.2)` works; dispatch jitter drops from up to ~1 s to milliseconds.
- **Zero idle polling** — an idle scheduler issues no database queries (bounded by a 60-second safety-net wake-up). Schedule changes (`add_task`, `run_task_immediately`, `resume_task`, `remove_task`) and job completions wake the loop immediately, so deferred tasks dispatch as soon as a pool slot frees and `shutdown()` returns promptly instead of waiting out the current sleep.
- **Handler signature caching** — each handler's injectable kwargs (`_job_id`, `_stop_event`, `_progress_hook`) are introspected once per handler lifetime (weakly cached) instead of three `inspect.signature()` calls per dispatch.

**Full Changelog**: https://github.com/nandyalu/quiv/compare/v0.5.0...v0.6.0

[Changes][v0.6.0]


<a id="v0.5.0"></a>
## [v0.5.0 - Correctness & Stability](https://github.com/nandyalu/quiv/releases/tag/v0.5.0) - 2026-07-26

Phase 1 of the [roadmap to v1.0.0](roadmap.md): three concurrency bug fixes.

### Behavior changes

- `run_task_immediately()` now raises `TaskNotActiveError` when the task is not `active`. Previously a `running` task could be dispatched a second time concurrently (breaking the no-overlap guarantee) and a `paused` task was silently un-paused. Resume paused tasks explicitly with `resume_task()`.

### What's new

- `shutdown(timeout=...)` / `stop(timeout=...)` — optional bound on how long shutdown waits for the scheduler thread and in-flight jobs. Jobs that do not exit within the deadline are abandoned on their worker threads with a warning, so a hung handler can no longer block application shutdown forever. Default (`timeout=None`) keeps the previous wait-forever behavior.
- New exception `TaskNotActiveError` (exported from `quiv`).

### Fixes

- `remove_task()` racing the dispatch loop could raise a `KeyError` that stalled the scheduler for 5 seconds; dispatch now skips removed tasks gracefully, and the shared handler/callback/stop-event registries are protected by a lock.
- `shutdown()` now signals cancellation only to `running` jobs instead of scanning the entire retained job history.

**Full Changelog**: https://github.com/nandyalu/quiv/compare/v0.4.1...v0.5.0

[Changes][v0.5.0]


<a id="v0.3.3"></a>
## [v0.3.3 - fixed interval option for tasks](https://github.com/nandyalu/quiv/releases/tag/v0.3.3) - 2026-04-09

### Breaking changes

- Default interval scheduling changed to fixed intervals: `fixed_interval` defaults to `True`, meaning next run is now scheduled from the job **start time** rather than completion time. Set `fixed_interval=False` to restore the previous wait-between-runs behavior.

### What's new

- `fixed_interval` per-task scheduling mode: `add_task()` accepts a new `fixed_interval` parameter:

    - **`True`** (default) — next run at fixed intervals from job start time. If a run exceeds the interval, missed intervals are skipped.
    - **`False`** — next run `interval` seconds after job completion (old behavior).

### Other changes

- `finalize_task_after_job()` accepts `job_started_at` for fixed-interval scheduling.

**Full Changelog**: https://github.com/nandyalu/quiv/compare/v0.3.2...v0.3.3

[Changes][v0.3.3]


<a id="v0.3.2"></a>
## [v0.3.2 - Removed unique task name constraint](https://github.com/nandyalu/quiv/releases/tag/v0.3.2) - 2026-04-09

### Breaking changes

- Task operations now use `task_id` instead of `task_name`: All public methods that previously accepted a `task_name` string now accept the `task_id` (UUID string) returned by `add_task()`. This removes the uniqueness constraint on task names — multiple tasks can now share the same `task_name`.

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


[v0.6.0]: https://github.com/nandyalu/quiv/compare/v0.5.0...v0.6.0
[v0.5.0]: https://github.com/nandyalu/quiv/compare/v0.4.1...v0.5.0
[v0.4.0]: https://github.com/nandyalu/quiv/compare/v0.3.3...v0.4.0
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
