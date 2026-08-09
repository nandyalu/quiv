# Failure Handling

quiv provides three per-task knobs for handling slow, failing, or synchronized tasks: `timeout`, `max_retries`/`retry_backoff`, and `jitter`. All are optional parameters of [`add_task()`](api.md#add_task).

## Timeouts

Timeout is **cooperative**: when a job exceeds `timeout` seconds, quiv sets its stop event — exactly as `cancel_job()` would. A handler that checks `_stop_event` exits promptly and the job finalizes as `cancelled` with `error_message = "Job exceeded timeout of {timeout}s"`. A handler that ignores its stop event keeps occupying its pool thread (quiv never kills threads); it still finalizes as `cancelled` when it eventually returns. There is no separate job status for timeouts — they are cancellations with an error message. If a timed-out handler also raises, the timeout message still leads and the handler's exception is appended (`... (handler raised: ...)`), so timeouts stay distinguishable from failures and manual cancellations.

```python
def poll_api(_stop_event):
    while not _stop_event.wait(1):
        do_one_poll()

scheduler.add_task("poll", poll_api, interval=60, timeout=30)
```

Timeouts are enforced by the scheduler loop, which wakes for the soonest pending deadline — enforcement latency is milliseconds, not tied to any polling interval.

!!! tip
    Write handlers to check `_stop_event` regularly (see [Cancellation](cancellation.md)); this makes both `cancel_job()` and timeouts effective.

## Retries

Retries apply to **failed** jobs only — those where an exception escaped the handler. Cancelled jobs, including timeouts, never retry (cancellation is deliberate). A job that raises *and* is cancelled counts as cancelled.

On failure with retries remaining, the next run is scheduled at `now + retry_backoff * 2**(failures_so_far - 1)` — exponential backoff: first retry after `retry_backoff` seconds, the second after twice that, then four times, and so on. Beware that large `max_retries` combined with a large `retry_backoff` grows quickly.

```python
scheduler.add_task("flaky-sync", sync_upstream, interval=300, max_retries=3, retry_backoff=10)
```

- A successful run resets the consecutive-failure counter.
- When retries are exhausted: recurring tasks fall back to their normal interval schedule (counter resets); run-once tasks are deleted.
- Each `Job` records its `attempt` number: `1` is the first try, `2` the first retry, and so on.
- `Event.JOB_RETRYING` fires (after `JOB_FAILED`) whenever a retry has been scheduled, with the usual `(event, task, job)` payload — see [Event Listeners](event-listeners.md).

## Jitter

`jitter=J` adds `uniform(0, J)` seconds to each computed **recurring** next-run time (in both `fixed_interval` modes). Its purpose is to de-synchronize many tasks aligned to the same interval boundaries (the thundering-herd problem). A fresh random offset is drawn for every run, so schedules decorrelate over time.

```python
for shard in range(20):
    scheduler.add_task(f"sync-{shard}", sync_shard, interval=60, jitter=5, args=(shard,))
```

Jitter is **not** applied to the initial `delay` (the caller controls that directly) nor to retry backoff (retries stay deterministic).
