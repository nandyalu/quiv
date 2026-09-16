# Getting Started

This guide starts `quiv`, adds a recurring task, reports progress, and shuts the scheduler down cleanly.

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

Configure `Quiv` in one of two ways: pass a `QuivConfig` object, or pass the values one by one.

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

Do not pass `config=` together with a separate configuration value. See [Quiv API](./api.md#quiv) for every option.

## 2) Add a task

!!! warning "Upgrading from 0.x: the injected parameters were renamed"

    quiv injects `job_id`, `stop_event`, and `progress_hook` into a handler that declares them. In `0.x` these names carried a leading underscore. `add_task()` rejects a handler that still declares `_job_id`, `_stop_event`, or `_progress_hook`, and names the new spelling in the error. See the [v1.0.0 release notes](release-notes.md#v1.0.0).

### Sync handler

```python
def my_task(
    job_id: str | None = None,
    stop_event: threading.Event | None = None,
    progress_hook: Callable | None = None,
):
    total = 5
    for step in range(1, total + 1):
        # <do some task work here>
        if progress_hook:
            progress_hook(step=step, total=total)
        if stop_event and stop_event.is_set():
            return

task_id = scheduler.add_task(
    task_name="demo-task",
    func=my_task,
    interval=10,
    delay=0,
    run_once=False,
    args=(),
    kwargs={},
)
```

### Async handler

quiv accepts an async handler. Each invocation creates its own event loop on the worker thread, so an async handler blocks neither the scheduler nor the main loop.

```python
import httpx

async def poll_api(
    stop_event: threading.Event | None = None,
    progress_hook: Callable | None = None,
):
    async with httpx.AsyncClient() as client:
        # example of doing some async work
        response = await client.get("https://api.example.com/status")
        if progress_hook:
            progress_hook(status_code=response.status_code)
        if stop_event and stop_event.is_set():
            return

scheduler.add_task(
    task_name="api-poll",
    func=poll_api,
    interval=30,
)
```

quiv injects `job_id`, `stop_event`, and `progress_hook` only when your handler declares them as keyword parameters. If the signature has none of them, and has no `**kwargs`, quiv injects nothing. Read [Progress Callbacks](progress-callbacks.md) and [Cancellation](cancellation.md) for the details.

!!! tip "Keep the `task_id`"
    `add_task()` returns a `task_id`, a UUID string. Every later operation uses it: `pause_task()`, `resume_task()`, `run_task_immediately()`, `remove_task()`, and `get_task()`. Several tasks can share one `task_name`, and each of them gets its own `task_id`.

## 3) Add progress callback (optional)

A progress callback can be sync or async. With an asyncio event loop available, quiv sends an async callback to the main loop with `run_coroutine_threadsafe`, and a sync callback with `call_soon_threadsafe`. Without an event loop, in a plain script for example, a sync callback runs on the worker thread, and an async callback runs in a temporary event loop on that thread.

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

## 4) Listen for events (optional)

An event listener lets your code react to what happens to a task and to a job. Register a callback with `add_listener()`:

```python
from quiv import Event
from quiv.models import Task, Job

def on_job_completed(event: Event, task: Task, job: Job):
    print(f"Job {job.id} for '{task.task_name}' completed in {job.duration_seconds}s")

def on_job_failed(event: Event, task: Task, job: Job):
    print(f"Job {job.id} for '{task.task_name}' failed: {job.error_message}")

scheduler.add_listener(Event.JOB_COMPLETED, on_job_completed)
scheduler.add_listener(Event.JOB_FAILED, on_job_failed)
```

!!! info "Typed callbacks"
    A `TASK_*` listener receives `(event, task)`. A `JOB_*` listener receives `(event, task, job)`. Both receive typed model objects, so your editor completes every field and you never look up a key in a dictionary.

A listener follows the same dispatch model as a progress callback. An async listener runs on the main loop. A sync listener runs through `call_soon_threadsafe`, or on the calling thread when no loop is available. If a listener raises, quiv writes the error to the log and continues. See [Event Listeners](event-listeners.md) for every event and for the dispatch rules.

## 5) Start and stop

```python
import asyncio

async def main() -> None:
    scheduler.start()
    await asyncio.sleep(25)
    scheduler.shutdown()

asyncio.run(main())
```

Always call `shutdown()` when your app exits.

`start()` and `shutdown()` are the canonical names, and the documentation uses them everywhere. `startup()` is an alias of `start()`, and `stop()` is an alias of `shutdown()`. Both aliases keep working.

## 6) Operate tasks at runtime

```python
task_id = scheduler.add_task(
    task_name="demo-task",
    func=my_task,
    interval=10,
)

scheduler.run_task_immediately(task_id)
scheduler.pause_task(task_id)
scheduler.resume_task(task_id)
```

## 7) Cancel a running job

```python
jobs = scheduler.get_all_jobs(status="running")
for job in jobs:
    scheduler.cancel_job(job.id)
```

Cancellation is cooperative. `cancel_job()` sets the stop event of the job. The handler must check `stop_event.is_set()` and return.

## 8) Inspect state

```python
tasks = scheduler.get_all_tasks(include_run_once=True)
jobs = scheduler.get_all_jobs()
failed_jobs = scheduler.get_all_jobs(status="failed")
```

## FastAPI integration example

`quiv` is built to schedule tasks inside an application, and above all inside FastAPI. Use the `lifespan` context manager, so that the scheduler starts and stops with the application:

```python
from contextlib import asynccontextmanager

from fastapi import FastAPI

from quiv import Quiv

scheduler = Quiv(timezone="UTC")


def reindex_documents(stop_event=None, progress_hook=None) -> None:
    total = 100
    for step in range(1, total + 1):
        if stop_event and stop_event.is_set():
            return

        # Simulate blocking work
        import time
        time.sleep(0.05)

        if progress_hook:
            progress_hook(step=step, total=total, stage="reindex")


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

- `stop_event` lets a long task stop safely at shutdown.
- `progress_hook` carries the progress of a task into the async context of FastAPI.
- The scheduler starts and stops with the application, in one place.

## Logging

`quiv` uses the standard `logging` module of Python. If you configure no logging, quiv writes nothing, which is the default behavior of `NullHandler`.

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

A second logger, `"quiv.models"`, writes DEBUG messages about datetime conversion. The constructor cannot configure this logger. Configure it the way you configure any other Python logger.

## Troubleshooting

- **`ConfigurationError` on startup**: check `pool_size > 0` and `history_retention_seconds >= 0`.
- **`InvalidTimezoneError`**: use a valid IANA timezone name (for example `UTC` or `America/New_York`).
- **`TaskNotFoundError` for immediate run**: the id is unknown. Call `add_task(...)` first and use the returned `task_id`. A run-once task removes itself after it runs, so its id stops resolving.
- **No log output**: configure Python logging (see [Logging](#logging) above).
- **An error about `args` or `kwargs`**: quiv serializes both with pickle, which accepts most Python objects. If you see an error, check that every object is picklable. A lambda and an inner function are not.
