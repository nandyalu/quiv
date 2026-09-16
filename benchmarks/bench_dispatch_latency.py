"""Measure how long a job waits between its due time and its first work.

Run it with::

    uv run python benchmarks/bench_dispatch_latency.py

The script reports two different numbers, because two different things are
worth knowing and one batch cannot show both.

**Wake latency** schedules each task at its own due time, spaced far enough
apart that the scheduler handles one at a time. This is the promptness of
the loop itself: the time from a task becoming due to its handler starting.
Phase 2 of the roadmap replaced a one-second poll with a wait that ends at
the next due time, and this is the number that shows the result.

**Burst dispatch** schedules every task for one shared instant. The loop
wakes once and dispatches the whole batch in a single pass, writing to the
database for each task in turn, so a task late in the batch waits for every
task before it. The per-task figure at the end is the marginal cost of one
more task in the same burst.

Both metrics use ``job.started_at - run_at``. ``started_at`` is written by
the worker thread at the top of the job, so the number covers the whole
path: the loop waking, the dispatch of anything ahead of this task, the
hand-off to the thread pool, and the wait for a free worker.
"""

from __future__ import annotations

import argparse
import logging
import platform
import sys
import threading
from datetime import datetime, timedelta

from quiv import Event, Quiv
from quiv.models import Job, Task

# Saturating the pool is the point of this script, so quiv's "started late"
# warnings are expected. Without a configured handler Python prints them to
# stderr through logging.lastResort, which buries the table. Errors still
# come through.
logging.getLogger("Quiv").setLevel(logging.ERROR)

TARGET_P95_MS = 50.0


def noop() -> None:
    """Do nothing. The benchmark measures quiv, not the handler."""


def percentile(values: list[float], fraction: float) -> float:
    """Return the nearest-rank percentile of ``values``.

    Args:
        values (list[float]): Samples. Must not be empty.
        fraction (float): Position between 0 and 1, so 0.95 is p95.

    Returns:
        float: The sample at that rank.
    """

    ordered = sorted(values)
    rank = max(1, min(len(ordered), round(fraction * len(ordered))))
    return ordered[rank - 1]


def run_batch(
    pool_size: int, offsets_ms: list[float], lead_seconds: float
) -> list[float]:
    """Schedule one task per offset and return each latency in ms.

    Each run anchors its own base instant. Do not hand in absolute times
    computed by the caller: a base that has already passed would be clamped
    to "now" by ``add_task``, and every latency would then be measured from
    the wrong moment.

    Args:
        pool_size (int): Worker threads available to the scheduler.
        offsets_ms (list[float]): Offset of each due time from the base
            instant, in milliseconds.
        lead_seconds (float): Head start before the first due time, so that
            every task is registered before any of them comes due.

    Returns:
        list[float]: One latency in milliseconds for each completed job.
    """

    base = datetime.now().astimezone() + timedelta(seconds=lead_seconds)
    due_times = [base + timedelta(milliseconds=ms) for ms in offsets_ms]
    scheduler = Quiv(pool_size=pool_size)
    expected = len(due_times)
    done = threading.Event()
    completed = 0
    lock = threading.Lock()

    def on_completed(event: Event, task: Task, job: Job) -> None:
        nonlocal completed
        with lock:
            completed += 1
            if completed >= expected:
                done.set()

    try:
        scheduler.add_listener(Event.JOB_COMPLETED, on_completed)

        # Each due time sits in the future, so add_task stores it verbatim
        # instead of clamping it to "now".
        scheduled: dict[str, datetime] = {}
        for i, due_at in enumerate(due_times):
            task_id = scheduler.add_task(
                task_name=f"bench-{i}",
                func=noop,
                run_once=True,
                run_at=due_at,
            )
            scheduled[task_id] = due_at

        # add_task clamps a run_at that has already passed to "now", which
        # would silently measure every latency from the wrong instant. Fail
        # instead of reporting a wrong number.
        overdue = scheduler._now_utc() - min(due_times)
        if overdue.total_seconds() > 0:
            raise SystemExit(
                f"registering {expected} tasks took longer than the"
                f" {lead_seconds}s lead; raise --lead-seconds"
            )

        scheduler.start()
        span = (max(due_times) - min(due_times)).total_seconds()
        if not done.wait(timeout=lead_seconds + span + 60):
            raise SystemExit(
                f"only {completed} of {expected} jobs completed in time"
            )

        return [
            (job.started_at - scheduled[job.task_id]).total_seconds() * 1000
            for job in scheduler.get_all_jobs()
            if job.task_id in scheduled
        ]
    finally:
        scheduler.shutdown(timeout=10)


def report(title: str, note: str, latencies: list[float]) -> None:
    """Print one labelled table of latency percentiles."""

    print(f"  {title}")
    print(f"    {note}")
    print()
    print("    metric        ms")
    print("    --------  ------")
    for label, value in (
        ("min", min(latencies)),
        ("p50", percentile(latencies, 0.50)),
        ("p95", percentile(latencies, 0.95)),
        ("max", max(latencies)),
    ):
        print(f"    {label:<8}  {value:6.2f}")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tasks", type=int, default=100, help="tasks in the burst"
    )
    parser.add_argument(
        "--trials", type=int, default=25, help="tasks in the spaced run"
    )
    parser.add_argument(
        "--gap-ms",
        type=float,
        default=150.0,
        help="spacing between due times in the spaced run",
    )
    parser.add_argument("--pool-size", type=int, default=32)
    parser.add_argument("--lead-seconds", type=float, default=2.0)
    args = parser.parse_args()

    print("quiv — dispatch latency")
    print(
        f"  pool_size={args.pool_size}"
        f"  python={platform.python_version()}"
        f"  platform={platform.system()} {platform.machine()}"
    )
    print()

    spaced = run_batch(
        args.pool_size,
        [i * args.gap_ms for i in range(args.trials)],
        args.lead_seconds,
    )
    report(
        "Wake latency — one task at a time",
        f"{args.trials} tasks, {args.gap_ms:.0f} ms apart",
        spaced,
    )

    burst = run_batch(args.pool_size, [0.0] * args.tasks, args.lead_seconds)
    report(
        "Burst dispatch — every task due at once",
        f"{args.tasks} tasks sharing one due time",
        burst,
    )

    marginal = (max(burst) - min(burst)) / max(1, len(burst) - 1)
    print(f"    marginal cost: {marginal:.2f} ms for each extra task")
    print()

    p95 = percentile(spaced, 0.95)
    verdict = "met" if p95 < TARGET_P95_MS else "MISSED"
    print(f"  target: wake latency p95 < {TARGET_P95_MS:.0f} ms")
    print(f"          {verdict} — p95 is {p95:.2f} ms")
    return 0


if __name__ == "__main__":
    sys.exit(main())
