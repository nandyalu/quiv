# Architecture

`quiv` is split into focused layers:

- `base` layer (`quiv/base.py`): runtime lifecycle, DB bootstrap,
  threadpool, callback plumbing, cancellation controls
- `scheduler` layer (`quiv/scheduler.py`): public API and scheduling loop
- `persistence` layer (`quiv/persistence.py`): task/job storage operations
- `execution` layer (`quiv/execution.py`): invocation preparation and
  sync/async dispatch
- `models` layer (`quiv/models.py`): SQLModel entities and status constants

## Runtime flow

1. `Quiv(...)` initializes runtime resources
   - resolves timezone
   - creates temporary SQLite database in OS temp directory
   - initializes SQLModel tables
   - creates threadpool executor
2. `add_task(...)` registers the handler and progress callback, then upserts a
   `Task` row with scheduling metadata.
3. `start()` launches scheduler loop thread.
4. Loop iteration (runs every 1 second):
   - cleans old job history according to retention window
   - selects due active tasks (`next_run_at <= now`)
   - creates a `Job` row for each due task
   - prepares invocation args (inject hooks if supported)
   - submits execution to threadpool
   - updates next run or removes one-shot task
5. Job completion updates terminal status (`completed`, `failed`, `cancelled`).

## Cancellation model

- each job receives its own `threading.Event` stop signal if handler accepts
  `_stop_event`
- `cancel_job(job_id)` sets that event when the job is currently tracked
- cancellation is cooperative: handler code must check the event

## Progress callback model

- handlers can receive `_progress_hook` when accepted in signature
- calling `_progress_hook(...)` dispatches configured progress callback on the
  main asyncio loop
- async callbacks are dispatched via `run_coroutine_threadsafe`
- sync callbacks are dispatched via `call_soon_threadsafe`

## Async execution model

Async task handlers do not run on the main application event loop. Instead,
each async invocation creates a dedicated thread-local event loop, runs the
coroutine to completion, and tears down the loop. This ensures async handlers
do not interfere with each other or with the main loop.

## Persistence model

- tasks and jobs are persisted in internal SQLite tables:
  - `quiv_task`
  - `quiv_job`
- `quiv` uses a private SQLAlchemy `registry` to keep its metadata separate
  from user-defined SQLModel models
- datetimes are normalized to UTC-aware values on model load
- history cleanup removes old finished jobs by retention cutoff

## Thread safety

- the scheduler loop runs in a single daemon thread
- task handlers execute in the threadpool (`ThreadPoolExecutor`)
- each handler invocation gets its own stop event and kwargs; there is no
  shared mutable state between concurrent handler runs
- persistence operations use short-lived `Session` scopes
- progress callbacks are dispatched thread-safely onto the main asyncio loop

## Lifecycle and teardown

- `shutdown()`:
  - requests loop shutdown
  - signals cancellation for tracked running jobs
  - joins scheduler thread
  - shuts down threadpool
  - disposes engine and removes temp DB file
- the temporary SQLite database does not survive process restarts
