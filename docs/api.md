# API

## `Quiv`

### Constructor

```python
Quiv(
    config: QuivConfig | None = None,
    pool_size: int = 10,
    history_retention_seconds: int = 86400,
    timezone_name: str | tzinfo = "UTC",
    *,
    logger: logging.Logger | None = None,
    main_loop: asyncio.AbstractEventLoop | None = None,
)
```

If `config` is provided, do not also pass explicit
`pool_size/history_retention_seconds/timezone_name`.

Parameters:

- `config`: grouped configuration object (see [`QuivConfig`](#quivconfig))
- `pool_size`: maximum worker threads (default 10)
- `history_retention_seconds`: how long finished job records are kept (default 86400 = 24 hours)
- `timezone_name`: [IANA timezone string](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones) or `tzinfo` for display formatting (default `"UTC"`)
- `logger`: optional custom logger instance; if not provided, a logger named `"Quiv"` is created with DEBUG level (see [Logging](getting-started.md#logging))
- `main_loop`: optional asyncio event loop for progress callbacks; defaults to `asyncio.get_event_loop()`

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

Behavior:

- `func` may be sync or async
- `args`/`kwargs` are JSON-serialized and persisted — only pass
  JSON-compatible values
- if `run_once=True`, task is executed once and then removed from storage
- if `progress_callback` is provided, it runs on the main loop
- calling `add_task()` with an existing `task_name` upserts the task row

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

## Advanced: manual handler registration

`add_task()` is the primary way to register handlers. If you need to register
a handler or progress callback separately (for example, to swap a handler at
runtime without re-adding the task), you can use these methods directly:

### `register_handler(name, func)`

Registers a callable as the handler for `name`. Raises
`HandlerRegistrationError` if the name is empty or `func` is not callable.

### `register_progress_callback(name, callback)`

Registers or clears a progress callback for `name`. Pass `None` to clear an
existing callback. Raises `HandlerRegistrationError` if `callback` is not
callable.

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
- `status: str` — `"active"` or `"paused"`
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
    The datetime values (`started_at`, `endd_at`) is always returned as a UTC-aware datetime. 
    
    - You can safely return this from fastapi endpoints which will have a `Z` at the end to indicate UTC datetime.
    - This is the golden-standard for Browsers as they can easily parse it and display in user's timezone.

## Status constants

### `TaskStatus`

- `active` — task is eligible for scheduling
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

Frozen dataclass. Note that `QuivConfig` uses `timezone` while the `Quiv`
constructor uses `timezone_name`.

## Public methods summary

- `Quiv(...)` — create scheduler instance
- `add_task(...)` — schedule a task (primary entry point)
- `start()` — start the scheduler loop
- `shutdown()` — stop scheduler and clean up resources
- `run_task_immediately(task_name)` — trigger a scheduled task now
- `pause_task(task_id)` — pause a task
- `resume_task(task_id)` — resume a paused task
- `cancel_job(job_id)` — signal cancellation for a running job
- `get_all_tasks(...)` — list persisted tasks
- `get_all_jobs(...)` — list persisted jobs
- `register_handler(name, func)` — manually register a handler (advanced)
- `register_progress_callback(name, callback)` — manually register/clear a
  progress callback (advanced)
