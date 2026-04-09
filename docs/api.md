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
    logger: logging.Logger | logging.LoggerAdapter | None = None,
    main_loop: asyncio.AbstractEventLoop | None = None,
)
```

If `config` is provided, do not also pass explicit `pool_size/history_retention_seconds/timezone`.

Parameters:

- `config`: grouped configuration object (see [`QuivConfig`](#quivconfig))
- `pool_size`: maximum number of tasks that can run concurrently (default 10). See [Choosing a pool size](#choosing-a-pool-size) below
- `history_retention_seconds`: how long finished job records are kept (default 86400 = 24 hours)
- `timezone`: [IANA timezone string](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones) or `tzinfo` for display formatting (default `"UTC"`)

    !!! note "Timezone is for display only"

        `timezone` is only used to format datetime values in quiv's log output.
        All internal datetime handling (scheduling, persistence, job lifecycle) uses
        UTC regardless of this setting.

- `logger`: optional custom logger or `LoggerAdapter` instance; if not provided, a logger named `"Quiv"` is used. The library does not set a log level — configure it in your application (see [Logging](getting-started.md#logging))

    !!! note "Logger scope"

        The `logger` is only used for quiv's own internal logs (scheduler loop events,
        job lifecycle, cleanup, warnings, etc.). Task handler logs are **not** routed
        through this logger — use your own loggers inside your task handlers as usual.

- `main_loop`: optional asyncio event loop for progress callbacks and event listeners. 
    
    - If not provided, resolution is attempted at `start()` time via `asyncio.get_running_loop()`, and lazily on first callback dispatch if still unset. 
    
    - This means `Quiv()` can be instantiated at module level before any event loop is running (common in FastAPI apps). 
    
    - If no event loop is available when a callback fires, async callbacks run in a temporary event loop on the worker thread, and sync callbacks run directly.

### `add_task(...)`

```python
add_task(
    task_name: str,
    func: Callable[..., Any],
    interval: float,
    delay: float = 0,
    run_once: bool = False,
    args: tuple | None = None,
    kwargs: dict | None = None,
    progress_callback: Callable[..., Any] | None = None,
) -> str
```

Adds a scheduled task and returns its task ID (UUID string)[^1].

[^1]: 
    Hold onto the returned `task_id` — it is the key for all subsequent operations:

    - `remove_task()`
    - `pause_task()`
    - `resume_task()`
    - `run_task_immediately()`
    - `get_task()`.

This is the primary way to register tasks. It handles handler registration,
progress callback registration, and task persistence in one call.

Validation:

- `task_name` must not be empty
- `interval > 0`
- `delay >= 0`

!!! note "Duplicate task names are allowed"
    Multiple tasks can share the same `task_name`. Each call to `add_task()`
    returns a unique `task_id` (UUID) which is the identifier used for all
    task operations. `task_name` is a display label, not a unique key.

Behavior:

- `func` may be sync or async
- `args`/`kwargs` are pickle-serialized and persisted — most Python objects are supported, but lambdas and inner functions are not picklable. 
  
    !!! warning
        The temporary database is trusted internal state and should not be exposed to untrusted input. Doing so might open to attackers injecting untrusted args/kwargs that gets passed to Tasks which could compromise the application.

- if `run_once=True`, task is executed once and then removed from storage
- if `progress_callback` is provided, it runs on the main loop when available,
  or directly on the worker thread otherwise

    !!! info "`interval`"
        Quiv schedules next run of the task after current run has finished. 

        So, if a task is set to run with `interval=3600` (1 hour), it will wait **1 hour** between runs.

        It might not exactly run once an hour as the task itself might take some time to finish.

### `start() -> None` / `startup() -> None`

Starts scheduler background loop thread. Safe to call multiple times.

!!! success "`startup()` is an alias for `start()`"
    use whichever reads better in your code. `startup()` pairs naturally with `shutdown()`.

### `shutdown() -> None` / `stop() -> None`

- Stops scheduler loop and worker threads
- Cancels running jobs via stop events
- Disposes DB engine
- And removes temporary scheduler SQLite file.

Always call this during app teardown.

!!! success "`stop()` is an alias for `shutdown()`"
    use whichever reads better in your code. `stop()` pairs naturally with `start()`.

### `run_task_immediately(task_id: str) -> int`

Queues an already-scheduled task to run now.

Raises:

- `HandlerNotRegisteredError` if no registered handler exists for the id
- `TaskNotScheduledError` if handler exists but no scheduled task row exists

Returns number of task rows queued.

### `pause_task(task_id: str) -> None`

Pause blocks future runs of the task.

Raises:

- `TaskNotFoundError` if no task with that id exists.

### `resume_task(task_id: str, delay: int = 0) -> None`

Resume re-activates and sets next run with an optional `delay` (in seconds, default=0).


Raises:

- `TaskNotFoundError` if no task with that id exists.

!!! info 
    
    If a `delay` is not provided or set to 0, next run will fire immediately.

### `cancel_job(job_id: str) -> bool`

Signals cancellation for a running job by setting its stop event.

Returns `True` if the stop event was found and set, `False` otherwise.

!!! info
    Cancellation is cooperative: the handler must check `_stop_event.is_set()` to actually stop.

### `get_task(task_id: str) -> Task`

Returns a single [`Task`](#task) by its UUID string.

Raises:

- `TaskNotFoundError` if no task with that ID exists.

### `get_job(job_id: str) -> Job`

Returns a single [`Job`](#job) by its UUID string.

Raises:

- `JobNotFoundError` if no job with that ID exists.

### `get_all_tasks(include_run_once: bool = False) -> list[Task]`

Returns persisted task rows as [`Task`](#task) objects.

- when `include_run_once=False`, run-once tasks are excluded
- when `include_run_once=True`, all persisted tasks are returned

### `get_all_jobs(status: str | None = None) -> list[Job]`

Returns persisted jobs, optionally filtered by status string (e.g. `"failed"`,
`"running"`).

### `remove_task(task_id: str) -> None`

Removes a scheduled task, its registered handler, and progress callback. If the
task has a running job, its stop event is set to signal cancellation.

Raises:

- `TaskNotFoundError` if no task with that id exists.

Any previously running job will finish on its own and clean up normally.

### `add_listener(event: Event, callback: Callable[..., Any]) -> None`

Register an event listener for a scheduler lifecycle event. Multiple listeners
can be registered for the same event. Both sync and async callbacks are
supported.

The callback signature depends on the event group:

- **`TASK_*` events**: `callback(event: Event, task: Task)`
- **`JOB_*` events**: `callback(event: Event, task: Task, job: Job)`

Listeners receive typed `Task` and `Job` model objects with full IDE
autocomplete — no dict key lookups needed.

Raises:

- `ConfigurationError` if `event` is not an `Event` enum member or `callback`
  is not callable.

See [Event Listeners](event-listeners.md) for the full event list and
dispatch details.

### `remove_listener(event: Event, callback: Callable[..., Any]) -> None`

Remove a previously registered event listener. If the callback is not found,
the call is silently ignored.

## Hooks and callback injection

When a task is dispatched, `quiv` inspects handler signatures:

- injects `_job_id` (`str`, UUID) only if accepted
- injects `_stop_event` (`threading.Event`) only if accepted
- injects `_progress_hook` (callable) only if accepted

If your handler does not define those parameters (and does not use `**kwargs`),
no injection is performed.

Async handlers run in thread-local event loops created per invocation. They do
not share the main application event loop.

## Models

### `Task`

Public API model returned by `get_task()` and `get_all_tasks()`.
Use directly in FastAPI endpoints — no manual conversion needed.

```python
# Methods return Task directly
task_id = scheduler.add_task("my-task", handler, interval=60)
task = scheduler.get_task(task_id)      # Task
tasks = scheduler.get_all_tasks()       # list[Task]

