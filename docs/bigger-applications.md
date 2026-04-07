# Bigger Applications

In larger FastAPI projects, code is split across multiple packages and modules.
This guide shows how to structure `quiv` in that setup — shared scheduler
instance, tasks defined in separate files, API endpoints for runtime control,
WebSocket progress updates, and graceful cancellation.

## Project structure

```
myapp/
├── main.py              # FastAPI app, lifespan, WebSocket
├── scheduler.py         # Quiv instance (shared singleton)
├── tasks/
│   ├── __init__.py
│   ├── cleanup.py       # DB cleanup task
│   └── report.py        # Report generation task
└── routes/
    ├── __init__.py
    └── tasks.py          # Task control endpoints
```

## 1) Create the scheduler instance

Define the `Quiv` instance in its own module so every other file can import it.
Do **not** call `start()` here — that happens in the FastAPI lifespan.

```python
# myapp/scheduler.py
from quiv import Quiv

scheduler = Quiv(
    pool_size=4,
    history_retention_seconds=7200,
    timezone="America/New_York",
)
```

Since `Quiv` lazily resolves the asyncio event loop, this works at module level
before FastAPI or uvicorn creates a loop.

## 2) Define tasks in separate files

Each task file imports the shared scheduler to register its task via
`add_task()`. Tasks are plain functions — sync or async.

### Cleanup task (sync, with stop event)

```python
# myapp/tasks/cleanup.py
import logging
import threading
import time

logger = logging.getLogger(__name__)


def cleanup_stale_records(
    days: int,
    _stop_event: threading.Event | None = None,
):
    """Delete records older than `days` from the database."""
    batches = 10
    for batch in range(1, batches + 1):
        if _stop_event and _stop_event.is_set():
            logger.info("Cleanup cancelled at batch %d/%d", batch, batches)
            return

        # ... delete a batch of old records ...
        time.sleep(1)  # simulate work

    logger.info("Cleanup finished: processed %d batches", batches)
```

### Report task (sync, with stop event and progress hook)

```python
# myapp/tasks/report.py
import logging
import threading
import time
from typing import Callable

logger = logging.getLogger(__name__)


def generate_report(
    report_type: str,
    _stop_event: threading.Event | None = None,
    _progress_hook: Callable | None = None,
):
    """Generate a report with progress updates."""
    steps = 5
    for step in range(1, steps + 1):
        if _stop_event and _stop_event.is_set():
            logger.info("Report generation cancelled at step %d/%d", step, steps)
            return

        # ... do a chunk of report work ...
        time.sleep(2)  # simulate work

        if _progress_hook:
            _progress_hook(
                step=step,
                total=steps,
                report_type=report_type,
            )

    logger.info("Report '%s' generated successfully", report_type)
```

## 3) Register tasks and wire up the lifespan

The main module registers tasks, starts the scheduler on startup, and shuts it
down on teardown. This is also where you set up WebSocket-based progress
callbacks.

```python
# myapp/main.py
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from quiv import Event

from myapp.scheduler import scheduler
from myapp.tasks.cleanup import cleanup_stale_records
from myapp.tasks.report import generate_report

logger = logging.getLogger(__name__)

# ---------- WebSocket connection manager ----------

class ConnectionManager:
    """Track active WebSocket connections for progress broadcasts."""

    def __init__(self):
        self.connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        self.connections.remove(websocket)

    async def broadcast(self, message: dict):
        for ws in self.connections:
            try:
                await ws.send_json(message)
            except Exception:
                pass

ws_manager = ConnectionManager()


# ---------- Progress callback ----------

async def on_report_progress(**payload):
    """Forward task progress to all connected WebSocket clients."""
    logger.info("Report progress: %s", payload)
    await ws_manager.broadcast({"event": "progress", "data": payload})


# ---------- Lifespan ----------

async def on_job_event(event: Event, data: dict) -> None:
    """Forward job lifecycle events to WebSocket clients."""
    payload = {"event": event.value, "task": data["task_name"]}
    if "duration" in data:
        payload["duration"] = str(data["duration"])
    if "error" in data:
        payload["error"] = str(data["error"])
    await ws_manager.broadcast(payload)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Register event listeners
    scheduler.add_listener(Event.JOB_STARTED, on_job_event)
    scheduler.add_listener(Event.JOB_COMPLETED, on_job_event)
    scheduler.add_listener(Event.JOB_FAILED, on_job_event)

    # Register and start tasks
    scheduler.add_task(
        task_name="db-cleanup",
        func=cleanup_stale_records,
        interval=3600,
        kwargs={"days": 30},
    )
    scheduler.add_task(
        task_name="weekly-report",
        func=generate_report,
        interval=604800,
        delay=10,
        kwargs={"report_type": "weekly-summary"},
        progress_callback=on_report_progress,
    )
    scheduler.start()
    yield
    scheduler.shutdown()


app = FastAPI(lifespan=lifespan)


# ---------- WebSocket endpoint ----------

@app.websocket("/ws/progress")
async def progress_websocket(websocket: WebSocket):
    await ws_manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)
```

