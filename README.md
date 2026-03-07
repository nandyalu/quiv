# quiv

`quiv` is a lightweight background scheduler for Python applications.

It combines a thread pool, async-aware execution, cooperative cancellation,
progress callbacks, and task/job persistence into a small API.

`quiv` is intended to fit naturally inside FastAPI applications where you need
in-process background scheduling tied to app lifecycle.

## What you get

- recurring and one-shot tasks
- sync and async handlers
- cooperative cancellation via `_stop_event`
- progress callbacks routed to your main event loop via `_progress_hook`
- persisted task/job state via SQLModel + SQLite

## Python support

`quiv` supports Python `3.10` through `3.14`.

## Install

```bash
pip install quiv
```

For local development:

```bash
pip install -e .
```

## Quick start

```python
import asyncio

from quiv import Quiv, QuivConfig

config = QuivConfig(
	pool_size=8,
	history_retention_seconds=3600,
	timezone="UTC",
)
scheduler = Quiv(config=config)

def my_task(_stop_event=None, _progress_hook=None):
	for i in range(5):
		if _stop_event and _stop_event.is_set():
			return
		if _progress_hook:
			_progress_hook(step=i + 1, total=5)

async def on_progress(**payload):
	print(payload)

async def main():
	task_id = scheduler.add_task(
		task_name="demo",
		func=my_task,
		interval=10,
		progress_callback=on_progress,
	)
	print("scheduled", task_id)

	scheduler.start()
	await asyncio.sleep(12)
	scheduler.shutdown()

asyncio.run(main())
```

## FastAPI example

```python
import asyncio
from fastapi import FastAPI

from quiv import Quiv

app = FastAPI()
scheduler = Quiv(timezone_name="UTC")


def ingest_loop(_stop_event=None, _progress_hook=None) -> None:
	total = 50
	for step in range(1, total + 1):
		if _stop_event and _stop_event.is_set():
			return
		if _progress_hook:
			_progress_hook(step=step, total=total, task="ingest")


async def on_progress(**payload) -> None:
	print("progress", payload)


@app.on_event("startup")
async def startup_event() -> None:
	scheduler.add_task(
		task_name="ingest",
		func=ingest_loop,
		interval=60,
		progress_callback=on_progress,
	)
	scheduler.start()


@app.on_event("shutdown")
async def shutdown_event() -> None:
	scheduler.shutdown()
```

In this example, `_stop_event` enables cooperative cancellation during app
shutdown, and `_progress_hook` forwards task progress to the async callback.

You can also pass `pool_size`, `history_retention_seconds`, and
`timezone_name` directly to `Quiv(...)`.

## Core concepts

- **Task**: persisted schedule definition (`task_name`, interval, args/kwargs).
- **Job**: one concrete execution of a task.
- **run_once task**: executes once, then is removed from task storage.
- **stop event**: `_stop_event` is injected only if your handler accepts it.
- **progress hook**: `_progress_hook` is injected only if your handler accepts it.

## Common operations

### Run now

```python
scheduler.run_task_immediately("demo")
```

### Pause / resume

```python
scheduler.pause_task(task_id)
scheduler.resume_task(task_id)
```

### Query state

```python
tasks = scheduler.get_all_tasks(include_run_once=True)
jobs = scheduler.get_all_jobs()
failed_jobs = scheduler.get_all_jobs(status="failed")
```

## Job statuses

- `scheduled`
- `running`
- `completed`
- `cancelled`
- `failed`

## Shutdown behavior

- `shutdown()` sets scheduler shutdown state
- cancels currently running jobs via stop events
- joins scheduler thread and shuts down the worker pool
- disposes engine and removes the temporary SQLite database file

Always call `shutdown()` during app teardown.

## Architecture

- `quiv/persistence.py`: database operations and task/job state transitions
- `quiv/execution.py`: invocation preparation and sync/async execution logic
- `quiv/scheduler.py`: loop orchestration and dispatch policy
- `quiv/base.py`: runtime lifecycle and shared infrastructure

## Exceptions

All library exceptions derive from `QuivError` and are defined in `quiv/exceptions.py`.

## Documentation

See:

- `docs/getting-started.md`
- `docs/api.md`
- `docs/architecture.md`
- `docs/exceptions.md`

## Publishing

Build and publish using your preferred tooling (for example `uv build` / `twine upload`).
