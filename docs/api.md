# API

## `Quiv`

### Constructor

```python
Quiv(
    config: QuivConfig | None = None,
    pool_size: int = 10,
    history_retention_seconds: int = 86400,
    timezone: str | tzinfo = "UTC",
    *,
    logger: logging.Logger | None = None,
    main_loop: asyncio.AbstractEventLoop | None = None,
)
```

If `config` is provided, do not also pass explicit
`pool_size/history_retention_seconds/timezone`.

Parameters:

- `config`: grouped configuration object (see [`QuivConfig`](#quivconfig))
- `pool_size`: maximum number of tasks that can run concurrently (default 10). See [Choosing a pool size](#choosing-a-pool-size) below
- `history_retention_seconds`: how long finished job records are kept (default 86400 = 24 hours)
- `timezone`: [IANA timezone string](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones) or `tzinfo` for display formatting (default `"UTC"`)

!!! note "Timezone is for display only"

    `timezone` is only used to format datetime values in quiv's log output.
    All internal datetime handling (scheduling, persistence, job lifecycle) uses
    UTC regardless of this setting.
- `logger`: optional custom logger instance; if not provided, a logger named `"Quiv"` is used. The library does not set a log level — configure it in your application (see [Logging](getting-started.md#logging))

!!! note "Logger scope"

    The `logger` is only used for quiv's own internal logs (scheduler loop events,
    job lifecycle, cleanup, warnings, etc.). Task handler logs are **not** routed
    through this logger — use your own loggers inside your task handlers as usual.
- `main_loop`: optional asyncio event loop for progress callbacks; if not provided, the loop is lazily resolved on first progress callback dispatch via `asyncio.get_running_loop()`. This means `Quiv()` can be instantiated at module level before any event loop is running (common in FastAPI apps). If no event loop is available when a progress callback fires, sync callbacks run directly on the worker thread and async callbacks are skipped with a warning.

### `add_task(...)`

```python
add_task(
    task_name: str,
    func: Callable[..., Any],
    interval: float,
    delay: float = 0,
    run_once: bool = False,
    args: list | None = None,
    kwargs: dict | None = None,
    progress_callback: Callable[..., Any] | None = None,
) -> str | None
```

Adds a scheduled task and returns its task ID (UUID string).

This is the primary way to register tasks. It handles handler registration,
progress callback registration, and task persistence in one call.

Validation:

- `task_name` must not be empty, has to be unique
- `interval > 0`
- `delay >= 0`

Raises `ConfigurationError` if a task with the same `task_name` is already
registered. Call [`remove_task()`](#remove_tasktask_name) first to replace it.

Behavior:

- `func` may be sync or async
- `args`/`kwargs` are JSON-serialized and persisted — only pass
  JSON-compatible values
- if `run_once=True`, task is executed once and then removed from storage
- if `progress_callback` is provided, it runs on the main loop when available,
  or directly on the worker thread otherwise

!!! info "`interval`"

    Quiv schedules next run of the task after current run has finished. 

    So, if a task is set to run with `interval=3600` (1 hour), it will wait **1 hour** between runs.

    It might not exactly run once an hour as the task itself might take some time to finish.

### `start()`

Starts scheduler background loop thread. Safe to call multiple times.

### `shutdown()`

- Stops scheduler loop and worker threads
- Cancels running jobs via stop events
- Disposes DB engine
- And removes temporary scheduler SQLite file.

Always call this during app teardown.

### `run_task_immediately(task_name)`

Queues already-scheduled task rows with this `task_name` to run now.

Raises:

- `HandlerNotRegisteredError` if no registered handler exists
- `TaskNotScheduledError` if handler exists but no scheduled task row exists

Returns number of task rows queued.

### `pause_task(task_id)` 

Pause blocks future runs of the task. 

Raises:

- `TaskNotFoundError` if task ID is missing.

### `resume_task(task_id, delay)`

Resume re-activates and sets next run with an optional `delay` (in seconds, default=0).


Raises:

- `TaskNotFoundError` if task ID is missing.

!!! info 
    
    If a `delay` is not provided or set to 0, next run will fire immediately.

### `cancel_job(job_id)`

Signals cancellation for a running job by setting its stop event.

Returns `True` if the stop event was found and set, `False` otherwise.

Cancellation is cooperative: the handler must check `_stop_event.is_set()` to
actually stop.

### `get_all_tasks(include_run_once=False)`

Returns persisted task rows.

- when `include_run_once=False`, run-once tasks are excluded
- when `include_run_once=True`, all persisted tasks are returned

### `get_all_jobs(status=None)`

Returns persisted jobs, optionally filtered by status string (e.g. `"failed"`,
`"running"`).

### `remove_task(task_name)`

Removes a scheduled task, its registered handler, and progress callback.

Raises:

- `TaskNotFoundError` if no task with that name exists.

Use this to replace a task: remove the existing one, then call `add_task()`
again with new parameters.

## Hooks and callback injection

When a task is dispatched, `quiv` inspects handler signatures:

- injects `_stop_event` (`threading.Event`) only if accepted
- injects `_progress_hook` (callable) only if accepted

If your handler does not define those parameters (and does not use `**kwargs`),
no injection is performed.

Async handlers run in thread-local event loops created per invocation. They do
not share the main application event loop.

## Models

### `Task`

Key fields:

- `id: str` — UUID identifier
- `task_name: str` — unique name mapped to a registered handler
- `args: str` — JSON-encoded positional arguments
- `kwargs: str` — JSON-encoded keyword arguments
- `interval_seconds: float` — seconds between runs
- `run_once: bool` — if `True`, task runs once then is removed
- `status: str` — `"active"`, `"running"`, or `"paused"`
- `next_run_at: datetime` — next scheduled run (UTC-aware)

!!! abstract "datetime objects are in UTC"
    The datetime values (`next_run_at`) is always returned as a UTC-aware datetime.
    
    - You can safely return this from fastapi endpoints which will have a `Z` at the end to indicate UTC datetime.
    - This is the golden-standard for Browsers as they can easily parse it and display in user's timezone.

### `Job`

Key fields:

- `id: int` — auto-incrementing identifier
- `task_id: str` — foreign key to source task
- `status: str` — lifecycle status
- `started_at: datetime` — UTC-aware start timestamp
- `ended_at: datetime | None` — UTC-aware end timestamp

!!! abstract "datetime objects are in UTC"
    The datetime values (`started_at`, `ended_at`) are always returned as UTC-aware datetimes. 
    
    - You can safely return this from fastapi endpoints which will have a `Z` at the end to indicate UTC datetime.
    - This is the golden-standard for Browsers as they can easily parse it and display in user's timezone.

## Status constants

### `TaskStatus`

- `active` — task is eligible for scheduling
- `running` — task is currently executing
- `paused` — task is temporarily disabled

### `JobStatus`

- `scheduled` — job is queued for execution
- `running` — job is currently executing
- `completed` — job finished successfully
- `cancelled` — job stopped via cancellation signal
- `failed` — job ended with an exception

## `QuivConfig`

```python
QuivConfig(
    pool_size: int = 10,
    history_retention_seconds: int = 86400,
    timezone: str | tzinfo = "UTC",
)
```

Frozen dataclass. Both `QuivConfig` and `Quiv` use `timezone` for the display
timezone parameter.

## Choosing a pool size

`pool_size` controls the maximum number of tasks that can run concurrently.
It is **not** tied to CPU cores — quiv uses threads, not processes, so the
deciding factor is your workload, not hardware.

**What to consider:**

- **How many tasks might overlap?** If you have 5 recurring tasks and at most
  3 could run at the same time, `pool_size=4` is sufficient.
- **Are tasks I/O-bound or CPU-bound?** I/O-bound tasks (API calls, database
  queries, file downloads) spend most of their time waiting, so many threads
  work fine. CPU-bound tasks contend for Python's GIL — more threads won't
  help and can hurt. For CPU-heavy work, offload to a process pool from within
  the handler rather than increasing `pool_size`.
- **Do tasks hold external resources?** Database connections, API rate limits,
  and file handles create practical caps regardless of thread count.

**Rules of thumb:**

- Start with the default (`10`) and only adjust if you see the
  `threadpool was busy` warning in your logs.
- For mostly I/O-bound workloads, set `pool_size` to 2–3x your expected max
  concurrent tasks.
- If the warning appears frequently, increase `pool_size` or check whether
  tasks are taking longer than expected.

When the pool is full, quiv defers due tasks to the next scheduler tick
rather than queuing them unboundedly. If a job starts late because all
workers were busy, a warning is logged with the delay.

## Public methods summary

- `Quiv(...)` — create scheduler instance
- `add_task(...)` — schedule a task (primary entry point)
- `start()` — start the scheduler loop
- `shutdown()` — stop scheduler and clean up resources
- `run_task_immediately(task_name)` — trigger a scheduled task now
- `pause_task(task_id)` — pause a task
- `resume_task(task_id)` — resume a paused task
- `cancel_job(job_id)` — signal cancellation for a running job
- `remove_task(task_name)` — remove a task and its registrations
- `get_all_tasks(...)` — list persisted tasks
- `get_all_jobs(...)` — list persisted jobs
