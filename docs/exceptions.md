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

Raised when registering invalid handlers/callbacks (empty task name,
non-callable handler/callback).

### `HandlerNotRegisteredError`

Raised when an operation requires a registered handler but none exists
for the given task name.

### `TaskNotScheduledError`

Raised when a task handler exists but there are no scheduled task rows,
for example calling `run_task_immediately(...)` before `add_task(...)`.

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
    scheduler = Quiv(pool_size=4, timezone_name="UTC")
except ConfigurationError as exc:
    print("bad config", exc)
except QuivError as exc:
    print("scheduler init failed", exc)
```

For application boundaries, catch `QuivError` to cover all library-specific
failures, and optionally catch specific subclasses when you need targeted
recovery.
