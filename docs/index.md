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

`quiv` is a lightweight background task scheduler for Python applications.

It is designed to work especially well with FastAPI apps that need predictable,
in-process background task orchestration.

Supports Python 3.10 through 3.14.

It provides:

- threadpool-backed execution
- support for sync and async task handlers
- cooperative cancellation (`_stop_event`)
- progress callbacks routed to your main async loop (`_progress_hook`)
- event listeners for task and job lifecycle events
- persistent task/job state via SQLModel + SQLite

## When to use quiv

Use `quiv` when you need in-process background scheduling for app-level jobs,
for example:

- polling APIs every N seconds
- periodic cleanup tasks
- one-shot delayed jobs
- progress-aware long-running workloads

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

```python
from contextlib import asynccontextmanager

from fastapi import FastAPI

from quiv import Quiv

scheduler = Quiv(timezone="UTC")


def ping(_progress_hook=None):
    for i in range(30):
        # do some work
        if _progress_hook:
            _progress_hook(message="ping", progress=i, total=30)


async def on_progress(**payload):
    # Replace with websocket broadcast, logging, metrics, etc.
    print("progress", payload)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    scheduler.start()
    yield
    # Shutdown
    scheduler.shutdown()


app = FastAPI(lifespan=lifespan)

@app.post("/start-heartbeat")
def start_heartbeat():
    scheduler.add_task(
        task_name="heartbeat",
        func=ping,
        interval=30,
        progress_callback=on_progress,
    )
    return {"message": "Heartbeat started successfully!"}
```

Async handlers work the same way:

```python
async def fetch_updates(_stop_event=None):
    # async handlers run in thread-local event loops
    await some_async_api_call()

scheduler.add_task(task_name="fetch", func=fetch_updates, interval=60)
```

## FastAPI usage

For a full FastAPI integration example (startup/shutdown lifecycle plus
`_stop_event` and `_progress_hook`), see the FastAPI section in
[Getting Started](getting-started.md).

## Concepts

- **Task**: scheduling definition (`interval`, `run_once`, args/kwargs, status)
- **Job**: one execution record of a task
- **Task statuses**: `active`, `running`, `paused`
- **Job statuses**: `scheduled`, `running`, `completed`, `cancelled`, `failed`

## Why quiv?

Python has several task schedulers — [APScheduler](https://pypi.org/project/APScheduler/), [arq](https://github.com/python-arq/arq), [rq](https://python-rq.org/), [sched](https://docs.python.org/3/library/sched.html), [schedule](https://schedule.readthedocs.io/en/stable/index.html), and others. `quiv` was born out of two gaps none of them filled well.

### Cooperative cancellation
I am the developor of [Trailarr](https://github.com/nandyalu/trailarr), an open-source app for downloading and managing trailers for media libraries, Trailarr is a fastapi app at it's core and was using APScheduler for background tasks and things that shouldn't block the main thread/async loop. 

As the app grew, users started requesting a way to stop long-running tasks mid-execution. None of the existing schedulers offered a clean mechanism for this. 

`quiv` solves it with `_stop_event`: a per-job `threading.Event` that is injected into your handler so you can check it at natural breakpoints and exit early when cancellation is requested.

### Progress callbacks across thread boundaries
Apps with a frontend or any sort of UI often need background tasks to report progress back to the main thread — for
example, to push websocket messages to a UI.

There was no straightforward way to call an async function on the main event loop from inside a threadpool worker. 

`quiv` solves this with `_progress_hook`: your handler calls it with arbitrary payload data, and the scheduler dispatches your registered callback on the main asyncio loop, where it can broadcast over websockets or update application state.

If your app needs either of these patterns, `quiv` might be a good fit.

## Important caveats

- **Temporary database**: each `Quiv` instance creates a temporary SQLite file
  that is deleted on `shutdown()`. Task/job state does not persist across
  restarts.
- **Single-process**: the scheduler runs in-process. It is not designed for
  distributed or multi-process deployments.
- **Picklable args**: `args` and `kwargs` passed to `add_task()` are
  pickle-serialized for persistence. Most Python objects are supported,
  but lambdas and inner functions are not picklable.


## Next pages

- [Getting Started](getting-started.md)
- [API](api.md)
- [Architecture](architecture.md)
- [Event Listeners](event-listeners.md)
- [Exceptions](exceptions.md)
