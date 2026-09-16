# Exceptions

Every exception that quiv raises inherits from `QuivError`.

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

quiv raises this when a configuration value or a scheduling value is invalid. The causes are:

- `pool_size` is 0 or less.
- `history_retention_seconds` is less than 0.
- An input to `add_task()` is invalid, such as `task_name`, `interval`, or `delay`.
- The call passes `config=` together with a separate configuration argument.
- The handler declares a parameter that quiv renamed in v1.0.0: `_job_id`, `_stop_event`, or `_progress_hook`. Rename it to `job_id`, `stop_event`, or `progress_hook`.
- A key in `kwargs` has the same name as a parameter that quiv injects into the handler. The injected value would overwrite yours.

### `InvalidTimezoneError`

quiv raises this when a timezone value is not a valid IANA name, or when its type is neither `str` nor `tzinfo`.

### `DatabaseInitializationError`

quiv raises this when it cannot set up SQLite and SQLModel while it creates the scheduler.

### `HandlerRegistrationError`

quiv raises this when a handler or a progress callback is invalid. A task id that is empty, a handler that is not callable, and a progress callback that is not callable all reach this error.

### `HandlerNotRegisteredError`

quiv raises this when an operation needs a registered handler and the task id has none[^1].

[^1]: The task exists, but no handler is registered for it. `add_task()` registers a handler with every task, and removing a task drops both, so this error usually means that something changed the `registry` dictionary directly. An unknown `task_id` raises `TaskNotFoundError` instead, and that includes the id of a removed task and of a run-once task that already ran.

### `TaskNotActiveError`

quiv raises this when an operation needs an `active` task. `run_task_immediately()` is the only method that raises it, in two cases:

- The task is `running`. A second run at the same time would break the guarantee that one task never overlaps itself.
- The task is `paused`. To continue a paused task, call `resume_task()`.

### `TaskNotFoundError`

quiv raises this when a task id is unknown. Every method that takes a `task_id` can raise it: `get_task()`, `update_task()`, `remove_task()`, `pause_task()`, `resume_task()`, and `run_task_immediately()`. quiv deletes a run-once task when it finishes, so its id stops resolving after the task runs.

### `JobNotFoundError`

quiv raises this when it looks up a job id and finds no row. This happens while it marks a job as running, and while it finalizes a job.

### `MainLoopUnavailableError`

quiv raises this from `run_on_main()` when it cannot reach a main event loop. There are two causes: no active Quiv instance is registered, or the active Quiv has no main loop that it can resolve. Pass `main_loop=` to `Quiv()`, or call `start()` from the thread that runs the main loop.

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

At the edge of your application, catch `QuivError`. It covers every failure that quiv reports. Catch one subclass as well when you need to recover from that cause alone.
