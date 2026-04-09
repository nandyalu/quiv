# Exceptions

All custom exceptions inherit from `QuivError`.

## Hierarchy

- `QuivError`
	- `ConfigurationError`
		- `InvalidTimezoneError`
	- `DatabaseInitializationError`
	- `HandlerRegistrationError`
	- `HandlerNotRegisteredError`
	- `TaskNotScheduledError`
	- `TaskNotFoundError`
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

Raised when registering invalid handlers/callbacks (empty task id,
non-callable handler/callback).

### `HandlerNotRegisteredError`

Raised when an operation requires a registered handler but none exists
for the given task id[^1].

[^1]: This typically means `run_task_immediately()` was called with a
    `task_id` that was never returned by `add_task()`, or the task was
    already removed.

### `TaskNotScheduledError`

Raised when a task handler is registered but the scheduled task row no longer
exists in the database, for example if it was deleted externally.

### `TaskNotFoundError`

Raised when a task ID lookup fails in persistence operations
(pause/resume/finalize).

### `JobNotFoundError`

Raised when a job ID lookup fails in persistence operations
(mark running/finalize).

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

For application boundaries, catch `QuivError` to cover all library-specific
failures, and optionally catch specific subclasses when you need targeted
recovery.
