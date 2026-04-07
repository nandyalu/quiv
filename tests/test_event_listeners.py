from __future__ import annotations

import asyncio
import threading
import time
from datetime import timedelta
from typing import Any

import pytest

from quiv import Event, Quiv
from quiv.exceptions import ConfigurationError


def test_add_listener_validates_event_type(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    try:
        with pytest.raises(ConfigurationError):
            scheduler.add_listener("not_an_event", lambda e, d: None)  # type: ignore[arg-type]
    finally:
        scheduler.shutdown()


def test_add_listener_validates_callback_is_callable(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    try:
        with pytest.raises(ConfigurationError):
            scheduler.add_listener(Event.TASK_ADDED, "not_callable")  # type: ignore[arg-type]
    finally:
        scheduler.shutdown()


def test_remove_listener(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    captured: list[Event] = []

    def listener(event: Event, data: dict[str, Any]) -> None:
        captured.append(event)

    try:
        scheduler.add_listener(Event.TASK_ADDED, listener)
        scheduler.remove_listener(Event.TASK_ADDED, listener)
        scheduler.add_task("test-remove", lambda: None, interval=60)
        time.sleep(0.3)
        assert len(captured) == 0
    finally:
        scheduler.shutdown()


def test_remove_listener_ignores_unknown_callback(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    try:
        # Should not raise
        scheduler.remove_listener(Event.TASK_ADDED, lambda e, d: None)
    finally:
        scheduler.shutdown()


def test_task_added_event(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    captured: list[dict[str, Any]] = []
    received = threading.Event()

    def listener(event: Event, data: dict[str, Any]) -> None:
        captured.append({"event": event, **data})
        received.set()

    try:
        scheduler.add_listener(Event.TASK_ADDED, listener)
        task_id = scheduler.add_task("my-task", lambda: None, interval=60)
        assert received.wait(timeout=2)
        assert len(captured) == 1
        assert captured[0]["event"] == Event.TASK_ADDED
        assert captured[0]["task_name"] == "my-task"
        assert captured[0]["task_id"] == task_id
    finally:
        scheduler.shutdown()


def test_task_removed_event(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    captured: list[dict[str, Any]] = []
    received = threading.Event()

    def listener(event: Event, data: dict[str, Any]) -> None:
        captured.append({"event": event, **data})
        received.set()

    try:
        scheduler.add_task("removable", lambda: None, interval=60)
        scheduler.add_listener(Event.TASK_REMOVED, listener)
        scheduler.remove_task("removable")
        assert received.wait(timeout=2)
        assert len(captured) == 1
        assert captured[0]["event"] == Event.TASK_REMOVED
        assert captured[0]["task_name"] == "removable"
        assert "task_id" in captured[0]
    finally:
        scheduler.shutdown()


def test_task_paused_event(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    captured: list[dict[str, Any]] = []
    received = threading.Event()

    def listener(event: Event, data: dict[str, Any]) -> None:
        captured.append({"event": event, **data})
        received.set()

    try:
        scheduler.add_task("pausable", lambda: None, interval=60, delay=300)
        scheduler.add_listener(Event.TASK_PAUSED, listener)
        scheduler.pause_task("pausable")
        assert received.wait(timeout=2)
        assert captured[0]["event"] == Event.TASK_PAUSED
        assert captured[0]["task_name"] == "pausable"
        assert "task_id" in captured[0]
    finally:
        scheduler.shutdown()


def test_task_resumed_event(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    captured: list[dict[str, Any]] = []
    received = threading.Event()

    def listener(event: Event, data: dict[str, Any]) -> None:
        captured.append({"event": event, **data})
        received.set()

    try:
        scheduler.add_task("resumable", lambda: None, interval=60, delay=300)
        scheduler.pause_task("resumable")
        scheduler.add_listener(Event.TASK_RESUMED, listener)
        scheduler.resume_task("resumable", delay=300)
        assert received.wait(timeout=2)
        assert captured[0]["event"] == Event.TASK_RESUMED
        assert captured[0]["task_name"] == "resumable"
        assert "task_id" in captured[0]
    finally:
        scheduler.shutdown()


def test_job_started_event(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    captured: list[dict[str, Any]] = []
    received = threading.Event()

    def listener(event: Event, data: dict[str, Any]) -> None:
        captured.append({"event": event, **data})
        received.set()

    try:
        scheduler.add_listener(Event.JOB_STARTED, listener)
        scheduler.add_task(
            "start-check", lambda: None, interval=60, run_once=True
        )
        scheduler.start()
        assert received.wait(timeout=3)
        assert captured[0]["event"] == Event.JOB_STARTED
        assert captured[0]["task_name"] == "start-check"
        assert "job_id" in captured[0]
    finally:
        scheduler.shutdown()


def test_job_completed_event(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    captured: list[dict[str, Any]] = []
    received = threading.Event()

    def listener(event: Event, data: dict[str, Any]) -> None:
        captured.append({"event": event, **data})
        received.set()

    try:
        scheduler.add_listener(Event.JOB_COMPLETED, listener)
        scheduler.add_task(
            "complete-check", lambda: None, interval=60, run_once=True
        )
        scheduler.start()
        assert received.wait(timeout=3)
        assert captured[0]["event"] == Event.JOB_COMPLETED
        assert captured[0]["task_name"] == "complete-check"
        assert "job_id" in captured[0]
        assert isinstance(captured[0]["duration"], timedelta)
    finally:
        scheduler.shutdown()


def test_job_failed_event(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    captured: list[dict[str, Any]] = []
    received = threading.Event()

    def listener(event: Event, data: dict[str, Any]) -> None:
        captured.append({"event": event, **data})
        received.set()

    def bad_handler() -> None:
        raise RuntimeError("boom")

    try:
        scheduler.add_listener(Event.JOB_FAILED, listener)
        scheduler.add_task(
            "fail-check", bad_handler, interval=60, run_once=True
        )
        scheduler.start()
        assert received.wait(timeout=3)
        assert captured[0]["event"] == Event.JOB_FAILED
        assert captured[0]["task_name"] == "fail-check"
        assert "job_id" in captured[0]
        assert isinstance(captured[0]["error"], RuntimeError)
        assert str(captured[0]["error"]) == "boom"
        assert isinstance(captured[0]["duration"], timedelta)
    finally:
        scheduler.shutdown()


def test_job_cancelled_event(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    captured: list[dict[str, Any]] = []
    received = threading.Event()
    started = threading.Event()

    def listener(event: Event, data: dict[str, Any]) -> None:
        captured.append({"event": event, **data})
        received.set()

    def blocking_handler(_stop_event=None) -> None:
        started.set()
        while not (_stop_event and _stop_event.is_set()):
            time.sleep(0.05)

    try:
        scheduler.add_listener(Event.JOB_CANCELLED, listener)
        scheduler.add_task(
            "cancel-check",
            blocking_handler,
            interval=60,
            run_once=True,
        )
        scheduler.start()
        assert started.wait(timeout=3)

        jobs = scheduler.get_all_jobs(status="running")
        for job in jobs:
            if job.id is not None:
                scheduler.cancel_job(job.id)

        assert received.wait(timeout=3)
        assert captured[0]["event"] == Event.JOB_CANCELLED
        assert captured[0]["task_name"] == "cancel-check"
        assert "job_id" in captured[0]
    finally:
        scheduler.shutdown()


def test_multiple_listeners_for_same_event(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    captured_a: list[Event] = []
    captured_b: list[Event] = []
    both_received = threading.Event()

    def listener_a(event: Event, data: dict[str, Any]) -> None:
        captured_a.append(event)
        if captured_a and captured_b:
            both_received.set()

    def listener_b(event: Event, data: dict[str, Any]) -> None:
        captured_b.append(event)
        if captured_a and captured_b:
            both_received.set()

    try:
        scheduler.add_listener(Event.TASK_ADDED, listener_a)
        scheduler.add_listener(Event.TASK_ADDED, listener_b)
        scheduler.add_task("multi-listen", lambda: None, interval=60)
        assert both_received.wait(timeout=2)
        assert len(captured_a) == 1
        assert len(captured_b) == 1
    finally:
        scheduler.shutdown()


def test_async_listener_dispatched_on_main_loop(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    captured: list[dict[str, Any]] = []
    received = threading.Event()

    async def listener(event: Event, data: dict[str, Any]) -> None:
        captured.append({"event": event, **data})
        received.set()

    try:
        scheduler.add_listener(Event.TASK_ADDED, listener)
        scheduler.add_task("async-listen", lambda: None, interval=60)
        assert received.wait(timeout=2)
        assert captured[0]["event"] == Event.TASK_ADDED
        assert captured[0]["task_name"] == "async-listen"
    finally:
        scheduler.shutdown()


def test_listener_exception_is_swallowed(
    running_main_loop: asyncio.AbstractEventLoop,
    caplog: pytest.LogCaptureFixture,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    good_captured: list[Event] = []
    good_received = threading.Event()

    def bad_listener(event: Event, data: dict[str, Any]) -> None:
        raise RuntimeError("listener error")

    def good_listener(event: Event, data: dict[str, Any]) -> None:
        good_captured.append(event)
        good_received.set()

    try:
        scheduler.add_listener(Event.TASK_ADDED, bad_listener)
        scheduler.add_listener(Event.TASK_ADDED, good_listener)
        with caplog.at_level("ERROR", logger="Quiv"):
            scheduler.add_task("error-listen", lambda: None, interval=60)
        assert good_received.wait(timeout=2)
        assert len(good_captured) == 1
    finally:
        scheduler.shutdown()


def test_listener_without_event_loop_sync(
) -> None:
    """Sync listeners work without an event loop (run on calling thread)."""
    scheduler = Quiv()
    captured: list[dict[str, Any]] = []

    def listener(event: Event, data: dict[str, Any]) -> None:
        captured.append({"event": event, **data})

    try:
        scheduler.add_listener(Event.TASK_ADDED, listener)
        scheduler.add_task("no-loop", lambda: None, interval=60)
        assert len(captured) == 1
        assert captured[0]["event"] == Event.TASK_ADDED
    finally:
        scheduler.shutdown()


def test_async_listener_without_event_loop_skipped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Async listeners are skipped with a warning when no event loop exists."""
    scheduler = Quiv()

    async def listener(event: Event, data: dict[str, Any]) -> None:
        pass  # pragma: no cover

    try:
        scheduler.add_listener(Event.TASK_ADDED, listener)
        with caplog.at_level("WARNING", logger="Quiv"):
            scheduler.add_task("no-loop-async", lambda: None, interval=60)
        assert any(
            "Async event listener" in r.message and "skipped" in r.message
            for r in caplog.records
        )
    finally:
        scheduler.shutdown()
