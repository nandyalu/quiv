# Getting Started

This guide gets `quiv` running with recurring tasks, progress callbacks,
and clean shutdown behavior.

## Install

```bash
pip install quiv
```

For local development:

```bash
pip install -e .
```

## 1) Create a scheduler

You can configure `Quiv` with either a `QuivConfig` object or direct args.

```python
from quiv import Quiv, QuivConfig

scheduler = Quiv(
	config=QuivConfig(
		pool_size=8,
		history_retention_seconds=3600,
		timezone="UTC",
	)
)
```

Equivalent direct parameters:

```python
from quiv import Quiv

scheduler = Quiv(
	pool_size=8,
	history_retention_seconds=3600,
	timezone_name="UTC",
)
```

Do not mix `config=...` with direct constructor config args.

## 2) Add a task

```python
def my_task(_stop_event=None, _progress_hook=None):
	total = 5
	for step in range(1, total + 1):
		if _stop_event and _stop_event.is_set():
			return
		if _progress_hook:
			_progress_hook(step=step, total=total)

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

`_stop_event` and `_progress_hook` are injected only if your handler accepts
those keyword parameters.

## 3) Add progress callback (optional)

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

Progress callbacks run on the scheduler's main async loop.

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
scheduler.pause_task(task_id)
scheduler.resume_task(task_id)
```

## 6) Inspect state

```python
tasks = scheduler.get_all_tasks(include_run_once=True)
jobs = scheduler.get_all_jobs()
failed_jobs = scheduler.get_all_jobs(status="failed")
```

## FastAPI integration example

`quiv` is intended for app-integrated task scheduling, especially in FastAPI.
The pattern is:

- create one scheduler instance
- add tasks during startup
- start scheduler during startup
- call `shutdown()` during FastAPI shutdown

```python
import asyncio
from fastapi import FastAPI

from quiv import Quiv

app = FastAPI()
scheduler = Quiv(timezone_name="UTC")


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


@app.on_event("startup")
async def startup_event() -> None:
	scheduler.add_task(
		task_name="reindex-docs",
		func=reindex_documents,
		interval=300,
		progress_callback=on_reindex_progress,
	)
	scheduler.start()


@app.on_event("shutdown")
async def shutdown_event() -> None:
	scheduler.shutdown()
```

Why this matters:

- `_stop_event` makes long tasks cancel safely on shutdown.
- `_progress_hook` sends task progress back into FastAPI's async context.
- Scheduler lifecycle is tied cleanly to app lifecycle.

## Troubleshooting

- **`ConfigurationError` on startup**: check `pool_size > 0` and
  `history_retention_seconds >= 0`.
- **`InvalidTimezoneError`**: use a valid IANA timezone name (for example
  `UTC` or `America/New_York`).
- **`HandlerNotRegisteredError` for immediate run**: call `add_task(...)`
  first.
- **`TaskNotScheduledError`**: the task handler exists, but no scheduled task
  row exists yet.

```python
from quiv import Quiv
scheduler = Quiv(timezone_name="UTC")
```
