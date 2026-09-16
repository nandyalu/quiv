"""Run a mixed quiv workload for a long time and check that nothing drifts.

Run a short one while you work on the script::

    uv run python scripts/soak.py --minutes 10

Run the real one once, before tagging a release::

    uv run python scripts/soak.py --hours 24

The workload mixes every execution path that quiv has: a sync task, an
async task, a task that fails and retries, a task that another thread
cancels, a task that passes its timeout, and a task that runs more than
once a second. A separate thread calls ``stats()`` and ``get_all_jobs()``
every second, the way an admin page would.

The script fails, and exits non-zero, on any of these:

- the number of live threads grows past its settled baseline,
- the retained job history grows past what the retention window allows,
- the "Quiv" logger records an ERROR that the failing task does not
  explain,
- the scheduler thread dies.

Nothing is left behind. quiv deletes its temporary database at shutdown.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import platform
import sys
import threading
import time
from datetime import datetime, timezone

from quiv import Event, Quiv
from quiv.models import Job, JobStatus, Task

# The failing task raises this on purpose. An ERROR record that mentions it,
# or the task that produces it, is expected; anything else is a real fault.
FAILURE_MARKER = "soak-expected-failure"
EXPECTED_ERROR_MARKERS = (FAILURE_MARKER, "failing")


class SoakFailure(RuntimeError):
    """Raised by the failing task so that retries have something to do."""


# --------------------------------------------------------------------------
# Handlers
# --------------------------------------------------------------------------


def sync_task(progress_hook=None) -> None:
    """Do a little work and report progress, like an ordinary sync job."""

    total = 0
    for i in range(1000):
        total += i * i
    if progress_hook:
        progress_hook(step=1, total=1, checksum=total)


async def async_task() -> None:
    """Await something, so each run builds and closes its own event loop."""

    await asyncio.sleep(0.01)


def failing_task() -> None:
    """Always raise, so the retry path runs for the whole soak."""

    raise SoakFailure(FAILURE_MARKER)


def cancellable_task(stop_event: threading.Event | None = None) -> None:
    """Wait on the stop event, so the canceller has something to stop."""

    if stop_event is not None:
        stop_event.wait(timeout=3.0)
    else:  # pragma: no cover - the scheduler always injects it here
        time.sleep(3.0)


def timing_out_task(stop_event: threading.Event | None = None) -> None:
    """Outlast the timeout, so quiv cancels the job every time."""

    if stop_event is not None:
        stop_event.wait(timeout=30.0)
    else:  # pragma: no cover - the scheduler always injects it here
        time.sleep(30.0)


def subsecond_task() -> None:
    """Return at once, on an interval shorter than a second."""


# --------------------------------------------------------------------------
# Watchers
# --------------------------------------------------------------------------


class ErrorWatch(logging.Handler):
    """Collect ERROR records that the failing task does not explain."""

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.unexpected: list[str] = []
        self.expected = 0
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        with self._lock:
            if any(m in message for m in EXPECTED_ERROR_MARKERS):
                self.expected += 1
            else:
                self.unexpected.append(message)


def read_rss_kb() -> int | None:
    """Return this process's resident memory in KB, or None if unknown."""

    try:
        with open("/proc/self/status", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except OSError:
        pass
    return None


# --------------------------------------------------------------------------
# The soak
# --------------------------------------------------------------------------


def build_workload(scheduler: Quiv) -> None:
    """Register every task the soak runs."""

    scheduler.add_task("soak-sync", sync_task, interval=2.0)
    scheduler.add_task("soak-async", async_task, interval=3.0)
    scheduler.add_task(
        "soak-failing",
        failing_task,
        interval=5.0,
        max_retries=2,
        retry_backoff=1.0,
    )
    scheduler.add_task("soak-cancelled", cancellable_task, interval=4.0)
    scheduler.add_task("soak-timeout", timing_out_task, interval=6.0, timeout=1.0)
    scheduler.add_task("soak-subsecond", subsecond_task, interval=0.5, jitter=0.1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--minutes", type=float, default=10.0)
    parser.add_argument(
        "--hours", type=float, default=None, help="overrides --minutes"
    )
    parser.add_argument("--pool-size", type=int, default=8)
    parser.add_argument(
        "--retention-seconds",
        type=int,
        default=600,
        help="short by default, so history cleanup runs during the soak",
    )
    parser.add_argument(
        "--settle-seconds",
        type=float,
        default=60.0,
        help="time before the thread baseline is fixed",
    )
    parser.add_argument(
        "--report-every",
        type=float,
        default=300.0,
        help="seconds between progress lines",
    )
    args = parser.parse_args()

    duration = args.hours * 3600 if args.hours is not None else args.minutes * 60

    watch = ErrorWatch()
    quiv_logger = logging.getLogger("Quiv")
    quiv_logger.addHandler(watch)
    quiv_logger.setLevel(logging.ERROR)

    scheduler = Quiv(
        pool_size=args.pool_size,
        history_retention_seconds=args.retention_seconds,
    )

    terminal = {"completed": 0, "failed": 0, "cancelled": 0, "retrying": 0}
    counter_lock = threading.Lock()

    def count(key: str):
        def listener(event: Event, task: Task, job: Job) -> None:
            with counter_lock:
                terminal[key] += 1

        return listener

    scheduler.add_listener(Event.JOB_COMPLETED, count("completed"))
    scheduler.add_listener(Event.JOB_FAILED, count("failed"))
    scheduler.add_listener(Event.JOB_CANCELLED, count("cancelled"))
    scheduler.add_listener(Event.JOB_RETRYING, count("retrying"))

    stop = threading.Event()
    observed = {
        "max_threads": 0,
        "max_history": 0,
        "max_rss_kb": 0,
        "poll_errors": [],
        "scheduler_died": False,
    }

    def admin_poller() -> None:
        """Read the scheduler once a second, the way a dashboard would."""

        while not stop.wait(1.0):
            try:
                stats = scheduler.stats()
                scheduler.get_all_jobs(limit=20)
                observed["max_threads"] = max(
                    observed["max_threads"], threading.active_count()
                )
                observed["max_history"] = max(
                    observed["max_history"], stats.job_history_count
                )
                rss = read_rss_kb()
                if rss:
                    observed["max_rss_kb"] = max(observed["max_rss_kb"], rss)
                if not scheduler.thread.is_alive():
                    observed["scheduler_died"] = True
                    stop.set()
            except Exception as exc:  # noqa: BLE001 - recorded, not raised
                observed["poll_errors"].append(f"{type(exc).__name__}: {exc}")

    def canceller() -> None:
        """Cancel the running job of the cancellable task now and then."""

        while not stop.wait(5.0):
            try:
                for job in scheduler.get_all_jobs(status=JobStatus.RUNNING):
                    if job.task_name == "soak-cancelled" and job.id:
                        scheduler.cancel_job(job.id)
            except Exception as exc:  # noqa: BLE001 - recorded, not raised
                observed["poll_errors"].append(f"{type(exc).__name__}: {exc}")

    started_at = datetime.now(timezone.utc)
    began = time.monotonic()

    print("quiv — soak test")
    print(f"  started      {started_at.isoformat(timespec='seconds')}")
    print(f"  duration     {duration / 3600:.2f} h")
    print(f"  pool_size    {args.pool_size}")
    print(f"  retention    {args.retention_seconds} s")
    print(f"  python       {platform.python_version()} on {platform.system()}")
    print(f"  pid          {__import__('os').getpid()}", flush=True)

    baseline_threads = 0
    try:
        build_workload(scheduler)
        scheduler.start()

        threading.Thread(target=admin_poller, daemon=True).start()
        threading.Thread(target=canceller, daemon=True).start()

        # Let the pool reach its working size before fixing the baseline.
        settle = min(args.settle_seconds, duration / 4)
        stop.wait(settle)
        baseline_threads = threading.active_count()
        observed["max_threads"] = baseline_threads
        print(f"  baseline threads after {settle:.0f}s: {baseline_threads}")
        print(flush=True)

        next_report = time.monotonic() + args.report_every
        while time.monotonic() - began < duration:
            if stop.wait(1.0):
                break
            if time.monotonic() >= next_report:
                elapsed = time.monotonic() - began
                with counter_lock:
                    snapshot = dict(terminal)
                print(
                    f"  +{elapsed / 3600:6.2f} h  threads={threading.active_count():3d}"
                    f"  history={observed['max_history']:6d}"
                    f"  completed={snapshot['completed']:7d}"
                    f"  failed={snapshot['failed']:6d}"
                    f"  cancelled={snapshot['cancelled']:6d}"
                    f"  rss={observed['max_rss_kb'] // 1024:5d} MB",
                    flush=True,
                )
                next_report += args.report_every
    finally:
        stop.set()
        scheduler.shutdown(timeout=30)
        quiv_logger.removeHandler(watch)

    elapsed = time.monotonic() - began
    with counter_lock:
        snapshot = dict(terminal)
    total_jobs = (
        snapshot["completed"] + snapshot["failed"] + snapshot["cancelled"]
    )
    rate = total_jobs / elapsed if elapsed else 0.0

    # A job stays in history for the retention window. Cleanup runs once a
    # minute, so allow the window plus two cycles, and double it for the
    # bursts that retries and jitter produce.
    history_bound = int(rate * (args.retention_seconds + 120) * 2) + 100

    # A ThreadPoolExecutor creates its workers on demand, so the baseline can
    # be taken before the pool has reached full size. Allow for a full pool
    # plus the four threads this script runs, or the settled baseline if that
    # is higher. A real leak grows without limit and clears this easily; a
    # bound drawn only from the baseline would fail a healthy 24-hour run.
    thread_bound = max(baseline_threads, 4 + args.pool_size) + 5

    failures = []
    if observed["scheduler_died"]:
        failures.append("the scheduler thread stopped before the soak ended")
    if observed["max_threads"] > thread_bound:
        failures.append(
            f"threads peaked at {observed['max_threads']}, over the bound of"
            f" {thread_bound} (baseline {baseline_threads})"
        )
    if observed["max_history"] > history_bound:
        failures.append(
            f"job history peaked at {observed['max_history']}, over the bound"
            f" of {history_bound} for a {args.retention_seconds}s window"
        )
    if watch.unexpected:
        failures.append(
            f"{len(watch.unexpected)} unexpected ERROR record(s); first:"
            f" {watch.unexpected[0]}"
        )
    if observed["poll_errors"]:
        failures.append(
            f"{len(observed['poll_errors'])} error(s) from the reader threads;"
            f" first: {observed['poll_errors'][0]}"
        )

    print()
    print("  summary")
    print(f"    ran for            {elapsed / 3600:.2f} h")
    print(f"    jobs finished      {total_jobs} ({rate:.1f}/s)")
    print(f"      completed        {snapshot['completed']}")
    print(f"      failed           {snapshot['failed']}")
    print(f"      cancelled        {snapshot['cancelled']}")
    print(f"      retries queued   {snapshot['retrying']}")
    print(f"    threads            {baseline_threads} baseline,"
          f" {observed['max_threads']} peak, bound {thread_bound}")
    print(f"    job history        {observed['max_history']} peak,"
          f" bound {history_bound}")
    print(f"    peak RSS           {observed['max_rss_kb'] // 1024} MB")
    print(f"    expected ERRORs    {watch.expected} (the failing task)")
    print(f"    unexpected ERRORs  {len(watch.unexpected)}")
    print()

    if failures:
        print("  RESULT: FAIL")
        for line in failures:
            print(f"    - {line}")
        return 1
    print("  RESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
