<p align="center">
  <img src="assets/quiv-logo-text-full-minified.svg" alt="quiv" width="100%">
</p>

# quiv

`quiv` is a lightweight background task scheduler for Python applications.

It is designed to work especially well with FastAPI apps that need predictable,
in-process background task orchestration.

Supports Python 3.10 through 3.14.

It provides:

- threadpool-backed execution
- support for sync and async task handlers
- cooperative cancellation (`_stop_event`)
- progress callbacks routed to your main async loop (`_progress_hook`)
- persistent task/job state via SQLModel + SQLite

## When to use quiv

Use `quiv` when you need in-process background scheduling for app-level jobs,
for example:

- polling APIs every N seconds
- periodic cleanup tasks
- one-shot delayed jobs
- progress-aware long-running workloads

## Quick example

```python
import asyncio

from quiv import Quiv

scheduler = Quiv(timezone_name="UTC")

def ping(_progress_hook=None):
    for i in range(30):
        # do some work
        if _progress_hook:
            _progress_hook(message="ping", progress=i, total=30)

async def on_progress(**payload):
    print(payload)

async def main() -> None:
    scheduler.add_task(
        task_name="heartbeat",
        func=ping,
        interval=30,
        progress_callback=on_progress,
    )
    scheduler.start()
    await asyncio.sleep(35)
    scheduler.shutdown()

asyncio.run(main())
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
- **Task statuses**: `active`, `paused`
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
- **JSON-serializable args**: `args` and `kwargs` passed to `add_task()` are
  JSON-serialized for persistence. Only pass JSON-compatible values.


## Next pages

- [Getting Started](getting-started.md)
- [API](api.md)
- [Architecture](architecture.md)
- [Exceptions](exceptions.md)
