# Event Listeners

Event listeners let you react to scheduler lifecycle events — tasks being
added, removed, paused, or resumed, and jobs starting, completing, failing,
or being cancelled. This is useful for logging, metrics, alerting, or
updating UI state.

## How it works

Register a callback for one or more `Event` types via `add_listener()`.
When the event fires, quiv dispatches your callback on the main event loop
(same dispatch model as [progress callbacks](progress-callbacks.md)).

```mermaid
flowchart TD
    A["Scheduler emits event"] --> B{Listeners registered?}
    B -- No --> C[Return silently]
    B -- Yes --> D{Event loop available?}
    D -- Yes --> E{Async listener?}
    D -- No --> F{Async listener?}
    E -- Yes --> G["run_coroutine_threadsafe()
    on main loop"]
    E -- No --> H["call_soon_threadsafe()
    on main loop"]
    F -- Yes --> I["Run in temporary event loop"]
    F -- No --> J["Call directly on
    calling thread"]
```

### Dispatch paths

| Event loop | Listener type | What happens |
|------------|--------------|--------------|
| Available | Async | Dispatched via `run_coroutine_threadsafe` on the main loop |
| Available | Sync | Dispatched via `call_soon_threadsafe` on the main loop |
| Unavailable | Sync | Called directly on the calling thread |
| Unavailable | Async | Run in a temporary event loop on the calling thread |

## Events

All events are defined in the `Event` enum:

| Event | When it fires | Callback receives |
|-------|--------------|-------------------|
| `TASK_ADDED` | After `add_task()` completes | `event`, `task` |
| `TASK_REMOVED` | After `remove_task()` completes | `event`, `task`[^1] |
| `TASK_PAUSED` | After `pause_task()` completes | `event`, `task` |
| `TASK_RESUMED` | After `resume_task()` completes | `event`, `task` |
| `JOB_STARTED` | When a job begins execution | `event`, `task`, `job` |
| `JOB_COMPLETED` | When a job finishes successfully | `event`, `task`, `job` |
| `JOB_FAILED` | When a job ends with an exception | `event`, `task`, `job` |
| `JOB_CANCELLED` | When a job is cancelled via stop event | `event`, `task`, `job` |

[^1]: For `TASK_REMOVED`, the `task` object is a snapshot taken before deletion.

## Callback signatures

Listeners receive typed model objects — better callbacks with predictable inputs. The signature depends on the event group:

### Task events (`TASK_*`)

```python
from quiv import Event
from quiv.models import Task

def on_task_event(event: Event, task: Task) -> None:
    print(f"[{event.value}] Task '{task.task_name}' (id={task.id})")
```

### Job events (`JOB_*`)

```python
from quiv import Event
from quiv.models import Task, Job

def on_job_event(event: Event, task: Task, job: Job) -> None:
    print(f"[{event.value}] Job {job.id} for '{task.task_name}'")
    if job.duration_seconds is not None:
        print(f"  Duration: {job.duration_seconds:.2f}s")
    if job.error_message is not None:
        print(f"  Error: {job.error_message}")
```

Async callbacks use the same signatures:

```python
async def on_task_event(event: Event, task: Task) -> None:
    ...

async def on_job_event(event: Event, task: Task, job: Job) -> None:
    ...
```

!!! tip "Full type safety"
    Since listeners receive `Task` and `Job` model objects, you get IDE
    autocomplete and type checking on every field — no more guessing dict
    keys at runtime.

## Registering listeners

Use `add_listener()` to register a callback for a specific event:

```python
from quiv import Quiv, Event
from quiv.models import Task

scheduler = Quiv()


def on_task_added(event: Event, task: Task) -> None:
    print(f"Task '{task.task_name}' added with ID {task.id}")


scheduler.add_listener(Event.TASK_ADDED, on_task_added)
```

### Multiple listeners

You can register multiple listeners for the same event. They are called in
registration order:

