# Running in a Container

quiv runs inside your application process. In a container, the platform starts and stops that process, and it gives the process a fixed time to stop. This page follows the problems in the order an operator meets them.

Some of this page is practice from the two applications that run quiv in containers, trailarr and ten-acre. Each such practice names its source.

## Stopping

When a container stops, the platform sends `SIGTERM`, waits for a grace period, and then sends `SIGKILL`. `SIGKILL` ends the process at once. No `finally` block runs, and no cleanup runs.

| Platform | Setting | Default |
|----------|---------|---------|
| Docker Compose | `stop_grace_period` | 10 s |
| `docker stop` | `--time` | 10 s |
| Kubernetes | `terminationGracePeriodSeconds` | 30 s |

`shutdown()` with no timeout waits for every running job to end. If a job runs for nine minutes, the stop waits nine minutes. The platform kills the process first, and nothing after `shutdown()` runs.

Do these four things:

1. **Make sure that the signal reaches Python.** Use the exec form of `CMD` or `ENTRYPOINT`, or start the server with `exec` in a shell script. A shell that runs as process 1 does not send `SIGTERM` on to its child.
2. **Pass a `timeout` to `shutdown()` that is well inside the grace period.** For example, use 5 seconds inside the default 10. The remaining time is for your own cleanup and for the exit of the process.
3. **Set the grace period to fit the longest wait that you accept.**
4. **Put your own flushes and writes before `shutdown()`**, unless they fit inside the grace period after it. trailarr lost a database flush that ran after `shutdown()`, because the platform killed the process first.

```yaml
# compose.yaml
services:
  app:
    image: myapp
    stop_grace_period: 30s
```

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler.start()
    yield
    flush_buffers()                # your own cleanup first
    scheduler.shutdown(timeout=5)  # well inside the grace period
```

A job that does not stop before the timeout is abandoned. It continues on its worker thread until it returns or the process ends. Its writes go to a database that quiv already deleted, so it can write errors to the log, and SQLite creates the database file again. The [`shutdown()`](api.md#shutdowntimeout-float-none-none-none-stop-none) reference has the details.

A job stops in time only if its handler stops when asked. Check the stop event in long loops, and use `run_subprocess()` for a child process. See [Cancellation](cancellation.md).

## The temporary database

Each `Quiv` instance creates a SQLite file in the directory that `tempfile.gettempdir()` returns. Python takes that directory from the `TMPDIR`, `TEMP`, or `TMP` environment variable, and uses `/tmp` when none is set. The file is `quiv_<8 hex digits>.db`, with `-wal` and `-shm` files beside it while the scheduler runs. `shutdown()` deletes all three.

- **A read-only root filesystem needs a writable temporary directory.** Mount a `tmpfs` on `/tmp`, or point `TMPDIR` at a writable volume. Set `TMPDIR` before the process starts: Python reads it once, the first time that it needs the directory.
- **A `tmpfs` mount uses memory.** That memory counts against the memory limit of the container.
- **A killed process leaves its files behind.** A container that restarts keeps its `/tmp`, so the files collect over restarts. A `tmpfs` mount starts empty at each start, so it removes them.
- **The size depends on the job history.** Every 60 seconds, quiv deletes the job rows that are older than `history_retention_seconds` (default 86400, one day). Each task has one row. A shorter retention keeps the file smaller.

```yaml
# compose.yaml
services:
  app:
    image: myapp
    read_only: true
    tmpfs:
      - /tmp
```

## Logging

quiv configures no logging. If you configure nothing, it writes nothing. In a container, send the `"Quiv"` logger to standard output or standard error, so that `docker logs` and your log collector receive it.

```python
import logging

logging.basicConfig(level=logging.INFO)   # writes to standard error
```

You can also pass your own logger with `Quiv(logger=...)`. See [Logging](getting-started.md#logging) for both ways. In a container, watch for these warnings: jobs abandoned at shutdown, a busy thread pool, and work that `run_on_main` left on the main loop.

## Restarts

quiv starts empty. Its database is temporary, so no task survives a restart. This is by design: the database cannot hold your handlers. The [roadmap](roadmap.md#out-of-scope-for-v100) explains why durable persistence is out of scope.

Both applications arrived at the same practice:

- **Re-add every task at startup, from your own configuration.** trailarr reads its schedules from its own database. ten-acre adds its tasks in code.
- **Stagger the first runs.** Give each task a different `delay`, so that a start does not run every task at the same time. trailarr starts its startup passes at 60 s, its disk scan at 480 s, and its downloads at 900 s.
- **Keep the time of a one-off in your own store.** Write the time when you add or move the one-off. At startup, read it and add the task again with `run_at`. A `run_at` that is already past runs at once, so a time that the restart missed is not lost. ten-acre does this for its wake-up alarm.
- **Keep a slow backstop poll while you build trust in the restore.** ten-acre checks every 300 seconds for an alarm that the restore missed. This is a safety net that one application chose, not a rule.

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Your own configuration, not quiv's database.
    for index, schedule in enumerate(load_schedules()):
        scheduler.add_task(
            schedule.name,
            HANDLERS[schedule.name],
            interval=schedule.interval,
            delay=60 + 120 * index,     # stagger the first runs
        )
    alarm_at = load_alarm_time()        # a datetime that you stored, or None
    if alarm_at is not None:
        scheduler.add_task("alarm", ring_alarm, run_once=True, run_at=alarm_at)
    scheduler.start()
    yield
    scheduler.shutdown(timeout=5)
```

