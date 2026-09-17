# Benchmarks

Two scripts measure the parts of quiv that a user feels: how soon a job starts after it becomes due, and how many jobs finish each second.

They are plain scripts, not tests. CI does not run them, because a timing assertion on a shared runner fails for reasons that have nothing to do with the code.

## Running them

```bash
uv run python benchmarks/bench_dispatch_latency.py
uv run python benchmarks/bench_throughput.py
```

Each script accepts `--help`. The useful switches are `--tasks`, `--pool-size`, and, for the latency script, `--trials` and `--gap-ms`.

Neither script leaves anything behind. Each one creates its own temporary SQLite database, and `shutdown()` deletes it.

## What the numbers mean

### Dispatch latency

The script reports two figures, because one batch cannot show both.

**Wake latency** gives each task its own due time, far enough apart that the scheduler handles one at a time. This measures the promptness of the loop: the time from a task becoming due to its handler starting. Phase 2 of the roadmap replaced a poll every second with a wait that ends at the next due time, and this number shows the result.

**Burst dispatch** gives every task the same due time. The loop wakes once and dispatches the whole batch in one pass, writing to the database for each task in turn, so a task at the end of the batch waits for every task before it. The marginal figure is the cost of one more task in the same burst.

Both use `job.started_at - run_at`. The worker thread writes `started_at` at the top of the job, so the number covers the whole path: the loop waking, the dispatch of every task ahead of this one, the hand-off to the thread pool, and the wait for a free worker.

### Throughput

Every task is registered before `start()`, so the cost of `add_task` stays outside the measured window. The clock runs from `start()` to the last `JOB_COMPLETED` event. Every task is due by then, so the scheduler works without pause.

The **no-op** handler returns at once, so the rate is set by quiv alone. The **10 ms sleep** handler holds a worker for a known time, which sets a ceiling of `pool_size / 0.01` jobs per second.

## Recorded numbers

Measured on 2026-09-16 for the `v1.0.0` release.

Machine: Intel Core i5-11600 at 2.80 GHz, 12 logical CPUs, 15 GB RAM, Linux x86_64, Python 3.10.12, SQLite in WAL mode on a local disk.

### Dispatch latency

`--trials 25 --gap-ms 150 --tasks 100 --pool-size 32`

| Measurement | min | p50 | p95 | max |
| --- | --- | --- | --- | --- |
| Wake latency, one task at a time | 3.3 ms | 14.3 ms | 18.0 ms | 21.2 ms |
| Burst dispatch, 100 tasks at once | 10.0 ms | 317 ms | 489 ms | 509 ms |

Target for wake latency: p95 below 50 ms. **Met**, at 18 ms.

The burst figures are large for a reason that the marginal cost explains: about 5 ms for each extra task in the batch. A task at position 100 waits for the 99 dispatches before it. Wake latency is stable between runs, within about 2 ms. The burst figure moves more, between about 4 ms and 6 ms per task, and an occasional run reaches 13 ms.

### Throughput

`--tasks 1000 --pool-size 16`

| Handler | Run time | Jobs per second | Ceiling from the pool |
| --- | --- | --- | --- |
| no-op | 4.98 s | 201 | — |
| 10 ms sleep | 5.60 s | 179 | 1600 |

Registering the 1000 tasks took about 1.0 s, which is roughly 1000 `add_task` calls per second. That time is outside the measured run.

## What limits the rate

The two handlers finish at almost the same rate, although one of them sleeps for 10 ms and the other returns at once. A larger pool does not help either:

| `pool_size` | no-op | 10 ms sleep |
| --- | --- | --- |
| 4 | 211 jobs/s | 142 jobs/s |
| 16 | 175 jobs/s | 144 jobs/s |
| 64 | 241 jobs/s | 161 jobs/s |

Sixteen times more workers gives no gain. The limit is therefore not the pool, and not the handler.

The limit is the database. Each job writes four times: it creates the job row, marks the job running, finalizes the job, and finalizes the task. Reads run without a lock under WAL, but a write takes the writer lock, so those four writes queue behind the writes of every other job. At roughly 5 ms of serialized writing for each job, about 200 jobs per second is the ceiling.

Two practical consequences:

- Raising `pool_size` helps only when your handlers wait on something. It does not raise the rate of short jobs.
- quiv suits jobs that last longer than a few milliseconds. For very short jobs in very large numbers, the bookkeeping costs more than the work.

## Soak results

`results/` holds the log of the 24-hour soak that ran before the `v1.0.0` release. The soak is not a benchmark: it runs a mixed workload for a day and checks that threads, the job history, and memory all stay flat. `scripts/soak.py` runs it, and [the Testing page](../docs/testing.md#soak-test) describes the method and the result.

## Reading these numbers

Treat them as one machine on one day, not as a specification.

- An idle desktop and a shared CI runner give different numbers. Compare a change against a run on the same machine, not against this table.
- SQLite writes reach the disk. A slower disk lowers the throughput figures.
- Run each script twice and use the second result. The first run pays for imports and for creating the database.
- Update this section at release time, and say which machine produced the numbers.
