# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

quiv is a lightweight threadpool-backed background scheduler for Python apps (designed for FastAPI). It provides recurring/one-shot tasks, sync/async handlers, cooperative cancellation (`_stop_event`), progress callbacks (`_progress_hook`), and task/job persistence via SQLModel + SQLite.

Python 3.10–3.14. Dependencies: `sqlmodel`, `tzdata`.

## Commands

```bash
# Install for development
pip install -e ".[dev]"

# Run all tests
pytest

# Run a single test
pytest tests/test_scheduler.py::test_add_task_validates_inputs

# Run tests with coverage
pytest --cov=quiv

# Type checking
mypy quiv

# Build docs (uses Zensical, not MkDocs)
zensical build --clean
```

## Architecture

The library has a layered design with four core modules:

- **`scheduler.py`** (`Quiv`) — Public API and orchestration loop. Extends `QuivBase`. The `_loop()` method runs in a daemon thread, polling for due tasks every second and dispatching them to the thread pool. `add_task()` is the main entry point for scheduling.

- **`base.py`** (`QuivBase`) — Abstract base with lifecycle management. Owns the `ThreadPoolExecutor`, SQLite engine (temp file), handler/callback registries, and stop events dict. Handles async execution bridge (`run_async` creates thread-local event loops) and progress callback dispatch to the main event loop.

- **`persistence.py`** (`PersistenceLayer`) — All SQLModel/SQLAlchemy database operations. Task CRUD, job lifecycle transitions, due-task queries, history cleanup. Stateless aside from engine reference.

- **`execution.py`** (`ExecutionLayer`) — Invocation preparation and callable dispatch. Introspects handler signatures to conditionally inject `_stop_event` and `_progress_hook` kwargs. Handles both sync and async callables.

- **`models.py`** — `Task` and `Job` SQLModel table classes with a private `quiv_registry` to isolate metadata from user models. `TaskStatus`/`JobStatus` are string constant classes. Model validators force UTC on datetime fields loaded from SQLite.

- **`config.py`** — `QuivConfig` frozen dataclass and `resolve_timezone()` helper.

- **`exceptions.py`** — Exception hierarchy rooted at `QuivError`.

### Key patterns

- **Handler injection**: `_stop_event` and `_progress_hook` are only injected if the handler's signature accepts them (checked via `inspect.signature`).
- **Async bridge**: Async handlers run in thread-local event loops created per invocation. Progress callbacks are dispatched to the main loop via `run_coroutine_threadsafe` or `call_soon_threadsafe`.
- **Database lifecycle**: Each `Quiv` instance creates a temp SQLite file; `shutdown()` disposes the engine and deletes the file.
- **Config precedence**: Pass either a `QuivConfig` object or individual kwargs (`pool_size`, etc.) to `Quiv()`, but not both.

## Testing

Tests use pytest. Most tests require the `running_main_loop` fixture (from `conftest.py`) which spins up an asyncio event loop in a background thread. Always call `scheduler.shutdown()` in a `finally` block to clean up threads and temp DB files.
