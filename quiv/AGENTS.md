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
    interval=300,              # seconds; must be > 0. Omit when run_once=True —
                               # a run-once task never repeats, so any interval
                               # given is ignored and stored as None
    delay=None,                # seconds before first run; >= 0. Defaults to
                               # no delay. Mutually exclusive with run_at
    run_once=False,            # True = run once, then the task row is deleted
    fixed_interval=True,       # True: next run aligned to start-time cadence (missed slots skipped)
                               # False: next run = completion time + interval
    args=(),                   # tuple, pickle-serialized (no lambdas/inner functions)
    kwargs={},
    progress_callback=None,    # sync or async; runs on the main asyncio loop
    # -- keyword-only from here --
    run_at=None,               # absolute datetime for the first run, instead of
                               # delay; naive = UTC (never the display timezone);
                               # a time already past runs at once
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

- `job_id: str` — UUID of this run; stamp it into a `LoggerAdapter`/`ContextVar` for per-job log tracing.
- `stop_event: threading.Event` — check `stop_event.is_set()` at natural breakpoints and `return` early. This is the ONLY way cancellation/shutdown stops a handler; threads are never killed.
- `progress_hook: Callable` — call `progress_hook(**payload)` from the worker; quiv forwards the payload to the registered `progress_callback` on the main loop.

