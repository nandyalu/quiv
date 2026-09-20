# Observability

quiv gives you a snapshot of the scheduler at one moment, and queries over tasks and jobs that accept filters. Together they are enough to build an admin page or a health check without reading the database directly.

## `stats()`

`stats()` returns a frozen `QuivStats` dataclass:

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

`QuivStats` is a plain dataclass. Call `dataclasses.asdict()` on it to build a JSON response.

## Job queries

`get_all_jobs()` accepts filters, an order, and paging:

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

`order_by` accepts `"started_at"` and `"ended_at"`. Any other value raises `ConfigurationError`. The two datetime filters follow the rule that holds everywhere in quiv: pass an aware UTC value.

## Task queries

`get_all_tasks()` accepts `status`, `limit`, and `offset`, next to `include_run_once`. It orders the results by `next_run_at`, earliest first.

```python
scheduler.get_all_tasks(status=TaskStatus.PAUSED)
scheduler.get_all_tasks(limit=50, offset=100)
```

## Outstanding main-loop work

`pending_main_loop_work()` returns how many callables handed to the main loop by [`run_on_main`](run-on-main.md) have not finished.

```python
scheduler.pending_main_loop_work()   # -> int
```

quiv never waits for that work and never cancels it, because the loop is your application's. This is the number that lets you decide when the loop may close:

```python
scheduler.shutdown()
while scheduler.pending_main_loop_work():
    await asyncio.sleep(0.05)
```

Unlike `stats()`, it reads one set under a lock and never touches the database, so it is cheap to poll.

It can read one higher than the number of handoffs for a few microseconds, while a queued sync callable that returned a coroutine is briefly counted both as itself and as the task it created. It never reads lower than the truth, which is the safe direction for deciding whether to close a loop.

## Example endpoints

The [FastAPI example app](https://github.com/nandyalu/quiv/tree/main/examples/fastapi_app) puts all three into routes:

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

See [`update_task()`](api.md#update_task) to change a task while the scheduler runs.
