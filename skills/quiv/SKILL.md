---
name: quiv
description: >
  Usage guide for quiv, the threadpool-backed background scheduler for
  Python/FastAPI apps. Use when adding, scheduling, cancelling, or debugging
  background/recurring tasks with quiv, or when the user mentions quiv,
  add_task, _stop_event, _progress_hook, run_on_main, or asks how to run
  periodic jobs in a FastAPI app that already depends on quiv.
---

# Working with quiv

quiv is a single-process, threadpool-backed scheduler: recurring or one-shot
tasks, sync **and** async handlers, cooperative cancellation, progress
callbacks on the main asyncio loop, per-job trace IDs. Python 3.10–3.14.

**Ground rules before writing code:**

- If quiv is installed, read the condensed reference shipped with it:
  `python -c "import quiv, pathlib; print(pathlib.Path(quiv.__file__).parent / 'AGENTS.md')"` —
  read that file if it exists (older quiv versions don't ship it).
- Full docs index (fetchable): https://nandyalu.github.io/quiv/llms.txt ·
  full text: https://nandyalu.github.io/quiv/llms-full.txt
- quiv is NOT Celery: no multi-process workers, no durable queues, no
  cron/calendar scheduling. Task state lives in a temp SQLite file deleted on
  `shutdown()` — nothing survives restarts; re-add tasks on startup.

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
    scheduler.shutdown()   # ALWAYS — cancels jobs, deletes temp DB

app = FastAPI(lifespan=lifespan)
```

`add_task(task_name, func, interval, delay=0, run_once=False,
fixed_interval=True, args=(), kwargs={}, progress_callback=None) -> task_id`.
The returned `task_id` (UUID string) keys everything: `pause_task`,
`resume_task`, `run_task_immediately`, `remove_task`, `get_task`.
`task_name` is a non-unique display label — never treat it as a key.
`args`/`kwargs` are pickled: no lambdas or inner functions.

## Handler injection

`_job_id: str`, `_stop_event: threading.Event`, and `_progress_hook: Callable`
are injected **only if the handler signature declares them**:

```python
def work(item_id: int, _stop_event=None, _progress_hook=None):
    for i, chunk in enumerate(chunks(item_id)):
        if _stop_event and _stop_event.is_set():
            return                      # cancellation is cooperative — this check is mandatory
        process(chunk)
        if _progress_hook:
            _progress_hook(step=i)      # forwarded to progress_callback on the main loop
```

Async handlers pass the same way; each invocation gets a fresh event loop on a
worker thread (never the main app loop — do not change this; isolation is a
design requirement). To touch main-loop resources from task code use
`from quiv import run_on_main; run_on_main(async_or_sync_fn, *args)`
(fire-and-forget, exceptions logged and swallowed).

## Observability

- Events: `scheduler.add_listener(Event.JOB_FAILED, cb)` — `TASK_*` callbacks
  get `(event, task)`, `JOB_*` get `(event, task, job)`; `job.error_message`
  and `job.duration_seconds` are set on finalization.
- Inspect: `get_all_tasks()`, `get_all_jobs(status="failed")` — return
  SQLModel objects safe to return from FastAPI endpoints (UTC-aware datetimes).
- Logging: quiv never configures logging; configure the `"Quiv"` logger to
  see scheduler output.

## Common mistakes to avoid

1. Forgetting `scheduler.shutdown()` (in tests: `finally:` block) — leaks the
   loop thread and temp DB file.
2. Passing both `config=QuivConfig(...)` and individual kwargs to `Quiv()` —
   raises `ConfigurationError`; pick one.
3. Expecting `cancel_job()`/`shutdown()` to kill threads — a handler that
   never checks `_stop_event` runs to completion.
4. Blocking on main-loop resources inside a handler instead of using
   `_progress_hook` / `run_on_main`.
5. `timezone=` only affects log formatting — scheduling and persistence are
   always UTC.