A task gets a new `task_id` at each start. Do not store a `task_id` across restarts. Store the name or your own key.

## A task that runs daily at a set time

quiv schedules by interval. It has no calendar expressions, and it will not get them. A daily run at a set time is still simple: compute the next occurrence in UTC, pass it as `run_at`, and pass `interval=86400`.

```python
import datetime


def next_utc_time(hour: int, minute: int) -> datetime.datetime:
    """The next occurrence of hour:minute UTC — today if still ahead,
    tomorrow otherwise."""
    now = datetime.datetime.now(datetime.timezone.utc)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += datetime.timedelta(days=1)
    return target


scheduler.add_task(
    "daily-report", daily_report, interval=86400, run_at=next_utc_time(21, 30)
)
```

This helper comes from ten-acre, which schedules four daily tasks this way. With the default `fixed_interval=True`, quiv measures each next run from the start of the previous job, so the task keeps its time of day.

**Keep weekday rules inside the handler.** The interval cannot skip days. Let the task run every day, and return early on a day when it has nothing to do:

```python
def daily_report() -> None:
    if datetime.datetime.now(datetime.timezone.utc).weekday() >= 5:
        return  # no report on Saturday or Sunday
    ...
```

**Local time.** quiv schedules in UTC. For a time in a local zone, compute the next occurrence in that zone and convert it to UTC:

```python
from zoneinfo import ZoneInfo


def next_local_time(hour: int, minute: int, zone: str) -> datetime.datetime:
    now = datetime.datetime.now(ZoneInfo(zone))
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += datetime.timedelta(days=1)
    return target.astimezone(datetime.timezone.utc)
```

An interval of 86400 seconds is 24 hours of UTC. When daylight saving time starts or ends, the run moves one hour in local time. To keep the local hour, move the next run after each job from a `JOB_COMPLETED` listener. quiv emits the event after the task is active again, so the new time stays.

```python
report_id = scheduler.add_task(
    "morning-report",
    morning_report,
    interval=86400,
    run_at=next_local_time(9, 0, "America/New_York"),
)


def keep_local_hour(event: Event, task: Task, job: Job) -> None:
    if task.id == report_id:
        scheduler.update_task(
            report_id, run_at=next_local_time(9, 0, "America/New_York")
        )


scheduler.add_listener(Event.JOB_COMPLETED, keep_local_hour)
```

This is not cron, and quiv will not become cron. The [roadmap](roadmap.md#out-of-scope-for-v100) records that decision.

## Health

`scheduler.is_running` is `True` while the scheduler loop runs: `start()` was called, `shutdown()` was not, and the loop thread is alive. The loop catches every `Exception` and continues. So if the loop thread is dead, something outside `Exception` stopped it, and a health check must catch exactly that.

`is_running` does not read the database, so a probe can call it every few seconds on every replica. `stats().loop_alive` holds the same value, but `stats()` reads the database. Use `is_running` in the probe.

```python
from fastapi.responses import JSONResponse


@app.get("/health")
def health() -> JSONResponse:
    if not scheduler.is_running:
        return JSONResponse({"scheduler": "stopped"}, status_code=503)
    return JSONResponse({"scheduler": "running"})
```

```dockerfile
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"]
```

`urlopen` raises an error on a 503 response, so the check fails without `curl` in the image. Plain Docker only marks the container `unhealthy`; it does not restart it. Kubernetes restarts a container when its liveness probe fails.

## Work handed to the main loop

`shutdown()` does not wait for work that a handler handed to the main loop with `run_on_main`. That work is not a job. If the loop closes right after `shutdown()`, the loop cancels it. To let it finish, wait for it in the lifespan, with a limit that fits the grace period:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler.start()
    yield
    scheduler.shutdown(timeout=5)
    deadline = time.monotonic() + 2
    while scheduler.pending_main_loop_work() and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
```

If the job must include the work, use `call_on_main` instead. The job then waits for the result, and the stop at shutdown cancels the work with the job. See [This work outlives shutdown](run-on-main.md#this-work-outlives-shutdown-and-closing-the-loop-is-yours).

## One process, one pool

- **`pool_size` is a number of threads in one process.** I/O-bound work scales with it. CPU-bound work does not, because the standard CPython build runs one thread of Python code at a time. Process jobs are planned for a later release; see [Phase 7](roadmap.md#phase-7-process-jobs-v150) of the roadmap.
- **Each process runs its own scheduler.** If you start several worker processes, for example `uvicorn --workers 4`, each worker creates a `Quiv` and runs every task. Several replicas of a container do the same. Start the scheduler in one process only.
