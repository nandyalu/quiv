# quiv

`quiv` is a lightweight background task scheduler for Python applications.

It is designed to work especially well with FastAPI apps that need predictable,
in-process background task orchestration.

It provides:

- threadpool-backed execution
- support for sync and async task handlers
- cooperative cancellation (`_stop_event`)
- progress callbacks routed to your main async loop (`_progress_hook`)
- persistent task/job state via SQLModel

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

## Concepts

- **Task**: scheduling definition (`interval`, `run_once`, args/kwargs, status)
- **Job**: one execution record of a task
- **Task statuses**: `active`, `paused`
- **Job statuses**: `scheduled`, `running`, `completed`, `cancelled`, `failed`

## FastAPI usage

For a full FastAPI integration example (startup/shutdown lifecycle plus
`_stop_event` and `_progress_hook`), see the FastAPI section in
[Getting Started](getting-started.md).

## Next pages

- [Getting Started](getting-started.md)
- [API](api.md)
- [Architecture](architecture.md)
- [Exceptions](exceptions.md)