```python
scheduler.add_listener(Event.JOB_FAILED, log_failure)
scheduler.add_listener(Event.JOB_FAILED, send_alert)
```

### Multiple events

Register the same callback for different events within the same event group:

```python
from quiv.models import Task, Job

def job_audit_log(event: Event, task: Task, job: Job) -> None:
    print(f"[{event.value}] task={task.task_name} job={job.id}")

scheduler.add_listener(Event.JOB_COMPLETED, job_audit_log)
scheduler.add_listener(Event.JOB_FAILED, job_audit_log)
```

## Removing listeners

Use `remove_listener()` to unregister a previously added callback:

```python
scheduler.remove_listener(Event.TASK_ADDED, on_task_added)
```

If the callback is not found, the call is silently ignored.

## Async listeners

Async listeners run on the main event loop via `run_coroutine_threadsafe`,
just like async progress callbacks. This makes them ideal for FastAPI apps
where you want to broadcast events to WebSocket clients:

```python
from quiv.models import Task, Job

async def on_job_completed(event: Event, task: Task, job: Job) -> None:
    await ws_manager.broadcast({
        "type": "job_completed",
        "task": task.task_name,
        "duration_seconds": job.duration_seconds,
    })

scheduler.add_listener(Event.JOB_COMPLETED, on_job_completed)
```

## Error handling

If a listener raises an exception, quiv logs the error but does **not**
fail the scheduler or the job. Other listeners for the same event still run.
This prevents a broken listener from disrupting task execution.

```mermaid
flowchart TD
    A["Event emitted"] --> B["Dispatch listener 1"]
    B --> C{Raises?}
    C -- No --> D["Dispatch listener 2"]
    C -- Yes --> E["Log error"]
    E --> D
    D --> F["Continue"]
```

## Without an event loop

In scripts without asyncio, sync event listeners work normally — they run
directly on the calling thread:

```python
from quiv import Quiv, Event
from quiv.models import Task

scheduler = Quiv()


def on_added(event: Event, task: Task) -> None:
    print(f"Added: {task.task_name}")


scheduler.add_listener(Event.TASK_ADDED, on_added)
scheduler.add_task("my-task", lambda: None, interval=10)
# Prints: Added: my-task
```

Async listeners also work in this scenario — they run in a temporary event
loop on the calling thread, so `await` calls inside the listener execute
correctly.

## FastAPI example

A complete example wiring event listeners into a FastAPI app with WebSocket
notifications:

```python
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from quiv import Event, Quiv
from quiv.models import Task, Job

scheduler = Quiv(timezone="UTC")
logger = logging.getLogger(__name__)

connected_clients: list[WebSocket] = []


async def broadcast(message: dict) -> None:
    for ws in connected_clients:
        try:
            await ws.send_json(message)
        except Exception:
            pass


async def on_job_event(event: Event, task: Task, job: Job) -> None:
    """Broadcast job lifecycle events to WebSocket clients."""
    payload: dict[str, Any] = {
        "event": event.value,
        "task_name": task.task_name,
        "job_id": job.id,
    }
    if job.duration_seconds is not None:
        payload["duration_seconds"] = job.duration_seconds
    if job.error_message is not None:
        payload["error"] = job.error_message
    await broadcast(payload)


def sync_task() -> None:
    pass  # your task logic


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Register event listeners
    scheduler.add_listener(Event.JOB_STARTED, on_job_event)
    scheduler.add_listener(Event.JOB_COMPLETED, on_job_event)
    scheduler.add_listener(Event.JOB_FAILED, on_job_event)
    scheduler.add_listener(Event.JOB_CANCELLED, on_job_event)

    scheduler.add_task("my-task", sync_task, interval=60)
    scheduler.start()
    yield
    scheduler.shutdown()


app = FastAPI(lifespan=lifespan)


@app.websocket("/ws/events")
async def events_websocket(websocket: WebSocket):
    await websocket.accept()
    connected_clients.append(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        connected_clients.remove(websocket)
```
