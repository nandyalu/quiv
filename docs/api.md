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

Adds a scheduled task and returns its task ID.

Validation:

- `task_name` must not be empty
- `interval > 0`
- `delay >= 0`

Behavior:

- `func` may be sync or async
- args/kwargs are JSON-serialized and persisted
- if `run_once=True`, task is executed once and then removed
- if `progress_callback` is provided, it runs on the main loop

### `start()`

Starts scheduler background loop thread. Safe to call multiple times.

### `shutdown()`

Stops scheduler loop and worker threads, cancels running jobs via stop events,
disposes DB engine, and removes temporary scheduler SQLite file.

### `run_task_immediately(task_name)`

Queues already-scheduled task rows with this `task_name` to run now.

Raises:

- `HandlerNotRegisteredError` if no registered handler exists
- `TaskNotScheduledError` if handler exists but no scheduled task row exists

Returns number of task rows queued.

### `pause_task(task_id)` / `resume_task(task_id)`

Pause blocks due-task dispatch. Resume re-activates and sets next run to now.

Raises `TaskNotFoundError` if task ID is missing.

### `get_all_tasks(include_run_once=False)`

Returns persisted task rows.

- when `include_run_once=False`, run-once tasks are excluded
- when `include_run_once=True`, all persisted tasks are returned

### `get_all_jobs(status=None)`

Returns persisted jobs, optionally filtered by status.

## Hooks and callback injection

When a task is dispatched, `quiv` inspects handler signatures:

- injects `_stop_event` only if accepted
- injects `_progress_hook` only if accepted

If your handler does not define those parameters (or `**kwargs`), no injection
is performed.

## Models

### `Task`

Key fields:

- `id: str`
- `task_name: str`
- `args: str` (JSON)
- `kwargs: str` (JSON)
- `interval_seconds: float`
- `run_once: bool`
- `status: str` (`active` or `paused`)
- `next_run_at: datetime` (UTC-aware)

### `Job`

Key fields:

- `id: int`
- `task_id: str`
- `status: str`
- `started_at: datetime` (UTC-aware)
- `ended_at: datetime | None` (UTC-aware)

## Status constants

### `TaskStatus`

- `active`
- `paused`

### `JobStatus`

- `scheduled`
- `running`
- `completed`
- `cancelled`
- `failed`

## `QuivConfig`

```python
QuivConfig(
	pool_size: int = 10,
	history_retention_seconds: int = 86400,
	timezone: str | tzinfo = "UTC",
)
```

## Public methods summary

- `Quiv(...)`
- `add_task(...)`
- `start()`
- `shutdown()`
- `run_task_immediately(task_name)`
- `pause_task(task_id)`
- `resume_task(task_id)`
- `get_all_tasks(...)`
- `get_all_jobs(...)`
