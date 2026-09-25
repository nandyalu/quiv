# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

quiv is a lightweight threadpool-backed background scheduler for Python apps (designed for FastAPI). It provides recurring/one-shot tasks, sync/async handlers, cooperative cancellation (`stop_event`), progress callbacks (`progress_hook`), and task/job persistence via SQLModel + SQLite.

Python 3.10–3.14. Dependencies: `sqlmodel`, `tzdata`.

## Roadmap and implementation plans

Work toward v1.0.0 followed a fixed, phased roadmap: `docs/roadmap.md` (what and why) and `plans/` (how — one detailed plan per phase with prescriptive design decisions, tests, pitfalls, and exit checklists; start at `plans/README.md`). Work after v1.0.0 continues the same way. v1.1.0 shipped outside the phases (`update_task(run_at=)`, `pending_main_loop_work()`), so the phase numbers and the version numbers no longer line up. The order decided on 2026-09-25 is Phase 8 (waiting on work: job/task waiters, `call_on_main()`, `run_subprocess()`; v1.2.0; `plans/phase-8-waiting-on-work.md`), Phase 9 (operations: `is_running`, `after_current`, `task_name` filter, the container page; v1.3.0; `plans/phase-9-operations.md`), then Phase 7 (process jobs; v1.4.0; `plans/phase-7-process-jobs.md`). Phases 8 and 9 came out of the 2026-09-25 review of trailarr and ten-acre and do not depend on Phase 7; Phase 7's §0, the three-platform CI matrix, landed first. Every 1.x phase is additive — no 1.0 name or default changes; a breaking change waits for 2.0.0.

**Before implementing any scheduler change, read the matching phase plan in `plans/` and follow it — the design decisions there are already settled; do not re-derive or deviate from them.** Explicitly rejected (do not propose or implement): cron/calendar scheduling, durable persistence (reviewed again on 2026-09-17 after 1.0.0 and still rejected — apps store the last run time themselves and pass `delay`/`run_at`), lazy or unpicklable arguments (compute them inside the handler), event-loop reuse in `run_async` (each async invocation must keep its own fresh event loop — isolation requirement, in the parent and in a worker process alike), lazy logging. When a phase completes, update `docs/roadmap.md`, `docs/release-notes.md`, the version in `pyproject.toml`, and any CLAUDE.md sections the phase changed.

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

# Benchmarks (scripts, not tests; never run in CI)
uv run python benchmarks/bench_dispatch_latency.py
uv run python benchmarks/bench_throughput.py

