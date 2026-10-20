"""Measure what a process job costs over a thread job.

Run it with::

    uv run python benchmarks/bench_process_overhead.py

A process job runs in a process that quiv spawns for that job alone. The
spawn starts a new interpreter, which imports quiv and the handler's
module before the handler runs. ``duration_seconds`` of a process job
includes that start, because quiv takes the start time before it spawns.

The script runs run-once no-op jobs one at a time, on the thread pool and
then on the process pool, and reports the median and the 95th percentile
of ``duration_seconds`` for each. The difference is the cost of a process
job on this machine. Spawn is the start method on every platform, so the
number is the same kind of number on Linux, macOS, and Windows, and it
differs between them.
"""

from __future__ import annotations

import argparse
import logging
import platform
import statistics
import sys
import threading
import time

from quiv import Event, Quiv
from quiv.models import Job, Task

# Every task is due at once, so quiv's "started late" warnings are expected.
logging.getLogger("Quiv").setLevel(logging.ERROR)


def noop() -> None:
    """Return at once. A spawned child imports this module to find it."""


def measure(executor: str, jobs: int) -> list[float]:
    """Run ``jobs`` no-op run-once tasks one at a time; return the durations.

    Args:
        executor (str): ``"thread"`` or ``"process"``.
        jobs (int): Number of jobs.

    Returns:
        list[float]: ``duration_seconds`` of every completed job.
    """

    scheduler = Quiv(pool_size=1, process_pool_size=1)
    done = threading.Event()
    completed = 0
    lock = threading.Lock()

    def on_completed(event: Event, task: Task, job: Job) -> None:
        nonlocal completed
        with lock:
            completed += 1
            if completed >= jobs:
                done.set()

    try:
        scheduler.add_listener(Event.JOB_COMPLETED, on_completed)
        for i in range(jobs):
            scheduler.add_task(
                task_name=f"bench-{executor}-{i}",
                func=noop,
                run_once=True,
                executor=executor,
            )
        scheduler.start()
        if not done.wait(timeout=600):
            raise SystemExit(
                f"only {completed} of {jobs} {executor} jobs completed in time"
            )
        return [
            job.duration_seconds
            for job in scheduler.get_all_jobs(limit=jobs)
            if job.duration_seconds is not None
        ]
    finally:
        scheduler.shutdown(timeout=30)


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return ordered[index]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs", type=int, default=50)
    args = parser.parse_args()

    print("quiv — process job overhead")
    print(
        f"  jobs={args.jobs}  python={platform.python_version()}"
        f"  platform={platform.system()} {platform.machine()}"
    )
    print()
    print("  executor     p50 ms    p95 ms    max ms")
    print("  ---------  --------  --------  --------")

    results: dict[str, list[float]] = {}
    for executor in ("thread", "process"):
        durations = measure(executor, args.jobs)
        results[executor] = durations
        print(
            f"  {executor:<9}  {percentile(durations, 0.5) * 1000:8.1f}"
            f"  {percentile(durations, 0.95) * 1000:8.1f}"
            f"  {max(durations) * 1000:8.1f}"
        )

    overhead = statistics.median(results["process"]) - statistics.median(
        results["thread"]
    )
    print()
    print(
        f"  A process job costs about {overhead * 1000:.0f} ms more than a"
        " thread job at the median: the spawn of a new interpreter and its"
        " imports."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
