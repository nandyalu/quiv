# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

quiv is a lightweight threadpool-backed background scheduler for Python apps (designed for FastAPI). It provides recurring/one-shot tasks, sync/async handlers, cooperative cancellation (`_stop_event`), progress callbacks (`_progress_hook`), and task/job persistence via SQLModel + SQLite.

Python 3.10–3.14. Dependencies: `sqlmodel`, `tzdata`.

## Commands

All commands must be run via `uv`.

```bash
# Install for development
uv pip install -e ".[dev]"

# Run all tests
uv run pytest

# Run a single test
uv run pytest tests/test_scheduler.py::test_add_task_validates_inputs

# Run tests with coverage
uv run pytest --cov=quiv

# Type checking
uv run mypy quiv

# Build docs (uses Zensical, not MkDocs)
uv run zensical build --clean
```

## Architecture

The library has a layered design with four core modules:

- **`scheduler.py`** (`Quiv`) — Public API and orchestration loop. Extends `QuivBase`. The `_loop()` method runs in a daemon thread, polling for due tasks every second and dispatching them to the thread pool. `add_task()` is the main entry point for scheduling; `remove_task()` removes a task and its registrations. `_run_job()` handles job execution and finalizes both job and task state on completion.

- **`base.py`** (`QuivBase`) — Abstract base with lifecycle management. Owns the `ThreadPoolExecutor`, SQLite engine (temp file), handler/callback/event-listener registries, and stop events dict. Handles async execution bridge (`run_async` creates thread-local event loops), progress callback dispatch, and event listener dispatch to the main event loop.

- **`persistence.py`** (`PersistenceLayer`) — All SQLModel/SQLAlchemy database operations. Task CRUD (`create_task`/`delete_task`), task lifecycle (`mark_task_running`/`finalize_task_after_job`), job lifecycle transitions, due-task queries, history cleanup (SQL-level, runs every 60s). Uses `col()` wrapper for typed SQLModel WHERE clauses. Thread-safe via `threading.Lock`.

- **`execution.py`** (`ExecutionLayer`) — Invocation preparation and callable dispatch. Introspects handler signatures to conditionally inject `_stop_event` and `_progress_hook` kwargs. Handles both sync and async callables. `prepare_invocation()` deserializes pickled args back into a `tuple` to preserve the type contract from `add_task()`.

- **`models.py`** — `Task` and `Job` SQLModel table classes with a private `quiv_registry` to isolate metadata from user models. `Event`/`TaskStatus`/`JobStatus` are `str, Enum` enums. Model validators force UTC on datetime fields loaded from SQLite.

- **`config.py`** — `QuivConfig` frozen dataclass and `resolve_timezone()` helper.

- **`exceptions.py`** — Exception hierarchy rooted at `QuivError`.

### Key patterns

- **Handler injection**: `_job_id`, `_stop_event`, and `_progress_hook` are only injected if the handler's signature accepts them (checked via `inspect.signature`).
- **Async bridge**: Async handlers run in thread-local event loops created per invocation. Progress callbacks are dispatched to the main loop via `run_coroutine_threadsafe` or `call_soon_threadsafe`.
- **Lazy event loop resolution**: `_main_loop` is `None` at init and lazily resolved via `_resolve_main_loop()` on first progress callback dispatch. This allows module-level `Quiv()` instantiation before an asyncio loop exists (common in FastAPI apps). Without an event loop, sync progress callbacks run directly on the worker thread; async progress callbacks run in a temporary event loop on the worker thread.
- **Database lifecycle**: Each `Quiv` instance creates a temp SQLite file (WAL mode); `shutdown()` disposes the engine and deletes the file along with `-wal` and `-shm` sidecar files.
- **Config precedence**: Pass either a `QuivConfig` object or individual kwargs (`pool_size`, etc.) to `Quiv()`, but not both. Both `QuivConfig` and `Quiv` use `timezone` for the display timezone parameter.
- **Timezone handling**: The `timezone` parameter is only used for formatting datetime values in log output. All internal datetime handling (scheduling, persistence, job lifecycle) uses UTC exclusively.
- **Task identification**: `add_task()` returns a `task_id` (UUID string) used as the key for all runtime operations (`remove_task`, `pause_task`, `resume_task`, `run_task_immediately`, `get_task`). Multiple tasks can share the same `task_name`; each gets its own unique `task_id`. Handler/callback registries are keyed by `task_id`. Handler/callback registration is private (`_register_handler`, `_register_progress_callback`).
- **Task lifecycle**: On dispatch, task status is set to `RUNNING` preventing concurrent runs. On job completion (or failure), `finalize_task_after_job` sets status back to `ACTIVE` and bumps `next_run_at = now + interval`. For run-once tasks, the task row is deleted instead.
- **Backpressure**: Scheduler skips dispatching when `_active_job_count >= pool_size` (protected by `_job_count_lock`). Deferred tasks stay in DB and are picked up on the next tick. Jobs that start late log a warning with the delay.
- **Cancellation**: `cancel_job()` sets the stop event in `self.stop_events`. `_run_job` checks this dict directly (not `kwargs`) so cancellation is detected even if the handler doesn't accept `_stop_event`.
- **Args as tuples**: `add_task()` accepts `args` as a `tuple` (not list) to preserve ordering intent. Args are pickle-serialized for persistence; `ExecutionLayer.prepare_invocation()` unpickles and wraps them in a `tuple` before passing to the handler.
- **Event listeners**: Global `add_listener(Event, callback)` / `remove_listener(Event, callback)` on `QuivBase`. Listeners are stored in `_event_listeners: dict[Event, list[Callable]]`. `_emit_event()` dispatches to the main loop using the same pattern as progress callbacks (async via `run_coroutine_threadsafe`, sync via `call_soon_threadsafe`, fallback to direct call). Exceptions in listeners are logged and swallowed. `TASK_*` callbacks receive `(event, task: Task)`, `JOB_*` callbacks receive `(event, task: Task, job: Job)`. The `Job` model includes `duration_seconds` and `error_message` fields set during finalization. Events: `TASK_ADDED`, `TASK_REMOVED`, `TASK_PAUSED`, `TASK_RESUMED`, `JOB_STARTED`, `JOB_COMPLETED`, `JOB_FAILED`, `JOB_CANCELLED`.
- **Logging**: The library does not set log levels. The `logger` parameter accepts `logging.Logger` or `logging.LoggerAdapter[Any]`. Applications configure the `"Quiv"` logger themselves.

## Testing

Tests use pytest. Most tests require the `running_main_loop` fixture (from `conftest.py`) which spins up an asyncio event loop in a background thread. Always call `scheduler.shutdown()` in a `finally` block to clean up threads and temp DB files.
