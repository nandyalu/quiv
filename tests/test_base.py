from __future__ import annotations

import asyncio
import threading
import time

import pytest

from quiv import Quiv
from quiv.config import QuivConfig
from quiv.config import resolve_timezone
from quiv.exceptions import (
    ConfigurationError,
    DatabaseInitializationError,
    HandlerRegistrationError,
    InvalidTimezoneError,
)


def test_quiv_base_validates_pool_size_and_history(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    with pytest.raises(ConfigurationError):
        Quiv(pool_size=0, main_loop=running_main_loop)
    with pytest.raises(ConfigurationError):
        Quiv(history_retention_seconds=-1, main_loop=running_main_loop)


def test_register_handler_validation(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    try:
        with pytest.raises(HandlerRegistrationError):
            scheduler._register_handler("", lambda: None)
        with pytest.raises(HandlerRegistrationError):
            scheduler._register_handler("x", None)  # type: ignore[arg-type]
    finally:
        scheduler.shutdown()


def test_register_progress_callback_validation_and_clear(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    called = threading.Event()

    def callback() -> None:
        called.set()

    try:
        scheduler._register_progress_callback("a", callback)
        scheduler.run_progress_callback("a")
        assert called.wait(timeout=2)

        scheduler._register_progress_callback("a", None)
        called.clear()
        scheduler.run_progress_callback("a")
        time.sleep(0.1)
        assert not called.is_set()

        with pytest.raises(HandlerRegistrationError):
            scheduler._register_progress_callback("a", "not-callable")  # type: ignore[arg-type]
    finally:
        scheduler.shutdown()


def test_run_progress_callback_with_closed_main_loop_is_safe() -> None:
    closed_loop = asyncio.new_event_loop()
    closed_loop.close()

    scheduler = Quiv(main_loop=closed_loop)
    try:
        scheduler._register_progress_callback("t", lambda *_a, **_k: None)
        scheduler.run_progress_callback("t", 1)
    finally:
        scheduler.shutdown()


def test_run_progress_callback_handles_async_and_sync_returning_coroutine(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    async_done = threading.Event()
    sync_done = threading.Event()

    async def async_progress(step: int) -> None:
        if step == 1:
            async_done.set()

    async def sync_result_coroutine() -> None:
        sync_done.set()

    def sync_progress(_step: int):
        return sync_result_coroutine()

    try:
        scheduler._register_progress_callback("async-task", async_progress)
        scheduler.run_progress_callback("async-task", 1)
        assert async_done.wait(timeout=2)

        scheduler._register_progress_callback("sync-task", sync_progress)
        scheduler.run_progress_callback("sync-task", 2)
        assert sync_done.wait(timeout=2)
    finally:
        scheduler.shutdown()


def test_start_is_idempotent_and_cancel_job_false_when_missing(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    try:
        scheduler.start()
        scheduler.start()
        assert scheduler.cancel_job(99999) is False
    finally:
        scheduler.shutdown()


def test_resolve_timezone_invalid_type_raises() -> None:
    with pytest.raises(InvalidTimezoneError):
        resolve_timezone(123)  # type: ignore[arg-type]


def test_quiv_init_uses_config_without_conflict(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(
        config=QuivConfig(
            pool_size=2,
            history_retention_seconds=10,
            timezone="UTC",
        ),
        main_loop=running_main_loop,
    )
    try:
        assert scheduler.history_limit == 10
    finally:
        scheduler.shutdown()


def test_quiv_init_defers_main_loop_resolution() -> None:
    scheduler = Quiv()
    try:
        assert scheduler._main_loop is None
    finally:
        scheduler.shutdown()


def test_run_progress_callback_logs_when_callback_fails(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)

    def bad_callback(_value: int) -> None:
        raise RuntimeError("sync callback failed")

    try:
        scheduler._register_progress_callback("bad", bad_callback)
        scheduler.run_progress_callback("bad", 1)
        time.sleep(0.2)
    finally:
        scheduler.shutdown()


def test_run_progress_callback_logs_when_async_callback_fails(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)

    async def bad_async_callback(_value: int) -> None:
        raise RuntimeError("async callback failed")

    try:
        scheduler._register_progress_callback("bad-async", bad_async_callback)
        scheduler.run_progress_callback("bad-async", 1)
        time.sleep(0.2)
    finally:
        scheduler.shutdown()


def test_pause_and_resume_wrappers_hit_base_methods(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    try:
        task_id = scheduler.add_task("pause-resume", lambda: None, interval=60)
        assert task_id is not None
        scheduler.pause_task(task_id)
        scheduler.resume_task(task_id)
    finally:
        scheduler.shutdown()


def test_quiv_base_loop_abstract_method_raises() -> None:
    with pytest.raises(NotImplementedError):
        Quiv.__mro__[1]._loop(object())  # type: ignore[misc]


def test_shutdown_handles_db_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    monkeypatch.setattr("quiv.base.os.path.exists", lambda _path: True)

    def fail_remove(_path: str) -> None:
        raise OSError("cannot remove")

    monkeypatch.setattr("quiv.base.os.remove", fail_remove)
    scheduler.shutdown()


def test_database_initialization_error_raised(
    monkeypatch: pytest.MonkeyPatch,
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    def fail_create_all(_engine) -> None:
        raise RuntimeError("db init failed")

    monkeypatch.setattr(
        "quiv.base.quiv_registry.metadata.create_all", fail_create_all
    )
    with pytest.raises(DatabaseInitializationError):
        Quiv(main_loop=running_main_loop)


def test_sync_progress_callback_without_event_loop() -> None:
    scheduler = Quiv()  # No main_loop
    called = threading.Event()

    def sync_callback(*args, **kwargs) -> None:
        called.set()

    try:
        scheduler._register_progress_callback("sync-no-loop", sync_callback)
        scheduler.run_progress_callback("sync-no-loop", 1)
        assert called.wait(timeout=2), (
            "Sync progress callback should have been called directly"
            " when no event loop is available"
        )
    finally:
        scheduler.shutdown()


def test_async_progress_callback_skipped_without_event_loop(
    caplog: pytest.LogCaptureFixture,
) -> None:
    scheduler = Quiv()  # No main_loop
    called = threading.Event()

    async def async_callback(*args, **kwargs) -> None:
        called.set()

    try:
        scheduler._register_progress_callback(
            "async-no-loop", async_callback
        )
        with caplog.at_level("WARNING", logger="Quiv"):
            scheduler.run_progress_callback("async-no-loop", 1)
        time.sleep(0.2)
        assert not called.is_set(), (
            "Async progress callback should NOT have been called"
            " when no event loop is available"
        )
        assert any(
            "skipped" in r.message and "no event loop" in r.message
            for r in caplog.records
        ), "Expected a warning about skipped async callback"
    finally:
        scheduler.shutdown()
