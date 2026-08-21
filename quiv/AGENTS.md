# quiv — guide for AI coding agents

This file ships inside the `quiv` package so AI tools can learn the library without leaving the project. Full docs: https://nandyalu.github.io/quiv (machine-readable index: https://nandyalu.github.io/quiv/llms.txt).

## What quiv is (and is not)

quiv is a single-process, threadpool-backed background scheduler for Python apps — "more than FastAPI `BackgroundTasks`, less than Celery". Recurring or one-shot tasks, sync **and** async handlers, cooperative cancellation, progress callbacks dispatched to the main asyncio loop, per-job trace IDs. Python 3.10–3.14.

Do NOT reach for quiv when the app needs: multi-process/distributed workers, durable queues that survive restarts, or cron/calendar scheduling — use Celery/arq/APScheduler for those. Task state lives in a **temporary SQLite file that is deleted on `shutdown()`**; nothing persists across restarts.

## Install

`pip install quiv` / `uv add quiv`. Runtime deps: `sqlmodel`, `tzdata`.

## Core API

```python
from quiv import Quiv

scheduler = Quiv(
    pool_size=10,                    # max concurrent jobs (threads, not processes)
    history_retention_seconds=86400, # how long finished job rows are kept
    timezone="UTC",                  # DISPLAY ONLY (log formatting); internals are always UTC
)
# Alternative: Quiv(config=QuivConfig(...)) — never mix config= with the kwargs above.

task_id = scheduler.add_task(
    task_name="sync-library",  # display label; duplicates allowed
    func=my_handler,           # sync or async callable
    interval=300,              # seconds; must be > 0
    delay=0,                   # seconds before first run; >= 0
    run_once=False,            # True = run once, then the task row is deleted
    fixed_interval=True,       # True: next run aligned to start-time cadence (missed slots skipped)
                               # False: next run = completion time + interval
    args=(),                   # tuple, pickle-serialized (no lambdas/inner functions)
    kwargs={},
    progress_callback=None,    # sync or async; runs on the main asyncio loop
    # -- keyword-only from here --
    timeout=None,              # seconds; cooperative — sets the job's stop event,
                               # job finalizes as cancelled with a timeout message
    max_retries=0,             # retries for FAILED jobs only (cancelled never retries)
    retry_backoff=30.0,        # base seconds; exponential: 1x, 2x, 4x, ...
    jitter=0.0,                # adds uniform(0, jitter)s to recurring next-run times
)

scheduler.start()      # alias: startup(). Safe to call multiple times.
scheduler.shutdown()   # alias: stop(). ALWAYS call on app exit — cancels jobs,
                       # disposes the engine, deletes the temp SQLite file.
                       # shutdown(timeout=5.0) bounds the wait; jobs that do
                       # not exit in time are abandoned with a warning.
```

`add_task()` returns a `task_id` (UUID string) — **hold onto it**; it is the key for every runtime operation:

| Method | Notes |
|---|---|
| `run_task_immediately(task_id) -> int` | queue a scheduled task now |
| `pause_task(task_id)` / `resume_task(task_id, delay=0)` | resume with `delay=0` fires immediately |
| `remove_task(task_id)` | unregisters handler + callback; signals a running job to stop |
| `get_task(task_id) -> Task` / `get_all_tasks(include_run_once=False) -> list[Task]` | |
| `get_job(job_id) -> Job` / `get_all_jobs(status=None) -> list[Job]` | status: `"running"`, `"failed"`, ... |
| `cancel_job(job_id) -> bool` | cooperative — sets the job's stop event |
| `add_listener(event, cb)` / `remove_listener(event, cb)` | lifecycle events, see below |

`Task` and `Job` are SQLModel objects safe to return directly from FastAPI endpoints (datetimes are UTC-aware). Task statuses: `active`, `running`, `paused`. Job statuses: `scheduled`, `running`, `completed`, `cancelled`, `failed`. `Job` carries `duration_seconds` and `error_message`.

## Handler injection (signature-based)

quiv inspects the handler signature and injects these kwargs **only if the handler declares them** (or takes `**kwargs`):

