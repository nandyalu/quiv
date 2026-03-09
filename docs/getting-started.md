# Getting Started

This guide gets `quiv` running with recurring tasks, progress callbacks,
and clean shutdown behavior.

## Install

=== "uv"

    ```bash
    uv add quiv
    ```

=== "pip"

    ```bash
    pip install quiv
    ```

For local development:

=== "uv"

    ```bash
    git clone https://github.com/nandyalu/quiv.git
    cd quiv
    uv pip install -e ".[dev]"
    ```

=== "pip"

    ```bash
    git clone https://github.com/nandyalu/quiv.git
    cd quiv
    pip install -e ".[dev]"
    ```

## 1) Create a scheduler

You can configure `Quiv` with either a `QuivConfig` object or direct args.

```python
from quiv import Quiv, QuivConfig

scheduler = Quiv(
    config=QuivConfig(
        pool_size=8,                    # default is 10
        history_retention_seconds=3600, # default is 86400 (1 day)
        timezone="UTC",                 # default is UTC
    )
)
```

Equivalent direct parameters:

```python
from quiv import Quiv

scheduler = Quiv(
    pool_size=8,                    # default is 10
    history_retention_seconds=3600, # default is 86400 (1 day)
    timezone="UTC",            # default is UTC
)
```

Do not mix `config=...` with direct constructor config args. See [Quiv API](./api.md#quiv) for full configuration options.

## 2) Add a task

### Sync handler

```python
def my_task(
    _stop_event: threading.Event | None=None,
    _progress_hook: Callable | None=None
):
    total = 5
    for step in range(1, total + 1):
        # <do some task work here>
        if _progress_hook:
            _progress_hook(step=step, total=total)
        if _stop_event and _stop_event.is_set():
            return

task_id = scheduler.add_task(
    task_name="demo-task",
    func=my_task,
    interval=10,
    delay=0,
    run_once=False,
    args=[],
    kwargs={},
)
```

### Async handler

Async handlers are fully supported. They run in thread-local event loops
created per invocation, so they do not block the scheduler or main loop.

```python
import httpx

async def poll_api(
    _stop_event: threading.Event | None=None,
    _progress_hook: Callable | None=None
):
    async with httpx.AsyncClient() as client:
        # example of doing some async work
        response = await client.get("https://api.example.com/status")
        if _progress_hook:
            _progress_hook(status_code=response.status_code)
        if _stop_event and _stop_event.is_set():
            return

scheduler.add_task(
    task_name="api-poll",
    func=poll_api,
    interval=30,
)
```

`_stop_event` and `_progress_hook` are injected only if your handler accepts
those keyword parameters. If your handler signature does not include them
(and does not use `**kwargs`), they are not injected. See
[Progress Callbacks](progress-callbacks.md) and [Cancellation](cancellation.md)
for in-depth guides.

## 3) Add progress callback (optional)

Progress callbacks can be sync or async. When an asyncio event loop is
available, async callbacks run via `run_coroutine_threadsafe` and sync
callbacks run via `call_soon_threadsafe` on the main loop. If no event loop
is available (e.g. in a plain script without asyncio), sync callbacks run
directly on the worker thread and async callbacks are skipped with a warning.

```python
async def on_progress(**payload):
    print("progress", payload)

scheduler.add_task(
    task_name="demo-task-with-progress",
    func=my_task,
    interval=10,
    progress_callback=on_progress,
)
```

## 4) Start and stop

```python
import asyncio

async def main() -> None:
    scheduler.start()
    await asyncio.sleep(25)
    scheduler.shutdown()

asyncio.run(main())
```

Always call `shutdown()` when your app exits.

## 5) Operate tasks at runtime

```python
scheduler.run_task_immediately("demo-task")
scheduler.pause_task("demo-task")
scheduler.resume_task("demo-task")
```

## 6) Cancel a running job

```python
jobs = scheduler.get_all_jobs(status="running")
for job in jobs:
    scheduler.cancel_job(job.id)
```

Cancellation is cooperative: it sets the job's stop event. The handler must
check `_stop_event.is_set()` to actually stop.

## 7) Inspect state

```python
tasks = scheduler.get_all_tasks(include_run_once=True)
jobs = scheduler.get_all_jobs()
failed_jobs = scheduler.get_all_jobs(status="failed")
```

## FastAPI integration example

`quiv` is intended for app-integrated task scheduling, especially in FastAPI.
Use the `lifespan` context manager to tie scheduler lifecycle to the app:

```python
from contextlib import asynccontextmanager

from fastapi import FastAPI

from quiv import Quiv

scheduler = Quiv(timezone="UTC")


def reindex_documents(_stop_event=None, _progress_hook=None) -> None:
    total = 100
    for step in range(1, total + 1):
        if _stop_event and _stop_event.is_set():
            return

        # Simulate blocking work
        import time
        time.sleep(0.05)

        if _progress_hook:
            _progress_hook(step=step, total=total, stage="reindex")


async def on_reindex_progress(**payload) -> None:
    # Replace with websocket broadcast, logging, metrics, etc.
    print("progress", payload)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    scheduler.add_task(
        task_name="reindex-docs",
        func=reindex_documents,
        interval=300,
        progress_callback=on_reindex_progress,
    )
    scheduler.start()
    yield
    # Shutdown
    scheduler.shutdown()


app = FastAPI(lifespan=lifespan)
```

Why this matters:

- `_stop_event` makes long tasks cancel safely on shutdown.
- `_progress_hook` sends task progress back into FastAPI's async context.
- Scheduler lifecycle is tied cleanly to app lifecycle.

## Logging

`quiv` uses Python's standard `logging` module. If you do not configure
logging, no output is produced (Python's default `NullHandler` behavior).

To see scheduler logs, configure the `"Quiv"` logger:

```python
import logging

logging.basicConfig(level=logging.INFO)
```

Or configure the `"Quiv"` logger directly for more control:

```python
import logging

quiv_logger = logging.getLogger("Quiv")
quiv_logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
quiv_logger.addHandler(handler)
```

You can also inject your own logger instance:

```python
import logging

my_logger = logging.getLogger("myapp.scheduler")
scheduler = Quiv(logger=my_logger)
```

The library logs at these levels:

| Level   | What is logged                                               |
|---------|--------------------------------------------------------------|
| DEBUG   | Database table creation, datetime normalization              |
| INFO    | Task added, scheduler loop start, job start/completion, cleanup |
| WARNING | Progress callback skipped (no event loop or main loop closed) |
| ERROR   | Job failures, scheduler loop errors, progress callback errors |

A separate `"quiv.models"` logger emits DEBUG-level messages for datetime
normalization. This logger is not configurable via the constructor and follows
standard Python logging configuration.

## Troubleshooting

- **`ConfigurationError` on startup**: check `pool_size > 0` and
  `history_retention_seconds >= 0`.
- **`InvalidTimezoneError`**: use a valid IANA timezone name (for example
  `UTC` or `America/New_York`).
- **`HandlerNotRegisteredError` for immediate run**: call `add_task(...)`
  first.
- **`TaskNotScheduledError`**: the task handler exists, but no scheduled task
  row exists yet.
- **No log output**: configure Python logging (see [Logging](#logging) above).
- **Args/kwargs errors**: ensure all values passed via `args` and `kwargs` are
  JSON-serializable (no custom objects, datetime instances, etc.).
