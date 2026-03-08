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
from quiv.models import Task


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
        scheduler.register_handler("demo", lambda: None)
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
        timezone_name="America/New_York", main_loop=running_main_loop
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
        assert any(job.status == "completed" for job in jobs)
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
        assert any(job.status == "cancelled" for job in jobs)
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
        assert any(job.status == "failed" for job in jobs)
    finally:
        scheduler.shutdown()


def test_add_task_raises_when_handler_not_registered_post_registration_call(
    monkeypatch: pytest.MonkeyPatch,
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    try:
        monkeypatch.setattr(
            scheduler, "register_handler", lambda *_a, **_k: None
        )
        with pytest.raises(HandlerNotRegisteredError):
            scheduler.add_task("no-reg", lambda: None, interval=1)
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

    def fake_get_all_jobs(*_args, **_kwargs):
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
            scheduler.persistence, "get_all_jobs", fake_get_all_jobs
        )
        monkeypatch.setattr("quiv.scheduler.time.sleep", fake_sleep)
        scheduler._loop()
        assert call_count["sleep"] >= 3
    finally:
        monkeypatch.undo()
        scheduler.shutdown()
