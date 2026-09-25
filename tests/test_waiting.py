"""Waiting for a job or for a task's next job (Phase 8)."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from concurrent.futures import Future
from typing import Any

import pytest

from quiv import (
    Event,
    Job,
    JobNotFoundError,
    JobStatus,
    Quiv,
    SchedulerStoppedError,
    Task,
    TaskNotFoundError,
)
from quiv.models import TaskDB


def _run_on(
    loop: asyncio.AbstractEventLoop, coro: Any, timeout: float = 10
) -> Any:
    """Run a coroutine on the fixture loop from the test thread."""

    return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout)


def test_wait_for_job_returns_the_finalized_job(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    started: list[str] = []
    seen = threading.Event()

    def on_started(event: Event, task: Task, job: Job) -> None:
        assert job.id is not None
        started.append(job.id)
        seen.set()

    try:
        scheduler.add_listener(Event.JOB_STARTED, on_started)
        scheduler.add_task(
            "slow", lambda: time.sleep(0.3), run_once=True
        )
        scheduler.start()
        assert seen.wait(timeout=5)

        job = scheduler.wait_for_job(started[0], timeout=5)

        assert job.id == started[0]
        assert job.status == JobStatus.COMPLETED
        assert job.duration_seconds is not None
        assert job.duration_seconds >= 0.25
        assert scheduler._job_waiters == {}
    finally:
        scheduler.shutdown()


def test_wait_for_job_on_a_finished_job_returns_at_once(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    try:
        task_id = scheduler.add_task("quick", lambda: None, run_once=True)
        scheduler.start()
        first = scheduler.wait_for_task(task_id, timeout=5)
        assert first.id is not None

        before = time.monotonic()
        again = scheduler.wait_for_job(first.id, timeout=5)

        assert time.monotonic() - before < 0.5
        assert again.status == JobStatus.COMPLETED
    finally:
        scheduler.shutdown()


def test_wait_for_job_unknown_id_raises_and_leaves_no_waiter(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    try:
        with pytest.raises(JobNotFoundError):
            scheduler.wait_for_job("no-such-job", timeout=1)
        assert scheduler._job_waiters == {}
    finally:
        scheduler.shutdown()


def test_wait_for_task_times_out_with_the_builtin_timeout_error(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    try:
        task_id = scheduler.add_task("later", lambda: None, interval=60, delay=60)
        scheduler.start()

        with pytest.raises(TimeoutError, match="did not finish within"):
            scheduler.wait_for_task(task_id, timeout=0.2)
        assert scheduler._task_waiters == {}
    finally:
        scheduler.shutdown()


def test_wait_for_task_returns_the_next_job_each_time(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    try:
        task_id = scheduler.add_task("tick", lambda: None, interval=0.2)
        scheduler.start()

        first = scheduler.wait_for_task(task_id, timeout=5)
        second = scheduler.wait_for_task(task_id, timeout=5)

        assert first.id != second.id
        assert first.ended_at is not None and second.started_at is not None
        assert first.ended_at <= second.started_at
    finally:
        scheduler.shutdown()


def test_wait_for_task_unknown_id_raises_task_not_found(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    try:
        with pytest.raises(TaskNotFoundError):
            scheduler.wait_for_task("no-such-task", timeout=1)
        assert scheduler._task_waiters == {}
    finally:
        scheduler.shutdown()


def test_wait_for_task_on_a_run_once_task_that_deletes_its_row(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    try:
        task_id = scheduler.add_task("once", lambda: None, run_once=True)
        scheduler.start()

        job = scheduler.wait_for_task(task_id, timeout=5)

        assert job.status == JobStatus.COMPLETED
        with pytest.raises(TaskNotFoundError):
            scheduler.get_task(task_id)
    finally:
        scheduler.shutdown()


def test_remove_task_fails_idle_waiters_with_task_not_found(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    outcome: list[BaseException | Job] = []
    try:
        task_id = scheduler.add_task("idle", lambda: None, interval=60, delay=60)
        scheduler.start()

        def waiter() -> None:
            try:
                outcome.append(scheduler.wait_for_task(task_id, timeout=5))
            except BaseException as exc:
                outcome.append(exc)

        thread = threading.Thread(target=waiter)
        thread.start()
        deadline = time.monotonic() + 5
        while task_id not in scheduler._task_waiters:
            assert time.monotonic() < deadline
            time.sleep(0.01)

        scheduler.remove_task(task_id)
        thread.join(timeout=5)

        assert len(outcome) == 1
        assert isinstance(outcome[0], TaskNotFoundError)
        assert scheduler._task_waiters == {}
    finally:
        scheduler.shutdown()


def test_remove_task_leaves_the_waiters_of_a_running_job(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    running = threading.Event()

    def handler(stop_event: threading.Event) -> None:
        running.set()
        stop_event.wait(timeout=5)

    try:
        task_id = scheduler.add_task("busy", handler, interval=60)
        scheduler.start()
        assert running.wait(timeout=5)

        result: Future[Job] = Future()

        def waiter() -> None:
            try:
                result.set_result(scheduler.wait_for_task(task_id, timeout=5))
            except BaseException as exc:
                result.set_exception(exc)

        threading.Thread(target=waiter).start()
        deadline = time.monotonic() + 5
        while task_id not in scheduler._task_waiters:
            assert time.monotonic() < deadline
            time.sleep(0.01)

        scheduler.remove_task(task_id)  # sets the job's stop event

        job = result.result(timeout=5)
        assert job.status == JobStatus.CANCELLED
    finally:
        scheduler.shutdown()


def test_await_job_and_await_task(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    try:
        task_id = scheduler.add_task("tick", lambda: None, interval=0.2)
        scheduler.start()

        first = _run_on(running_main_loop, scheduler.await_task(task_id, timeout=5))
        assert first.status == JobStatus.COMPLETED
        assert first.id is not None

        same = _run_on(running_main_loop, scheduler.await_job(first.id, timeout=5))
        assert same.id == first.id
    finally:
        scheduler.shutdown()


def test_await_task_timeout_is_the_builtin_timeout_error(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    try:
        task_id = scheduler.add_task("later", lambda: None, interval=60, delay=60)
        scheduler.start()

        with pytest.raises(TimeoutError, match="did not finish within"):
            _run_on(
                running_main_loop, scheduler.await_task(task_id, timeout=0.2)
            )
        assert scheduler._task_waiters == {}
    finally:
        scheduler.shutdown()


def test_await_job_timeout_and_unknown_id(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    started: list[str] = []
    seen = threading.Event()

    def on_started(event: Event, task: Task, job: Job) -> None:
        assert job.id is not None
        started.append(job.id)
        seen.set()

    try:
        scheduler.add_listener(Event.JOB_STARTED, on_started)
        scheduler.add_task("slow", lambda: time.sleep(1.0), run_once=True)
        scheduler.start()
        assert seen.wait(timeout=5)

        with pytest.raises(TimeoutError):
            _run_on(
                running_main_loop, scheduler.await_job(started[0], timeout=0.1)
            )
        with pytest.raises(JobNotFoundError):
            _run_on(running_main_loop, scheduler.await_job("nope", timeout=1))
        assert scheduler._job_waiters == {}
    finally:
        scheduler.shutdown()


def test_a_cancelled_await_does_not_break_resolution(
    running_main_loop: asyncio.AbstractEventLoop,
    caplog: pytest.LogCaptureFixture,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    release = threading.Event()

    def handler() -> None:
        release.wait(timeout=5)

    try:
        task_id = scheduler.add_task("held", handler, run_once=True)
        scheduler.start()

        async def start_and_cancel() -> None:
            awaiting = asyncio.ensure_future(
                scheduler.await_task(task_id, timeout=10)
            )
            await asyncio.sleep(0.1)  # let it register
            awaiting.cancel()
            with pytest.raises(asyncio.CancelledError):
                await awaiting

        with caplog.at_level(logging.ERROR):
            _run_on(running_main_loop, start_and_cancel())
            assert scheduler._task_waiters == {}
            release.set()
            job = scheduler.get_all_jobs(task_id=task_id)
            deadline = time.monotonic() + 5
            while not job or job[0].status != JobStatus.COMPLETED:
                assert time.monotonic() < deadline
                time.sleep(0.05)
                job = scheduler.get_all_jobs(task_id=task_id)
        assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []
    finally:
        scheduler.shutdown()


def test_resolve_and_fail_skip_a_waiter_that_is_already_done(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    """A future that timed out or was cancelled must not raise
    InvalidStateError when the job it waited for finalizes."""

    scheduler = Quiv(main_loop=running_main_loop)
    try:
        done_fut = scheduler._add_waiter(scheduler._job_waiters, "j1")
        done_fut.cancel()
        live_fut = scheduler._add_waiter(scheduler._job_waiters, "j1")
        stub = Job(task_id="t", task_name="n", status=JobStatus.COMPLETED)

        scheduler._resolve_waiters(scheduler._job_waiters, "j1", stub)

        assert live_fut.result(timeout=1) is stub
        assert done_fut.cancelled()

        done_fut = scheduler._add_waiter(scheduler._task_waiters, "t1")
        done_fut.cancel()
        live_fut = scheduler._add_waiter(scheduler._task_waiters, "t1")
        scheduler._fail_waiters(
            scheduler._task_waiters, "t1", TaskNotFoundError("gone")
        )
        with pytest.raises(TaskNotFoundError):
            live_fut.result(timeout=1)
        assert scheduler._job_waiters == {} and scheduler._task_waiters == {}
    finally:
        scheduler.shutdown()


def test_events_are_emitted_before_waiters_resolve(
    running_main_loop: asyncio.AbstractEventLoop,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    order: list[str] = []
    real_emit = scheduler._emit_event
    real_resolve = scheduler._resolve_waiters

    def emit(event: Event, *args: Any) -> None:
        order.append(f"emit:{event.value}")
        real_emit(event, *args)

    def resolve(table: Any, key: str, job: Job) -> None:
        order.append("resolve")
        real_resolve(table, key, job)

    monkeypatch.setattr(scheduler, "_emit_event", emit)
    monkeypatch.setattr(scheduler, "_resolve_waiters", resolve)
    try:
        task_id = scheduler.add_task("once", lambda: None, run_once=True)
        scheduler.start()
        scheduler.wait_for_task(task_id, timeout=5)

        assert order.index("emit:job_completed") < order.index("resolve")
    finally:
        scheduler.shutdown()


def test_shutdown_with_a_timeout_fails_abandoned_waiters(
    running_main_loop: asyncio.AbstractEventLoop,
    leftover_db_paths: list[str],
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    leftover_db_paths.append(scheduler._db_path)
    running = threading.Event()

    def ignores_stop() -> None:
        running.set()
        time.sleep(2.0)

    try:
        task_id = scheduler.add_task("stuck", ignores_stop, run_once=True)
        scheduler.start()
        assert running.wait(timeout=5)

        result: Future[Job] = Future()

        def waiter() -> None:
            try:
                result.set_result(scheduler.wait_for_task(task_id))
            except BaseException as exc:
                result.set_exception(exc)

        threading.Thread(target=waiter).start()
        deadline = time.monotonic() + 5
        while task_id not in scheduler._task_waiters:
            assert time.monotonic() < deadline
            time.sleep(0.01)

        scheduler.shutdown(timeout=0.2)

        with pytest.raises(SchedulerStoppedError):
            result.result(timeout=5)
    finally:
        pass  # shutdown already ran; a second call would fail


def test_waiting_after_shutdown_raises_scheduler_stopped(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    scheduler.start()
    scheduler.shutdown()

    with pytest.raises(SchedulerStoppedError):
        scheduler.wait_for_job("any", timeout=1)
    with pytest.raises(SchedulerStoppedError):
        scheduler.wait_for_task("any", timeout=1)
    with pytest.raises(SchedulerStoppedError):
        _run_on(running_main_loop, scheduler.await_task("any", timeout=1))


def test_exception_hierarchy_for_the_new_errors() -> None:
    from quiv import JobCancelledError, QuivError

    assert issubclass(JobCancelledError, QuivError)
    assert issubclass(SchedulerStoppedError, QuivError)


def test_remove_task_treats_a_scheduled_job_as_in_flight(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    """A job that dispatch created but _run_job has not started yet has a
    stop event already. remove_task signals it and keeps the waiters."""

    scheduler = Quiv(main_loop=running_main_loop)
    try:
        task_id = scheduler.add_task("later", lambda: None, interval=60, delay=60)
        job_id = scheduler.persistence.create_job(task_id, "later", attempt=1)
        stop_event = threading.Event()
        scheduler.stop_events[job_id] = stop_event
        fut = scheduler._add_waiter(scheduler._task_waiters, task_id)

        scheduler.remove_task(task_id)

        assert stop_event.is_set()
        assert not fut.done()
        assert task_id in scheduler._task_waiters
    finally:
        scheduler.shutdown()
    with pytest.raises(SchedulerStoppedError):
        fut.result(timeout=1)


def test_remove_task_keeps_waiters_when_the_row_was_running_at_deletion(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    """Between mark_task_running and create_job there is no job row yet.
    The status the row had at deletion says a dispatch is in flight."""

    scheduler = Quiv(main_loop=running_main_loop)
    try:
        task_id = scheduler.add_task("later", lambda: None, interval=60, delay=60)
        scheduler.persistence.mark_task_running(task_id)
        fut = scheduler._add_waiter(scheduler._task_waiters, task_id)

        scheduler.remove_task(task_id)

        assert not fut.done()
        assert task_id in scheduler._task_waiters
    finally:
        scheduler.shutdown()


def test_dispatch_fails_the_waiters_when_the_row_vanished_after_marking(
    running_main_loop: asyncio.AbstractEventLoop,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The other half of that race: the dispatch marked the row, remove_task
    deleted it and kept the waiters, and the dispatch finds it gone."""

    scheduler = Quiv(main_loop=running_main_loop)
    try:
        task_id = scheduler.add_task("later", lambda: None, interval=60, delay=60)
        fut = scheduler._add_waiter(scheduler._task_waiters, task_id)
        scheduler.persistence.mark_task_running(task_id)
        assert scheduler.persistence.delete_task(task_id) == "running"

        with caplog.at_level(logging.WARNING, logger="Quiv"):
            scheduler._dispatch_due_task(
                TaskDB(id=task_id, task_name="later"), scheduler._now_utc()
            )

        with pytest.raises(TaskNotFoundError, match="before its job started"):
            fut.result(timeout=1)
        assert scheduler._task_waiters == {}
        assert any("deleted before dispatch" in r.message for r in caplog.records)
    finally:
        scheduler.shutdown()


def test_delete_task_returns_the_status_the_row_had(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    try:
        a = scheduler.add_task("a", lambda: None, interval=60, delay=60)
        b = scheduler.add_task("b", lambda: None, interval=60, delay=60)
        scheduler.persistence.mark_task_running(b)

        assert scheduler.persistence.delete_task(a) == "active"
        assert scheduler.persistence.delete_task(b) == "running"
    finally:
        scheduler.shutdown()
