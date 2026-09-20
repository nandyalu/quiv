---
name: quiv
description: >
  Usage guide for quiv, the threadpool-backed background scheduler for
  Python/FastAPI apps. Use when adding, scheduling, cancelling, or debugging
  background/recurring tasks with quiv, or when the user mentions quiv,
  add_task, stop_event, progress_hook, run_on_main, or asks how to run
  periodic jobs in a FastAPI app that already depends on quiv.
---

# Working with quiv

quiv is a single-process, threadpool-backed scheduler: recurring or one-shot tasks, sync **and** async handlers, cooperative cancellation, progress callbacks on the main asyncio loop, per-job trace IDs. Python 3.10–3.14.

**Ground rules before writing code:**

- If quiv is installed, read the condensed reference shipped with it: `python -c "import quiv, pathlib; print(pathlib.Path(quiv.__file__).parent / 'AGENTS.md')"` — read that file if it exists (older quiv versions don't ship it).
- Full docs index (fetchable): https://nandyalu.github.io/quiv/llms.txt · full text: https://nandyalu.github.io/quiv/llms-full.txt
- quiv is NOT Celery: no multi-process workers, no durable queues, no cron/calendar scheduling. Task state lives in a temp SQLite file deleted on `shutdown()` — nothing survives restarts; re-add tasks on startup.

## Core pattern (FastAPI)

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
from quiv import Quiv

scheduler = Quiv()  # module level is fine; main loop resolves lazily

@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler.add_task(task_name="reindex", func=reindex, interval=300)
    scheduler.start()
    yield
    await scheduler.ashutdown()   # ALWAYS — cancels jobs, deletes temp DB,
                                  # and waits for run_on_main work

app = FastAPI(lifespan=lifespan)
```

`add_task(task_name, func, interval=None, delay=None, run_once=False, fixed_interval=True, args=None, kwargs=None, progress_callback=None, *, run_at=None, timeout=None, max_retries=0, retry_backoff=30.0, jitter=0.0) -> task_id` (the failure-handling options are keyword-only). `timeout` cooperatively cancels overlong jobs (sets the stop event; job ends `cancelled`); `max_retries`/`retry_backoff` retry FAILED jobs with exponential backoff (cancelled jobs never retry); `jitter` adds `uniform(0, jitter)` seconds to recurring next-run times. Schedule the first run with `delay` (seconds) or `run_at` (an absolute `datetime`), never both — a naive `run_at` is UTC, and one already past runs at once. `interval` is required only for recurring tasks: a `run_once=True` task never repeats, so its interval is optional, ignored if given, and reads back as `None`. The returned `task_id` (UUID string) keys everything: `pause_task`, `resume_task`, `run_task_immediately`, `remove_task`, `get_task` — all of which raise `TaskNotFoundError` for an unknown id, including a run-once task that already fired and removed itself. `task_name` is a non-unique display label — never treat it as a key. `args`/`kwargs` are pickled: no lambdas or inner functions.

`update_task(task_id, *, interval=..., jitter=..., timeout=..., run_at=..., ...)` mutates a task in place (keyword-only, omit what you don't change; changing `interval` reschedules to now + interval). `run_at` sets the next run's absolute time instead, mutually exclusive with `interval` — it moves the next scheduled run of any task (a recurring one keeps its interval afterwards), and is how a pending one-off moves its time while keeping its `task_id`, rather than `remove_task` + `add_task`. It is discarded if the task is already `running`, because finalization rewrites the schedule; quiv logs a warning. `stats()` returns a `QuivStats` snapshot (active jobs, pool utilization, tasks by status). `get_all_jobs(...)` supports `task_id`/`since`/`until` filters, `order_by` (`started_at`/`ended_at`), and `limit`/`offset` pagination.

## Handler injection

`job_id: str`, `stop_event: threading.Event`, and `progress_hook: Callable` are injected **only if the handler signature declares them**:

```python
def work(item_id: int, stop_event=None, progress_hook=None):
    for i, chunk in enumerate(chunks(item_id)):
        if stop_event and stop_event.is_set():
            return                      # cancellation is cooperative — this check is mandatory
        process(chunk)
        if progress_hook:
            progress_hook(step=i)      # forwarded to progress_callback on the main loop
```

Renamed in v1.0.0 (were `_job_id`, `_stop_event`, `_progress_hook`). `add_task()` raises `ConfigurationError` if a handler still declares an old name, or if a `kwargs` key collides with an injected name.

Async handlers pass the same way; each invocation gets a fresh event loop on a worker thread (never the main app loop — do not change this; isolation is a design requirement). To touch main-loop resources from task code use `from quiv import run_on_main; run_on_main(async_or_sync_fn, *args)` (fire-and-forget, exceptions logged and swallowed).

## Observability

- Events: `scheduler.add_listener(Event.JOB_FAILED, cb)` — `TASK_*` callbacks get `(event, task)`, `JOB_*` get `(event, task, job)`; `job.error_message` and `job.duration_seconds` are set on finalization.
- Inspect: `get_all_tasks()`, `get_all_jobs(status="failed")` — return SQLModel objects safe to return from FastAPI endpoints (UTC-aware datetimes).
- Logging: quiv never configures logging; configure the `"Quiv"` logger to see scheduler output.

## Common mistakes to avoid

1. Forgetting `scheduler.shutdown()` (in tests: `finally:` block) — leaks the loop thread and temp DB file.
2. Passing both `config=QuivConfig(...)` and individual kwargs to `Quiv()` — raises `ConfigurationError`; pick one.
3. Expecting `cancel_job()`/`shutdown()` to kill threads — a handler that never checks `stop_event` runs to completion. Use `shutdown(timeout=...)` to bound the wait; jobs exceeding it are abandoned with a warning.
4. Blocking on main-loop resources inside a handler instead of using `progress_hook` / `run_on_main`.
5. Expecting `shutdown()` to wait for `run_on_main` work — it does not, and it warns when it leaves some behind. The handler returns as soon as it hands the work over, so the job is already complete and `shutdown()` finds nothing running; in FastAPI the loop then closes and cancels the work. Use `await scheduler.ashutdown()` in an async shutdown path instead (a lifespan always is one). The sync version cannot wait: it runs on the loop's own thread, so blocking stalls the thread the work needs.
6. `timezone=` only affects log formatting — scheduling and persistence are always UTC.
7. Calling `run_task_immediately()` on a `running` or `paused` task raises `TaskNotActiveError` — resume paused tasks with `resume_task()` instead.
