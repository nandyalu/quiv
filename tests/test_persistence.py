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
        scheduler.persistence.finalize_task_after_job(task_id, scheduler._now_utc())
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
        scheduler.persistence.finalize_task_after_job(task_id, start_time)
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
        scheduler.persistence.finalize_task_after_job(task_id, start_time)

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
        scheduler.persistence.finalize_task_after_job(task_id, start_time)

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
        scheduler.persistence.finalize_task_after_job(task_id, start_time_2)

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
        scheduler.persistence.finalize_task_after_job(task_id, start_time)
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
