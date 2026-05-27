# 

![quiv Logo](https://raw.githubusercontent.com/nandyalu/quiv/main/assets/quiv-logo-text-full-minified.png)

<hr>


<p align="center">
  <a href="https://www.python.org/" target="_blank"><img src="https://img.shields.io/badge/python-3.10|3.11|3.12|3.13|3.14-3670A0?style=flat&logo=python" alt="Python"></a>
  <a href="https://github.com/psf/black" target="_blank"><img src="https://img.shields.io/badge/code%20style-black-000000.svg" alt="Code style: black"></a>
  <a href="https://github.com/nandyalu/quiv?tab=MIT-1-ov-file" target="_blank"><img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="License: MIT"></a>
  <a href="https://pypi.org/project/quiv/" target="_blank"><img src="https://img.shields.io/pypi/dm/quiv" alt="PyPI Pulls"></a>
</p>

<p align="center">
  <a href="https://github.com/nandyalu/quiv/actions/workflows/build.yml" target="_blank"><img src="https://github.com/nandyalu/quiv/actions/workflows/build.yml/badge.svg" alt="Build"></a>
  <a href="https://github.com/nandyalu/quiv/actions/workflows/tests.yml" target="_blank"><img src="https://github.com/nandyalu/quiv/actions/workflows/tests.yml/badge.svg" alt="Tests"></a>
  <a href="https://github.com/nandyalu/quiv/actions/workflows/typecheck.yml" target="_blank"><img src="https://github.com/nandyalu/quiv/actions/workflows/typecheck.yml/badge.svg" alt="Type Check"></a>
  <a href="https://github.com/nandyalu/quiv/issues" target="_blank"><img src="https://img.shields.io/github/issues/nandyalu/quiv?logo=github" alt="GitHub Issues"></a>
  <a href="https://github.com/nandyalu/quiv/commits/" target="_blank"><img src="https://img.shields.io/github/last-commit/nandyalu/quiv?logo=github" alt="GitHub last commit"></a>
</p>

Background tasks for FastAPI apps that need more than `BackgroundTasks` and less than Celery.

If you've reached for APScheduler inside a FastAPI app, you've probably hit one of these:

- A task is running too long and the user wants to cancel it — but there's no clean way to signal the worker mid-run.
- A background job needs to push progress to a websocket, and you're writing `run_coroutine_threadsafe` glue to hop back onto the main loop.
- You want a job id stamped on every log line for one specific run, and you're threading it through call sites by hand.
- You have a complete async pipeline you want to run in the background, and you're wrapping it in `asyncio.run` just to hand it to a sync-only scheduler.

`quiv` was built inside [Trailarr](https://github.com/nandyalu/trailarr) — a FastAPI app that outgrew APScheduler for exactly these reasons. It's a single-process, threadpool-backed scheduler with first-class support for cooperative cancellation (`_stop_event`), main-loop progress callbacks (`_progress_hook`), and per-job tracing (`_job_id`).

It is not a Celery replacement. If you need multi-process workers, durable queues, or distributed execution, use Celery or arq. `quiv` is for the in-process case those tools are overkill for.

Supports Python 3.10 through 3.14.

## Install

=== "uv"

    ```bash
    uv add quiv
    ```

=== "pip"

    ```bash
    pip install quiv
    ```

## Quick example

A full FastAPI integration — lifespan startup, an endpoint that schedules work, and progress streaming back to the main loop:

```python
from contextlib import asynccontextmanager

from fastapi import FastAPI

from quiv import Quiv

# Create the Quiv scheduler
scheduler = Quiv(timezone="UTC")

# Wire it up in FastAPI's lifespan so that it starts and dies with your app
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    scheduler.start()
    yield
    # Shutdown
    scheduler.shutdown()

# Create FastAPI app
app = FastAPI(lifespan=lifespan)

# Create a test function that we can later schedule to broadcast progress
# sync/async - doesn't matter; quiv handles them all
def ping(_progress_hook=None):
    for i in range(30):
        # do some work
        if _progress_hook:
            _progress_hook(message="ping", progress=i, total=30)

# Now the actual progress callback function that we want to run on FastAPI thread
async def on_progress(**payload):
    # Replace with websocket broadcast, logging, metrics, etc.
    print("progress", payload)

# Create the endpoint function that will schedule the task when triggered
@app.post("/start-heartbeat")
def start_heartbeat():
    task_id = scheduler.add_task(
        task_name="heartbeat",
        func=ping,
        interval=30,
        progress_callback=on_progress,
    )
    return {"task_id": task_id}
```

## What you actually get

### Run async handlers natively, no `asyncio.run` wrapper

APScheduler is sync-only — async pipelines have to be wrapped in `asyncio.run(...)` for every invocation, which spins up and tears down an event loop each time. `quiv` accepts async handlers directly; they run in a thread-local event loop managed by the scheduler. Sync and async handlers coexist in the same scheduler.

```python
async def fetch_updates(_stop_event=None):
    await some_async_api_call()

scheduler.add_task(task_name="fetch", func=fetch_updates, interval=60)
```

### Cancel a running task from an HTTP endpoint

`_stop_event` is a per-job `threading.Event` injected into your handler. Check it at natural breakpoints and exit early when an endpoint calls `scheduler.cancel_job(job_id)` — no thread killing, no exceptions raised across thread boundaries.

```python
def download(media_id: int, _stop_event=None):
    for chunk in stream_chunks(media_id):
        if _stop_event and _stop_event.is_set():
            return  # cooperative exit
        write(chunk)
```

### Stream progress to a websocket without the `run_coroutine_threadsafe` dance

Your handler calls `_progress_hook(**payload)` from inside the threadpool. `quiv` dispatches your registered async callback on the main asyncio loop — where it can broadcast over a websocket, update app state, or push to a metrics client.

```python
async def on_progress(**payload):
    await websocket_manager.broadcast(payload)  # runs on the main loop

scheduler.add_task(
    task_name="download",
    func=download,
    progress_callback=on_progress,
    run_once=True,
)
```

### Correlate logs for one job, across threads

Every invocation gets a `_job_id` (UUID). Stamp it into a context var or a `LoggerAdapter` and every log line from that run carries the same trace id — filtering logs by a single job is one query, even when N tasks run concurrently.

```python
@with_logging_context  # stores _job_id as trace_id for this run
def download_trailer(media_id: int, _job_id: str | None = None, _stop_event=None):
    logger.info("Starting download for media %s", media_id)
    # every log line below carries the same trace_id
```

This is the pattern Trailarr uses in production today.

## Concepts

- **Task**: scheduling definition (`interval`, `run_once`, args/kwargs, status)
- **Job**: one execution record of a task
- **Task statuses**: `active`, `running`, `paused`
- **Job statuses**: `scheduled`, `running`, `completed`, `cancelled`, `failed`

## Important caveats

- **Temporary database**: each `Quiv` instance creates a temporary SQLite file
  that is deleted on `shutdown()`. Task/job state does not persist across
  restarts.
- **Single-process**: the scheduler runs in-process. It is not designed for
  distributed or multi-process deployments.
- **Picklable args**: `args` and `kwargs` passed to `add_task()` are
  pickle-serialized for persistence. Most Python objects are supported,
  but lambdas and inner functions are not picklable. The temporary SQLite
  database is trusted internal state — only your application code writes to
  it, and it is deleted on `shutdown()`. Do not expose the database file to
  untrusted input.


## Next pages

- [Getting Started](getting-started.md)
- [API](api.md)
- [Architecture](architecture.md)
- [Event Listeners](event-listeners.md)
- [Exceptions](exceptions.md)
- [Testing](testing.md)