## 4) API endpoints for runtime control

A separate router imports the same scheduler instance to expose task management
endpoints.

```python
# myapp/routes/tasks.py
from fastapi import APIRouter, HTTPException

from myapp.scheduler import scheduler

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.post("/{task_name}/run")
def run_task_now(task_name: str):
    """Trigger a scheduled task to run immediately."""
    try:
        count = scheduler.run_task_immediately(task_name)
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"queued": count}


@router.post("/{task_name}/pause")
def pause_task(task_name: str):
    """Pause a task by name."""
    try:
        scheduler.pause_task(task_name)
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"status": "paused"}


@router.post("/{task_name}/resume")
def resume_task(task_name: str, delay: int = 0):
    """Resume a paused task, optionally with a delay."""
    try:
        scheduler.resume_task(task_name, delay=delay)
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"status": "resumed"}


@router.get("/")
def list_tasks():
    """List all scheduled tasks."""
    return scheduler.get_all_tasks()


@router.get("/jobs")
def list_jobs(status: str | None = None):
    """List jobs, optionally filtered by status."""
    return scheduler.get_all_jobs(status=status)


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: int):
    """Cancel a running job."""
    cancelled = scheduler.cancel_job(job_id)
    if not cancelled:
        raise HTTPException(status_code=404, detail="Job not found or not running")
    return {"status": "cancelled"}
```

`Task` and `Job` are SQLModel objects, so FastAPI serializes them directly —
no manual conversion needed. All datetime fields (`next_run_at`, `started_at`,
`ended_at`) are guaranteed to be timezone-aware UTC, so the JSON output will
include a `+00:00` suffix that browsers can parse and display in the user's
local timezone.

Register the router in your app:

```python
# add to myapp/main.py
from myapp.routes.tasks import router as tasks_router

app.include_router(tasks_router)
```

## Run the full example

A complete runnable version of this app is in the
[`examples/fastapi_app`](https://github.com/nandyalu/quiv/tree/main/examples/fastapi_app)
directory. From the repository root:

```bash
uv run uvicorn examples.fastapi_app.main:app --reload
```

Then open [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs) for
interactive API docs, or connect to `ws://127.0.0.1:8000/ws/progress` for live
progress updates.

## Key takeaways

- **Single instance, shared everywhere.** Create `Quiv` in one module and
  import it wherever needed. This avoids multiple schedulers and duplicate DB
  files.
- **Module-level init is safe.** `Quiv()` does not require a running asyncio
  loop at creation time. The event loop is resolved lazily when progress
  callbacks fire.
- **Lifespan owns the lifecycle.** Call `start()` and `shutdown()` in the
  FastAPI lifespan so the scheduler is tied to the app process.
- **Tasks are plain functions.** Define them anywhere. They only need
  `_stop_event` and `_progress_hook` in their signature if they want
  cancellation or progress support. See [Cancellation](cancellation.md) and
  [Progress Callbacks](progress-callbacks.md) for detailed guides.
- **Progress goes through WebSocket.** Async progress callbacks run on
  FastAPI's event loop, so they can broadcast to WebSocket clients directly.
  See [Progress Callbacks](progress-callbacks.md) for dispatch details.
- **Event listeners for observability.** Use `add_listener()` to react to
  task and job lifecycle events. Async listeners run on the main loop, so
  they can broadcast to WebSocket clients alongside progress callbacks.
  See [Event Listeners](event-listeners.md) for the full event list.
