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
- persistent task/job state via SQLModel + SQLite

## When to use quiv

Use `quiv` when you need in-process background scheduling for app-level jobs,
for example:

- polling APIs every N seconds
- periodic cleanup tasks
- one-shot delayed jobs
- progress-aware long-running workloads

## Install

```bash
uv add quiv
# or
pip install quiv
```

## Quick start

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

## Documentation

Full documentation is available at
**[nandyalu.github.io/quiv](https://nandyalu.github.io/quiv/)**.

- [Getting Started](https://nandyalu.github.io/quiv/getting-started/)
- [Bigger Applications](https://nandyalu.github.io/quiv/bigger-applications/)
- [API Reference](https://nandyalu.github.io/quiv/api/)
- [Architecture](https://nandyalu.github.io/quiv/architecture/)
- [Progress Callbacks](https://nandyalu.github.io/quiv/progress-callbacks/)
- [Cancellation](https://nandyalu.github.io/quiv/cancellation/)
- [Exceptions](https://nandyalu.github.io/quiv/exceptions/)

## License

MIT