# Use directly in FastAPI endpoints
@app.get("/tasks")
def list_tasks():
    return scheduler.get_all_tasks()
```

Key fields:

- `id: str` — UUID identifier
- `task_name: str` — display name (not necessarily unique)
- `args: tuple[Any, ...]` — positional arguments (unpickled)
- `kwargs: dict[str, Any]` — keyword arguments (unpickled)
- `interval_seconds: float` — seconds between runs
- `run_once: bool` — if `True`, task runs once then is removed
- `status: str` — `"active"`, `"running"`, or `"paused"`
- `next_run_at: datetime` — next scheduled run (UTC-aware)

!!! abstract "datetime objects are in UTC"
    The datetime values (`next_run_at`) are always returned as a UTC-aware datetime.
    
    - You can safely return this from fastapi endpoints which will have a `Z` at the end to indicate UTC datetime.
    - This is the golden-standard for Browsers as they can easily parse it and display in user's timezone.

!!! info "Internal TaskDB model"
    Internally, quiv stores `args` and `kwargs` as pickle-encoded bytes in the
    `TaskDB` model for flexibility. The public API automatically converts to
    `Task` with unpickled values and correct types for JSON/OpenAPI.

### `Job`

Key fields:

- `id: str` — UUID identifier
- `task_id: str` — foreign key to source task
- `task_name: str` — name of the task that spawned this job
- `status: str` — lifecycle status
- `started_at: datetime` — UTC-aware start timestamp
- `ended_at: datetime | None` — UTC-aware end timestamp
- `duration_seconds: float | None` — job duration in seconds (set on completion)
- `error_message: str | None` — error description if job failed

!!! abstract "datetime objects are in UTC"
    The datetime values (`started_at`, `ended_at`) are always returned as UTC-aware datetimes. 
    
    - You can safely return this from fastapi endpoints which will have a `Z` at the end to indicate UTC datetime.
    - This is the golden-standard for Browsers as they can easily parse it and display in user's timezone.

## Event types

### `Event`

- `task_added` — fired after a task is registered
- `task_removed` — fired after a task is removed
- `task_paused` — fired after a task is paused
- `task_resumed` — fired after a task is resumed
- `job_started` — fired when a job begins execution
- `job_completed` — fired when a job finishes successfully
- `job_failed` — fired when a job ends with an exception
- `job_cancelled` — fired when a job is cancelled

See [Event Listeners](event-listeners.md) for the data each event carries.

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
- `add_task(...)` — schedule a task, returns `task_id`
- `start()` / `startup()` — start the scheduler loop
- `shutdown()` / `stop()` — stop scheduler and clean up resources
- `run_task_immediately(task_id)` — trigger a scheduled task now
- `pause_task(task_id)` — pause a task
- `resume_task(task_id)` — resume a paused task
- `cancel_job(job_id)` — signal cancellation for a running job
- `remove_task(task_id)` — remove a task and its registrations
- `add_listener(event, callback)` — register an event listener
- `remove_listener(event, callback)` — remove an event listener
- `get_task(task_id)` — get a single task by UUID
- `get_job(job_id)` — get a single job by ID
- `get_all_tasks(...)` — list persisted tasks
- `get_all_jobs(...)` — list persisted jobs
