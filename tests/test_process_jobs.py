"""Process jobs: one spawned process per job, relayed over a pipe.

Every handler lives in ``quiv_process_handlers`` (a module-level function
a child can import by name). Deadlines are generous: a spawn on a Windows
runner can take more than a second per job.
"""

from __future__ import annotations

import asyncio
import logging
import multiprocessing
import os
import pickle
import threading
import time
from pathlib import Path
from typing import Any, Callable

import pytest

import quiv_process_handlers as handlers
from quiv import Quiv, WorkerProcessError
from quiv import _worker
from quiv.exceptions import QuivError
from quiv.models import Event, Executor, Job, JobStatus

DEADLINE = 20.0
TERMINAL = {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}


def _poll(
    predicate: Callable[[], Any], timeout: float = DEADLINE
) -> Any:
    """Return the first truthy value of ``predicate`` before ``timeout``."""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    return predicate()


def _finished_jobs(scheduler: Quiv, task_id: str, count: int = 1) -> list[Job]:
    """Wait until ``count`` jobs of the task are terminal; oldest first."""

    def done() -> list[Job]:
        jobs = [
            j
            for j in scheduler.get_all_jobs(task_id=task_id, descending=False)
            if j.status in TERMINAL
        ]
        return jobs if len(jobs) >= count else []

    jobs = _poll(done)
    assert len(jobs) >= count, f"{len(jobs)} finished job(s) of {task_id}"
    return list(jobs)


def _running_job(scheduler: Quiv, task_id: str) -> Job:
    """Wait for a RUNNING job of the task whose process has a pid."""

    def running() -> Job | None:
        for job in scheduler.get_all_jobs(
            status=JobStatus.RUNNING, task_id=task_id
        ):
            if job.pid is not None:
                return job
        return None

    job = _poll(running)
    assert job is not None, f"no running job of {task_id}"
    return job


def _no_children() -> bool:
    """Whether every spawned child has been reaped."""

    return _poll(lambda: not multiprocessing.active_children(), timeout=5)


def test_process_job_completes_and_records_pid(
    running_main_loop: asyncio.AbstractEventLoop, tmp_path: Path
) -> None:
    pid_file = tmp_path / "pid"
    scheduler = Quiv(process_pool_size=1, main_loop=running_main_loop)
    try:
        scheduler.start()
        task_id = scheduler.add_task(
            "pid",
            handlers.write_pid,
            interval=60,
            args=(str(pid_file),),
            executor="process",
        )
        job = scheduler.wait_for_task(task_id, timeout=DEADLINE)
        assert job.status == JobStatus.COMPLETED
        assert job.executor == Executor.PROCESS
        assert isinstance(job.pid, int)
        assert job.pid != os.getpid()
        assert pid_file.read_text() == str(job.pid)
        assert job.error_message is None
        assert scheduler.get_task(task_id).executor == "process"
    finally:
        scheduler.shutdown()


def test_process_job_exception_is_failed_with_message_and_traceback(
    running_main_loop: asyncio.AbstractEventLoop,
    caplog: pytest.LogCaptureFixture,
) -> None:
    scheduler = Quiv(process_pool_size=1, main_loop=running_main_loop)
    try:
        scheduler.start()
        with caplog.at_level(logging.ERROR, logger="Quiv"):
            task_id = scheduler.add_task(
                "boom", handlers.raise_value_error, run_once=True,
                executor="process",
            )
            (job,) = _finished_jobs(scheduler, task_id)
        assert job.status == JobStatus.FAILED
        assert job.error_message == "boom"
        # The child's traceback, not the waiter's, is what the log holds.
        assert any(
            "raise_value_error" in r.message and "ValueError: boom" in r.message
            for r in caplog.records
        )
    finally:
        scheduler.shutdown()


