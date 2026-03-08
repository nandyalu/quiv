# 

![quiv Logo](https://raw.githubusercontent.com/nandyalu/quiv/main/assets/quiv-logo-text-full-minified.png)

<hr>

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
pip install quiv
```

## Quick start

```python
from contextlib import asynccontextmanager

from fastapi import FastAPI

from quiv import Quiv

scheduler = Quiv(timezone_name="UTC")


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
    return {"message": "Heartbeat stated successfully!"}
```

## Documentation

Full documentation is available at
**[nandyalu.github.io/quiv](https://nandyalu.github.io/quiv/)**.

- [Getting Started](https://nandyalu.github.io/quiv/getting-started/)
- [API Reference](https://nandyalu.github.io/quiv/api/)
- [Architecture](https://nandyalu.github.io/quiv/architecture/)
- [Exceptions](https://nandyalu.github.io/quiv/exceptions/)

## License

MIT
