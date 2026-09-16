# Exceptions

All custom exceptions inherit from `QuivError`.

## Hierarchy

- `QuivError`
	- `ConfigurationError`
		- `InvalidTimezoneError`
	- `DatabaseInitializationError`
	- `HandlerRegistrationError`
	- `HandlerNotRegisteredError`
	- `TaskNotActiveError`
	- `TaskNotFoundError`
	- `JobNotFoundError`
	- `MainLoopUnavailableError` (also inherits `RuntimeError`)

## Exception reference

### `ConfigurationError`

Raised when runtime or scheduling configuration is invalid, for example:

- `pool_size <= 0`
- `history_retention_seconds < 0`
- invalid `add_task(...)` inputs (`task_name`, `interval`, `delay`)
- mixing `config=...` with direct constructor config args
- a handler that declares a pre-1.0 injected parameter (`_job_id`, `_stop_event`, `_progress_hook`) — rename it to `job_id`, `stop_event`, or `progress_hook`
- a key in `kwargs` that collides with a parameter quiv injects into the handler

### `InvalidTimezoneError`

Raised when timezone input is not a valid IANA timezone or not a `str/tzinfo`.

### `DatabaseInitializationError`

Raised when SQLite/SQLModel initialization fails during scheduler creation.

### `HandlerRegistrationError`

Raised when registering invalid handlers/callbacks (empty task id, non-callable handler/callback).

### `HandlerNotRegisteredError`

Raised when an operation requires a registered handler but none exists for the given task id[^1].

[^1]: The task exists, but no handler is registered for it. `add_task()` registers a handler with every task, and removal drops both, so this normally means the `registry` dict was changed directly. An unknown `task_id` — including one whose task was removed, or a run-once task that already fired — raises `TaskNotFoundError` instead.

### `TaskNotActiveError`

Raised when an operation requires an `active` task. Currently raised by `run_task_immediately()` when the task is `running` (a second concurrent run would break the no-overlap guarantee) or `paused` (un-pausing must be an explicit `resume_task()` call).

### `TaskNotFoundError`

Raised when a task id is unknown. Every method that takes a `task_id` raises it: `get_task()`, `update_task()`, `remove_task()`, `pause_task()`, `resume_task()` and `run_task_immediately()`. A run-once task deletes itself when it finishes, so its id stops resolving after it runs.

### `JobNotFoundError`

Raised when a job ID lookup fails in persistence operations (mark running/finalize).

### `MainLoopUnavailableError`

Raised by `run_on_main()` when it cannot reach a main event loop. There are two causes: no active Quiv instance is registered, or the active Quiv has no resolvable main loop. Pass `main_loop=` to `Quiv()`, or call `start()` from the thread that runs the main loop.

This exception inherits `RuntimeError` as well as `QuivError`. quiv raised a bare `RuntimeError` here before v1.0.0, so an existing `except RuntimeError` clause keeps working. `except QuivError` now catches it too.

## Handling pattern

```python
from quiv import Quiv
from quiv.exceptions import QuivError, ConfigurationError

try:
    scheduler = Quiv(pool_size=4, timezone="UTC")
except ConfigurationError as exc:
    print("bad config", exc)
except QuivError as exc:
    print("scheduler init failed", exc)
```

For application boundaries, catch `QuivError` to cover all library-specific failures, and optionally catch specific subclasses when you need targeted recovery.
