from __future__ import annotations

import asyncio
import contextlib
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


def test_main_loop_unavailable_error_catches_both_ways(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The v1.0.0 exception keeps 0.x ``except RuntimeError`` working.

    It inherits QuivError and RuntimeError, so both clauses catch it.
    """
    from quiv.exceptions import MainLoopUnavailableError, QuivError

    assert issubclass(MainLoopUnavailableError, QuivError)
    assert issubclass(MainLoopUnavailableError, RuntimeError)

    monkeypatch.setattr(quiv_context, "_active_quiv", None)
    with pytest.raises(QuivError):
        run_on_main(lambda: None)
    with pytest.raises(MainLoopUnavailableError):
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


def test_run_on_main_cancelled_async_target_from_worker_is_not_an_error(
    running_main_loop: asyncio.AbstractEventLoop,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A coroutine sent from a worker thread rides a concurrent.futures
    future. When that future is cancelled, ``exception()`` raises
    ``concurrent.futures.CancelledError``, a different class from
    ``asyncio.CancelledError`` since Python 3.8, so the done-callback used
    to raise and the futures module logged a traceback for every coroutine
    cancelled at shutdown. Seen in a FastAPI app on 2026-09-19."""
    scheduler = Quiv(main_loop=running_main_loop)
    cancelled = threading.Event()

    async def cancels_itself() -> None:
        task = asyncio.current_task()
        assert task is not None
        task.cancel()
        try:
            await asyncio.sleep(0)
        finally:
            cancelled.set()

    def handler() -> None:
        run_on_main(cancels_itself)

    try:
        scheduler.add_task(
            task_name="cancelled-target",
            func=handler,
            interval=60,
            run_once=True,
        )
        with caplog.at_level(logging.ERROR):
            scheduler.start()
            assert cancelled.wait(timeout=3)
            time.sleep(0.2)  # the done-callback runs after the task settles
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert errors == []
    finally:
        scheduler.shutdown()


def _cancel_and_settle(
    loop: asyncio.AbstractEventLoop, task: asyncio.Task[Any] | None
) -> None:
    """Cancel a task left pending, and wait for it to settle.

    A pending task at loop teardown emits "Task was destroyed but it is
    pending", which would mask a real failure in another test.
    """
    if task is None:
        return
    loop.call_soon_threadsafe(task.cancel)
    deadline = time.monotonic() + 2.0
    while not task.done() and time.monotonic() < deadline:
        time.sleep(0.01)


def test_shutdown_does_not_block_on_main_loop_work(
    running_main_loop: asyncio.AbstractEventLoop,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """shutdown() does not wait for work handed over by run_on_main.

    The handler returns the instant it hands the work over, so its job is
    already complete and there is nothing running for shutdown() to wait
    on. It cannot wait either: it is synchronous and the FastAPI lifespan
    calls it from the main loop's own thread, so blocking there would wait
    on the very thread the work needs. ``ashutdown`` is the way to wait;
    this test guards against a blocking wait being added here.
    """
    scheduler = Quiv(main_loop=running_main_loop)
    started = threading.Event()
    finished = threading.Event()
    holder: dict[str, asyncio.Task[Any]] = {}
    stopped = False

    async def slow_work() -> None:
        current = asyncio.current_task()
        assert current is not None
        holder["task"] = current
        started.set()
        await asyncio.sleep(2.0)
        finished.set()

    def handler() -> None:
        run_on_main(slow_work)

    try:
        scheduler.add_task(
            task_name="hand-off",
            func=handler,
            interval=60,
            run_once=True,
        )
        scheduler.start()
        assert started.wait(timeout=3)

        before = time.monotonic()
        with caplog.at_level(logging.WARNING, logger="Quiv"):
            scheduler.shutdown()
        stopped = True
        elapsed = time.monotonic() - before

        assert elapsed < 1.0, "shutdown() waited for main-loop work"
        assert not finished.is_set(), "the work was still pending"
        # Silence is the real hazard here, so it says what it left behind.
        assert any("still pending" in r.message for r in caplog.records)
    finally:
        # shutdown() deletes the temp database, so calling it twice fails.
        if not stopped:
            scheduler.shutdown()
        _cancel_and_settle(running_main_loop, holder.get("task"))


def test_ashutdown_waits_for_main_loop_work(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    """The point of ashutdown(): awaiting yields the thread, so the work
    being waited on can actually run."""
    finished = threading.Event()
    started = threading.Event()

    async def scenario() -> None:
        scheduler = Quiv(main_loop=asyncio.get_running_loop())

        async def slow_work() -> None:
            started.set()
            await asyncio.sleep(0.4)
            finished.set()

        def handler() -> None:
            run_on_main(slow_work)

        scheduler.add_task(
            task_name="hand-off", func=handler, interval=60, run_once=True
        )
        scheduler.start()
        while not started.is_set():
            await asyncio.sleep(0.01)
        await scheduler.ashutdown()

    asyncio.run_coroutine_threadsafe(scenario(), running_main_loop).result(
        timeout=10
    )
    assert finished.is_set(), "ashutdown() returned before the work finished"


def test_ashutdown_waits_for_a_queued_sync_callable(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    """A sync callable sent with call_soon_threadsafe has no future to
    await. Without a marker standing in for it, a drain would see an empty
    set while the callback was still in the loop's ready queue."""
    ran = threading.Event()

    async def scenario() -> None:
        scheduler = Quiv(main_loop=asyncio.get_running_loop())

        def on_loop() -> None:
            time.sleep(0.2)
            ran.set()

        def handler() -> None:
            run_on_main(on_loop)

        scheduler.add_task(
            task_name="queued", func=handler, interval=60, run_once=True
        )
        scheduler.start()
        await asyncio.sleep(0.3)  # let the job dispatch and hand over
        await scheduler.ashutdown()

    asyncio.run_coroutine_threadsafe(scenario(), running_main_loop).result(
        timeout=10
    )
    assert ran.is_set(), "ashutdown() returned before the callable ran"


def test_drain_yields_for_a_marker_that_cannot_be_awaited(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    """A queued sync callable is represented by a plain marker, which has
    no future. The drain waits those out by yielding to the loop instead of
    awaiting, so it must not return while one is still present."""

    async def scenario() -> None:
        scheduler = Quiv(main_loop=asyncio.get_running_loop())
        marker = object()
        scheduler._track_main_loop_work(marker)

        async def release_later() -> None:
            await asyncio.sleep(0.05)
            scheduler._untrack_main_loop_work(marker)

        asyncio.ensure_future(release_later())
        try:
            await scheduler._drain_main_loop_work(timeout=2.0)
            assert not scheduler._main_loop_work
        finally:
            scheduler.shutdown()

    asyncio.run_coroutine_threadsafe(scenario(), running_main_loop).result(
        timeout=10
    )


def test_ashutdown_from_another_loop_marshals_the_drain(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    """A drain must run on the loop that owns the work it waits on.

    ``run_on_main`` called from main-loop code, such as a FastAPI route,
    creates an ``asyncio.Task`` on that loop. ``asyncio.wait`` on a task
    from a different loop raises ``ValueError: The future belongs to a
    different loop``, so the drain is marshalled to the owning loop rather
    than awaited from wherever ``ashutdown`` was called.

    A handoff from a worker thread is not affected: that one produces a
    ``concurrent.futures.Future``, which ``asyncio.wrap_future`` adopts on
    any loop. Only a task hits this.
    """
    scheduler = Quiv(main_loop=running_main_loop)
    started = threading.Event()
    finished = threading.Event()

    async def slow_work() -> None:
        started.set()
        # Long enough to still be pending once shutdown() has returned,
        # which is what puts a foreign-loop task in front of the drain.
        await asyncio.sleep(1.5)
        finished.set()

    async def prime() -> None:
        # On the main loop, so this creates a Task rather than a Future.
        run_on_main(slow_work)

    scheduler.start()
    asyncio.run_coroutine_threadsafe(prime(), running_main_loop).result(
        timeout=3
    )
    assert started.wait(timeout=3)

    # A second, unrelated loop on this thread — not the one Quiv was given.
    asyncio.run(scheduler.ashutdown())

    assert finished.is_set(), "the drain did not wait from the other loop"


def test_ashutdown_says_so_when_the_main_loop_is_already_gone(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """If the main loop closed while quiv was shutting down, the drain
    cannot run. Say what was left instead of raising out of shutdown."""
    dead_loop = asyncio.new_event_loop()
    dead_loop.close()

    scheduler = Quiv(main_loop=dead_loop)
    try:
        scheduler._track_main_loop_work(object())
        with caplog.at_level(logging.WARNING, logger="Quiv"):
            asyncio.run(scheduler.ashutdown())
        assert any(
            "no longer running" in r.message for r in caplog.records
        )
    finally:
        with contextlib.suppress(Exception):
            scheduler.shutdown()


def test_ashutdown_says_so_when_the_loop_stops_mid_shutdown(
    running_main_loop: asyncio.AbstractEventLoop,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The loop can still be alive when ashutdown() resolves it and gone by
    the time the drain is marshalled. Report it rather than raising out of
    shutdown."""
    scheduler = Quiv(main_loop=running_main_loop)
    try:
        scheduler._track_main_loop_work(object())

        def gone(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("Event loop is closed")

        monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", gone)
        with caplog.at_level(logging.WARNING, logger="Quiv"):
            asyncio.run(scheduler.ashutdown())

        assert any(
            "stopped while quiv was shutting down" in r.message
            for r in caplog.records
        )
    finally:
        monkeypatch.undo()
        with contextlib.suppress(Exception):
            scheduler.shutdown()


def test_marker_is_removed_when_the_loop_enqueue_fails(
    running_main_loop: asyncio.AbstractEventLoop,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The marker is tracked before the enqueue, so a failing enqueue would
    otherwise leave it in the set for good: a later ashutdown() would wait
    out its whole timeout, and shutdown() would report work that was never
    queued."""
    scheduler = Quiv(main_loop=running_main_loop)
    try:
        scheduler.start()

        def boom(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("Event loop is closed")

        monkeypatch.setattr(running_main_loop, "call_soon_threadsafe", boom)
        with pytest.raises(RuntimeError, match="Event loop is closed"):
            run_on_main(lambda: None)

        assert not scheduler._main_loop_work, "the marker was left behind"
    finally:
        monkeypatch.undo()
        scheduler.shutdown()


def test_ashutdown_warns_when_it_leaves_abandoned_jobs_running(
    running_main_loop: asyncio.AbstractEventLoop,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """With a timeout, a job that ignores its stop event is abandoned and
    keeps running. It can hand work over after the drain has finished, and
    nothing can prevent that, so ashutdown() says so rather than implying
    the queue is closed."""
    records: list[str] = []
    release = threading.Event()

    async def scenario() -> None:
        scheduler = Quiv(main_loop=asyncio.get_running_loop())

        def ignores_cancellation() -> None:
            release.wait(timeout=5)

        scheduler.add_task(
            task_name="stubborn",
            func=ignores_cancellation,
            interval=60,
            run_once=True,
            delay=0,
        )
        scheduler.start()
        await asyncio.sleep(0.3)
        with caplog.at_level(logging.WARNING, logger="Quiv"):
            await scheduler.ashutdown(timeout=0.3)
        records.extend(r.message for r in caplog.records)

    try:
        asyncio.run_coroutine_threadsafe(
            scenario(), running_main_loop
        ).result(timeout=15)
        assert any("abandoned job" in m for m in records)
    finally:
        release.set()


def test_ashutdown_drain_timeout_gives_up_with_a_warning(
    running_main_loop: asyncio.AbstractEventLoop,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A drain is bounded, so one stuck callable cannot hold up exit."""
    holder: dict[str, asyncio.Task[Any]] = {}
    records: list[str] = []

    async def scenario() -> None:
        scheduler = Quiv(main_loop=asyncio.get_running_loop())

        async def never_finishes() -> None:
            current = asyncio.current_task()
            assert current is not None
            holder["task"] = current
            await asyncio.sleep(30)

        def handler() -> None:
            run_on_main(never_finishes)

        scheduler.add_task(
            task_name="stuck", func=handler, interval=60, run_once=True
        )
        scheduler.start()
        await asyncio.sleep(0.3)
        with caplog.at_level(logging.WARNING, logger="Quiv"):
            await scheduler.ashutdown(timeout=0.3)
        records.extend(r.message for r in caplog.records)

    asyncio.run_coroutine_threadsafe(scenario(), running_main_loop).result(
        timeout=10
    )
    assert any("drain timeout" in m for m in records)
    _cancel_and_settle(running_main_loop, holder.get("task"))


def test_run_on_main_on_loop_async_target_exception_is_logged(
    running_main_loop: asyncio.AbstractEventLoop,
    caplog: pytest.LogCaptureFixture,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    raised = threading.Event()

    async def boom() -> None:
        raised.set()
        raise RuntimeError("on-loop async boom")

    async def caller() -> None:
        run_on_main(boom)

    try:
        scheduler.start()
        with caplog.at_level(logging.ERROR, logger="Quiv"):
            asyncio.run_coroutine_threadsafe(
                caller(), running_main_loop
            ).result(timeout=2)
            assert raised.wait(timeout=2)
            # Give the done callback a tick to fire.
            time.sleep(0.1)
        assert any(
            "on-loop async boom" in r.message for r in caplog.records
        )
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


def test_run_on_main_with_unresolvable_main_loop_raises() -> None:
    scheduler = Quiv()
    try:
        scheduler.start()
        with pytest.raises(RuntimeError, match="resolvable main event loop"):
            run_on_main(lambda: None)
    finally:
        scheduler.shutdown()


def test_run_on_main_swallows_cancelled_target(
    running_main_loop: asyncio.AbstractEventLoop,
    caplog: pytest.LogCaptureFixture,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    started = threading.Event()

    async def raises_cancelled() -> None:
        started.set()
        raise asyncio.CancelledError("explicit")

    async def caller() -> None:
        run_on_main(raises_cancelled)

    try:
        scheduler.start()
        with caplog.at_level(logging.ERROR, logger="Quiv"):
            asyncio.run_coroutine_threadsafe(
                caller(), running_main_loop
            ).result(timeout=2)
            assert started.wait(timeout=3)
            time.sleep(0.2)
        assert not any(
            "run_on_main callable" in r.message for r in caplog.records
        )
    finally:
        scheduler.shutdown()


def test_run_on_main_on_loop_sync_target_returning_coroutine(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    main_thread_id = _main_thread_id(running_main_loop)
    captured: dict[str, int] = {}
    done = threading.Event()

    def factory() -> Any:
        async def inner() -> None:
            captured["thread_id"] = threading.get_ident()
            done.set()

        return inner()

    async def caller() -> None:
        run_on_main(factory)

    try:
        scheduler.start()
        asyncio.run_coroutine_threadsafe(
            caller(), running_main_loop
        ).result(timeout=2)
        assert done.wait(timeout=3)
        assert captured["thread_id"] == main_thread_id
    finally:
        scheduler.shutdown()


def test_run_on_main_on_loop_sync_target_exception_is_logged(
    running_main_loop: asyncio.AbstractEventLoop,
    caplog: pytest.LogCaptureFixture,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)

    def boom() -> None:
        raise RuntimeError("on-loop sync boom")

    async def caller() -> None:
        run_on_main(boom)

    try:
        scheduler.start()
        with caplog.at_level(logging.ERROR, logger="Quiv"):
            asyncio.run_coroutine_threadsafe(
                caller(), running_main_loop
            ).result(timeout=2)
        assert any(
            "on-loop sync boom" in r.message for r in caplog.records
        )
    finally:
        scheduler.shutdown()


def test_run_on_main_cross_thread_sync_target_returning_coroutine(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    main_thread_id = _main_thread_id(running_main_loop)
    captured: dict[str, int] = {}
    done = threading.Event()

    def factory() -> Any:
        async def inner() -> None:
            captured["thread_id"] = threading.get_ident()
            done.set()

        return inner()

    def handler() -> None:
        run_on_main(factory)

    try:
        scheduler.add_task(
            task_name="cross-thread-coro-factory",
            func=handler,
            interval=60,
            run_once=True,
        )
        scheduler.start()
        assert done.wait(timeout=3)
        assert captured["thread_id"] == main_thread_id
    finally:
        scheduler.shutdown()


def test_shutdown_unregisters_active_instance(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    scheduler.start()
    assert quiv_context._get_active_quiv() is scheduler
    scheduler.shutdown()
    assert quiv_context._get_active_quiv() is None