Renamed in v1.0.0: these were `_job_id`, `_stop_event`, `_progress_hook` in 0.x. `add_task()` raises `ConfigurationError` if a handler still declares an old name, and again if a key in `kwargs` collides with an injected name (the injected value would overwrite the caller's).

```python
def download(media_id: int, job_id=None, stop_event=None, progress_hook=None):
    for i, chunk in enumerate(stream_chunks(media_id)):
        if stop_event and stop_event.is_set():
            return  # cooperative exit
        write(chunk)
        if progress_hook:
            progress_hook(step=i, stage="download")
```

Async handlers are passed the same way (`func=my_async_handler`) — each invocation runs in a **fresh event loop on the worker thread**. Handlers never share the main app loop, so never touch main-loop-bound resources directly from a handler; use `progress_hook` or `run_on_main` (below).

## Reaching the main loop from task code

```python
from quiv import run_on_main

async def broadcast(payload: dict):
    await ws_manager.broadcast(payload)  # lives on the main loop

def deeply_nested_step():
    run_on_main(broadcast, {"event": "step_done"})  # fire-and-forget, exceptions logged+swallowed
```

Works from anywhere in a task's call stack (no parameter threading) and also from main-loop code (e.g. FastAPI routes). Raises `MainLoopUnavailableError` if no active Quiv instance or main loop can be resolved.

**quiv never waits for work handed over this way, and never cancels it.** The handler returns the instant it hands the work over, so the job is already complete and `shutdown()` finds nothing running — measured at ~12 ms while a 1 s coroutine was still queued. **The loop is the application's**: quiv is a guest on it, does not close it, and cannot know whether a half-finished callable should be stopped or allowed to end. `shutdown()` warns, naming how many callables it left. `scheduler.pending_main_loop_work() -> int` reports the same count at any time — poll it in the lifespan before letting the loop close, and bound that wait however the app wants. Cheap to poll: one set under a lock, no database, unlike `stats()`. It counts a queued sync callable too, which has no future of its own.

**`call_on_main(func, *args, **kwargs) -> Any` waits.** Same dispatch, but the result comes back, the target's exception reaches the handler (the job fails, `JOB_FAILED` fires, retries apply), and the job's stop event cancels the coroutine and raises `JobCancelledError` — let it propagate, quiv finalizes the job as `cancelled` with no error logged. Use it when a handler's whole body is a hop to the main loop; with `run_on_main` such a job records a 1 ms success whatever the work did, and the task `timeout` can never fire. Every keyword goes to the target (no options of its own). From the main loop's own thread a sync target runs inline; an async target raises `MainLoopUnavailableError` — await it there.

**`run_subprocess(args, *, input=None, stop_event=None, timeout=None, kill_grace=5.0, check=False, capture_output=False, **popen_kwargs)`** — a drop-in for `subprocess.run` inside a handler that a cancel can stop. It watches the job's stop event (found through the job context, no parameter needed at any depth); on stop: `terminate()`, `kill()` after `kill_grace`, then `JobCancelledError`. `timeout` raises `subprocess.TimeoutExpired` as the stdlib does. Stops the direct child only — `start_new_session=True` for a process group. Pass `stop_event=` only on a thread you started yourself.

**Waiting for a job.** `wait_for_job(job_id, timeout)` / `await_job(...)` and `wait_for_task(task_id, timeout)` / `await_task(...)` return the finalized `Job` (`status`, `duration_seconds`, `error_message`). `wait_for_task` returns the next job of the task to finish — the way to wait on a one-off you just added from an endpoint. Timeouts raise the builtin `TimeoutError`; after `shutdown()` they raise `SchedulerStoppedError`; `remove_task` on a task with nothing running fails its waiters with `TaskNotFoundError`. This is how to test handlers against a real `Quiv()` instead of stubbing `add_task`.

## Event listeners

```python
from quiv import Event

def on_job_failed(event, task, job):   # JOB_* -> (event, task, job); TASK_* -> (event, task)
    alert(f"{task.task_name} failed: {job.error_message}")

scheduler.add_listener(Event.JOB_FAILED, on_job_failed)
```

Events: `TASK_ADDED`, `TASK_REMOVED`, `TASK_PAUSED`, `TASK_RESUMED`, `TASK_UPDATED`, `JOB_STARTED`, `JOB_COMPLETED`, `JOB_FAILED`, `JOB_CANCELLED`, `JOB_RETRYING` (fires after `JOB_FAILED` when a retry was scheduled). Sync or async callbacks; exceptions in listeners are logged and swallowed.

## Management & observability

- `update_task(task_id, *, task_name=..., interval=..., fixed_interval=..., args=..., kwargs=..., timeout=..., max_retries=..., retry_backoff=..., jitter=..., progress_callback=..., run_at=...)` mutates a task in place (keyword-only; omit what you don't change; `timeout=None` disables, `progress_callback=None` clears). Changing `interval` reschedules to `now + interval`. `run_at` names the next run's absolute time instead — naive is UTC, a past time runs at once, and it is mutually exclusive with `interval`. It moves the next scheduled run of any task, run before or not: a recurring task keeps its interval and just runs next at that time. Use it to move a pending one-off rather than `remove_task` + `add_task`, which changes the `task_id` and leaves a gap with nothing scheduled. **It does not survive a task that is already `running`**: finalization deletes a run-once row and recomputes `next_run_at` for a recurring one, so the new time is discarded when the job ends (quiv logs a warning, best-effort). Move a recurring task after its job finishes, or change `interval`, which finalization honours. Not updatable: `run_once`, `delay`, `func`.
- `get_all_jobs(status=None, task_id=None, since=None, until=None, order_by="started_at", descending=True, limit=None, offset=0)` — filters + pagination; `order_by` is `"started_at"` or `"ended_at"` only. `get_all_tasks(include_run_once=False, status=None, limit=None, offset=0)` orders by `next_run_at`.
- `stats() -> QuivStats` (frozen dataclass, exported from `quiv`): `active_jobs`, `pool_size`, `pool_utilization`, `tasks_by_status`, `next_run_at`, `job_history_count`. Serialize with `dataclasses.asdict()`.

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
4. **Expecting hard kills** — `cancel_job()`/`remove_task()`/`shutdown()` only set the stop event. A handler that never checks `stop_event` runs to completion. A child process needs `run_subprocess()` to be stopped; `subprocess.run` never sees the event.
5. **Blocking the main loop from a handler** — handlers run on worker threads with their own event loops. Use `progress_hook`/`run_on_main` to hop back, or `call_on_main` when the job should span the work.
6. **Catching `Exception` around `call_on_main`/`run_subprocess`** — that swallows `JobCancelledError` and the job keeps running after a cancel. Let it propagate.
7. **`config=` plus kwargs** — passing both to `Quiv()` raises `ConfigurationError`.
8. **Treating `task_name` as a key** — it is a label; duplicates are allowed. Only `task_id` identifies a task.
9. **Pool exhaustion** — when `pool_size` jobs are running, due tasks are deferred; they dispatch as soon as a job finishes and frees a slot (a warning logs the delay). Raise `pool_size` for I/O-bound overlap; for CPU-bound work use a process pool inside the handler.
10. **No log output** — quiv never configures logging. Configure the `"Quiv"` logger (or pass `logger=`) to see scheduler logs.
11. **Stubbing `add_task` in tests** — a stub proves nothing about the schedule, cancellation, or the events. Use a real `Quiv()` and `wait_for_task(task_id, timeout=...)`.

## Exceptions

All inherit `QuivError`: `ConfigurationError`, `InvalidTimezoneError`, `DatabaseInitializationError`, `HandlerRegistrationError`, `HandlerNotRegisteredError`, `TaskNotActiveError`, `TaskNotFoundError`, `JobNotFoundError`, `JobCancelledError` (raised by `call_on_main()`/`run_subprocess()` when the job's stop event fires while they wait — let it propagate), `SchedulerStoppedError` (the wait methods, after or during `shutdown()`), `MainLoopUnavailableError` (raised by `run_on_main()`/`call_on_main()`; also inherits `RuntimeError`). `TaskNotScheduledError` was removed in v1.0.0 — catch `TaskNotFoundError`.

`run_task_immediately()` raises `TaskNotActiveError` for `running` tasks (no concurrent second run) and `paused` tasks (use `resume_task()` instead).
