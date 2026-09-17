# Testing

quiv has **more than 200 tests**. They cover the whole life of a task and of a job, the event listeners, the progress callbacks, the configuration, the models, and the rare cases. CI runs them on every commit, against Python 3.10 through 3.14.

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

Most tests need a running asyncio event loop, because quiv dispatches callbacks onto it. The `running_main_loop` fixture in `conftest.py` starts an event loop in a background thread and yields it to the test. Every test calls `scheduler.shutdown()` in a `finally` block, which releases the threads and deletes the temporary database files.

## What is tested

### Scheduler lifecycle and configuration

- Mixing `config=QuivConfig(...)` with explicit kwargs raises `ConfigurationError`
- `pool_size <= 0` and `history_retention_seconds < 0` are rejected
- `start()` is idempotent (safe to call multiple times)
- `shutdown()` continues and logs the problem when it cannot delete the database files
- Database initialization failure raises `DatabaseInitializationError`
- The internal tables of quiv (`quiv_task`, `quiv_job`) stay out of the SQLModel metadata of the application

### Task input validation

- Empty `task_name` is rejected
- `interval <= 0` and `delay < 0` are rejected
- `args` must be a `tuple` (not list)
- `kwargs` must be a `dict` (not string)
- Unpicklable args (e.g. lambdas) raise `ConfigurationError` with a clear message

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
- Sync handler without `stop_event` or `progress_hook` still runs correctly
- Failed handler sets job status to `failed`
- `job_id` is injected as a UUID string when handler accepts it
- `args` and `kwargs` ordering is preserved through pickle round-trip (tested with 8 positional args and 5 keyword args)

### Concurrent execution and backpressure

- Same task is never dispatched concurrently (status set to `running` blocks re-dispatch)
- While the thread pool is full, quiv holds due tasks back instead of adding them to a queue that never stops growing. Each one dispatches as soon as a job finishes and frees a slot
- A task that quiv held back runs as soon as a worker is free
- `_active_job_count` decrements correctly after job completion
- A job that starts late, because the pool was full, logs a warning that names the delay

### Interval scheduling (`fixed_interval`)

- **Fixed interval** (`fixed_interval=True`): next run is aligned to `start_time + interval`
- **Skipped intervals**: a 70-second job with 60-second interval skips to `start_time + 120s`; a 130-second job skips to `start_time + 180s`
- **Wait between runs** (`fixed_interval=False`): next run is `completion_time + interval`
- Recurring task finalization sets status back to `active` and updates `next_run_at`
- Run-once task finalization deletes the task row

### Cancellation

- `cancel_job()` returns `True` when stop event exists, `False` otherwise
- Handler that sets `stop_event` results in `cancelled` status
- `remove_task()` on a running task cancels its active job
- `shutdown()` cancels all tracked running jobs

### Progress callbacks

- Async progress callback dispatched on main event loop via handler's `progress_hook`
- Sync progress callback dispatched on main event loop via `call_soon_threadsafe`
- Async handler with sync progress callback works correctly
- Progress callback registration and clearing via `None`
- Sync callback works without an event loop (runs on worker thread)
- Async callback works without an event loop (runs in temporary event loop)
- quiv logs a sync callback that raises, and the job continues
- quiv logs an async callback that raises, and the job continues
- Closed main loop does not crash progress dispatch

### Event listeners

- Invalid event type (non-`Event` enum) raises `ConfigurationError`
- Non-callable callback raises `ConfigurationError`
- Removing a listener that was never registered does nothing and raises nothing
- **`TASK_ADDED`**: listener receives `Event` and `Task` with correct `task_name` and `task_id`
- **`TASK_REMOVED`**: listener receives snapshot of task before deletion
- **`TASK_PAUSED`**: listener receives task with `paused` status
- **`TASK_RESUMED`**: listener receives task with `active` status
- **`JOB_STARTED`**: listener receives `Task` and `Job` with `running` status
- **`JOB_COMPLETED`**: listener receives `Job` with `duration_seconds` set and `error_message` as `None`
- **`JOB_FAILED`**: listener receives `Job` with `error_message` matching the exception
- **`JOB_CANCELLED`**: listener receives `Job` with `cancelled` status
- Multiple listeners for the same event are all called
- Async listener dispatched on main event loop
- quiv logs a listener that raises and continues. The listeners after it still run
- Sync listener works without an event loop
- Async listener works without an event loop (temporary event loop)
- Async listener failure without an event loop is caught and logged

### Handler injection

- `job_id`, `stop_event`, and `progress_hook` are injected when handler accepts them
- Injection is skipped when handler signature does not include the parameters
- Handlers with `**kwargs` receive all injected parameters
- A callable whose signature quiv cannot read, such as `object()`, injects nothing and raises nothing

### Deserialization safety