# Soak test (long-running; --hours 24 before a release)
uv run python scripts/soak.py --minutes 10
```

## Architecture

The library has a layered design with four core modules:

- **`scheduler.py`** (`Quiv`) — Public API and orchestration loop. Extends `QuivBase`. The `_loop()` method runs in a daemon thread, sleeping until the next due task on an interruptible wait (`_wake_event`) and dispatching due tasks to the thread pool; mutating API calls (`add_task`, `run_task_immediately`, `resume_task`, `remove_task`), job completion, and `shutdown()` wake it early. Sub-second intervals are supported; an idle scheduler issues no DB queries (60s sleep ceiling as a safety net). `add_task()` is the main entry point for scheduling; `remove_task()` removes a task and its registrations. `_run_job()` handles job execution and finalizes both job and task state on completion.

- **`base.py`** (`QuivBase`) — Abstract base with lifecycle management. Owns the `ThreadPoolExecutor`, SQLite engine (temp file), handler/callback/event-listener registries, and stop events dict. Handles async execution bridge (`run_async` creates thread-local event loops), progress callback dispatch, and event listener dispatch to the main event loop.

- **`persistence.py`** (`PersistenceLayer`) — All SQLModel/SQLAlchemy database operations. Task CRUD (`create_task`/`delete_task`), task lifecycle (`mark_task_running`/`finalize_task_after_job`), job lifecycle transitions, due-task queries, history cleanup (SQL-level, runs every 60s). Uses `col()` wrapper for typed SQLModel WHERE clauses. Reads are lock-free (SQLite WAL provides reader/writer coordination); read-modify-write operations serialize on `_write_lock`.

  **SQLAlchemy predicate conventions**: never compare columns to literal booleans or `None` with `==`/`!=` — use `col(Model.field).is_(False)`, `.is_not(None)`, `.in_(...)`. Plain value comparisons (`Job.status == status`, `TaskDB.next_run_at <= now`) stay bare. `col()` is applied exactly where needed: nullable columns (narrows `Optional` for mypy strict) and method-style operators (`is_`, `is_not`, `in_`); do not wrap non-nullable value comparisons.

- **`execution.py`** (`ExecutionLayer`) — Invocation preparation and callable dispatch. Introspects handler signatures to conditionally inject `stop_event` and `progress_hook` kwargs. Handles both sync and async callables. `prepare_invocation()` deserializes pickled args back into a `tuple` to preserve the type contract from `add_task()`.

- **`models.py`** — `Task` and `Job` SQLModel table classes with a private `quiv_registry` to isolate metadata from user models. `Event`/`TaskStatus`/`JobStatus` are `str, Enum` enums. Model validators force UTC on datetime fields loaded from SQLite.

- **`config.py`** — `QuivConfig` frozen dataclass and `resolve_timezone()` helper.

- **`exceptions.py`** — Exception hierarchy rooted at `QuivError`.

### Key patterns

- **Handler injection**: `job_id`, `stop_event`, and `progress_hook` are only injected if the handler's signature accepts them (checked via `inspect.signature`). Renamed in v1.0.0 from `_job_id`/`_stop_event`/`_progress_hook` — two guards in `add_task()` raise `ConfigurationError`: `_validate_no_legacy_params` (handler still declares an old name; without it cancellation and timeouts fail silently — remove the guard in 2.0.0) and `_validate_no_injectable_clash` (a `kwargs` key collides with an injected name; permanent, and `update_task()` runs it too). Rationale in `plans/api-freeze-notes.md`.
- **Async bridge**: Async handlers run in thread-local event loops created per invocation. Progress callbacks are dispatched to the main loop via `run_coroutine_threadsafe` or `call_soon_threadsafe`.
- **Lazy event loop resolution**: `_main_loop` is `None` at init and lazily resolved via `_resolve_main_loop()` on first progress callback dispatch. This allows module-level `Quiv()` instantiation before an asyncio loop exists (common in FastAPI apps). Without an event loop, sync progress callbacks run directly on the worker thread; async progress callbacks run in a temporary event loop on the worker thread.
- **Database lifecycle**: Each `Quiv` instance creates a temp SQLite file (WAL mode); `shutdown()` disposes the engine and deletes the file along with `-wal` and `-shm` sidecar files. `shutdown(timeout=...)` bounds the wait for the loop thread and in-flight jobs — jobs that don't exit in time are abandoned on their worker threads with a warning; only `RUNNING` jobs are signalled for cancellation.
- **quiv never waits for `run_on_main` work and never cancels it. It reports, and that is the whole feature.** A handler that hands work to the main loop returns at once, so its job is already complete and `shutdown()` finds nothing running. **The loop belongs to the application** — quiv is a guest, does not close it, and cannot know whether a half-finished callable should be stopped or allowed to end. `shutdown()` warns naming the count, and `pending_main_loop_work() -> int` returns it at any time, cheaply (one set under a lock, no DB, unlike `stats()`), so the app can poll before closing its own loop. `QuivBase._main_loop_work` is the tracked set, written by `run_on_main` across all four dispatch shapes; a **sync** callable sent with `call_soon_threadsafe` has no future, so a plain marker stands in for it from the moment it is queued until it has run, and `run_on_main` untracks that marker if the enqueue raises, or the count never returns to zero.

  **Do not build a drain here.** An `ashutdown()` that waited for this work was written and then removed on 2026-09-20: three review rounds found nine defects in it, including three separate ways to hang (a closed loop, a stopped-but-open loop, and a cross-loop `asyncio.wait` raising `ValueError: The future belongs to a different loop`), plus a case that is unfixable in principle — with `shutdown(timeout=...)`, an abandoned job keeps running on a daemon thread that cannot be stopped, so it can hand work over after any drain has finished. Building a reliable drain on top of a fire-and-forget primitive costs far more than it returns, and it takes ownership of a loop quiv does not own. Report the count and let the application decide.
- **Config precedence**: Pass either a `QuivConfig` object or individual kwargs (`pool_size`, etc.) to `Quiv()`, but not both. Both `QuivConfig` and `Quiv` use `timezone` for the display timezone parameter.
- **Timezone handling**: The `timezone` parameter is only used for formatting datetime values in log output. All internal datetime handling (scheduling, persistence, job lifecycle) uses UTC exclusively.
- **Task identification**: `add_task()` takes `interval` only for recurring tasks — a `run_once=True` task never repeats, so its interval is optional, ignored if given, and stored as `None`. The first run is scheduled by either `delay` (seconds from now) or the keyword-only `run_at` (an absolute `datetime`); passing both raises `ConfigurationError`, a naive `run_at` is read as UTC (never the display `timezone`), and a `run_at` already past runs at once rather than raising. `add_task()` returns a `task_id` (UUID string) used as the key for all runtime operations (`remove_task`, `pause_task`, `resume_task`, `run_task_immediately`, `get_task`, `update_task`). `update_task()` mutates task fields in place via keyword-only params with an `_UNSET` sentinel (so `None` stays meaningful for `timeout`/`progress_callback`); `run_once`, `delay`, and the handler `func` are deliberately not updatable. `run_at` moves the next run to an absolute time and is mutually exclusive with `interval`, because `persistence.update_task` recomputes `next_run_at` from a new interval and would otherwise overwrite it silently; it exists so a pending one-off can change its time without `remove_task` + `add_task`, which returns a new `task_id` and leaves a window with nothing scheduled. **`run_at` is discarded on a RUNNING task** — `finalize_task_after_job` deletes a run-once row and recomputes `next_run_at` from the interval for a recurring one — so `update_task` logs a warning when it sees that, best-effort because the task can enter RUNNING after the check. Multiple tasks can share the same `task_name`; each gets its own unique `task_id`. Handler/callback registries are keyed by `task_id`. Handler/callback registration is private (`_register_handler`, `_register_progress_callback`). `run_task_immediately()` requires the task to be `ACTIVE` — it raises `TaskNotActiveError` for `running` (no-overlap invariant) and `paused` tasks, and `TaskNotFoundError` for an unknown id (it checks the task row before the handler registry, so a removed or already-fired run-once task reports the missing task, not a missing handler). The `registry`/`progress_callbacks`/`stop_events` dicts are guarded by `_registries_lock` (held only around dict operations, never around handler or DB calls); `_dispatch_due_task` tolerates tasks removed between the due-query and dispatch (skips with a warning instead of stalling the loop).
- **Task lifecycle**: On dispatch, task status is set to `RUNNING` preventing concurrent runs. On job completion (or failure), `finalize_task_after_job` sets status back to `ACTIVE` and calculates `next_run_at`. When `fixed_interval=True` (default), next run is `start_time + (floor(elapsed / interval) + 1) * interval` — the next strictly-future interval boundary (missed intervals are skipped; `floor+1` rather than `ceil` so an exact-boundary elapsed never yields `next_run_at == now` and an immediate re-dispatch). When `fixed_interval=False`, next run is `now + interval` (wait between runs). For run-once tasks, the task row is deleted instead. On failure with retries remaining (`max_retries`), the next run is `now + retry_backoff * 2**(failures_so_far - 1)` (exponential backoff; `retry_attempt` counter resets on success or exhaustion; cancelled jobs never retry). `jitter` adds `uniform(0, jitter)` seconds to each recurring next-run time (never to retry backoff or the initial delay).
- **Timeouts are cooperative cancellations**: `timeout` sets the job's stop event via the scheduler loop (`_enforce_timeouts`, deadline-aware sleep) — the job finalizes as `CANCELLED` with a timeout `error_message`; no separate status, and handlers that ignore the stop event keep their pool thread until they return.
- **Backpressure**: Scheduler skips dispatching when `_active_job_count >= pool_size` (protected by `_job_count_lock`). Deferred tasks stay in DB; a finishing job wakes the loop so they dispatch as soon as a slot frees. Jobs that start late log a warning with the delay.
- **Cancellation**: `cancel_job()` sets the stop event in `self.stop_events`. `_run_job` checks this dict directly (not `kwargs`) so cancellation is detected even if the handler doesn't accept `stop_event`.
- **Args as tuples**: `add_task()` accepts `args` as a `tuple` (not list) to preserve ordering intent. Args are pickle-serialized for persistence; `ExecutionLayer.prepare_invocation()` unpickles and wraps them in a `tuple` before passing to the handler.
- **Event listeners**: Global `add_listener(Event, callback)` / `remove_listener(Event, callback)` on `QuivBase`. Listeners are stored in `_event_listeners: dict[Event, list[Callable]]`. `_emit_event()` dispatches to the main loop using the same pattern as progress callbacks (async via `run_coroutine_threadsafe`, sync via `call_soon_threadsafe`, fallback to direct call). Exceptions in listeners are logged and swallowed. `TASK_*` callbacks receive `(event, task: Task)`, `JOB_*` callbacks receive `(event, task: Task, job: Job)`. The `Job` model includes `duration_seconds` and `error_message` fields set during finalization. Events: `TASK_ADDED`, `TASK_REMOVED`, `TASK_PAUSED`, `TASK_RESUMED`, `TASK_UPDATED`, `JOB_STARTED`, `JOB_COMPLETED`, `JOB_FAILED`, `JOB_CANCELLED`, `JOB_RETRYING` (fires after `JOB_FAILED` when a retry was scheduled).
- **Logging**: The library does not set log levels. The `logger` parameter accepts `logging.Logger` or `logging.LoggerAdapter[Any]`. Applications configure the `"Quiv"` logger themselves.

## AI-tooling artifacts

Three artifacts teach AI assistants how to use quiv; when the public API or key patterns change, update them alongside the docs:

- `quiv/AGENTS.md` — condensed agent-facing reference shipped inside the wheel (lands in site-packages).
- `docs/llms.txt` — llms.txt index served at the docs site root; `docs/llms-full.txt` is generated (gitignored) by `scripts/generate_llms_full.py`, which CI runs before `zensical build`.
- `.claude-plugin/` + `skills/quiv/SKILL.md` — the repo doubles as a Claude Code plugin marketplace shipping a `quiv` skill (install: `/plugin marketplace add nandyalu/quiv`, `/plugin install quiv@quiv`). Validate with `claude plugin validate .`. Plugin version is intentionally omitted so each commit counts as a new version.

## Testing

Tests use pytest. Most tests require the `running_main_loop` fixture (from `conftest.py`) which spins up an asyncio event loop in a background thread. Always call `scheduler.shutdown()` in a `finally` block to clean up threads and temp DB files.

Coverage is gated: `[tool.coverage.report] fail_under = 95` in `pyproject.toml` makes pytest exit non-zero below that, in CI as well as locally. The suite currently sits at 100%.

A test that deliberately leaves a temp database behind must request the `leftover_db_paths` fixture and append `scheduler._db_path` to it. Two situations need this: a thread abandoned by `shutdown(timeout=...)` recreates the file when its write lands (SQLite creates a missing file on write), and a test that patches `os.remove` to fail never deletes it. Without the fixture each run drops a file in the temp directory.

`scripts/soak.py` runs the long mixed workload — sync, async, failing/retrying, cancelled, timing out, and sub-second tasks at once — and fails on thread growth, unbounded job history, an unexplained ERROR, or a dead scheduler thread. The 24-hour run before `v1.0.0` is logged at `benchmarks/results/soak-24h-2026-09-17.log`.