def test_job_cancelled_error_without_a_stop_is_a_failure(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(process_pool_size=1, main_loop=running_main_loop)
    try:
        scheduler.start()
        task_id = scheduler.add_task(
            "cancelled-by-nobody", handlers.raise_job_cancelled,
            run_once=True, executor="process",
        )
        (job,) = _finished_jobs(scheduler, task_id)
        assert job.status == JobStatus.FAILED
        assert job.error_message == "nobody asked"
    finally:
        scheduler.shutdown()


def test_process_job_retries_then_succeeds(
    running_main_loop: asyncio.AbstractEventLoop, tmp_path: Path
) -> None:
    counter = tmp_path / "counter"
    scheduler = Quiv(process_pool_size=1, main_loop=running_main_loop)
    try:
        scheduler.start()
        task_id = scheduler.add_task(
            "flaky",
            handlers.fail_until,
            run_once=True,
            args=(str(counter), 2),
            max_retries=1,
            retry_backoff=0.1,
            executor="process",
        )
        first, second = _finished_jobs(scheduler, task_id, count=2)
        assert (first.status, first.attempt) == (JobStatus.FAILED, 1)
        assert first.error_message == "attempt 1 fails"
        assert (second.status, second.attempt) == (JobStatus.COMPLETED, 2)
        assert counter.read_text() == "2"
    finally:
        scheduler.shutdown()


def test_process_job_crash_is_failed(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(process_pool_size=1, main_loop=running_main_loop)
    try:
        scheduler.start()
        task_id = scheduler.add_task(
            "crash", handlers.crash, run_once=True, args=(3,),
            executor="process",
        )
        (job,) = _finished_jobs(scheduler, task_id)
        assert job.status == JobStatus.FAILED
        assert job.error_message is not None
        assert "exit code 3" in job.error_message
    finally:
        scheduler.shutdown()


def test_process_job_killed_by_a_signal_is_failed(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(process_pool_size=1, main_loop=running_main_loop)
    try:
        scheduler.start()
        task_id = scheduler.add_task(
            "killed", handlers.kill_self, run_once=True, executor="process"
        )
        (job,) = _finished_jobs(scheduler, task_id)
        assert job.status == JobStatus.FAILED
        assert job.error_message is not None
        if os.name == "nt":
            # os.kill on Windows ends the process with the signal number
            # as its exit code; there is no signal to report.
            assert "exit code" in job.error_message
        else:
            assert job.error_message == "process killed by signal 9"
    finally:
        scheduler.shutdown()


def test_process_job_cooperative_cancel_is_not_terminated(
    running_main_loop: asyncio.AbstractEventLoop,
    caplog: pytest.LogCaptureFixture,
) -> None:
    scheduler = Quiv(
        process_pool_size=1, process_kill_grace=30, main_loop=running_main_loop
    )
    try:
        scheduler.start()
        task_id = scheduler.add_task(
            "cooperative", handlers.wait_for_stop, interval=60,
            executor="process",
        )
        running = _running_job(scheduler, task_id)
        assert running.id is not None
        with caplog.at_level(logging.WARNING, logger="Quiv"):
            assert scheduler.cancel_job(running.id)
            job = scheduler.wait_for_job(running.id, timeout=DEADLINE)
        assert job.status == JobStatus.CANCELLED
        assert job.error_message is None
        assert not any("terminated" in r.message for r in caplog.records)
        assert _no_children()
    finally:
        scheduler.shutdown()


def test_process_job_timeout_terminates_after_grace(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(
        process_pool_size=1, process_kill_grace=0.5, main_loop=running_main_loop
    )
    try:
        scheduler.start()
        task_id = scheduler.add_task(
            "stubborn",
            handlers.sleep_ignoring_stop,
            interval=60,
            args=(30,),
            timeout=0.3,
            executor="process",
        )
        began = time.monotonic()
        job = scheduler.wait_for_task(task_id, timeout=DEADLINE)
        assert time.monotonic() - began < 10
        assert job.status == JobStatus.CANCELLED
        assert job.error_message is not None
        assert job.error_message.startswith("Job exceeded timeout of 0.3s")
        assert "process terminated" in job.error_message
        assert "ignored its stop event for 0.5s" in job.error_message
        assert _no_children()
        # A terminated job is cancelled, so it does not retry: the task
        # waits for its interval.
        task = scheduler.get_task(task_id)
        assert task.retry_attempt == 0
    finally:
        scheduler.shutdown()


def test_process_kill_grace_zero_terminates_on_next_tick(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(
        process_pool_size=1, process_kill_grace=0, main_loop=running_main_loop
    )
    try:
        scheduler.start()
        task_id = scheduler.add_task(
            "stubborn", handlers.sleep_ignoring_stop, interval=60,
            args=(30,), executor="process",
        )
        running = _running_job(scheduler, task_id)
        assert running.id is not None
        began = time.monotonic()
        scheduler.cancel_job(running.id)
        job = scheduler.wait_for_job(running.id, timeout=DEADLINE)
        assert time.monotonic() - began < 5
        assert job.status == JobStatus.CANCELLED
        assert job.error_message == (
            "process terminated: ignored its stop event for 0s"
        )
    finally:
        scheduler.shutdown()


def test_remove_task_terminates_running_process_job(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(
        process_pool_size=1, process_kill_grace=0.3, main_loop=running_main_loop
    )
    try:
        scheduler.start()
        task_id = scheduler.add_task(
            "removed", handlers.sleep_ignoring_stop, interval=60,
            args=(30,), executor="process",
        )
        running = _running_job(scheduler, task_id)
        assert running.id is not None
        scheduler.remove_task(task_id)
        job = scheduler.wait_for_job(running.id, timeout=DEADLINE)
        assert job.status == JobStatus.CANCELLED
        assert job.error_message is not None
        assert "process terminated" in job.error_message
        assert _no_children()
    finally:
        scheduler.shutdown()


def test_progress_hook_crosses_the_pipe(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    seen: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def on_progress(*args: Any, **kwargs: Any) -> None:
        assert asyncio.get_running_loop() is running_main_loop
        seen.append((args, kwargs))

    scheduler = Quiv(process_pool_size=1, main_loop=running_main_loop)
    try:
        scheduler.start()
        task_id = scheduler.add_task(
            "progress",
            handlers.report_progress,
            run_once=True,
            kwargs={"count": 3},
            progress_callback=on_progress,
            executor="process",
        )
        (job,) = _finished_jobs(scheduler, task_id)
        assert job.status == JobStatus.COMPLETED
        assert _poll(lambda: len(seen) == 3)
        assert seen == [
            ((0,), {"label": "step-0"}),
            ((1,), {"label": "step-1"}),
            ((2,), {"label": "step-2"}),
        ]
    finally:
        scheduler.shutdown()


def test_unpicklable_progress_payload_fails_the_job(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(process_pool_size=1, main_loop=running_main_loop)
    try:
        scheduler.start()
        task_id = scheduler.add_task(
            "bad-progress",
            handlers.progress_unpicklable,
            run_once=True,
            progress_callback=lambda *a, **k: None,
            executor="process",
        )
        (job,) = _finished_jobs(scheduler, task_id)
        assert job.status == JobStatus.FAILED
        assert job.error_message is not None
        assert "progress_hook payload must be picklable" in job.error_message
    finally:
        scheduler.shutdown()


def test_payload_the_parent_cannot_unpickle_fails_the_job_and_kills_the_child(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(
        process_pool_size=1, process_kill_grace=30, main_loop=running_main_loop
    )
    try:
        scheduler.start()
        task_id = scheduler.add_task(
            "parent-cannot-read",
            handlers.progress_unpicklable_in_parent,
            run_once=True,
            progress_callback=lambda *a, **k: None,
            executor="process",
        )
        began = time.monotonic()
        (job,) = _finished_jobs(scheduler, task_id)
        # The child was asleep for 30 s; the parent did not wait for it.
        assert time.monotonic() - began < 15
        assert job.status == JobStatus.FAILED
        assert job.error_message is not None
        assert "quiv_test_child_only_module" in job.error_message
        assert _no_children()
    finally:
        scheduler.shutdown()


def test_run_on_main_from_process_job(
    running_main_loop: asyncio.AbstractEventLoop, tmp_path: Path
) -> None:
    marker = tmp_path / "marker"
    scheduler = Quiv(process_pool_size=1, main_loop=running_main_loop)
    try:
        scheduler.start()
        task_id = scheduler.add_task(
            "run-on-main", handlers.call_run_on_main, run_once=True,
            args=(str(marker),), executor="process",
        )
        (job,) = _finished_jobs(scheduler, task_id)
        assert job.status == JobStatus.COMPLETED
        # The target runs on the parent's main loop, so it may land after
        # the job has finished.
        assert _poll(marker.exists)
        assert marker.read_text() == str(os.getpid())
    finally:
        scheduler.shutdown()


def test_run_on_main_from_process_job_without_a_main_loop_is_logged(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    marker = tmp_path / "marker"
    # No main loop anywhere: the parent cannot run the callable, and the
    # child cannot be told, because its message has already been sent.
    scheduler = Quiv(process_pool_size=1)
    try:
        scheduler.start()
        with caplog.at_level(logging.ERROR, logger="Quiv"):
            task_id = scheduler.add_task(
                "run-on-main", handlers.call_run_on_main, run_once=True,
                args=(str(marker),), executor="process",
            )
            (job,) = _finished_jobs(scheduler, task_id)
        assert job.status == JobStatus.COMPLETED
        assert any(
            "run_on_main() from a process job" in r.message
            for r in caplog.records
        )
        assert not marker.exists()
    finally:
        scheduler.shutdown()


def test_run_on_main_lambda_in_process_job_raises(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(process_pool_size=1, main_loop=running_main_loop)
    try:
        scheduler.start()
        task_id = scheduler.add_task(
            "lambda", handlers.run_on_main_lambda, run_once=True,
            executor="process",
        )
        (job,) = _finished_jobs(scheduler, task_id)
        assert job.status == JobStatus.FAILED
        assert job.error_message is not None
        assert "run_on_main() function and arguments" in job.error_message
    finally:
        scheduler.shutdown()


def test_async_handler_in_process_job(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(process_pool_size=1, main_loop=running_main_loop)
    try:
        scheduler.start()
        task_id = scheduler.add_task(
            "async", handlers.async_noop, run_once=True, executor="process"
        )
        (job,) = _finished_jobs(scheduler, task_id)
        assert job.status == JobStatus.COMPLETED
    finally:
        scheduler.shutdown()


def test_run_subprocess_in_process_job_honours_the_stop_event(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    # The grace is long, so a quick end proves the stop event reached
    # run_subprocess inside the child, not that quiv killed the child.
    scheduler = Quiv(
        process_pool_size=1, process_kill_grace=30, main_loop=running_main_loop
    )
    try:
        scheduler.start()
        task_id = scheduler.add_task(
            "subprocess", handlers.subprocess_until_stop, interval=60,
            executor="process",
        )
        running = _running_job(scheduler, task_id)
        assert running.id is not None
        time.sleep(0.5)  # let the grandchild start
        began = time.monotonic()
        scheduler.cancel_job(running.id)
        job = scheduler.wait_for_job(running.id, timeout=DEADLINE)
        assert time.monotonic() - began < 10
        assert job.status == JobStatus.CANCELLED
        # JobCancelledError raised in the child with the stop requested is
        # the same clean stop as in a thread job.
        assert job.error_message is None
        assert _no_children()
    finally:
        scheduler.shutdown()


def test_backpressure_is_per_pool(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(
        pool_size=1, process_pool_size=1, main_loop=running_main_loop
    )
    try:
        scheduler.start()
        # A long thread job must not delay a due process task.
        slow = scheduler.add_task(
            "slow-thread", handlers.sleep_seconds, run_once=True, args=(3,)
        )
        # A second thread task waits for the slot; the loop skips it and
        # goes on to the process task instead of stopping the pass.
        queued = scheduler.add_task(
            "queued-thread", handlers.noop, run_once=True
        )
        quick = scheduler.add_task(
            "quick-process", handlers.noop, run_once=True, executor="process"
        )
        (quick_job,) = _finished_jobs(scheduler, quick)
        (slow_job,) = _finished_jobs(scheduler, slow)
        _finished_jobs(scheduler, queued)
        assert quick_job.ended_at is not None and slow_job.ended_at is not None
        assert quick_job.ended_at < slow_job.ended_at

        # And a long process job must not delay a due thread task.
        slow = scheduler.add_task(
            "slow-process", handlers.sleep_seconds, run_once=True,
            args=(3,), executor="process",
        )
        quick = scheduler.add_task("quick-thread", handlers.noop, run_once=True)
        (quick_job,) = _finished_jobs(scheduler, quick)
        (slow_job,) = _finished_jobs(scheduler, slow)
        assert quick_job.ended_at is not None and slow_job.ended_at is not None
        assert quick_job.ended_at < slow_job.ended_at
    finally:
        scheduler.shutdown()


def test_late_start_warning_names_process_pool(
    running_main_loop: asyncio.AbstractEventLoop,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from datetime import timedelta

    scheduler = Quiv(process_pool_size=1, main_loop=running_main_loop)
    try:
        task_id = scheduler.add_task(
            "late", handlers.noop, interval=60, delay=600, executor="process"
        )
        task = scheduler.get_task(task_id)
        job_id = scheduler.persistence.create_job(
            task_id, "late", executor="process"
        )
        with scheduler._job_count_lock:
            scheduler._active_process_count += 1
        with caplog.at_level(logging.WARNING, logger="Quiv"):
            scheduler._run_process_job(
                job_id,
                task_id,
                "late",
                False,
                scheduler._now_utc() - timedelta(seconds=5),
                task,
                handlers.noop,
                pickle.dumps(()),
                pickle.dumps({}),
                scheduler._mp_context.Event(),
            )
        assert any(
            "process pool was busy" in r.message for r in caplog.records
        )
        assert scheduler.get_job(job_id).status == JobStatus.COMPLETED
    finally:
        scheduler.shutdown()


def test_spawn_failure_is_a_failed_job(
    running_main_loop: asyncio.AbstractEventLoop,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler = Quiv(process_pool_size=1, main_loop=running_main_loop)
    real_process = scheduler._mp_context.Process

    class FailingProcess(real_process):  # type: ignore[misc, valid-type]
        def start(self) -> None:
            raise OSError("no more processes")

    # The spawn context is a module-level singleton; monkeypatch undoes
    # this for the tests after this one.
    monkeypatch.setattr(scheduler._mp_context, "Process", FailingProcess)
    try:
        scheduler.start()
        task_id = scheduler.add_task(
            "unspawnable", handlers.noop, run_once=True, executor="process"
        )
        (job,) = _finished_jobs(scheduler, task_id)
        assert job.status == JobStatus.FAILED
        assert job.error_message == "no more processes"
        assert job.pid is None
        assert scheduler._job_processes == {}
    finally:
        scheduler.shutdown()


def test_shutdown_timeout_terminates_process_jobs(
    running_main_loop: asyncio.AbstractEventLoop,
    leftover_db_paths: list[str],
) -> None:
    scheduler = Quiv(
        process_pool_size=1, process_kill_grace=30, main_loop=running_main_loop
    )
    leftover_db_paths.append(scheduler._db_path)
    scheduler.start()
    task_id = scheduler.add_task(
        "stubborn", handlers.sleep_ignoring_stop, interval=60, args=(30,),
        executor="process",
    )
    _running_job(scheduler, task_id)
    began = time.monotonic()
    scheduler.shutdown(timeout=0.5)
    assert time.monotonic() - began < 5
    assert _no_children()


def test_shutdown_timeout_warns_when_a_terminated_job_is_still_recording(
    running_main_loop: asyncio.AbstractEventLoop,
    leftover_db_paths: list[str],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import quiv.base as base_module

    # No time to record the outcome: the warning branch runs.
    monkeypatch.setattr(base_module, "_PROCESS_FINALIZE_SECONDS", 0.0)
    scheduler = Quiv(
        process_pool_size=1, process_kill_grace=30, main_loop=running_main_loop
    )
    leftover_db_paths.append(scheduler._db_path)
    scheduler.start()
    task_id = scheduler.add_task(
        "stubborn", handlers.sleep_ignoring_stop, interval=60, args=(30,),
        executor="process",
    )
    _running_job(scheduler, task_id)
    with caplog.at_level(logging.WARNING, logger="Quiv"):
        scheduler.shutdown(timeout=0.5)
    assert any(
        "process job(s) were terminated" in r.message for r in caplog.records
    )
    assert _no_children()


def test_shutdown_without_timeout_waits_for_grace_kill(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(
        process_pool_size=1, process_kill_grace=0.3, main_loop=running_main_loop
    )
    scheduler.start()
    task_id = scheduler.add_task(
        "stubborn", handlers.sleep_ignoring_stop, interval=60, args=(30,),
        executor="process",
    )
    _running_job(scheduler, task_id)
    began = time.monotonic()
    scheduler.shutdown()
    assert time.monotonic() - began < 5
    assert _no_children()


def test_stats_reports_process_pool(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(
        pool_size=3, process_pool_size=2, main_loop=running_main_loop
    )
    try:
        idle = scheduler.stats()
        assert idle.process_pool_size == 2
        assert idle.active_process_jobs == 0
        assert idle.process_pool_utilization == 0.0

        scheduler.start()
        task_id = scheduler.add_task(
            "cooperative", handlers.wait_for_stop, interval=60,
            executor="process",
        )
        running = _running_job(scheduler, task_id)
        busy = scheduler.stats()
        assert busy.active_process_jobs == 1
        assert busy.process_pool_utilization == 0.5
        assert busy.active_jobs == 0
        assert busy.pool_utilization == 0.0

        assert running.id is not None
        scheduler.cancel_job(running.id)
        scheduler.wait_for_job(running.id, timeout=DEADLINE)
        assert _poll(lambda: scheduler.stats().active_process_jobs == 0)
    finally:
        scheduler.shutdown()


def test_module_level_quiv_is_inert_in_child(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    import quiv_process_handlers_with_scheduler as app_module

    scheduler = Quiv(process_pool_size=1, main_loop=running_main_loop)
    try:
        assert app_module.scheduler._inert is False
        scheduler.start()
        task_id = scheduler.add_task(
            "inert-check", app_module.handler, run_once=True,
            executor="process",
        )
        (job,) = _finished_jobs(scheduler, task_id)
        assert job.status == JobStatus.COMPLETED, job.error_message
    finally:
        scheduler.shutdown()
        app_module.scheduler.shutdown()


def test_inert_instance_raises_on_lifecycle_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Through monkeypatch, never by assignment: a leaked flag makes every
    # later Quiv() in the session inert.
    monkeypatch.setattr(_worker, "_in_worker", True)
    scheduler = Quiv(process_pool_size=1)
    assert scheduler._inert is True
    assert scheduler.is_running is False
    assert not hasattr(scheduler, "_engine")

    for call in (
        scheduler.start,
        scheduler.shutdown,
        scheduler.stats,
        scheduler.pending_main_loop_work,
        lambda: scheduler.add_task("t", handlers.noop, interval=60),
        lambda: scheduler.update_task("t", interval=5),
        lambda: scheduler.remove_task("t"),
        lambda: scheduler.pause_task("t"),
        lambda: scheduler.resume_task("t"),
        lambda: scheduler.run_task_immediately("t"),
        lambda: scheduler.cancel_job("j"),
        lambda: scheduler.get_task("t"),
        lambda: scheduler.get_job("j"),
        scheduler.get_all_tasks,
        scheduler.get_all_jobs,
        lambda: scheduler.wait_for_job("j"),
        lambda: scheduler.wait_for_task("t"),
    ):
        with pytest.raises(WorkerProcessError, match="inert"):
            call()

    async def awaits() -> None:
        with pytest.raises(WorkerProcessError):
            await scheduler.await_job("j")
        with pytest.raises(WorkerProcessError):
            await scheduler.await_task("t")

    asyncio.run(awaits())

    listener = lambda event, task: None  # noqa: E731
    scheduler.add_listener(Event.TASK_ADDED, listener)
    assert scheduler._event_listeners[Event.TASK_ADDED] == [listener]
    scheduler.remove_listener(Event.TASK_ADDED, listener)
    assert scheduler._event_listeners[Event.TASK_ADDED] == []


class _RecordingConnection:
    """Stands in for the pipe end in an in-process run of the child code."""

    def __init__(self) -> None:
        self.sent: list[tuple[Any, ...]] = []
        self.closed = False

    def send(self, message: tuple[Any, ...]) -> None:
        pickle.dumps(message)  # what a real pipe would do
        self.sent.append(message)

    def close(self) -> None:
        self.closed = True


def test_worker_run_job_reports_done_and_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # run_job sets the module globals; restore them through monkeypatch.
    monkeypatch.setattr(_worker, "_in_worker", False)
    monkeypatch.setattr(_worker, "_channel", None)
    monkeypatch.setattr(_worker, "_stop_event", None)
    assert _worker.in_worker_process() is False
    assert _worker.channel() is None
    assert _worker.stop_event() is None
    with pytest.raises(QuivError, match="no parent channel"):
        _worker.send(("progress", (), {}), "progress_hook payload")

    stop_event = threading.Event()
    conn = _RecordingConnection()
    _worker.run_job(
        conn,  # type: ignore[arg-type]
        stop_event,
        "job-1",
        "task-1",
        "quiv_process_handlers",
        "noop",
        pickle.dumps(()),
        pickle.dumps({}),
    )
    assert conn.sent == [("done",)]
    assert conn.closed
    assert _worker.in_worker_process() is True
    assert _worker.channel() is conn
    assert _worker.stop_event() is stop_event

    conn = _RecordingConnection()
    _worker.run_job(
        conn,  # type: ignore[arg-type]
        stop_event,
        "job-2",
        "task-1",
        "quiv_process_handlers",
        "no_such_handler",
        pickle.dumps(()),
        pickle.dumps({}),
    )
    assert len(conn.sent) == 1
    kind, message, traceback_text, cancelled = conn.sent[0]
    assert kind == "error"
    assert "no_such_handler" in message
    assert "AttributeError" in traceback_text
    assert cancelled is False
