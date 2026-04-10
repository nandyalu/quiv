# Testing

quiv is extensively tested with **108 tests** covering the full lifecycle of
tasks, jobs, event listeners, progress callbacks, configuration, models, and
edge cases. Tests run on every commit via CI across Python 3.10 through 3.14.

```bash
# Run all tests
uv run pytest

# Run with coverage
uv run pytest --cov=quiv

# Run a specific test file
uv run pytest tests/test_scheduler.py

# Run a single test
uv run pytest tests/test_scheduler.py::test_backpressure_skips_dispatch_when_pool_full
```

## Test architecture

Most tests require a running asyncio event loop for callback dispatch. The
`running_main_loop` fixture (in `conftest.py`) spins up an event loop in a
background thread and yields it to each test. Every test calls
`scheduler.shutdown()` in a `finally` block to clean up threads and temp DB
files.

## What is tested

### Scheduler lifecycle and configuration

- Mixing `config=QuivConfig(...)` with explicit kwargs raises
  `ConfigurationError`
- `pool_size <= 0` and `history_retention_seconds < 0` are rejected
- `start()` is idempotent (safe to call multiple times)
- `shutdown()` handles DB cleanup failures gracefully
- Database initialization failure raises `DatabaseInitializationError`
- quiv's internal tables (`quiv_task`, `quiv_job`) do not leak into user
  SQLModel metadata

### Task input validation

- Empty `task_name` is rejected
- `interval <= 0` and `delay < 0` are rejected
- `args` must be a `tuple` (not list)
- `kwargs` must be a `dict` (not string)
- Unpicklable args (e.g. lambdas) raise `ConfigurationError` with a clear
  message

### Task registration and identification

- `add_task()` returns a unique `task_id` (UUID)
- Duplicate `task_name` values are allowed, each getting a distinct `task_id`
- Handlers, progress callbacks, and DB rows are all keyed by `task_id`
- `remove_task()` cleans up handler, progress callback, and DB row
- Removing a non-existent task raises `TaskNotFoundError`
- Deleting a non-existent task from persistence raises `TaskNotFoundError`

### Task execution

- Sync run-once task executes and produces a `completed` job
- Async run-once task executes via thread-local event loop
- Sync handler without `_stop_event` or `_progress_hook` still runs correctly
- Failed handler sets job status to `failed`
- `_job_id` is injected as a UUID string when handler accepts it
- `args` and `kwargs` ordering is preserved through pickle round-trip (tested
  with 8 positional args and 5 keyword args)

### Concurrent execution and backpressure

- Same task is never dispatched concurrently (status set to `running` blocks
  re-dispatch)
- When the thread pool is full, due tasks are deferred to the next tick
  instead of queued unboundedly
- Deferred tasks execute once a worker becomes available
- `_active_job_count` decrements correctly after job completion
- Late-starting jobs (due to pool saturation) log a warning with the delay

### Interval scheduling (`fixed_interval`)

- **Fixed interval** (`fixed_interval=True`): next run is aligned to
  `start_time + interval`
- **Skipped intervals**: a 70-second job with 60-second interval skips to
  `start_time + 120s`; a 130-second job skips to `start_time + 180s`
- **Wait between runs** (`fixed_interval=False`): next run is
  `completion_time + interval`
- Recurring task finalization sets status back to `active` and updates
  `next_run_at`
- Run-once task finalization deletes the task row

### Cancellation

- `cancel_job()` returns `True` when stop event exists, `False` otherwise
- Handler that sets `_stop_event` results in `cancelled` status
- `remove_task()` on a running task cancels its active job
- `shutdown()` cancels all tracked running jobs

### Progress callbacks

- Async progress callback dispatched on main event loop via handler's
  `_progress_hook`
- Sync progress callback dispatched on main event loop via
  `call_soon_threadsafe`
- Async handler with sync progress callback works correctly
- Progress callback registration and clearing via `None`
- Sync callback works without an event loop (runs on worker thread)
- Async callback works without an event loop (runs in temporary event loop)
- Failing sync callback is logged, does not crash the job
- Failing async callback is logged, does not crash the job
- Closed main loop does not crash progress dispatch

### Event listeners

