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
		- `TaskNotScheduledError` (deprecated alias)
	- `JobNotFoundError`

## Exception reference

### `ConfigurationError`

Raised when runtime or scheduling configuration is invalid, for example:

- `pool_size <= 0`
- `history_retention_seconds < 0`
- invalid `add_task(...)` inputs (`task_name`, `interval`, `delay`)
- mixing `config=...` with direct constructor config args

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

### `TaskNotScheduledError`

Deprecated alias of `TaskNotFoundError`, removed in 1.0.0. quiv no longer raises it. The name stays exported so that imports in 0.x code keep working. Catch `TaskNotFoundError` instead — an `except TaskNotScheduledError` clause no longer catches these errors.

### `JobNotFoundError`

Raised when a job ID lookup fails in persistence operations (mark running/finalize).

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
