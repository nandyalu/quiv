# Observability

quiv exposes a point-in-time statistics snapshot and rich, filterable job/task queries — enough to build an admin or health endpoint without touching the database directly.

## `stats()`

Returns a frozen `QuivStats` dataclass:

```python
from quiv import Quiv, QuivStats

stats: QuivStats = scheduler.stats()
stats.active_jobs        # jobs currently executing
stats.pool_size          # thread-pool size
stats.pool_utilization   # active_jobs / pool_size, 0.0-1.0
stats.tasks_by_status    # e.g. {"active": 3, "paused": 1}
stats.next_run_at        # earliest upcoming run (UTC), or None
stats.job_history_count  # job rows currently retained
```

`QuivStats` is a plain dataclass — serialize it with `dataclasses.asdict()` for JSON responses.

## Job queries

`get_all_jobs()` accepts filters, ordering, and pagination:

```python
scheduler.get_all_jobs(
    status=JobStatus.FAILED,   # optional status filter
    task_id=task_id,           # only this task's jobs
    since=window_start,        # started_at >= since (aware UTC)
    until=window_end,          # started_at <= until (aware UTC)
    order_by="started_at",     # "started_at" | "ended_at"
    descending=True,           # newest first by default
    limit=20,
    offset=0,
)
```

`order_by` accepts exactly `"started_at"` and `"ended_at"` — anything else raises `ConfigurationError`. Datetime filters follow the library-wide contract: pass timezone-aware UTC values.

## Task queries

`get_all_tasks()` gained `status`, `limit`, and `offset` alongside the existing `include_run_once`; results are ordered by `next_run_at` ascending:

```python
scheduler.get_all_tasks(status=TaskStatus.PAUSED)
scheduler.get_all_tasks(limit=50, offset=100)
```

## Example endpoints

The [FastAPI example app](https://github.com/nandyalu/quiv/tree/main/examples/fastapi_app) wires all three into routes:

```python
@router.get("/stats")
def get_stats():
    return asdict(scheduler.stats())


@router.get("/{task_id}/jobs")
def list_task_jobs(task_id: str, limit: int | None = None, offset: int = 0):
    return scheduler.get_all_jobs(task_id=task_id, limit=limit, offset=offset)


@router.patch("/{task_id}")
def update_task(task_id: str, update: TaskUpdate):
    return scheduler.update_task(task_id, **update.model_dump(exclude_none=True))
```

See [`update_task()`](api.md#update_task) for runtime task mutation.
