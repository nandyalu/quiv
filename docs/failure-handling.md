# Failure Handling

quiv has three optional settings for a task that runs too long, fails, or starts at the same moment as many other tasks: `timeout`, `max_retries` with `retry_backoff`, and `jitter`. All three are parameters of [`add_task()`](api.md#add_task).

## Timeouts

A timeout is cooperative. When a job runs for longer than `timeout` seconds, quiv sets the stop event of that job. This is the same signal that `cancel_job()` sends.

What happens next depends on the handler:

- The handler checks `stop_event` and returns. The job finalizes as `cancelled`, and `error_message` reads `"Job exceeded timeout of {timeout}s"`.
- The handler ignores `stop_event`. It keeps its thread in the pool until it returns, because quiv never kills a thread. The job still finalizes as `cancelled`.

There is no separate job status for a timeout. A timeout is a cancellation that carries an error message.

If a handler passes its timeout and also raises, the timeout message comes first, and quiv adds the exception after it: `... (handler raised: ...)`. This keeps a timeout easy to tell apart from a failure and from a manual cancellation.

```python
def poll_api(stop_event):
    while not stop_event.wait(1):
        do_one_poll()

scheduler.add_task("poll", poll_api, interval=60, timeout=30)
```

The scheduler loop enforces timeouts. It wakes for the deadline that comes soonest, so it reacts in milliseconds. No polling interval limits it.

!!! tip
    Check `stop_event` often in your handlers. See [Cancellation](cancellation.md). The same check makes `cancel_job()` and timeouts both work.

## Retries

quiv retries a job only when the job **fails**. A job fails when an exception left the handler, or when a database error stopped the job before the handler started (see [When the database fails](#when-the-database-fails)). A cancelled job never retries, because a cancellation is deliberate, and a timeout is a cancellation. A job that raises and is cancelled as well counts as cancelled.

When a job fails and retries remain, quiv schedules the next run at `now + retry_backoff * 2**(failures_so_far - 1)`. The delay doubles every time: `retry_backoff` seconds before the first retry, twice that before the second, and four times that before the third.

The delay grows quickly. A large `max_retries` together with a large `retry_backoff` can move the next run hours into the future.

quiv never schedules a run later than 30 December 9999 (UTC), because Python cannot represent a date after year 9999. A retry backoff, an `interval`, a `jitter`, or a `delay` that goes past that date is clamped to it. The task stays `active`, so `run_task_immediately()` still runs it, and a run that succeeds resets the counter.

```python
scheduler.add_task("flaky-sync", sync_upstream, interval=300, max_retries=3, retry_backoff=10)
```

- A run that succeeds resets the counter of consecutive failures.
- When the retries run out, a recurring task returns to its normal interval and the counter resets. quiv deletes a run-once task.
- Each `Job` records its `attempt` number. `1` is the first try, and `2` is the first retry.
- `Event.JOB_RETRYING` fires after `JOB_FAILED` when quiv schedules a retry. It carries the usual `(event, task, job)` payload. See [Event Listeners](event-listeners.md).

## Jitter

`jitter=J` adds a random offset of 0 to `J` seconds to each recurring next-run time. It applies in both `fixed_interval` modes.

Use jitter when many tasks share one interval. Without it, those tasks align to the same interval boundaries and start at the same moment, which puts all of the load in one instant. quiv draws a new random offset for every run, so the schedules move apart over time.

```python
for shard in range(20):
    scheduler.add_task(f"sync-{shard}", sync_shard, interval=60, jitter=5, args=(shard,))
```

quiv does not add jitter to the initial `delay`, because the caller sets that value directly. quiv does not add jitter to the retry backoff either, so retries stay predictable.

## When the database fails

quiv keeps its tasks and jobs in a temporary SQLite database. A read or a write on that database can fail, for example when the disk is full or when the database stays locked on slow storage. quiv cannot prevent this. It limits the cost of one error to one job, and the scheduler continues to run:

- **Before the handler starts.** The handler does not run. The job finalizes as `failed`, `error_message` holds the database error, and `JOB_FAILED` fires. The task returns to `active`, so `max_retries` applies and the next run starts on schedule.
- **While the loop dispatches a task.** The loop logs `Error in scheduler loop` and waits 5 seconds. The task returns to `active`, and the loop dispatches it again. If `pause_task()` paused the task in the meantime, the task stays paused.
- **After the handler returns.** quiv logs the error and always frees the worker. If quiv cannot write the end status, the job row keeps the status `running`. If quiv cannot return the task to `active`, the task stays `running` and does not run again until the process restarts. The log line names the task. To find such a task, compare two values: `stats().tasks_by_status.get("running", 0)` stays higher than `stats().active_jobs`. To clear it, call `remove_task()` and then `add_task()`.
- **When quiv cannot write the end status, or cannot read the finished job back.** The `JOB_*` event fires, and `wait_for_job()` and `wait_for_task()` return. Both get a `Job` made from the values that quiv tried to write: the end status, the duration, and the error of the handler. quiv never gives a listener or a waiter a job that still has the status `running`. If the end status was not written, the job row keeps the status `running`. `get_job()` returns that row, and a later call to `wait_for_job()` for that job waits until its timeout.
- **During the history cleanup.** quiv logs `Job history cleanup failed`, and tries again 60 seconds later. Dispatch continues.