- Corrupt pickle data in `args_pickled` raises `ConfigurationError`
- Corrupt pickle data in `kwargs_pickled` raises `ConfigurationError`
- Pickled kwargs that are not a `dict` raise `ConfigurationError`
- Corrupt pickle in `Task.model_validate()` from dict falls back to empty defaults
- Corrupt pickle in `Task.model_validate()` from `TaskDB` object falls back to empty defaults
- Non-standard input types pass through the validator without crashing

### Datetime normalization

- Naive datetimes are normalized to UTC-aware (treated as UTC)
- Timezone-aware datetimes are converted to UTC
- `None` datetimes pass through unchanged
- `Task` public model normalizes `next_run_at` from `TaskDB`
- `TaskDB` datetimes are normalized on DB load via `@reconstructor`
- `Job` datetimes (`started_at`, `ended_at`) are normalized on DB load via `@reconstructor`
- `Job` with `None` `ended_at` is handled correctly
- `get_all_tasks()` returns UTC-aware `next_run_at` regardless of configured display timezone
- `get_job()` returns UTC-aware `started_at` and `ended_at`

### Persistence layer

- `queue_task_for_immediate_run()` raises `TaskNotFoundError` for missing task
- `pause_task()` and `resume_task()` raise `TaskNotFoundError` for missing task
- `mark_task_running()` raises `TaskNotFoundError` for missing task
- `mark_job_running()` and `finalize_job()` raise `JobNotFoundError` for missing job
- History cleanup deletes old finished jobs while keeping recent ones
- Job status filtering (`completed`, `failed`) returns correct subsets
- `get_all_tasks(include_run_once=False)` excludes run-once tasks
- Paused tasks are excluded from due-task queries
- Resumed tasks appear in due-task queries

### Immediate execution

- `run_task_immediately()` raises `HandlerNotRegisteredError` for unregistered handler
- `run_task_immediately()` raises `TaskNotFoundError` when task row is missing
- `run_task_immediately()` successfully queues a registered task

### Configuration

- IANA timezone string resolves correctly
- `tzinfo` instance passes through
- Invalid timezone string raises `InvalidTimezoneError`
- Invalid type raises `InvalidTimezoneError`
- `QuivConfig` works without conflict when no explicit kwargs are passed

### Scheduler loop resilience

- The loop catches an exception, logs it, and tries again after a sleep
- The loop keeps running when the same error repeats, which the test checks over several turns

### Model serialization

- `TaskDB` field serializer unpickles valid bytes for JSON output
- `Task` model serializes args/kwargs as unpickled Python objects
- JSON serialization produces correct output (tested with `model_dump_json()`)
- Time helpers (`next_run_time`, `get_current_time`) return UTC-aware datetimes
- `id_generator()` returns UUID strings

## Soak test

The test suite runs in under a minute, so it cannot show what happens to a scheduler that runs for a day. A separate script does that. `scripts/soak.py` runs a mixed workload for as long as you ask, and checks for the faults that only appear with time: threads that accumulate, a database that never stops growing, memory that climbs, and errors that nobody sees.

```bash
uv run python scripts/soak.py --minutes 10    # while you work
uv run python scripts/soak.py --hours 24      # before a release
```

The workload covers every execution path at once: a sync task, an async task, a task that fails and retries, a task that another thread cancels, a task that passes its timeout, and a task on a sub-second interval. A separate thread calls `stats()` and `get_all_jobs()` every second, the way an admin page would.

The script exits non-zero on any of four conditions:

- The number of live threads grows past its settled baseline.
- The retained job history grows past what the retention window allows.
- The `"Quiv"` logger records an ERROR that the failing task does not explain.
- The scheduler thread dies.

### Result for v1.0.0

Run on 2026-09-17, for the full 24 hours, on an Intel Core i5-11600 with Python 3.10.12 on Linux. **It passed.** The complete log is in the repository at [`benchmarks/results/soak-24h-2026-09-17.log`](https://github.com/nandyalu/quiv/blob/main/benchmarks/results/soak-24h-2026-09-17.log).

| Measurement | Result |
| --- | --- |
| Ran for | 24.00 h |
| Jobs finished | 294,400, at 3.4 each second |
| — completed | 234,876 |
| — failed | 32,229 |
| — cancelled | 27,295 |
| — retries queued | 21,486 |
| Live threads | 8 at the baseline, 8 at the peak |
| Retained job history | 2,264 rows at the peak, against a bound of 5,006 |
| Peak memory | 53 MB |
| Unexpected errors | 0 |

Three of those rows carry the weight:

**The thread count never moved.** It read 8 in all 280 samples, across roughly 294,000 job lifecycles. Each of those built a stop event and a set of injected arguments, and each run of the async task built its own event loop and closed it again. Nothing accumulated.

**The database stopped growing after the first hour.** The retained history reached about 2,260 rows and stayed there for the next 22 hours, which means the cleanup keeps pace with the work indefinitely. The file does not grow without limit.

**Memory held at 53 MB** for the whole day. That rules out the slow leaks: job rows kept in memory, stop events never dropped from the registry, and event loops retained after their job ends.
