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

If you pass `config`, do not also pass `pool_size`, `history_retention_seconds`, or `timezone`.

Parameters:

- `config`: grouped configuration object (see [`QuivConfig`](#quivconfig))
- `pool_size`: the largest number of tasks that can run at the same time. The default is 10. See [Choosing a pool size](#choosing-a-pool-size) below
- `history_retention_seconds`: how long quiv keeps the record of a finished job. The default is 86400 seconds, which is 24 hours
- `timezone`: [IANA timezone string](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones) or `tzinfo` for display formatting (default `"UTC"`)

    !!! note "Timezone is for display only"

        quiv uses `timezone` only to format the datetime values that it writes to the log. Everything inside quiv uses UTC, whatever you set here. That covers the schedule, the stored rows, and the life of each job.

- `logger`: optional custom logger or `LoggerAdapter` instance; if not provided, a logger named `"Quiv"` is used. The library does not set a log level — configure it in your application (see [Logging](getting-started.md#logging))

    !!! note "Logger scope"

        quiv uses `logger` only for its own messages, such as the events of the scheduler loop, the life of a job, the cleanup, and the warnings. quiv does **not** send the output of your handlers through it. Use your own logger inside a handler, as you would anywhere else.

- `main_loop`: optional asyncio event loop for progress callbacks and event listeners. 
    
    - If you pass nothing, quiv tries `asyncio.get_running_loop()` when you call `start()`. If it still has no loop, it tries again on the first callback.
    
    - You can therefore create `Quiv()` at module level, before any event loop runs. A FastAPI application usually does this.
    
    - If no event loop is available when a callback fires, an async callback runs in a temporary event loop on the worker thread, and a sync callback runs on that thread directly.

### `add_task(...)`

```python
add_task(
    task_name: str,
    func: Callable[..., Any],
    interval: float | None = None,
    delay: float | None = None,
    run_once: bool = False,
    fixed_interval: bool = True,
    args: tuple | None = None,
    kwargs: dict | None = None,
    progress_callback: Callable[..., Any] | None = None,
    *,
    run_at: datetime | None = None,
    timeout: float | None = None,
    max_retries: int = 0,
    retry_backoff: float = 30.0,
    jitter: float = 0.0,
) -> str
```

The older parameters keep the positions that they had in earlier releases. The four failure-handling options are keyword-only: `timeout`, `max_retries`, `retry_backoff`, and `jitter`.

Adds a scheduled task and returns its task id, a UUID string[^1].

[^1]: Keep the `task_id` that `add_task()` returns. Every later operation uses it:

    - `remove_task()`
    - `pause_task()`
    - `resume_task()`
    - `run_task_immediately()`
    - `get_task()`
    - `update_task()`.

This is the main way to register a task. One call registers the handler, registers the progress callback, and stores the task.

Validation:

- `task_name` must not be empty
- `interval > 0` — sub-second intervals (e.g. `interval=0.2`) are supported. Required unless `run_once=True`
- `delay >= 0`, and not combined with `run_at`
- `run_at` must be a `datetime`
- `timeout > 0` when provided
- `max_retries >= 0`
- `retry_backoff > 0`
- `jitter >= 0`

Failure handling: `timeout` asks a job that runs too long to stop, `max_retries` with `retry_backoff` runs a failed job again after a delay that doubles each time, and `jitter` moves apart the tasks that share an interval. Read [Failure Handling](failure-handling.md) for the rules.

!!! note "Duplicate task names are allowed"
    Several tasks can share one `task_name`. Each call to `add_task()` returns its own `task_id`, a UUID, and every operation on a task uses that id. `task_name` is a label for a reader, not a key.

Behavior:

- `func` may be sync or async
- a run-once task never repeats, so it needs no `interval`. An interval passed with `run_once=True` is ignored, and `Task.interval_seconds` reads `None`
- `run_at` schedules the first run at an absolute time, as an alternative to `delay`. A naive `datetime` is read as UTC — the `timezone` setting only formats log output. A time already past runs at once, because the process may have been down when the time came due. Passing both `run_at` and `delay` raises `ConfigurationError`
- quiv serializes `args` and `kwargs` with pickle and stores them. Pickle accepts most Python objects, but it cannot accept a lambda or an inner function.
  
    !!! warning
        The temporary database holds trusted internal state. Never let untrusted input reach it. An attacker who can write to it can put their own `args` and `kwargs` into a task, and your application will then run them.

- With `run_once=True`, the task runs one time, and quiv then deletes it
- If you pass a `progress_callback`, it runs on the main loop when one is available, and on the worker thread when none is

    !!! info "`fixed_interval` scheduling modes"

        **`fixed_interval=True`** (default) — Next run is scheduled at fixed intervals from the job **start time**. A task with `interval=3600` runs every 3600 seconds relative to that start-time anchor (for example, if a run starts at 12:00:10, subsequent targets are 13:00:10, 14:00:10, etc.), regardless of how long the task takes. If a run exceeds the interval, missed intervals are skipped and the next run lands on the next scheduled time in that cadence.

        **`fixed_interval=False`** — Next run is scheduled `interval` seconds after job **completion**. A task with `interval=3600` that takes 10 minutes to run will have 70-minute gaps between start times.

### `update_task(...)`

```python
update_task(
    task_id: str,
    *,
    task_name: str = ...,
    interval: float = ...,
    fixed_interval: bool = ...,
    args: tuple = ...,
    kwargs: dict = ...,
    timeout: float | None = ...,
    max_retries: int = ...,
    retry_backoff: float = ...,
    jitter: float = ...,
    progress_callback: Callable[..., Any] | None = ...,
    run_at: datetime = ...,
) -> Task
```

Changes a scheduled task in place and keeps its `task_id`. Only the parameters that you pass change, and quiv leaves every other value as it is. If you change `interval`, quiv moves the next run to `now + interval`. You can update a `running` task: the changes take effect from its next run. `update_task()` emits `Event.TASK_UPDATED`, carrying the `Task` as it is after the change.

`None` carries meaning here: `timeout=None` turns the timeout off, and `progress_callback=None` removes the callback. quiv therefore marks an argument that you did not pass with an internal sentinel. To leave a value alone, omit it.

Three things cannot change: `run_once`, `delay`, and the handler `func`. `delay` belongs to the moment when you create the task. To change the handler, remove the task and add it again.

#### Moving the next run with `run_at`

`run_at` names the absolute time of the next run, the same way it does in `add_task()`. Naive input is read as UTC, never as the display timezone, and a time already past runs at once.

This is how a one-off alarm changes its time:

```python
task_id = scheduler.add_task(
    task_name="wakeup", func=wake, run_at=tonight, run_once=True
)

# Later: the alarm should ring earlier instead.
scheduler.update_task(task_id, run_at=this_afternoon)
```

Without it, moving an alarm means `remove_task()` followed by `add_task()`. That hands back a new `task_id` for the caller to store, and it leaves a window with no task scheduled at all. `update_task()` keeps the id and writes one row.

`run_at` and `interval` are mutually exclusive. Changing the interval already reschedules the next run, so passing both is ambiguous and raises `ConfigurationError`.

A run-once task that has already started is deleted when it finishes, so a new time has nothing left to apply to.

Raises:

- `TaskNotFoundError` for unknown ids
- `ConfigurationError` for invalid values (same rules as `add_task`), or for `run_at` together with `interval`

### `start() -> None` / `startup() -> None`

Starts the background thread that runs the scheduler loop. You can call it more than once without harm.

!!! success "`startup()` is an alias for `start()`"
    `start()` is the canonical name, and this documentation uses it everywhere. `startup()` calls the same code and keeps working.

### `shutdown(timeout: float | None = None) -> None` / `stop(...) -> None`

`shutdown()` does four things:

- It stops the scheduler loop and the worker threads.
- It cancels each running job, by setting its stop event.
- It disposes the database engine.
- It deletes the temporary SQLite file of the scheduler.

Always call it when your application stops.

With `timeout=None`, the default, `shutdown()` waits for every running job to finish, however long that takes. Pass a `timeout` in seconds to limit the wait. quiv leaves a job that does not exit before the deadline on its worker thread, and writes a warning. Use a timeout at the end of a FastAPI lifespan, where one stuck handler must not hold up the whole application.

A job left behind still holds its database connection. When it finishes, its write reaches the database that quiv already deleted. Two things follow: the job can write errors to the log, and SQLite recreates the temporary database file, because a write creates the file again. The file is small and nothing reads it. Delete it yourself if a stray file in the temp directory matters to you.

!!! success "`stop()` is an alias for `shutdown()`"
    `shutdown()` is the canonical name, and this documentation uses it everywhere. `stop()` calls the same code and keeps working.

### `run_task_immediately(task_id: str) -> int`

Queues an already-scheduled task to run now.

Raises:

- `TaskNotFoundError` if no task with that id exists — including a run-once task that already fired and removed itself
- `HandlerNotRegisteredError` if the task exists but no handler is registered for it
- `TaskNotActiveError` if the task is `running` (no concurrent second run) or `paused` (use `resume_task()` instead)

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

Asks a running job to stop, by setting its stop event.

Returns `True` when it finds the stop event and sets it. Returns `False` when it finds no such job.

!!! info
    Cancellation is cooperative. The handler must check `stop_event.is_set()` and return.

### `get_task(task_id: str) -> Task`

Returns a single [`Task`](#task) by its UUID string.

Raises:

- `TaskNotFoundError` if no task with that ID exists.

### `get_job(job_id: str) -> Job`

Returns a single [`Job`](#job) by its UUID string.

Raises:

- `JobNotFoundError` if no job with that ID exists.

### `get_all_tasks(include_run_once: bool = False, status: str | None = None, limit: int | None = None, offset: int = 0) -> list[Task]`

Returns persisted task rows as [`Task`](#task) objects, ordered by `next_run_at` ascending.

- when `include_run_once=False`, run-once tasks are excluded
- when `include_run_once=True`, all persisted tasks are returned
- `status` filters by task status (e.g. `"paused"`); `limit`/`offset` paginate

### `get_all_jobs(status=None, task_id=None, since=None, until=None, order_by="started_at", descending=True, limit=None, offset=0) -> list[Job]`

Returns persisted jobs with optional filters and pagination — see [Observability](observability.md) for details. `status` filters by status string (e.g. `"failed"`, `"running"`); `task_id` restricts to one task; `since`/`until` bound `started_at` (pass aware UTC datetimes); `order_by` accepts `"started_at"` or `"ended_at"` (anything else raises `ConfigurationError`).

### `stats() -> QuivStats`

Returns a snapshot of the scheduler at one moment: `active_jobs`, `pool_size`, `pool_utilization`, `tasks_by_status`, `next_run_at`, and `job_history_count`. `QuivStats` is a frozen dataclass, exported from `quiv` — serialize with `dataclasses.asdict()`. See [Observability](observability.md).

### `remove_task(task_id: str) -> None`

Removes a scheduled task, the handler registered for it, and its progress callback. If a job of that task is running, quiv sets its stop event to ask it to stop.

Raises:

- `TaskNotFoundError` if no task with that id exists.

A job that was already running finishes on its own, and quiv cleans up after it as usual.

### `add_listener(event: Event, callback: Callable[..., Any]) -> None`

Registers a listener for one event of the scheduler. One event can have several listeners. A callback can be sync or async.

The callback signature depends on the event group:

- **`TASK_*` events**: `callback(event: Event, task: Task)`
- **`JOB_*` events**: `callback(event: Event, task: Task, job: Job)`

A listener receives typed `Task` and `Job` model objects, so your editor completes every field and you never look up a key in a dictionary.

Raises:

- `ConfigurationError` if `event` is not an `Event` enum member or `callback` is not callable.

See [Event Listeners](event-listeners.md) for the full event list and dispatch details.

### `remove_listener(event: Event, callback: Callable[..., Any]) -> None`

Removes a listener that you registered before. If quiv does not find the callback, the call does nothing and raises nothing.

## `run_on_main`

```python
from quiv import run_on_main

run_on_main(func: Callable[..., Any], *args: Any, **kwargs: Any) -> None
```

A module-level helper that sends `func` to the main event loop of the active Quiv instance. Call it from anywhere inside the call stack of a task handler, without passing a callback parameter through the functions in between. Call it also from code that already runs on the main loop, such as a FastAPI route handler that shares a utility with your task code.

Resolution order for the active instance:

1. A `ContextVar` set by `_run_job` for the duration of a handler invocation (propagates through nested sync calls, the per-job async loop, and `asyncio.create_task`).
2. A process-level fallback registered by `Quiv.start()` and cleared by `Quiv.shutdown()`.

Behavior:

- Fire-and-forget; returns `None` immediately on cross-thread dispatch.
- Sync targets run inline when called from the main loop's thread, else via `call_soon_threadsafe`. Async targets are scheduled via `main_loop.create_task` on-loop, else `run_coroutine_threadsafe`.
- If `func` raises, quiv writes the error to the logger of the active instance and continues.

Raises:

- `MainLoopUnavailableError` if no active Quiv instance is registered, or if the active instance has no main event loop that it can resolve. It inherits `QuivError` and `RuntimeError`.

See [Running on the main event loop](run-on-main.md) for the full walkthrough, dispatch table, and caveats.

## Hooks and callback injection

When quiv dispatches a task, it reads the signature of the handler:

- injects `job_id` (`str`, UUID) only if accepted
- injects `stop_event` (`threading.Event`) only if accepted
- injects `progress_hook` (callable) only if accepted

If your handler declares none of those parameters, and declares no `**kwargs`, quiv injects nothing.

!!! warning "Renamed in v1.0.0"

    quiv `0.x` injected these as `_job_id`, `_stop_event`, and `_progress_hook`. `add_task()` rejects a handler that still declares an old name, and raises `ConfigurationError` naming the new spelling. A key in `kwargs` that collides with an injected name is rejected the same way, because the injected value would overwrite yours. See the [v1.0.0 release notes](release-notes.md#v1.0.0).

An async handler runs in an event loop that quiv creates on the worker thread, one for each invocation. It never shares the main event loop of your application.

## Models

### `Task`

The public model that `get_task()` and `get_all_tasks()` return. Return it straight from a FastAPI endpoint. You convert nothing by hand.

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
- `interval_seconds: float | None` — seconds between runs; `None` for a run-once task
- `fixed_interval: bool` — if `True`, next run is measured from job start time; if `False`, from completion
- `run_once: bool` — if `True`, task runs once then is removed
- `status: str` — `"active"`, `"running"`, or `"paused"`
- `next_run_at: datetime` — next scheduled run (UTC-aware)
- `timeout_seconds: float | None` — cooperative per-job timeout; `None` when disabled
- `max_retries: int` — retry limit for failed jobs
- `retry_backoff_seconds: float` — base delay for exponential retry backoff
- `retry_attempt: int` — consecutive-failure counter (0 unless mid-retry)
- `jitter_seconds: float` — random offset bound added to recurring next-run times

!!! abstract "datetime objects are in UTC"
    The datetime values (`next_run_at`) are always returned as a UTC-aware datetime.
    
    - You can safely return this from fastapi endpoints which will have a `Z` at the end to indicate UTC datetime.
    - This is the golden-standard for Browsers as they can easily parse it and display in user's timezone.

!!! info "Internal TaskDB model"
    Internally, quiv stores `args` and `kwargs` as pickle-encoded bytes in the `TaskDB` model for flexibility. The public API automatically converts to `Task` with unpickled values and correct types for JSON/OpenAPI.

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
- `attempt: int` — attempt number; `1` = first try, `2` = first retry, and so on

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
- `task_updated` — fired after a task is mutated via `update_task()`
- `job_started` — fired when a job begins execution
- `job_completed` — fired when a job finishes successfully
- `job_failed` — fired when a job ends with an exception
- `job_cancelled` — fired when a job is cancelled
- `job_retrying` — fired after `job_failed` when a retry has been scheduled

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

Frozen dataclass. Both `QuivConfig` and `Quiv` use `timezone` for the display timezone parameter.

## Choosing a pool size

`pool_size` controls the maximum number of tasks that can run concurrently. It is **not** tied to CPU cores — quiv uses threads, not processes, so the deciding factor is your workload, not hardware.

**What to consider:**

- **How many tasks might overlap?** If you have 5 recurring tasks and at most 3 could run at the same time, `pool_size=4` is sufficient.
- **Are tasks I/O-bound or CPU-bound?** I/O-bound tasks (API calls, database queries, file downloads) spend most of their time waiting, so many threads work fine. CPU-bound tasks contend for Python's GIL — more threads won't help and can hurt. For CPU-heavy work, offload to a process pool from within the handler rather than increasing `pool_size`.
- **Do tasks hold external resources?** Database connections, API rate limits, and file handles create practical caps regardless of thread count.

**Rules of thumb:**

- Start with the default (`10`) and only adjust if you see the `threadpool was busy` warning in your logs.
- For mostly I/O-bound workloads, set `pool_size` to 2–3x your expected max concurrent tasks.
- If the warning appears frequently, increase `pool_size` or check whether tasks are taking longer than expected.

When the pool is full, quiv defers due tasks rather than queuing them unboundedly; a finishing job wakes the scheduler loop, so deferred tasks dispatch as soon as a slot frees. If a job starts late because all workers were busy, a warning is logged with the delay.

## Public methods summary

- `Quiv(...)` — create scheduler instance
- `run_on_main(func, *args, **kwargs)` — fire-and-forget dispatch onto the active Quiv's main loop
- `add_task(...)` — schedule a task, returns `task_id`
- `start()` / `startup()` — start the scheduler loop
- `shutdown(timeout=None)` / `stop(timeout=None)` — stop scheduler and clean up resources
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
