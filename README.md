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

If you have used APScheduler inside a FastAPI app, you have probably met one of these problems:

- A task runs too long and the user wants to cancel it, but there is no clean way to signal the worker while it runs.
- A background job must send progress to a websocket, and you write `run_coroutine_threadsafe` code by hand to get back onto the main loop.
- You want a job id on every log line of one run, and you pass that id through every call by hand.
- You have a complete async pipeline to run in the background, and you wrap it in `asyncio.run` to give it to a scheduler that accepts sync code only.

`quiv` was built inside [Trailarr](https://github.com/nandyalu/trailarr), a FastAPI app that left APScheduler for these reasons. It is a scheduler for one process, backed by a thread pool. It has three things built in: cooperative cancellation through `stop_event`, progress callbacks that run on the main loop through `progress_hook`, and a job id for tracing through `job_id`.

`quiv` does not replace Celery. If you need workers in several processes, a queue that survives a restart, or work spread over machines, use Celery or arq. Use `quiv` when the work belongs inside your own process, where those tools would be far more than you need.

Supports Python 3.10 through 3.14.

## Install

### With `uv`

```bash
uv add quiv
```

### With pip

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
def ping(progress_hook=None):
    for i in range(30):
        # do some work
        if progress_hook:
            progress_hook(message="ping", progress=i, total=30)

# Now the actual progress callback function that we want to run on the main asyncio loop
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

APScheduler integrates with asyncio, but an async pipeline still needs a wrapper or a bridge when you schedule it from a thread pool. `quiv` accepts an async handler as it is. Each invocation runs in an event loop that quiv creates on the worker thread of that job. Sync and async handlers live in the same scheduler.


```python
async def fetch_updates(stop_event=None):
    await some_async_api_call()

scheduler.add_task(task_name="fetch", func=fetch_updates, interval=60)
```

### Cancel a running task from an HTTP endpoint

`stop_event` is a `threading.Event` for one job, which quiv injects into your handler. Check it at the natural breakpoints and return early when an endpoint calls `scheduler.cancel_job(job_id)`. quiv kills no thread, and raises no exception across a thread boundary.

```python
def download(media_id: int, stop_event=None):
    for chunk in stream_chunks(media_id):
        if stop_event and stop_event.is_set():
            return  # cooperative exit
        write(chunk)
```

### Send progress to a websocket, without writing `run_coroutine_threadsafe` yourself

Your handler calls `progress_hook(**payload)` inside the thread pool. `quiv` runs the async callback that you registered on the main asyncio loop. There it can send a websocket message, change the state of the application, or report a metric.

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

Every invocation gets a `job_id`, a UUID. Put it into a `LoggerAdapter`, or into a `ContextVar`, and every log line of that run carries the same trace id. You can then filter the logs of one job with a single query, while many tasks run at the same time.

```python
import logging

base_logger = logging.getLogger(__name__)

def download_trailer(media_id: int, job_id: str | None = None, stop_event=None):
    logger = logging.LoggerAdapter(base_logger, {"trace_id": job_id})
    logger.info("Starting download for media %s", media_id)
    # every log line through `logger` below carries trace_id=<job_id>
```

Trailarr uses the `ContextVar` form of this in production, so that the modules it calls read the trace id without any extra code. See [Getting Started](getting-started.md) for that version.

## Concepts

- **Task**: scheduling definition (`interval`, `run_once`, args/kwargs, status)
- **Job**: one execution record of a task
- **Task statuses**: `active`, `running`, `paused`
- **Job statuses**: `scheduled`, `running`, `completed`, `cancelled`, `failed`

## How quiv compares

The table describes the default behavior of each tool. All four can be stretched further with extra work.

| | quiv | FastAPI `BackgroundTasks` | APScheduler | Celery |
| --- | --- | --- | --- | --- |
| Runs inside your process | yes | yes | yes | no — separate worker processes |
| Recurring schedules | interval only | no | interval and cron | interval and cron, through beat |
| Cooperative cancellation of a running job | yes, through `stop_event` | no | no | partial — `revoke` reaches a queued task, and terminating a running one kills the worker |
| Progress updates on the main event loop | yes, through `progress_hook` | not needed — the task already runs there | no | no |
| Retries with backoff | yes | no | no | yes |
| Per-task timeout | yes, cooperative | no | no | yes, soft and hard limits |
| State survives a restart | no | no | optional, through a job store | yes, through the broker and the result backend |
| Spreads work over processes or machines | no | no | no | yes |

Read the last three rows first. quiv keeps no state across a restart, runs no cron expression, and spreads no work beyond one process. If you need any of those, use APScheduler or Celery. quiv is for the case where a job must run in your process, report progress to your event loop, and stop when a user asks it to.

Before the `1.0.0` release quiv ran for 24 hours under a mixed workload — recurring, async, failing, cancelled, timing out, and sub-second tasks at once. It finished 294,400 jobs with the thread count unchanged, the retained job history flat after the first hour, and memory steady at 53 MB. The log is in the repository at [`benchmarks/results/soak-24h-2026-09-17.log`](https://github.com/nandyalu/quiv/blob/main/benchmarks/results/soak-24h-2026-09-17.log).

## Important caveats

- **A temporary database**: each `Quiv` instance creates a temporary SQLite file, and `shutdown()` deletes it. The state of your tasks and jobs does not survive a restart.
- **One process**: the scheduler runs inside your process. It is not built to spread work over several processes or machines.
- **Picklable arguments**: quiv serializes the `args` and `kwargs` of `add_task()` with pickle, to store them. Pickle accepts most Python objects, but it cannot accept a lambda or an inner function. The temporary SQLite database holds trusted internal state: only your application writes to it, and `shutdown()` deletes it. Never let untrusted input reach that database file.


## Next pages

Read the full documentation here:

- [Getting Started](getting-started.md) — install, scheduler setup, and your first task
- [API](api.md) — the full reference for `Quiv`, `add_task`, and every other method
- [Architecture](architecture.md) — how the scheduler, persistence, and execution layers fit together
- [Event Listeners](event-listeners.md) — react to what happens to a task and to a job
- [Exceptions](exceptions.md) — the `QuivError` hierarchy and when each is raised
- [Testing](testing.md) — patterns for testing handlers and the scheduler in your suite

## Using quiv with AI coding tools

quiv is built to be understood by AI assistants as well as humans:

- **Bundled agent guide** — every install ships a condensed reference at `quiv/AGENTS.md` inside the package (find it in `site-packages`); agents exploring your dependencies pick it up automatically.
- **llms.txt** — the docs site publishes [llms.txt](https://nandyalu.github.io/quiv/llms.txt) and [llms-full.txt](https://nandyalu.github.io/quiv/llms-full.txt) per the [llms.txt convention](https://llmstxt.org/).
- **Claude Code plugin** — this repo doubles as a plugin marketplace with a `quiv` skill: `/plugin marketplace add nandyalu/quiv` then `/plugin install quiv@quiv`.

See [AI Tools](https://nandyalu.github.io/quiv/ai-tools/) for details.

## Ideas, bugs, and contributions

`quiv` started from one app's needs, so the best way it gets better is when other people's apps push it in new directions. If you have a use case it doesn't cover, a rough edge it should smooth out, or a PR you'd like to land — all welcome.

- [Open an issue](https://github.com/nandyalu/quiv/issues) for bugs or feature requests
- [Start a discussion](https://github.com/nandyalu/quiv/discussions) if you'd like to talk through an idea first
- PRs are welcome — for anything non-trivial, opening an issue first is usually the fastest path

And if `quiv` saved you some time, a [GitHub star](https://github.com/nandyalu/quiv) is a nice way to let us know it was useful.
