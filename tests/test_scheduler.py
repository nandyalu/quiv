from __future__ import annotations

import asyncio
import threading
import time

import pytest

from quiv import Quiv, QuivConfig
from quiv.exceptions import (
    ConfigurationError,
    HandlerNotRegisteredError,
    TaskNotScheduledError,
)
from quiv.models import JobStatus, Task


def test_quiv_config_conflict_raises(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    with pytest.raises(ConfigurationError):
        Quiv(
            config=QuivConfig(pool_size=2),
            pool_size=3,
            main_loop=running_main_loop,
        )


def test_add_task_validates_inputs(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    try:
        with pytest.raises(ConfigurationError):
            scheduler.add_task(task_name="", func=lambda: None, interval=1)
        with pytest.raises(ConfigurationError):
            scheduler.add_task(task_name="a", func=lambda: None, interval=0)
        with pytest.raises(ConfigurationError):
            scheduler.add_task(
                task_name="a", func=lambda: None, interval=1, delay=-1
            )
    finally:
        scheduler.shutdown()


def test_run_task_immediately_requires_registered_handler(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    try:
        with pytest.raises(HandlerNotRegisteredError):
            scheduler.run_task_immediately("missing")
    finally:
        scheduler.shutdown()


def test_run_task_immediately_requires_scheduled_task(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    try:
        scheduler._register_handler("demo", lambda: None)
        with pytest.raises(TaskNotScheduledError):
            scheduler.run_task_immediately("demo")
    finally:
        scheduler.shutdown()


def test_add_task_and_run_task_immediately_queues_task(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    try:
        scheduler.add_task(task_name="demo", func=lambda: None, interval=60)
        count = scheduler.run_task_immediately("demo")
        assert count >= 1
    finally:
        scheduler.shutdown()


def test_get_all_tasks_returns_utc_aware_next_run(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(
        timezone="America/New_York", main_loop=running_main_loop
    )
    try:
        scheduler.add_task(
            task_name="aware-check", func=lambda: None, interval=60
        )
        tasks = scheduler.get_all_tasks(include_run_once=True)
        print(tasks[0].model_dump())
        print(Task(**tasks[0].model_dump()))
        print(Task.model_validate(tasks[0]))
        task = next(item for item in tasks if item.task_name == "aware-check")
        assert task.next_run_at.tzinfo is not None
        offset = task.next_run_at.utcoffset()
        assert offset is not None
        assert offset.total_seconds() == 0
    finally:
        scheduler.shutdown()


def test_run_once_sync_task_executes_and_creates_completed_job(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    finished = threading.Event()

    def handler(_stop_event=None) -> None:
        finished.set()

    try:
        scheduler.add_task(
            task_name="sync-once",
            func=handler,
            interval=60,
            run_once=True,
            delay=0,
        )
        scheduler.start()
        assert finished.wait(timeout=3)
        time.sleep(0.2)
        jobs = scheduler.get_all_jobs()
        assert any(job.status == JobStatus.COMPLETED for job in jobs)
    finally:
        scheduler.shutdown()


def test_run_once_async_task_executes(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    finished = threading.Event()

    async def handler(_stop_event=None) -> None:
        finished.set()

    try:
        scheduler.add_task(
            task_name="async-once",
            func=handler,
            interval=60,
            run_once=True,
            delay=0,
        )
        scheduler.start()
        assert finished.wait(timeout=3)
    finally:
        scheduler.shutdown()


def test_progress_callback_runs_on_main_loop(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    progress_done = threading.Event()

    async def progress_callback(step: int) -> None:
        if step == 1:
            progress_done.set()

    def handler(_progress_hook=None, _stop_event=None) -> None:
        assert _progress_hook is not None
        _progress_hook(step=1)

    try:
        scheduler.add_task(
            task_name="progress-task",
            func=handler,
            interval=60,
            run_once=True,
            progress_callback=progress_callback,
        )
        scheduler.start()
        assert progress_done.wait(timeout=3)
    finally:
        scheduler.shutdown()


def test_run_once_sync_task_without_optional_hooks_executes(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    finished = threading.Event()

    def handler() -> None:
        finished.set()

    try:
        scheduler.add_task(
            task_name="sync-no-hooks",
            func=handler,
            interval=60,
            run_once=True,
            delay=0,
        )
        scheduler.start()
        assert finished.wait(timeout=3)
    finally:
        scheduler.shutdown()


def test_run_once_sync_task_with_stop_event_cancels_job(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    finished = threading.Event()

    def handler(_stop_event=None) -> None:
        assert _stop_event is not None
        _stop_event.set()
        finished.set()

    try:
        scheduler.add_task(
            task_name="sync-stop-event",
            func=handler,
            interval=60,
            run_once=True,
            delay=0,
        )
        scheduler.start()
        assert finished.wait(timeout=3)
        time.sleep(0.2)
        jobs = scheduler.get_all_jobs()
        assert any(job.status == JobStatus.CANCELLED for job in jobs)
    finally:
        scheduler.shutdown()


def test_async_task_with_progress_hook_and_sync_progress_callback(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    progress_done = threading.Event()

    def progress_callback(step: int) -> None:
        if step == 2:
            progress_done.set()

    async def handler(_progress_hook=None) -> None:
        assert _progress_hook is not None
        _progress_hook(step=2)

    try:
        scheduler.add_task(
            task_name="async-progress-sync-callback",
            func=handler,
            interval=60,
            run_once=True,
            progress_callback=progress_callback,
        )
        scheduler.start()
        assert progress_done.wait(timeout=3)
    finally:
        scheduler.shutdown()


def test_failed_job_sets_failed_status(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)

    def handler() -> None:
        raise RuntimeError("boom")

    try:
        scheduler.add_task(
            task_name="failing-task",
            func=handler,
            interval=60,
            run_once=True,
            delay=0,
        )
        scheduler.start()
        time.sleep(1.5)
        jobs = scheduler.get_all_jobs()
        assert any(job.status == JobStatus.FAILED for job in jobs)
    finally:
        scheduler.shutdown()


def test_add_task_raises_on_duplicate_task_name(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    try:
        scheduler.add_task("dup", lambda: None, interval=60)
        with pytest.raises(ConfigurationError):
            scheduler.add_task("dup", lambda: None, interval=60)
    finally:
        scheduler.shutdown()


def test_dispatch_due_task_logs_next_run_for_recurring_task(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    try:
        task_id = scheduler.add_task(
            task_name="recurring-dispatch",
            func=lambda: None,
            interval=60,
            run_once=False,
        )
        assert task_id is not None
        task = next(
            item
            for item in scheduler.get_all_tasks(include_run_once=True)
            if item.id == task_id
        )
        scheduler._dispatch_due_task(task, scheduler._now_utc())
        time.sleep(0.2)
    finally:
        scheduler.shutdown()


def test_loop_handles_exceptions_and_retries_sleep(
    monkeypatch: pytest.MonkeyPatch,
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    call_count = {"sleep": 0}

    def fake_cleanup_history(*_args, **_kwargs):
        raise RuntimeError("boom")

    def fake_sleep(seconds: float) -> None:
        call_count["sleep"] += 1
        if call_count["sleep"] == 1:
            scheduler._initialized = True
        if call_count["sleep"] >= 3:
            scheduler._shutdown = True

    try:
        scheduler._initialized = False
        monkeypatch.setattr(
            scheduler.persistence, "cleanup_history", fake_cleanup_history
        )
        monkeypatch.setattr("quiv.scheduler.time.sleep", fake_sleep)
        scheduler._loop()
        assert call_count["sleep"] >= 3
    finally:
        monkeypatch.undo()
        scheduler.shutdown()


def test_backpressure_skips_dispatch_when_pool_full(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(pool_size=1, main_loop=running_main_loop)
    blocker = threading.Event()
    started = threading.Event()

    def blocking_handler() -> None:
        started.set()
        blocker.wait(timeout=5)

    try:
        # Fill the single-worker pool
        scheduler.add_task(
            task_name="blocker",
            func=blocking_handler,
            interval=60,
            run_once=True,
        )
        scheduler.start()
        assert started.wait(timeout=3)

        # Add another task that is immediately due
        deferred_id = scheduler.add_task(
            task_name="deferred",
            func=lambda: None,
            interval=60,
            run_once=True,
        )
        scheduler.run_task_immediately("deferred")
        time.sleep(1.5)

        # deferred task should still be pending — pool is full
        assert scheduler._active_job_count >= 1
        completed_jobs = scheduler.get_all_jobs(status=JobStatus.COMPLETED)
        assert not any(j.task_id == deferred_id for j in completed_jobs), (
            "Deferred task should not have completed while pool is full"
        )

        # Release blocker — deferred should now run
        blocker.set()
        time.sleep(2)
        assert scheduler._active_job_count == 0
    finally:
        blocker.set()
        scheduler.shutdown()


def test_late_start_warning_logged_when_pool_busy(
    running_main_loop: asyncio.AbstractEventLoop,
    caplog: pytest.LogCaptureFixture,
) -> None:
    scheduler = Quiv(pool_size=1, main_loop=running_main_loop)

    from datetime import datetime, timedelta, timezone

    try:
        scheduler.add_task(
            task_name="late-task",
            func=lambda: None,
            interval=60,
            run_once=True,
        )
        task = scheduler.get_all_tasks(include_run_once=True)[0]
        scheduled_at = scheduler._now_utc() - timedelta(seconds=5)

        with caplog.at_level("WARNING", logger="Quiv"):
            scheduler._run_job(
                job_id=scheduler.persistence.create_job(task.id),
                task_id=task.id,
                task_name="late-task",
                run_once=True,
                scheduled_at=scheduled_at,
                func=lambda: None,
                args=[],
                kwargs={},
            )

        assert any("threadpool was busy" in r.message for r in caplog.records)
    finally:
        scheduler.shutdown()


def test_active_job_count_decrements_after_completion(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    finished = threading.Event()

    def handler() -> None:
        finished.set()

    try:
        scheduler.add_task(
            task_name="count-check",
            func=handler,
            interval=60,
            run_once=True,
        )
        scheduler.start()
        assert finished.wait(timeout=3)
        time.sleep(0.5)
        assert scheduler._active_job_count == 0
    finally:
        scheduler.shutdown()


def test_remove_task(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    from quiv.exceptions import TaskNotFoundError

    scheduler = Quiv(main_loop=running_main_loop)

    def my_handler() -> None:
        pass

    def my_progress(step: int) -> None:
        pass

    try:
        scheduler.add_task(
            task_name="removable",
            func=my_handler,
            interval=60,
            progress_callback=my_progress,
        )

        # Verify the task, handler, and callback are present
        tasks = scheduler.get_all_tasks(include_run_once=True)
        assert any(t.task_name == "removable" for t in tasks)
        assert "removable" in scheduler.registry
        assert "removable" in scheduler.progress_callbacks

        # Remove the task
        scheduler.remove_task("removable")

        # Verify the task, handler, and callback are gone
        tasks = scheduler.get_all_tasks(include_run_once=True)
        assert all(t.task_name != "removable" for t in tasks)
        assert "removable" not in scheduler.registry
        assert "removable" not in scheduler.progress_callbacks

        # Removing a non-existent task should raise TaskNotFoundError
        with pytest.raises(TaskNotFoundError):
            scheduler.remove_task("non-existent-task")
    finally:
        scheduler.shutdown()


def test_remove_task_cancels_running_job(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    blocker = threading.Event()
    started = threading.Event()

    def blocking_handler(_stop_event=None) -> None:
        started.set()
        while not (_stop_event and _stop_event.is_set()):
            blocker.wait(timeout=0.1)

    try:
        scheduler.add_task(
            task_name="remove-while-running",
            func=blocking_handler,
            interval=60,
            run_once=False,
            delay=0,
        )
        scheduler.start()
        assert started.wait(timeout=3)

        # Task is running — remove should cancel the running job
        scheduler.remove_task("remove-while-running")

        # Wait for job to finalize after stop event is set
        time.sleep(1)
        jobs = scheduler.get_all_jobs(status=JobStatus.CANCELLED)
        assert any(j.status == JobStatus.CANCELLED for j in jobs)
    finally:
        blocker.set()
        scheduler.shutdown()


def test_concurrent_runs_prevented(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(pool_size=2, main_loop=running_main_loop)
    enter_count = 0
    max_concurrent = 0
    current_concurrent = 0
    lock = threading.Lock()
    blocker = threading.Event()

    def slow_handler() -> None:
        nonlocal enter_count, max_concurrent, current_concurrent
        with lock:
            enter_count += 1
            current_concurrent += 1
            if current_concurrent > max_concurrent:
                max_concurrent = current_concurrent
        blocker.wait(timeout=4)
        with lock:
            current_concurrent -= 1

    try:
        scheduler.add_task(
            task_name="slow-task",
            func=slow_handler,
            interval=2,
            delay=0,
            run_once=False,
        )
        scheduler.start()
        # Wait long enough for the task to potentially be dispatched
        # multiple times if concurrent runs were allowed
        time.sleep(6)
        blocker.set()
        time.sleep(1)

        with lock:
            assert max_concurrent == 1, (
                f"Expected max concurrent to be 1, got {max_concurrent}"
            )
    finally:
        blocker.set()
        scheduler.shutdown()