- Invalid event type (non-`Event` enum) raises `ConfigurationError`
- Non-callable callback raises `ConfigurationError`
- Removing an unregistered listener is silently ignored
- **`TASK_ADDED`**: listener receives `Event` and `Task` with correct
  `task_name` and `task_id`
- **`TASK_REMOVED`**: listener receives snapshot of task before deletion
- **`TASK_PAUSED`**: listener receives task with `paused` status
- **`TASK_RESUMED`**: listener receives task with `active` status
- **`JOB_STARTED`**: listener receives `Task` and `Job` with `running` status
- **`JOB_COMPLETED`**: listener receives `Job` with `duration_seconds` set and
  `error_message` as `None`
- **`JOB_FAILED`**: listener receives `Job` with `error_message` matching the
  exception
- **`JOB_CANCELLED`**: listener receives `Job` with `cancelled` status
- Multiple listeners for the same event are all called
- Async listener dispatched on main event loop
- Failing listener is logged and swallowed; subsequent listeners still run
- Sync listener works without an event loop
- Async listener works without an event loop (temporary event loop)
- Async listener failure without an event loop is caught and logged

### Handler injection

- `_job_id`, `_stop_event`, and `_progress_hook` are injected when handler
  accepts them
- Injection is skipped when handler signature does not include the parameters
- Handlers with `**kwargs` receive all injected parameters
- Uninspectable callables (e.g. `object()`) are handled gracefully (no
  injection, no crash)

### Deserialization safety

- Corrupt pickle data in `args_pickled` raises `ConfigurationError`
- Corrupt pickle data in `kwargs_pickled` raises `ConfigurationError`
- Pickled kwargs that aren't a `dict` raises `ConfigurationError`
- Corrupt pickle in `Task.model_validate()` from dict falls back to empty
  defaults
- Corrupt pickle in `Task.model_validate()` from `TaskDB` object falls back
  to empty defaults
- Non-standard input types pass through the validator without crashing

### Datetime normalization

- Naive datetimes are normalized to UTC-aware (treated as UTC)
- Timezone-aware datetimes are converted to UTC
- `None` datetimes pass through unchanged
- `Task` public model normalizes `next_run_at` from `TaskDB`
- `TaskDB` datetimes are normalized on DB load via `@reconstructor`
- `Job` datetimes (`started_at`, `ended_at`) are normalized on DB load via
  `@reconstructor`
- `Job` with `None` `ended_at` is handled correctly
- `get_all_tasks()` returns UTC-aware `next_run_at` regardless of configured
  display timezone
- `get_job()` returns UTC-aware `started_at` and `ended_at`

### Persistence layer

- `queue_task_for_immediate_run()` raises `TaskNotScheduledError` for missing
  task
- `pause_task()` and `resume_task()` raise `TaskNotFoundError` for missing
  task
- `mark_task_running()` raises `TaskNotFoundError` for missing task
- `mark_job_running()` and `finalize_job()` raise `JobNotFoundError` for
  missing job
- History cleanup deletes old finished jobs while keeping recent ones
- Job status filtering (`completed`, `failed`) returns correct subsets
- `get_all_tasks(include_run_once=False)` excludes run-once tasks
- Paused tasks are excluded from due-task queries
- Resumed tasks appear in due-task queries

### Immediate execution

- `run_task_immediately()` raises `HandlerNotRegisteredError` for
  unregistered handler
- `run_task_immediately()` raises `TaskNotScheduledError` when task row is
  missing
- `run_task_immediately()` successfully queues a registered task

### Configuration

- IANA timezone string resolves correctly
- `tzinfo` instance passes through
- Invalid timezone string raises `InvalidTimezoneError`
- Invalid type raises `InvalidTimezoneError`
- `QuivConfig` works without conflict when no explicit kwargs are passed

### Scheduler loop resilience

- Loop catches and logs exceptions, retries after sleep
- Loop does not crash on persistent errors (verified over multiple iterations)

### Model serialization

- `TaskDB` field serializer unpickles valid bytes for JSON output
- `Task` model serializes args/kwargs as unpickled Python objects
- JSON serialization produces correct output (tested with `model_dump_json()`)
- Time helpers (`next_run_time`, `get_current_time`) return UTC-aware datetimes
- `id_generator()` returns UUID strings
