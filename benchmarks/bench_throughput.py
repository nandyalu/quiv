"""Measure how many jobs quiv completes each second at pool saturation.

Run it with::

    uv run python benchmarks/bench_throughput.py

Every task is registered before the scheduler starts, so the cost of
``add_task`` stays out of the measured window. The clock runs from
``start()` until the last job reports ``JOB_COMPLETED``. Each task is due
by then, so the scheduler works without pause and the pool stays full.

Two handlers run, because they show different limits:

**no-op** returns at once, so nothing waits on the handler. The rate is
then set by quiv itself: the dispatch loop, and the database writes that
each job makes to start and to finish.

**10 ms sleep** holds a worker for a known time. With a pool of 16 the
ceiling is 16 jobs every 10 ms, or 1600 per second. The distance between
that ceiling and the measured rate is the overhead quiv adds for each job.
"""

from __future__ import annotations

import argparse
import logging
import platform
import sys
import threading
import time

from quiv import Event, Quiv
from quiv.models import Job, Task

# Saturating the pool is the point of this script, so quiv's "started late"
# warnings are expected. Without a configured handler Python prints them to
# stderr through logging.lastResort, which buries the table. Errors still
# come through.
logging.getLogger("Quiv").setLevel(logging.ERROR)


def noop() -> None:
    """Return at once, so the handler never limits the rate."""


def sleep_10ms() -> None:
    """Hold a worker for 10 ms, so the pool size sets the ceiling."""

    time.sleep(0.01)


def measure(
    handler: object, tasks: int, pool_size: int
) -> tuple[float, float]:
    """Run one workload and return its setup and run times in seconds.

    Args:
        handler (object): Callable to schedule for every task.
        tasks (int): Number of run-once tasks.
        pool_size (int): Worker threads available to the scheduler.

    Returns:
        tuple[float, float]: Seconds spent registering the tasks, and
            seconds from ``start()`` to the last completed job.
    """

    scheduler = Quiv(pool_size=pool_size)
    done = threading.Event()
    completed = 0
    lock = threading.Lock()

    def on_completed(event: Event, task: Task, job: Job) -> None:
        nonlocal completed
        with lock:
            completed += 1
            if completed >= tasks:
                done.set()

    try:
        scheduler.add_listener(Event.JOB_COMPLETED, on_completed)

        setup_begin = time.perf_counter()
        for i in range(tasks):
            scheduler.add_task(
                task_name=f"bench-{i}",
                func=handler,  # type: ignore[arg-type]
                run_once=True,
            )
        setup = time.perf_counter() - setup_begin

        # Every task is already due, so the loop runs flat out from here.
        run_begin = time.perf_counter()
        scheduler.start()
        if not done.wait(timeout=600):
            raise SystemExit(
                f"only {completed} of {tasks} jobs completed in time"
            )
        return setup, time.perf_counter() - run_begin
    finally:
        scheduler.shutdown(timeout=30)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", type=int, default=1000)
    parser.add_argument("--pool-size", type=int, default=16)
    args = parser.parse_args()

    print("quiv — throughput")
    print(
        f"  tasks={args.tasks}  pool_size={args.pool_size}"
        f"  python={platform.python_version()}"
        f"  platform={platform.system()} {platform.machine()}"
    )
    print()
    print("  handler        setup s    run s    jobs/s    ceiling")
    print("  -----------  ---------  -------  --------  ---------")

    for label, handler, ceiling in (
        ("no-op", noop, None),
        ("10 ms sleep", sleep_10ms, args.pool_size / 0.01),
    ):
        setup, run = measure(handler, args.tasks, args.pool_size)
        rate = args.tasks / run
        ceiling_text = f"{ceiling:9.0f}" if ceiling else "        —"
        print(
            f"  {label:<11}  {setup:9.2f}  {run:7.2f}  {rate:8.1f}"
            f"  {ceiling_text}"
        )

    print()
    print(
        "  setup s is the time to register every task before start(); it is"
        " outside the measured run."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
