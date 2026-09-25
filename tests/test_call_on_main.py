"""call_on_main(): run_on_main's sibling that waits for the result."""

from __future__ import annotations

import asyncio
import gc
import logging
import threading
import time
import warnings
from typing import Any

import pytest

from quiv import (
    Event,
    Job,
    JobStatus,
    MainLoopUnavailableError,
    Quiv,
    Task,
    call_on_main,
)


def _on_loop(loop: asyncio.AbstractEventLoop, coro: Any, timeout: float = 5) -> Any:
    return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout)


def test_call_on_main_returns_the_result_of_an_async_target(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    captured: dict[str, Any] = {}

    async def target(a: int, b: int = 0) -> int:
        await asyncio.sleep(0.05)
        return a + b

    def handler() -> None:
        captured["result"] = call_on_main(target, 2, b=3)

    try:
        task_id = scheduler.add_task("hop", handler, run_once=True)
        scheduler.start()
        job = scheduler.wait_for_task(task_id, timeout=5)

        assert captured["result"] == 5
        assert job.status == JobStatus.COMPLETED
        assert job.duration_seconds is not None and job.duration_seconds >= 0.04
    finally:
        scheduler.shutdown()


def test_call_on_main_returns_the_result_of_a_sync_target(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    captured: dict[str, Any] = {}

    def target() -> int:
        captured["thread"] = threading.get_ident()
        return 7

    def handler() -> None:
        captured["result"] = call_on_main(target)

    try:
        task_id = scheduler.add_task("hop", handler, run_once=True)
        scheduler.start()
        scheduler.wait_for_task(task_id, timeout=5)

        assert captured["result"] == 7
        assert captured["thread"] != threading.get_ident()
    finally:
        scheduler.shutdown()


def test_call_on_main_propagates_the_targets_exception_and_fails_the_job(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)

    async def target() -> None:
        raise ValueError("body failed")

    def handler() -> None:
        call_on_main(target)

    try:
        task_id = scheduler.add_task("hop", handler, run_once=True)
        scheduler.start()
        job = scheduler.wait_for_task(task_id, timeout=5)

        assert job.status == JobStatus.FAILED
        assert job.error_message == "body failed"
    finally:
        scheduler.shutdown()


def test_call_on_main_is_cancelled_by_the_stop_event(
    running_main_loop: asyncio.AbstractEventLoop,
    caplog: pytest.LogCaptureFixture,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    started: list[str] = []
    running = threading.Event()
    target_cancelled = threading.Event()

    async def target() -> None:
        running.set()
        try:
            await asyncio.sleep(5)
        finally:
            target_cancelled.set()

    def handler() -> None:
        call_on_main(target)

    def on_started(event: Event, task: Task, job: Job) -> None:
        assert job.id is not None
        started.append(job.id)

    try:
        scheduler.add_listener(Event.JOB_STARTED, on_started)
        task_id = scheduler.add_task("hop", handler, run_once=True)
        scheduler.start()
        assert running.wait(timeout=5)
        deadline = time.monotonic() + 5
        while not started:
            assert time.monotonic() < deadline
            time.sleep(0.01)

        before = time.monotonic()
        with caplog.at_level(logging.ERROR, logger="Quiv"):
            assert scheduler.cancel_job(started[0])
            job = scheduler.wait_for_task(task_id, timeout=5)

        assert job.status == JobStatus.CANCELLED
        assert job.error_message is None
        assert time.monotonic() - before < 2
        assert target_cancelled.wait(timeout=2)
        # A requested stop is not a failure: no traceback in the log.
        assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []
    finally:
        scheduler.shutdown()


def test_call_on_main_is_cancelled_by_the_task_timeout(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)

    async def target() -> None:
        await asyncio.sleep(5)

    def handler() -> None:
        call_on_main(target)

    try:
        task_id = scheduler.add_task("hop", handler, run_once=True, timeout=0.3)
        scheduler.start()
        job = scheduler.wait_for_task(task_id, timeout=5)

        assert job.status == JobStatus.CANCELLED
        assert job.error_message is not None
        assert job.error_message.startswith("Job exceeded timeout")
    finally:
        scheduler.shutdown()


def test_call_on_main_on_the_main_loop_thread(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    """A sync target runs inline; an async one cannot be waited for."""

    scheduler = Quiv(main_loop=running_main_loop)

    def sync_target() -> int:
        return threading.get_ident()

    async def async_target() -> None:  # pragma: no cover - never awaited
        pass

    def returns_a_coroutine() -> Any:
        return async_target()

    async def probe() -> dict[str, Any]:
        out: dict[str, Any] = {"inline": call_on_main(sync_target)}
        with pytest.raises(MainLoopUnavailableError, match="await it there"):
            call_on_main(async_target)
        with pytest.raises(MainLoopUnavailableError, match="await it there"):
            call_on_main(returns_a_coroutine)
        out["thread"] = threading.get_ident()
        return out

    try:
        scheduler.start()
        result = _on_loop(running_main_loop, probe())
        assert result["inline"] == result["thread"]
    finally:
        scheduler.shutdown()


def test_call_on_main_is_counted_by_pending_main_loop_work(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    running = threading.Event()
    release = threading.Event()

    async def target() -> None:
        running.set()
        while not release.is_set():
            await asyncio.sleep(0.01)

    def handler() -> None:
        call_on_main(target)

    try:
        task_id = scheduler.add_task("hop", handler, run_once=True)
        scheduler.start()
        assert running.wait(timeout=5)
        assert scheduler.pending_main_loop_work() == 1

        release.set()
        scheduler.wait_for_task(task_id, timeout=5)
        deadline = time.monotonic() + 2
        while scheduler.pending_main_loop_work():
            assert time.monotonic() < deadline
            time.sleep(0.01)
    finally:
        scheduler.shutdown()


def test_call_on_main_outside_a_job_waits_without_a_stop_event(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)

    async def target() -> str:
        await asyncio.sleep(0.15)  # longer than one poll slice
        return "done"

    try:
        scheduler.start()
        result: list[Any] = []
        thread = threading.Thread(
            target=lambda: result.append(call_on_main(target))
        )
        thread.start()
        thread.join(timeout=5)
        assert result == ["done"]
    finally:
        scheduler.shutdown()


def test_call_on_main_with_no_active_quiv_raises() -> None:
    with pytest.raises(MainLoopUnavailableError, match="no active Quiv"):
        call_on_main(lambda: None)


def test_call_on_main_with_no_resolvable_loop_raises(
    running_main_loop: asyncio.AbstractEventLoop,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    try:
        scheduler.start()
        monkeypatch.setattr(scheduler, "_resolve_main_loop", lambda: None)
        with pytest.raises(MainLoopUnavailableError, match="resolvable"):
            call_on_main(lambda: None)
    finally:
        scheduler.shutdown()


def test_call_on_main_untracks_and_closes_when_the_enqueue_fails(
    running_main_loop: asyncio.AbstractEventLoop,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)

    async def target() -> None:  # pragma: no cover - never scheduled
        pass

    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("Event loop is closed")

    try:
        scheduler.start()
        monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", boom)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with pytest.raises(RuntimeError, match="closed"):
                call_on_main(target)
            gc.collect()
        assert scheduler.pending_main_loop_work() == 0
        assert not [w for w in caught if issubclass(w.category, RuntimeWarning)]
    finally:
        scheduler.shutdown()
