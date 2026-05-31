from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any

import pytest

from quiv import Quiv, run_on_main
import quiv.context as quiv_context


def _main_thread_id(loop: asyncio.AbstractEventLoop) -> int:
    async def _ident() -> int:
        return threading.get_ident()

    return asyncio.run_coroutine_threadsafe(_ident(), loop).result(timeout=2)


def test_run_on_main_from_sync_handler_dispatches_to_main_loop(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    main_thread_id = _main_thread_id(running_main_loop)
    captured: dict[str, int] = {}
    done = threading.Event()

    def target() -> None:
        captured["thread_id"] = threading.get_ident()
        done.set()

    def handler() -> None:
        run_on_main(target)

    try:
        scheduler.add_task(
            task_name="sync-run-on-main",
            func=handler,
            interval=60,
            run_once=True,
        )
        scheduler.start()
        assert done.wait(timeout=3)
        assert captured["thread_id"] == main_thread_id
    finally:
        scheduler.shutdown()


def test_run_on_main_from_async_handler_with_async_target(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    main_thread_id = _main_thread_id(running_main_loop)
    captured: dict[str, Any] = {}
    done = threading.Event()

    async def target(payload: str) -> None:
        captured["thread_id"] = threading.get_ident()
        captured["payload"] = payload
        done.set()

    async def handler() -> None:
        run_on_main(target, "hello")

    try:
        scheduler.add_task(
            task_name="async-run-on-main-async-target",
            func=handler,
            interval=60,
            run_once=True,
        )
        scheduler.start()
        assert done.wait(timeout=3)
        assert captured["thread_id"] == main_thread_id
        assert captured["payload"] == "hello"
    finally:
        scheduler.shutdown()


def test_run_on_main_from_async_handler_with_sync_target(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    main_thread_id = _main_thread_id(running_main_loop)
    captured: dict[str, int] = {}
    done = threading.Event()

    def target() -> None:
        captured["thread_id"] = threading.get_ident()
        done.set()

    async def handler() -> None:
        run_on_main(target)

    try:
        scheduler.add_task(
            task_name="async-run-on-main-sync-target",
            func=handler,
            interval=60,
            run_once=True,
        )
        scheduler.start()
        assert done.wait(timeout=3)
        assert captured["thread_id"] == main_thread_id
    finally:
        scheduler.shutdown()


def test_run_on_main_propagates_through_nested_calls(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    main_thread_id = _main_thread_id(running_main_loop)
    captured: dict[str, int] = {}
    done = threading.Event()

    def level_three() -> None:
        def target() -> None:
            captured["thread_id"] = threading.get_ident()
            done.set()

        run_on_main(target)

    def level_two() -> None:
        level_three()

    def level_one() -> None:
        level_two()

    def handler() -> None:
        level_one()

    try:
        scheduler.add_task(
            task_name="nested-run-on-main",
            func=handler,
            interval=60,
            run_once=True,
        )
        scheduler.start()
        assert done.wait(timeout=3)
        assert captured["thread_id"] == main_thread_id
    finally:
        scheduler.shutdown()


def test_run_on_main_propagates_into_asyncio_create_task(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    main_thread_id = _main_thread_id(running_main_loop)
    captured: dict[str, int] = {}
    done = threading.Event()

    def target() -> None:
        captured["thread_id"] = threading.get_ident()
        done.set()

    async def spawned() -> None:
        run_on_main(target)

    async def handler() -> None:
        task = asyncio.create_task(spawned())
        await task

    try:
        scheduler.add_task(
            task_name="create-task-run-on-main",
            func=handler,
            interval=60,
            run_once=True,
        )
        scheduler.start()
        assert done.wait(timeout=3)
        assert captured["thread_id"] == main_thread_id
    finally:
        scheduler.shutdown()


def test_run_on_main_from_main_loop_thread_runs_sync_inline(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    main_thread_id = _main_thread_id(running_main_loop)
    captured: dict[str, int] = {}

    def target() -> None:
        captured["thread_id"] = threading.get_ident()

    async def caller() -> None:
        run_on_main(target)

    try:
        scheduler.start()
        asyncio.run_coroutine_threadsafe(
            caller(), running_main_loop
        ).result(timeout=2)
        assert captured["thread_id"] == main_thread_id
    finally:
        scheduler.shutdown()


def test_run_on_main_from_main_loop_thread_schedules_async_target(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    main_thread_id = _main_thread_id(running_main_loop)
    captured: dict[str, int] = {}
    done = threading.Event()

    async def target() -> None:
        captured["thread_id"] = threading.get_ident()
        done.set()

    async def caller() -> None:
        run_on_main(target)

    try:
        scheduler.start()
        asyncio.run_coroutine_threadsafe(
            caller(), running_main_loop
        ).result(timeout=2)
        assert done.wait(timeout=3)
        assert captured["thread_id"] == main_thread_id
    finally:
        scheduler.shutdown()


def test_run_on_main_without_active_quiv_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(quiv_context, "_active_quiv", None)
    with pytest.raises(RuntimeError, match="no active Quiv"):
        run_on_main(lambda: None)


def test_run_on_main_logs_and_swallows_target_exception(
    running_main_loop: asyncio.AbstractEventLoop,
    caplog: pytest.LogCaptureFixture,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    second_done = threading.Event()

    def boom() -> None:
        raise RuntimeError("boom")

    def ok() -> None:
        second_done.set()

    def handler() -> None:
        run_on_main(boom)
        run_on_main(ok)

    try:
        scheduler.add_task(
            task_name="failing-target",
            func=handler,
            interval=60,
            run_once=True,
        )
        with caplog.at_level(logging.ERROR, logger="Quiv"):
            scheduler.start()
            assert second_done.wait(timeout=3)

        # The handler itself completed (exception swallowed in dispatch).
        time.sleep(0.2)
        jobs = scheduler.get_all_jobs()
        assert all(job.status != "failed" for job in jobs)
        assert any("boom" in r.message for r in caplog.records)
    finally:
        scheduler.shutdown()


def test_multiple_active_instances_logs_warning(
    running_main_loop: asyncio.AbstractEventLoop,
    caplog: pytest.LogCaptureFixture,
) -> None:
    first = Quiv(main_loop=running_main_loop)
    second = Quiv(main_loop=running_main_loop)
    try:
        first.start()
        with caplog.at_level(logging.WARNING, logger="Quiv"):
            second.start()
        assert any(
            "Multiple Quiv instances" in r.message for r in caplog.records
        )
    finally:
        second.shutdown()
        first.shutdown()


def test_shutdown_unregisters_active_instance(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    scheduler.start()
    assert quiv_context._get_active_quiv() is scheduler
    scheduler.shutdown()
    assert quiv_context._get_active_quiv() is None
