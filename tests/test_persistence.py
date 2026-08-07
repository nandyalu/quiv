from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from sqlmodel import Session, select

from quiv import Quiv
from quiv.exceptions import (
    JobNotFoundError,
    TaskNotFoundError,
    TaskNotScheduledError,
)
from quiv.models import Job, JobStatus, TaskDB


def test_queue_task_for_immediate_run_raises_for_missing_task(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    try:
        with pytest.raises(TaskNotScheduledError):
            scheduler.persistence.queue_task_for_immediate_run("missing-id")
    finally:
        scheduler.shutdown()


def test_delete_task_raises_for_missing_task(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    try:
        with pytest.raises(TaskNotFoundError):
            scheduler.persistence.delete_task("missing-id")
    finally:
        scheduler.shutdown()


def test_pause_and_resume_missing_task_raise(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    try:
        with pytest.raises(TaskNotFoundError):
            scheduler.persistence.pause_task("missing")
        with pytest.raises(TaskNotFoundError):
            scheduler.persistence.resume_task("missing")
    finally:
        scheduler.shutdown()


def test_mark_task_running_missing_task_raises(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    try:
        with pytest.raises(TaskNotFoundError):
            scheduler.persistence.mark_task_running("missing")
    finally:
        scheduler.shutdown()


def test_mark_and_finalize_missing_job_raise(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    try:
        with pytest.raises(JobNotFoundError):
            scheduler.persistence.mark_job_running("nonexistent-job-id")
        with pytest.raises(JobNotFoundError):
            scheduler.persistence.finalize_job("nonexistent-job-id", JobStatus.COMPLETED)
    finally:
        scheduler.shutdown()


def test_finalize_task_after_job_run_once_removes_task(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    try:
        task_id = scheduler.add_task(
            task_name="run-once-finalize",
            func=lambda: None,
            interval=60,
            run_once=True,
        )
        assert task_id is not None
        scheduler.persistence.finalize_task_after_job(task_id, scheduler._now_utc(), job_failed=False)
        tasks = scheduler.get_all_tasks(include_run_once=True)
        assert all(task.id != task_id for task in tasks)
    finally:
        scheduler.shutdown()


def test_cleanup_history_deletes_old_finished_jobs(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    try:
        task_id = scheduler.add_task(
            task_name="history-cleanup",
            func=lambda: None,
            interval=60,
        )
        assert task_id is not None

        old_job_id = scheduler.persistence.create_job(task_id, "history-cleanup")
        scheduler.persistence.finalize_job(old_job_id, JobStatus.COMPLETED)

        new_job_id = scheduler.persistence.create_job(task_id, "history-cleanup")
        scheduler.persistence.finalize_job(new_job_id, JobStatus.COMPLETED)

        with Session(scheduler._engine) as session:
            old_job = session.get(Job, old_job_id)
            assert old_job is not None
            old_job.ended_at = scheduler._now_utc() - timedelta(days=2)
            session.commit()

        scheduler.persistence.cleanup_history(60)
        remaining = scheduler.persistence.get_all_jobs()
        remaining_ids = {job.id for job in remaining}
        assert old_job_id not in remaining_ids
        assert new_job_id in remaining_ids
    finally:
        scheduler.shutdown()


def test_get_all_jobs_status_filter_and_task_filter(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    try:
        recurring_id = scheduler.add_task(
            task_name="jobs-filter-recurring",
            func=lambda: None,
            interval=60,
            run_once=False,
        )
        run_once_id = scheduler.add_task(
            task_name="jobs-filter-run-once",
            func=lambda: None,
            interval=60,
            run_once=True,
        )
        assert recurring_id is not None
        assert run_once_id is not None

        first_job = scheduler.persistence.create_job(recurring_id, "jobs-filter-recurring")
        scheduler.persistence.mark_job_running(first_job)
        scheduler.persistence.finalize_job(first_job, JobStatus.COMPLETED)

        second_job = scheduler.persistence.create_job(run_once_id, "jobs-filter-run-once")
        scheduler.persistence.mark_job_running(second_job)
        scheduler.persistence.finalize_job(second_job, JobStatus.FAILED)

        completed_jobs = scheduler.persistence.get_all_jobs(
            status=JobStatus.COMPLETED
        )
        failed_jobs = scheduler.persistence.get_all_jobs(
            status=JobStatus.FAILED
        )
        assert len(completed_jobs) == 1
        assert len(failed_jobs) == 1

        recurring_tasks = scheduler.get_all_tasks(include_run_once=False)
        all_tasks = scheduler.get_all_tasks(include_run_once=True)
        assert any(task.id == recurring_id for task in recurring_tasks)
        assert all(task.id != run_once_id for task in recurring_tasks)
        assert any(task.id == run_once_id for task in all_tasks)

        with Session(scheduler._engine) as session:
            fetched = session.exec(
                select(TaskDB).where(TaskDB.id == recurring_id)
            ).one_or_none()
            assert fetched is not None
    finally:
        scheduler.shutdown()


def test_pause_resume_and_due_task_filtering_success_paths(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    try:
        active_id = scheduler.add_task(
            task_name="due-active",
            func=lambda: None,
            interval=60,
            delay=0,
        )
        paused_id = scheduler.add_task(
            task_name="due-paused",
            func=lambda: None,
            interval=60,
            delay=0,
        )
        assert active_id is not None
        assert paused_id is not None

        scheduler.persistence.pause_task(paused_id)
        due_tasks = scheduler.persistence.get_due_tasks(scheduler._now_utc())
        due_ids = {task.id for task in due_tasks}
        assert active_id in due_ids
        assert paused_id not in due_ids

        scheduler.persistence.resume_task(paused_id)
        resumed_due_tasks = scheduler.persistence.get_due_tasks(
            scheduler._now_utc()
        )
        resumed_due_ids = {task.id for task in resumed_due_tasks}
        assert paused_id in resumed_due_ids
    finally:
        scheduler.shutdown()


def test_finalize_task_after_job_updates_recurring_next_run(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    try:
        task_id = scheduler.add_task(
            task_name="recurring-finalize",
            func=lambda: None,
            interval=120,
            run_once=False,
            delay=0,
        )
        assert task_id is not None
        start_time = scheduler._now_utc()
        scheduler.persistence.finalize_task_after_job(task_id, start_time, job_failed=False)
        tasks = scheduler.get_all_tasks(include_run_once=True)
        task = next(item for item in tasks if item.id == task_id)
        task_next = (
            task.next_run_at.replace(tzinfo=None)
            if task.next_run_at.tzinfo is not None
            else task.next_run_at
        )
        start_naive = start_time.replace(tzinfo=None)
        assert task_next >= start_naive
    finally:
        scheduler.shutdown()


def test_finalize_task_fixed_interval_schedules_from_start(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    from quiv.models import TaskStatus

    scheduler = Quiv(main_loop=running_main_loop)
    try:
        interval = 60
        task_id = scheduler.add_task(
            task_name="fixed-interval-schedule",
            func=lambda: None,
            interval=interval,
            run_once=False,
            fixed_interval=True,
            delay=0,
        )
        assert task_id is not None

        scheduler.persistence.mark_task_running(task_id)

        # Simulate job that started 5s ago
        start_time = scheduler._now_utc() - timedelta(seconds=5)
        scheduler.persistence.finalize_task_after_job(task_id, start_time, job_failed=False)

        task_after = next(
            t for t in scheduler.get_all_tasks(include_run_once=True)
            if t.id == task_id
        )
        assert task_after.status == TaskStatus.ACTIVE

        # next_run_at should be start_time + interval (1 period)
        next_run = task_after.next_run_at.replace(tzinfo=None)
        expected = (start_time + timedelta(seconds=interval)).replace(
            tzinfo=None
        )
        # Allow 1s tolerance for test execution time
        assert abs((next_run - expected).total_seconds()) < 1, (
            f"next_run_at {next_run} should be near {expected}"
        )
    finally:
        scheduler.shutdown()


def test_finalize_task_fixed_interval_skips_missed_intervals(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    """When a job takes longer than the interval, intermediate runs are skipped."""
    scheduler = Quiv(main_loop=running_main_loop)
    try:
        interval = 60
        task_id = scheduler.add_task(
            task_name="fixed-interval-skip",
            func=lambda: None,
            interval=interval,
            run_once=False,
            fixed_interval=True,
            delay=0,
        )

        scheduler.persistence.mark_task_running(task_id)

        # Simulate job that started 70s ago (missed one interval)
        start_time = scheduler._now_utc() - timedelta(seconds=70)
        scheduler.persistence.finalize_task_after_job(task_id, start_time, job_failed=False)

        task_after = next(
            t for t in scheduler.get_all_tasks(include_run_once=True)
            if t.id == task_id
        )
        # Should skip to 2*interval from start (120s)
        next_run = task_after.next_run_at.replace(tzinfo=None)
        expected = (start_time + timedelta(seconds=2 * interval)).replace(
            tzinfo=None
        )
        assert abs((next_run - expected).total_seconds()) < 1, (
            f"next_run_at {next_run} should be near {expected} (skipped 1 interval)"
        )

        # Simulate job that started 130s ago (missed two intervals)
        scheduler.persistence.mark_task_running(task_id)
        start_time_2 = scheduler._now_utc() - timedelta(seconds=130)
        scheduler.persistence.finalize_task_after_job(task_id, start_time_2, job_failed=False)

        task_after_2 = next(
            t for t in scheduler.get_all_tasks(include_run_once=True)
            if t.id == task_id
        )
        # Should skip to 3*interval from start (180s)
        next_run_2 = task_after_2.next_run_at.replace(tzinfo=None)
        expected_2 = (start_time_2 + timedelta(seconds=3 * interval)).replace(
            tzinfo=None
        )
        assert abs((next_run_2 - expected_2).total_seconds()) < 1, (
            f"next_run_at {next_run_2} should be near {expected_2} (skipped 2 intervals)"
        )
    finally:
        scheduler.shutdown()


def test_finalize_task_wait_between_runs_schedules_from_completion(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    from quiv.models import TaskStatus

    scheduler = Quiv(main_loop=running_main_loop)
    try:
        interval = 60
        task_id = scheduler.add_task(
            task_name="wait-between-schedule",
            func=lambda: None,
            interval=interval,
            run_once=False,
            fixed_interval=False,
            delay=0,
        )
        assert task_id is not None

        scheduler.persistence.mark_task_running(task_id)

        now_before = scheduler._now_utc()
        start_time = now_before - timedelta(seconds=5)
        scheduler.persistence.finalize_task_after_job(task_id, start_time, job_failed=False)
        now_after = scheduler._now_utc()

        task_after = next(
            t for t in scheduler.get_all_tasks(include_run_once=True)
            if t.id == task_id
        )
        assert task_after.status == TaskStatus.ACTIVE

        # next_run_at should be approximately now + interval (from completion)
        next_run = task_after.next_run_at.replace(tzinfo=None)
        expected_lower = (
            now_before.replace(tzinfo=None) + timedelta(seconds=interval)
        )
        expected_upper = (
            now_after.replace(tzinfo=None)
            + timedelta(seconds=interval)
            + timedelta(seconds=1)
        )
        assert next_run >= expected_lower, (
            f"next_run_at {next_run} is before expected lower bound"
            f" {expected_lower}"
        )
        assert next_run <= expected_upper, (
            f"next_run_at {next_run} is after expected upper bound"
            f" {expected_upper}"
        )
    finally:
        scheduler.shutdown()


def test_get_next_due_time_returns_utc_aware(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    try:
        assert scheduler.persistence.get_next_due_time() is None

        soon_id = scheduler.add_task(
            task_name="soon", func=lambda: None, interval=60, delay=5
        )
        scheduler.add_task(
            task_name="later", func=lambda: None, interval=60, delay=600
        )

        next_due = scheduler.persistence.get_next_due_time()
        assert next_due is not None
        assert next_due.tzinfo is not None
        soon_row = scheduler.persistence.get_task(soon_id)
        assert next_due == soon_row.next_run_at

        scheduler.pause_task(soon_id)
        next_due = scheduler.persistence.get_next_due_time()
        assert next_due is not None
        assert next_due != soon_row.next_run_at
    finally:
        scheduler.shutdown()


def test_get_next_due_time_none_when_all_paused(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    try:
        task_id = scheduler.add_task(
            task_name="paused-only", func=lambda: None, interval=60
        )
        scheduler.pause_task(task_id)
        assert scheduler.persistence.get_next_due_time() is None
    finally:
        scheduler.shutdown()


def test_jitter_offsets_next_run(
    running_main_loop: asyncio.AbstractEventLoop,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import quiv.persistence as persistence_module

    scheduler = Quiv(main_loop=running_main_loop)
    try:
        task_id = scheduler.add_task(
            task_name="jittered",
            func=lambda: None,
            interval=100,
            jitter=5,
            delay=0,
        )
        monkeypatch.setattr(
            persistence_module.random, "uniform", lambda a, b: 3.25
        )
        start_time = scheduler._now_utc()
        scheduler.persistence.finalize_task_after_job(
            task_id, start_time, job_failed=False
        )
        task = scheduler.get_task(task_id)
        expected = start_time + timedelta(seconds=100 + 3.25)
        assert abs((task.next_run_at - expected).total_seconds()) < 0.001
    finally:
        scheduler.shutdown()


def test_zero_jitter_next_run_exactly_on_boundary(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    try:
        task_id = scheduler.add_task(
            task_name="no-jitter",
            func=lambda: None,
            interval=100,
            delay=0,
        )
        start_time = scheduler._now_utc()
        scheduler.persistence.finalize_task_after_job(
            task_id, start_time, job_failed=False
        )
        task = scheduler.get_task(task_id)
        expected = start_time + timedelta(seconds=100)
        assert abs((task.next_run_at - expected).total_seconds()) < 0.001
    finally:
        scheduler.shutdown()


# ---------------------------------------------------------------------------
# Phase 5: rich job/task queries
# ---------------------------------------------------------------------------


def _seed_jobs_at_controlled_times(scheduler: Quiv) -> tuple[str, str, list[str]]:
    """Create two tasks and four finalized jobs at stepped timestamps.

    Returns (task_a_id, task_b_id, job_ids ordered by started_at asc).
    """
    from datetime import datetime, timezone

    from quiv.persistence import PersistenceLayer

    base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    current = {"now": base}
    fake_persistence = PersistenceLayer(
        scheduler._engine, lambda: current["now"]
    )

    task_a = scheduler.add_task(
        task_name="query-a", func=lambda: None, interval=60
    )
    task_b = scheduler.add_task(
        task_name="query-b", func=lambda: None, interval=60
    )

    job_ids = []
    for i, (task_id, status) in enumerate(
        [
            (task_a, JobStatus.COMPLETED),
            (task_a, JobStatus.FAILED),
            (task_b, JobStatus.COMPLETED),
            (task_b, JobStatus.COMPLETED),
        ]
    ):
        current["now"] = base + timedelta(minutes=i)
        job_id = fake_persistence.create_job(task_id, "seeded")
        fake_persistence.mark_job_running(job_id)
        fake_persistence.finalize_job(job_id, status)
        job_ids.append(job_id)
    return task_a, task_b, job_ids


def test_get_all_jobs_filters_by_task_and_window(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    from datetime import datetime, timezone

    scheduler = Quiv(main_loop=running_main_loop)
    try:
        task_a, task_b, job_ids = _seed_jobs_at_controlled_times(scheduler)
        base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

        by_task = scheduler.get_all_jobs(task_id=task_a)
        assert {job.id for job in by_task} == set(job_ids[:2])

        windowed = scheduler.get_all_jobs(
            since=base + timedelta(minutes=1),
            until=base + timedelta(minutes=2),
        )
        assert {job.id for job in windowed} == set(job_ids[1:3])

        combined = scheduler.get_all_jobs(
            status=JobStatus.COMPLETED, task_id=task_b
        )
        assert {job.id for job in combined} == set(job_ids[2:])
    finally:
        scheduler.shutdown()


def test_get_all_jobs_ordering_and_validation(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    from quiv.exceptions import ConfigurationError

    scheduler = Quiv(main_loop=running_main_loop)
    try:
        _, _, job_ids = _seed_jobs_at_controlled_times(scheduler)

        ascending = scheduler.get_all_jobs(
            order_by="started_at", descending=False
        )
        assert [job.id for job in ascending] == job_ids

        descending = scheduler.get_all_jobs(order_by="started_at")
        assert [job.id for job in descending] == list(reversed(job_ids))

        by_ended = scheduler.get_all_jobs(
            order_by="ended_at", descending=False
        )
        assert [job.id for job in by_ended] == job_ids

        with pytest.raises(ConfigurationError):
            scheduler.get_all_jobs(order_by="task_name")
    finally:
        scheduler.shutdown()


def test_get_all_jobs_limit_offset_slice(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    try:
        _, _, job_ids = _seed_jobs_at_controlled_times(scheduler)
        middle = scheduler.get_all_jobs(
            order_by="started_at", descending=False, limit=2, offset=1
        )
        assert [job.id for job in middle] == job_ids[1:3]
    finally:
        scheduler.shutdown()


def test_get_all_tasks_status_filter_and_pagination(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    from quiv.models import TaskStatus

    scheduler = Quiv(main_loop=running_main_loop)
    try:
        ids = [
            scheduler.add_task(
                task_name=f"page-{i}",
                func=lambda: None,
                interval=60,
                delay=i * 10,
            )
            for i in range(4)
        ]
        scheduler.pause_task(ids[3])

        paused = scheduler.get_all_tasks(status=TaskStatus.PAUSED)
        assert [task.id for task in paused] == [ids[3]]

        # Ordered by next_run_at ascending; slice the middle two.
        page = scheduler.get_all_tasks(limit=2, offset=1)
        assert [task.id for task in page] == ids[1:3]
    finally:
        scheduler.shutdown()


def test_fixed_interval_next_run_is_strictly_future_at_boundaries(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    from datetime import datetime, timezone

    from quiv.persistence import PersistenceLayer

    scheduler = Quiv(main_loop=running_main_loop)
    try:
        frozen = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        current = {"now": frozen}
        fake_persistence = PersistenceLayer(
            scheduler._engine, lambda: current["now"]
        )
        task_id = scheduler.add_task(
            task_name="boundary", func=lambda: None, interval=100
        )

        # elapsed == 0: job finished within clock resolution of its start.
        fake_persistence.finalize_task_after_job(
            task_id, frozen, job_failed=False
        )
        task = scheduler.get_task(task_id)
        assert task.next_run_at == frozen + timedelta(seconds=100)

        # elapsed == exactly one interval: also must be strictly future.
        current["now"] = frozen + timedelta(seconds=100)
        fake_persistence.finalize_task_after_job(
            task_id, frozen, job_failed=False
        )
        task = scheduler.get_task(task_id)
        assert task.next_run_at == frozen + timedelta(seconds=200)
    finally:
        scheduler.shutdown()