- `_job_id: str` — UUID of this run; stamp it into a `LoggerAdapter`/`ContextVar` for per-job log tracing.
- `_stop_event: threading.Event` — check `_stop_event.is_set()` at natural breakpoints and `return` early. This is the ONLY way cancellation/shutdown stops a handler; threads are never killed.
- `_progress_hook: Callable` — call `_progress_hook(**payload)` from the worker; quiv forwards the payload to the registered `progress_callback` on the main loop.

```python
def download(media_id: int, _job_id=None, _stop_event=None, _progress_hook=None):
    for i, chunk in enumerate(stream_chunks(media_id)):
        if _stop_event and _stop_event.is_set():
            return  # cooperative exit
        write(chunk)
        if _progress_hook:
            _progress_hook(step=i, stage="download")
```

Async handlers are passed the same way (`func=my_async_handler`) — each invocation runs in a **fresh event loop on the worker thread**. Handlers never share the main app loop, so never touch main-loop-bound resources directly from a handler; use `_progress_hook` or `run_on_main` (below).

## Reaching the main loop from task code

```python
from quiv import run_on_main

async def broadcast(payload: dict):
    await ws_manager.broadcast(payload)  # lives on the main loop

def deeply_nested_step():
    run_on_main(broadcast, {"event": "step_done"})  # fire-and-forget, exceptions logged+swallowed
```

Works from anywhere in a task's call stack (no parameter threading) and also from main-loop code (e.g. FastAPI routes). Raises `RuntimeError` if no active Quiv instance / main loop is resolvable.

## Event listeners

```python
from quiv import Event

def on_job_failed(event, task, job):   # JOB_* -> (event, task, job); TASK_* -> (event, task)
    alert(f"{task.task_name} failed: {job.error_message}")

scheduler.add_listener(Event.JOB_FAILED, on_job_failed)
```

Events: `TASK_ADDED`, `TASK_REMOVED`, `TASK_PAUSED`, `TASK_RESUMED`, `JOB_STARTED`, `JOB_COMPLETED`, `JOB_FAILED`, `JOB_CANCELLED`, `JOB_RETRYING` (fires after `JOB_FAILED` when a retry was scheduled). Sync or async callbacks; exceptions in listeners are logged and swallowed.

## Canonical FastAPI wiring

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
from quiv import Quiv

scheduler = Quiv()  # module level is fine — the main loop is resolved lazily

@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler.add_task(task_name="reindex", func=reindex, interval=300)
    scheduler.start()
    yield
    scheduler.shutdown()

app = FastAPI(lifespan=lifespan)
```

## Pitfalls agents commonly hit

1. **Forgetting `shutdown()`** — leaks the loop thread and the temp SQLite file. In tests, call it in a `finally:` block.
2. **Expecting persistence** — the DB is temporary by design; re-`add_task` on every startup.
3. **Unpicklable `args`/`kwargs`** — lambdas, inner functions, open handles fail pickle serialization. Pass plain data; make `func` a module-level callable.
4. **Expecting hard kills** — `cancel_job()`/`remove_task()`/`shutdown()` only set the stop event. A handler that never checks `_stop_event` runs to completion.
5. **Blocking the main loop from a handler** — handlers run on worker threads with their own event loops. Use `_progress_hook`/`run_on_main` to hop back.
6. **`config=` plus kwargs** — passing both to `Quiv()` raises `ConfigurationError`.
7. **Treating `task_name` as a key** — it is a label; duplicates are allowed. Only `task_id` identifies a task.
8. **Pool exhaustion** — when `pool_size` jobs are running, due tasks are deferred; they dispatch as soon as a job finishes and frees a slot (a warning logs the delay). Raise `pool_size` for I/O-bound overlap; for CPU-bound work use a process pool inside the handler.
9. **No log output** — quiv never configures logging. Configure the `"Quiv"` logger (or pass `logger=`) to see scheduler logs.

## Exceptions

All inherit `QuivError`: `ConfigurationError`, `InvalidTimezoneError`, `DatabaseInitializationError`, `HandlerRegistrationError`, `HandlerNotRegisteredError`, `TaskNotScheduledError`, `TaskNotActiveError`, `TaskNotFoundError`, `JobNotFoundError`.

`run_task_immediately()` raises `TaskNotActiveError` for `running` tasks (no concurrent second run) and `paused` tasks (use `resume_task()` instead).
