"""One error costs one job, not the scheduler (#86)."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from concurrent.futures import Future
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import pytest
from sqlalchemy import Engine, event, text

from quiv import (
    Event,
    Job,
    JobStatus,
    Quiv,
    Task,
    TaskNotFoundError,
    TaskStatus,
)
from quiv.models import LATEST_RUN_AT, seconds_after


def _wait_until(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition not met in time")
        time.sleep(0.02)


def _fail_once(real: Callable[..., Any], message: str) -> Callable[..., Any]:
    """Wrap ``real`` so that its first call raises and later calls pass."""

    calls = {"n": 0}

    def flaky(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError(message)
        return real(*args, **kwargs)

    return flaky


# ---------------------------------------------------------------------------
# _run_job: before the handler starts
# ---------------------------------------------------------------------------


def test_an_error_before_the_handler_starts_fails_the_job(
    monkeypatch: pytest.MonkeyPatch,
    running_main_loop: asyncio.AbstractEventLoop,
    caplog: pytest.LogCaptureFixture,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    ran: list[int] = []
    failed: list[Job] = []

    def on_failed(event: Event, task: Task, job: Job) -> None:
        failed.append(job)

    try:
        monkeypatch.setattr(
            scheduler.persistence,
            "mark_job_running",
            _fail_once(
                scheduler.persistence.mark_job_running, "database is locked"
            ),
        )
        scheduler.add_listener(Event.JOB_FAILED, on_failed)
        task_id = scheduler.add_task(
            "recurring", lambda: ran.append(1), interval=60, delay=0.2
        )
        scheduler.start()

        job = scheduler.wait_for_task(task_id, timeout=5)

        assert job.status == JobStatus.FAILED
        assert job.error_message == "database is locked"
        assert ran == []
        assert scheduler.get_task(task_id).status == TaskStatus.ACTIVE
        assert scheduler._active_job_count == 0
        assert "could not start: database is locked" in caplog.text
        _wait_until(lambda: len(failed) == 1)

        # The task is ACTIVE again, so it can still run. Before the fix it
        # stayed RUNNING, and this raised TaskNotActiveError.
        scheduler.run_task_immediately(task_id)
        _wait_until(lambda: ran == [1])
    finally:
        scheduler.shutdown()


# ---------------------------------------------------------------------------
# _run_job: a BaseException from the handler
# ---------------------------------------------------------------------------


def test_cancelled_error_from_the_handler_is_recorded_as_failed(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    completed: list[Job] = []
    failed: list[Job] = []

    async def cancelled() -> None:
        await asyncio.sleep(0.2)
        raise asyncio.CancelledError()

    try:
        scheduler.add_listener(
            Event.JOB_COMPLETED, lambda e, t, j: completed.append(j)
        )
        scheduler.add_listener(Event.JOB_FAILED, lambda e, t, j: failed.append(j))
        task_id = scheduler.add_task(
            "cancelled-once", cancelled, run_once=True, delay=0.2
        )
        scheduler.start()

        job = scheduler.wait_for_task(task_id, timeout=5)

        assert job.status == JobStatus.FAILED
        assert job.error_message == "CancelledError()"
        assert job.duration_seconds is not None
        assert job.duration_seconds >= 0.15
        _wait_until(lambda: len(failed) == 1)
        assert completed == []
    finally:
        scheduler.shutdown()


@pytest.mark.parametrize(
    ("raised", "expected_message"),
    [(SystemExit(3), "SystemExit(3)"), (KeyboardInterrupt(), "KeyboardInterrupt()")],
)
def test_system_exit_and_keyboard_interrupt_are_recorded_then_reraised(
    monkeypatch: pytest.MonkeyPatch,
    running_main_loop: asyncio.AbstractEventLoop,
    raised: BaseException,
    expected_message: str,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    futures: list[Future[Any]] = []
    real_submit = scheduler.executor.submit

    def submit(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Future[Any]:
        fut = real_submit(fn, *args, **kwargs)
        futures.append(fut)
        return fut

    def handler() -> None:
        raise raised

    try:
        monkeypatch.setattr(scheduler.executor, "submit", submit)
        task_id = scheduler.add_task(
            "exits", handler, run_once=True, delay=0.2
        )
        scheduler.start()

        job = scheduler.wait_for_task(task_id, timeout=5)

        assert job.status == JobStatus.FAILED
        assert job.error_message == expected_message
        assert scheduler._active_job_count == 0
        # The job can finish and wake the waiter before real_submit
        # returns on the loop thread, so the future is not in the list yet.
        _wait_until(lambda: len(futures) == 1)
        # Recorded, but not swallowed: the exception reaches the future.
        assert futures[0].exception(timeout=5) is raised
    finally:
        scheduler.shutdown()


# ---------------------------------------------------------------------------
# _run_job: the finalizer
# ---------------------------------------------------------------------------


def test_a_failed_finalize_job_still_hands_over_the_finished_job(
    monkeypatch: pytest.MonkeyPatch,
    running_main_loop: asyncio.AbstractEventLoop,
    caplog: pytest.LogCaptureFixture,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    ended: list[Job] = []

    def on_end(event: Event, task: Task, job: Job) -> None:
        ended.append(job)

    try:
        monkeypatch.setattr(
            scheduler.persistence,
            "finalize_job",
            _fail_once(scheduler.persistence.finalize_job, "disk I/O error"),
        )
        for end_event in (
            Event.JOB_COMPLETED,
            Event.JOB_FAILED,
            Event.JOB_CANCELLED,
        ):
            scheduler.add_listener(end_event, on_end)
        task_id = scheduler.add_task(
            "recurring", lambda: None, interval=60, delay=0.2
        )
        scheduler.start()

        job = scheduler.wait_for_task(task_id, timeout=5)

        # The end status was not written, but the waiter still gets the
        # outcome of the handler, not the row that says RUNNING.
        assert job.status == JobStatus.COMPLETED
        assert job.ended_at is not None
        assert job.duration_seconds is not None
        assert job.error_message is None
        assert job.attempt == 1
        assert scheduler.get_job(job.id).status == JobStatus.RUNNING
        # Listeners run on the main loop: the JOB_* event emitted before
        # the waiter woke has run once this round trip returns.
        asyncio.run_coroutine_threadsafe(
            asyncio.sleep(0), running_main_loop
        ).result(timeout=5)
        assert [(j.id, j.status) for j in ended] == [
            (job.id, JobStatus.COMPLETED)
        ]
        # The task and the slot are not lost with the job's end status.
        assert scheduler.get_task(task_id).status == TaskStatus.ACTIVE
        assert scheduler._active_job_count == 0
        assert "could not record its end status 'completed'" in caplog.text
    finally:
        scheduler.shutdown()


def test_a_job_built_after_a_failed_write_keeps_the_error_and_the_attempt(
    monkeypatch: pytest.MonkeyPatch,
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    real_finalize_job = scheduler.persistence.finalize_job
    calls = {"n": 0}
    failed: list[Job] = []
    retrying: list[Job] = []

    def finalize_job(*args: Any, **kwargs: Any) -> None:
        # Fail the second write: the one for the retry, attempt 2.
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("disk I/O error")
        real_finalize_job(*args, **kwargs)

    def boom() -> None:
        raise ValueError("boom")

    def on_failed(event: Event, task: Task, job: Job) -> None:
        failed.append(job)

    def on_retrying(event: Event, task: Task, job: Job) -> None:
        retrying.append(job)

    try:
        monkeypatch.setattr(
            scheduler.persistence, "finalize_job", finalize_job
        )
        scheduler.add_listener(Event.JOB_FAILED, on_failed)
        scheduler.add_listener(Event.JOB_RETRYING, on_retrying)
        scheduler.add_task(
            "failing", boom, interval=60, max_retries=1, retry_backoff=0.1
        )
        scheduler.start()

        _wait_until(lambda: len(failed) == 2)

        built = failed[1]
        assert built.status == JobStatus.FAILED
        assert built.attempt == 2
        assert built.error_message is not None
        assert "boom" in built.error_message
        assert scheduler.get_job(built.id).status == JobStatus.RUNNING
        # Only the first failure scheduled a retry; the second used it up.
        assert [j.attempt for j in retrying] == [1]
    finally:
        scheduler.shutdown()


def test_a_failed_finalize_task_still_frees_the_slot(
    monkeypatch: pytest.MonkeyPatch,
    running_main_loop: asyncio.AbstractEventLoop,
    caplog: pytest.LogCaptureFixture,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop, pool_size=1)
    ran: list[str] = []
    try:
        monkeypatch.setattr(
            scheduler.persistence,
            "finalize_task_after_job",
            _fail_once(
                scheduler.persistence.finalize_task_after_job,
                "database is locked",
            ),
        )
        first = scheduler.add_task(
            "first", lambda: ran.append("first"), run_once=True, delay=0.2
        )
        scheduler.start()

        job = scheduler.wait_for_task(first, timeout=5)

        assert job.status == JobStatus.COMPLETED
        assert scheduler._active_job_count == 0
        assert "could not return task" in caplog.text
        # With pool_size=1, a leaked slot would stop every later dispatch.
        scheduler.add_task("second", lambda: ran.append("second"), run_once=True)
        _wait_until(lambda: ran == ["first", "second"])
    finally:
        scheduler.shutdown()


def test_a_failed_read_back_still_hands_over_the_finished_job(
    monkeypatch: pytest.MonkeyPatch,
    running_main_loop: asyncio.AbstractEventLoop,
    caplog: pytest.LogCaptureFixture,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    real_get_job = scheduler.get_job
    calls = {"n": 0}

    def get_job(job_id: str) -> Job:
        # The first call is the read for JOB_STARTED; fail the second,
        # the read after the job finished.
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("database is locked")
        return real_get_job(job_id)

    try:
        monkeypatch.setattr(scheduler, "get_job", get_job)
        task_id = scheduler.add_task(
            "once", lambda: None, run_once=True, delay=0.2
        )
        scheduler.start()

        # The row is written but cannot be read; the waiter gets the job
        # made from the values quiv wrote, instead of the error.
        job = scheduler.wait_for_task(task_id, timeout=5)

        assert job.status == JobStatus.COMPLETED
        assert job.duration_seconds is not None
        assert scheduler._active_job_count == 0
        assert scheduler._task_waiters == {}
        assert "could not be read back after it finished" in caplog.text
    finally:
        scheduler.shutdown()


# ---------------------------------------------------------------------------
# _loop: history cleanup
# ---------------------------------------------------------------------------


def test_a_failing_history_cleanup_does_not_stop_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    running_main_loop: asyncio.AbstractEventLoop,
    caplog: pytest.LogCaptureFixture,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    ran: list[int] = []

    def broken(_limit: int) -> None:
        raise RuntimeError("database disk image is malformed")

    try:
        monkeypatch.setattr(scheduler.persistence, "cleanup_history", broken)
        scheduler.add_task("due-now", lambda: ran.append(1), interval=0.1)
        scheduler.start()

        _wait_until(lambda: len(ran) >= 3)

        # The deadline moved before the call, so one failure is one log
        # line, not one per loop pass.
        assert caplog.text.count("Job history cleanup failed") == 1
        assert "Error in scheduler loop" not in caplog.text
    finally:
        scheduler.shutdown()


# ---------------------------------------------------------------------------
# _dispatch_due_task: after the task is marked RUNNING
# ---------------------------------------------------------------------------


def test_a_failed_create_job_returns_the_task_to_the_schedule(
    monkeypatch: pytest.MonkeyPatch,
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)

    def broken(*_args: Any, **_kwargs: Any) -> str:
        raise RuntimeError("database is locked")

    try:
        task_id = scheduler.add_task("recurring", lambda: None, interval=60)
        monkeypatch.setattr(scheduler.persistence, "create_job", broken)
        task = scheduler.persistence.get_task(task_id)

        with pytest.raises(RuntimeError, match="database is locked"):
            scheduler._dispatch_due_task(task, datetime.now(timezone.utc))

        # Still due and ACTIVE, so the loop's next pass dispatches it.
        assert scheduler.get_task(task_id).status == TaskStatus.ACTIVE
        assert scheduler._active_job_count == 0
        assert scheduler.stop_events == {}
        # No job started, so resume_task() must not see one.
        assert scheduler._tasks_in_flight == set()
    finally:
        scheduler.shutdown()


def test_a_failed_mark_task_running_leaves_the_task_as_it_was(
    monkeypatch: pytest.MonkeyPatch,
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    updates: list[str] = []

    def broken(_task_id: str) -> None:
        raise RuntimeError("database is locked")

    try:
        task_id = scheduler.add_task("recurring", lambda: None, interval=60)
        scheduler.pause_task(task_id)
        monkeypatch.setattr(scheduler.persistence, "mark_task_running", broken)
        monkeypatch.setattr(
            scheduler.persistence, "unmark_task_running", updates.append
        )
        task = scheduler.persistence.get_task(task_id)

        with pytest.raises(RuntimeError, match="database is locked"):
            scheduler._dispatch_due_task(task, datetime.now(timezone.utc))

        # This dispatch never marked the row, so it must not write it: a
        # pause that landed in between would be undone.
        assert updates == []
        assert scheduler.get_task(task_id).status == TaskStatus.PAUSED
    finally:
        scheduler.shutdown()


def test_a_pause_after_the_mark_survives_a_failed_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    try:
        task_id = scheduler.add_task("recurring", lambda: None, interval=60)

        def create_job_after_pause(*_args: Any, **_kwargs: Any) -> str:
            # pause_task() lands between mark_task_running and the job
            # insert, then the insert fails.
            scheduler.pause_task(task_id)
            raise RuntimeError("database is locked")

        monkeypatch.setattr(
            scheduler.persistence, "create_job", create_job_after_pause
        )
        task = scheduler.persistence.get_task(task_id)

        with pytest.raises(RuntimeError, match="database is locked"):
            scheduler._dispatch_due_task(task, datetime.now(timezone.utc))

        # Still due: returned to ACTIVE, the next pass would run it.
        assert scheduler.get_task(task_id).status == TaskStatus.PAUSED
    finally:
        scheduler.shutdown()


def test_a_failed_create_job_after_remove_task_fails_the_waiters(
    monkeypatch: pytest.MonkeyPatch,
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    outcome: list[BaseException] = []

    def wait() -> None:
        try:
            scheduler.wait_for_task(task_id, timeout=5)
        except BaseException as e:
            outcome.append(e)

    try:
        task_id = scheduler.add_task("recurring", lambda: None, interval=60)
        waiter = threading.Thread(target=wait)
        waiter.start()
        _wait_until(lambda: task_id in scheduler._task_waiters)

        def create_job_after_remove(*_args: Any, **_kwargs: Any) -> str:
            # remove_task() lands between mark_task_running and the job
            # insert. It saw RUNNING, so it left the waiters for the job.
            scheduler.remove_task(task_id)
            raise RuntimeError("database is locked")

        monkeypatch.setattr(
            scheduler.persistence, "create_job", create_job_after_remove
        )
        task = scheduler.persistence.get_task(task_id)

        with pytest.raises(RuntimeError, match="database is locked"):
            scheduler._dispatch_due_task(task, datetime.now(timezone.utc))

        waiter.join(timeout=5)
        assert len(outcome) == 1
        assert isinstance(outcome[0], TaskNotFoundError)
        assert scheduler._tasks_in_flight == set()
    finally:
        scheduler.shutdown()


def test_a_failed_return_to_the_schedule_is_logged(
    monkeypatch: pytest.MonkeyPatch,
    running_main_loop: asyncio.AbstractEventLoop,
    caplog: pytest.LogCaptureFixture,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)

    def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("database is locked")

    try:
        task_id = scheduler.add_task("recurring", lambda: None, interval=60)
        monkeypatch.setattr(scheduler.persistence, "create_job", broken)
        monkeypatch.setattr(
            scheduler.persistence, "unmark_task_running", broken
        )
        task = scheduler.persistence.get_task(task_id)

        with caplog.at_level(logging.ERROR, logger="Quiv"):
            with pytest.raises(RuntimeError, match="database is locked"):
                scheduler._dispatch_due_task(task, datetime.now(timezone.utc))

        assert "could not be returned to the schedule" in caplog.text
    finally:
        scheduler.shutdown()


# ---------------------------------------------------------------------------
# Host applications that enforce SQLite foreign keys
# ---------------------------------------------------------------------------


@pytest.fixture
def foreign_keys_on_every_engine() -> Any:
    """Do what a host app may do: PRAGMA foreign_keys=ON on all engines."""

    def fk_on(dbapi_conn: Any, _record: Any) -> None:
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    event.listen(Engine, "connect", fk_on)
    try:
        yield
    finally:
        event.remove(Engine, "connect", fk_on)


@pytest.mark.usefixtures("foreign_keys_on_every_engine")
def test_run_once_and_remove_task_work_with_foreign_keys_enforced(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop, pool_size=1)
    try:
        with scheduler._engine.connect() as conn:
            assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1

        scheduler.start()
        # Each run-once task deletes its row while its job row remains.
        # With a foreign key that delete failed and leaked the only slot.
        for i in range(3):
            task_id = scheduler.add_task(
                f"once-{i}", lambda: None, run_once=True, delay=0.2
            )
            job = scheduler.wait_for_task(task_id, timeout=5)
            assert job.status == JobStatus.COMPLETED
            assert scheduler._active_job_count == 0

        recurring = scheduler.add_task(
            "recurring", lambda: None, interval=60, delay=0.2
        )
        scheduler.wait_for_task(recurring, timeout=5)
        # The task now has job history; deleting it must not fail.
        scheduler.remove_task(recurring)
        assert len(scheduler.get_all_jobs(task_id=recurring)) == 1
    finally:
        scheduler.shutdown()


# ---------------------------------------------------------------------------
# Run times past year 9999
# ---------------------------------------------------------------------------


def test_seconds_after_clamps_what_a_datetime_cannot_hold() -> None:
    now = datetime.now(timezone.utc)

    assert seconds_after(now, 60) == now + timedelta(seconds=60)
    # Too far for a datetime, too far for a timedelta, and infinity.
    for seconds in (1e12, 1e20, float("inf")):
        assert seconds_after(now, seconds) == LATEST_RUN_AT
    assert seconds_after(LATEST_RUN_AT, 5) == LATEST_RUN_AT
    # The day of room keeps a display-timezone conversion in range.
    assert LATEST_RUN_AT.astimezone(timezone(timedelta(hours=14))).year == 9999


@pytest.mark.parametrize("fixed_interval", [True, False])
@pytest.mark.parametrize("jitter", [0, 5])
def test_an_interval_past_year_9999_returns_the_task_to_active(
    running_main_loop: asyncio.AbstractEventLoop,
    fixed_interval: bool,
    jitter: float,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    try:
        task_id = scheduler.add_task(
            "huge-interval",
            lambda: None,
            interval=1e12,
            fixed_interval=fixed_interval,
            jitter=jitter,
            delay=0.2,
        )
        scheduler.start()

        job = scheduler.wait_for_task(task_id, timeout=5)

        assert job.status == JobStatus.COMPLETED
        task = scheduler.get_task(task_id)
        # Before the fix the OverflowError left the task RUNNING.
        assert task.status == TaskStatus.ACTIVE
        assert task.next_run_at == LATEST_RUN_AT
    finally:
        scheduler.shutdown()


@pytest.mark.parametrize("retry_attempt", [40, 2000])
def test_a_retry_backoff_past_year_9999_is_clamped(
    running_main_loop: asyncio.AbstractEventLoop,
    retry_attempt: int,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    try:
        task_id = scheduler.add_task(
            "flaky", lambda: None, interval=3600, max_retries=10_000,
            retry_backoff=10,
        )
        # 40 failures: 10 s * 2**40 is past year 9999. 2000 failures: the
        # power of two is past the float range as well.
        scheduler.persistence.update_task(task_id, retry_attempt=retry_attempt)

        will_retry = scheduler.persistence.finalize_task_after_job(
            task_id, datetime.now(timezone.utc), job_failed=True
        )

        assert will_retry is True
        task = scheduler.get_task(task_id)
        assert task.status == TaskStatus.ACTIVE
        assert task.next_run_at == LATEST_RUN_AT
        # A manual run still works, and a success resets the counter.
        scheduler.run_task_immediately(task_id)
    finally:
        scheduler.shutdown()


def test_api_delays_and_intervals_past_year_9999_are_clamped(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    # A display timezone at UTC+14, so the add_task log line converts the
    # clamped time to the latest local date there is.
    scheduler = Quiv(main_loop=running_main_loop, timezone="Pacific/Kiritimati")
    try:
        # These raised OverflowError to the caller before.
        delayed = scheduler.add_task("later", lambda: None, interval=60, delay=1e12)
        assert scheduler.get_task(delayed).next_run_at == LATEST_RUN_AT

        updated = scheduler.add_task("updated", lambda: None, interval=60)
        scheduler.update_task(updated, interval=1e12)
        assert scheduler.get_task(updated).next_run_at == LATEST_RUN_AT

        resumed = scheduler.add_task("resumed", lambda: None, interval=60)
        scheduler.pause_task(resumed)
        scheduler.resume_task(resumed, delay=10**12)
        assert scheduler.get_task(resumed).next_run_at == LATEST_RUN_AT
    finally:
        scheduler.shutdown()
