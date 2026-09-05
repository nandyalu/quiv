from __future__ import annotations

import math
import random
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy import func
from sqlmodel import Session, select, col

from .exceptions import (
    ConfigurationError,
    JobNotFoundError,
    TaskNotActiveError,
    TaskNotFoundError,
)
from .models import Job, JobStatus, TaskDB, TaskStatus

_JOB_ORDER_COLUMNS = {
    "started_at": Job.started_at,
    "ended_at": Job.ended_at,
}


class PersistenceLayer:
    """Persistence operations for tasks and jobs.

    Attributes:
        _engine (Any): SQLModel engine used for sessions.
        _now_utc (Callable[[], datetime]):
            Callable that returns current UTC datetime.
    """

    def __init__(self, engine: Any, now_utc: Callable[[], datetime]):
        """Initialize the persistence layer.

        Args:
            engine (Any): SQLModel/SQLAlchemy engine instance.
            now_utc (Callable[[], datetime]):
                Function returning the current UTC datetime.
        """

        self._engine = engine
        self._now_utc = now_utc
        # Serializes read-modify-write transactions; reads run lock-free
        # under SQLite WAL (concurrent readers alongside one writer).
        self._write_lock = threading.Lock()

    def create_task(
        self,
        task_name: str,
        interval: float | None,
        run_once: bool,
        fixed_interval: bool,
        next_run_at: datetime,
        args_pickled: bytes,
        kwargs_pickled: bytes,
        timeout: float | None = None,
        max_retries: int = 0,
        retry_backoff: float = 30.0,
        jitter: float = 0.0,
    ) -> str:
        """Insert a new scheduled task.

        Args:
            task_name (str): Task display name.
            interval (float | None): Seconds between task runs;
                ``None`` for a run-once task, which never repeats.
            run_once (bool): Whether task should be single-run.
            fixed_interval (bool): Whether to schedule from job start time.
            next_run_at (datetime): Next UTC run timestamp.
            args_pickled (bytes): Pickle-encoded positional args.
            kwargs_pickled (bytes): Pickle-encoded keyword args.
            timeout (float, Optional=None): Cooperative per-job timeout.
            max_retries (int, Optional=0): Maximum consecutive retries.
            retry_backoff (float, Optional=30.0): Base backoff delay.
            jitter (float, Optional=0.0): Random next-run offset bound.
        Returns:
            str: Task id string (UUID).
        """

        with self._write_lock, Session(self._engine) as session:
            task = TaskDB(
                task_name=task_name,
                interval_seconds=interval,
                next_run_at=next_run_at,
                run_once=run_once,
                fixed_interval=fixed_interval,
                args=args_pickled,
                kwargs=kwargs_pickled,
                timeout_seconds=timeout,
                max_retries=max_retries,
                retry_backoff_seconds=retry_backoff,
                jitter_seconds=jitter,
            )
            session.add(task)
            session.commit()
            return task.id

    def delete_task(self, task_id: str) -> None:
        """Delete a task by id.

        Args:
            task_id (str): Task id to delete.

        Raises:
            TaskNotFoundError: If no task with that id exists.
        """

        with self._write_lock, Session(self._engine) as session:
            task = session.get(TaskDB, task_id)
            if task is None:
                raise TaskNotFoundError(f"Task '{task_id}' was not found")
            session.delete(task)
            session.commit()

    def get_all_tasks(
        self,
        include_run_once: bool = False,
        status: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[TaskDB]:
        """Fetch persisted tasks, ordered by ``next_run_at`` ascending.

        Args:
            include_run_once (bool, Optional=False):
                Include single-run tasks when ``True``.
            status (str, Optional=None): Optional task status filter.
            limit (int, Optional=None): Maximum rows to return.
            offset (int, Optional=0): Rows to skip.

        Returns:
            list[TaskDB]: A list of task records.
        """

        statement = select(TaskDB)
        if not include_run_once:
            statement = statement.where(col(TaskDB.run_once).is_(False))
        if status:
            statement = statement.where(TaskDB.status == status)
        statement = statement.order_by(col(TaskDB.next_run_at).asc())
        if limit is not None:
            statement = statement.limit(limit)
        if offset > 0:
            statement = statement.offset(offset)
        with Session(self._engine) as session:
            tasks = list(session.exec(statement).all())
            return tasks

    def get_task(self, task_id: str) -> TaskDB:
        """Fetch a single task by ID.

        Args:
            task_id (str): Task UUID to look up.

        Returns:
            TaskDB: The task record.

        Raises:
            TaskNotFoundError: If no task with that ID exists.
        """

        with Session(self._engine) as session:
            task = session.get(TaskDB, task_id)
            if task is None:
                raise TaskNotFoundError(f"Task '{task_id}' was not found")
            return task

    def get_job(self, job_id: str) -> Job:
        """Fetch a single job by ID.

        Args:
            job_id (str): Job identifier (UUID string).

        Returns:
            Job: The job record.

        Raises:
            JobNotFoundError: If no job with that ID exists.
        """

        with Session(self._engine) as session:
            job = session.get(Job, job_id)
            if job is None:
                raise JobNotFoundError(f"Job '{job_id}' was not found")
            return job

    def get_all_jobs(
        self,
        status: str | None = None,
        task_id: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        order_by: str = "started_at",
        descending: bool = True,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[Job]:
        """Fetch job records with optional filters and pagination.

        Args:
            status (str, Optional=None): Optional job status filter.
            task_id (str, Optional=None): Only jobs of this task.
            since (datetime, Optional=None): Only jobs with
                ``started_at >= since`` (aware UTC).
            until (datetime, Optional=None): Only jobs with
                ``started_at <= until`` (aware UTC).
            order_by (str, Optional="started_at"): Sort column —
                ``"started_at"`` or ``"ended_at"``.
            descending (bool, Optional=True): Sort direction.
            limit (int, Optional=None): Maximum rows to return.
            offset (int, Optional=0): Rows to skip.

        Returns:
            list[Job]: A list of job records.

        Raises:
            ConfigurationError: If ``order_by`` is not a supported column.
        """

        if order_by not in _JOB_ORDER_COLUMNS:
            valid = ", ".join(sorted(_JOB_ORDER_COLUMNS))
            raise ConfigurationError(
                f"order_by must be one of: {valid} (got '{order_by}')"
            )
        order_column = col(_JOB_ORDER_COLUMNS[order_by])
        with Session(self._engine) as session:
            statement = select(Job)
            if status:
                statement = statement.where(Job.status == status)
            if task_id is not None:
                statement = statement.where(Job.task_id == task_id)
            if since is not None:
                statement = statement.where(Job.started_at >= since)
            if until is not None:
                statement = statement.where(Job.started_at <= until)
            statement = statement.order_by(
                order_column.desc() if descending else order_column.asc()
            )
            if limit is not None:
                statement = statement.limit(limit)
            if offset > 0:
                statement = statement.offset(offset)
            return list(session.exec(statement).all())

    def update_task(self, task_id: str, **column_updates: Any) -> None:
        """Apply concrete column updates to a task row.

        When ``interval_seconds`` is among the updates, ``next_run_at``
        is rescheduled to ``now + interval``. Callers resolve any
        not-passed sentinels before this method — it only ever receives
        concrete column values.

        Args:
            task_id (str): Task identifier.
            **column_updates (Any): ``TaskDB`` column names to new values.

        Raises:
            TaskNotFoundError: If no task with that id exists.
        """

        with self._write_lock, Session(self._engine) as session:
            task = session.get(TaskDB, task_id)
            if task is None:
                raise TaskNotFoundError(f"Task '{task_id}' was not found")
            for column, value in column_updates.items():
                setattr(task, column, value)
            if "interval_seconds" in column_updates:
                task.next_run_at = self._now_utc() + timedelta(
                    seconds=column_updates["interval_seconds"]
                )
            session.commit()

    def count_tasks_by_status(self) -> dict[str, int]:
        """Count task rows grouped by status.

        Returns:
            dict[str, int]: Task counts keyed by status value.
        """

        with Session(self._engine) as session:
            statement = select(TaskDB.status, func.count()).group_by(
                TaskDB.status
            )
            return {
                status: count
                for status, count in session.exec(statement).all()
            }

    def count_jobs(self) -> int:
        """Count all retained job rows.

        Returns:
            int: Number of job rows.
        """

        with Session(self._engine) as session:
            statement = select(func.count()).select_from(Job)
            return int(session.exec(statement).one())

    def queue_task_for_immediate_run(self, task_id: str) -> int:
        """Mark a scheduled task for immediate execution.

        Args:
            task_id (str): Task id to enqueue.

        Returns:
            int: Number of task rows updated.

        Raises:
            TaskNotFoundError: If no task with that id exists.
            TaskNotActiveError: If the task is not in ACTIVE status
                (e.g. currently running or paused).
        """

        with self._write_lock, Session(self._engine) as session:
            task = session.get(TaskDB, task_id)
            if task is None:
                raise TaskNotFoundError(
                    f"Task '{task_id}' was not found. Add it with"
                    " add_task before running immediately."
                )
            if task.status != TaskStatus.ACTIVE:
                hint = (
                    " Use resume_task() to resume it first."
                    if task.status == TaskStatus.PAUSED
                    else ""
                )
                raise TaskNotActiveError(
                    f"Task '{task_id}' is {task.status}; only active tasks"
                    f" can be queued for immediate run.{hint}"
                )
            now = self._now_utc()
            task.next_run_at = now
            session.commit()
            return 1

    def pause_task(self, task_id: str) -> None:
        """Pause a task so it will not be dispatched.

        Args:
            task_id (str): Task identifier.

        Raises:
            TaskNotFoundError: If task does not exist.
        """

        with self._write_lock, Session(self._engine) as session:
            task = session.get(TaskDB, task_id)
            if task is None:
                raise TaskNotFoundError(f"Task '{task_id}' was not found")
            task.status = TaskStatus.PAUSED
            session.commit()

    def resume_task(self, task_id: str, delay: int = 0) -> None:
        """Resume a paused task and schedule it to run immediately.

        Args:
            task_id (str): Task identifier.
            delay (int, Optional=0): Seconds to delay before next run.

        Raises:
            TaskNotFoundError: If task does not exist.
        """

        with self._write_lock, Session(self._engine) as session:
            task = session.get(TaskDB, task_id)
            if task is None:
                raise TaskNotFoundError(f"Task '{task_id}' was not found")
            task.status = TaskStatus.ACTIVE
            task.next_run_at = self._now_utc() + timedelta(seconds=delay)
            session.commit()

    def cleanup_history(self, history_limit_seconds: int) -> None:
        """Delete old finished jobs based on retention configuration.

        Args:
            history_limit_seconds (int): Retention window in seconds.
        """

        cutoff = self._now_utc() - timedelta(seconds=history_limit_seconds)
        terminal = (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED)
        with self._write_lock, Session(self._engine) as session:
            statement = (
                select(Job)
                .where(col(Job.ended_at).is_not(None))
                .where(col(Job.ended_at) < cutoff)
                .where(col(Job.status).in_(terminal))
            )
            for job in session.exec(statement).all():
                session.delete(job)
            session.commit()

    def get_due_tasks(self, now: datetime) -> list[TaskDB]:
        """Return tasks that are due for execution.

        Args:
            now (datetime): Current UTC timestamp used for due comparison.

        Returns:
            list[TaskDB]: A list of active due tasks.
        """

        with Session(self._engine) as session:
            statement = select(TaskDB).where(TaskDB.next_run_at <= now)
            statement = statement.where(TaskDB.status == TaskStatus.ACTIVE)
            return list(session.exec(statement).all())

    def get_next_due_time(self) -> datetime | None:
        """Return the earliest ``next_run_at`` among ACTIVE tasks.

        Returns:
            datetime | None: Earliest UTC-aware due time, or ``None`` when
                no active task exists.
        """

        with Session(self._engine) as session:
            statement = (
                select(TaskDB.next_run_at)
                .where(TaskDB.status == TaskStatus.ACTIVE)
                .order_by(col(TaskDB.next_run_at).asc())
                .limit(1)
            )
            value = session.exec(statement).first()
            # Selecting a bare column bypasses the model reconstructor, so
            # SQLite returns a naive datetime; normalize like model loads do.
            if value is not None and value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value

    def create_job(
        self, task_id: str, task_name: str, attempt: int = 1
    ) -> str:
        """Create a scheduled job record for a task.

        Args:
            task_id (str): Source task identifier.
            task_name (str): Name of the task.
            attempt (int, Optional=1): Attempt number; 1 = first try,
                2 = first retry, and so on.

        Returns:
            str: Newly created job id (UUID string).
        """

        with self._write_lock, Session(self._engine) as session:
            job = Job(
                task_id=task_id,
                task_name=task_name,
                status=JobStatus.SCHEDULED,
                attempt=attempt,
            )
            session.add(job)
            session.commit()
            return job.id

    def mark_task_running(self, task_id: str) -> None:
        """Mark a task as running when dispatched to the executor.

        Args:
            task_id (str): Task identifier.

        Raises:
            TaskNotFoundError: If task does not exist.
        """

        with self._write_lock, Session(self._engine) as session:
            existing = session.get(TaskDB, task_id)
            if existing is None:
                raise TaskNotFoundError(f"Task '{task_id}' was not found")
            existing.status = TaskStatus.RUNNING
            session.commit()

    def finalize_task_after_job(
        self, task_id: str, job_started_at: datetime, job_failed: bool
    ) -> bool:
        """Update task state after job completion.

        On failure with retries remaining, schedules the next run at
        ``now + retry_backoff * 2**(failures_so_far - 1)`` (exponential
        backoff) and increments the consecutive-failure counter. A
        successful run — or exhausting retries — resets the counter;
        exhausted recurring tasks fall back to their normal interval
        schedule.

        Otherwise, for run-once tasks, deletes the task row. For
        recurring tasks, sets status back to active and schedules the
        next run.

        When ``fixed_interval`` is ``True``, the next run is aligned to
        the next interval boundary after now, measured from
        ``job_started_at``. If the job took longer than one interval,
        intermediate intervals are skipped.

        When ``fixed_interval`` is ``False``, the next run is simply
        ``now + interval``.

        When ``jitter_seconds > 0``, adds ``uniform(0, jitter_seconds)``
        to the recurring next-run time (not to retry backoff).

        Args:
            task_id (str): Task identifier.
            job_started_at (datetime): UTC time when the job started.
            job_failed (bool): Whether the job ended in FAILED status.

        Returns:
            bool: ``True`` when a retry was scheduled.
        """

        with self._write_lock, Session(self._engine) as session:
            existing = session.get(TaskDB, task_id)
            if existing is None:
                # run-once task already deleted, or task was removed
                return False

            now = self._now_utc()
            if job_failed and existing.retry_attempt < existing.max_retries:
                existing.retry_attempt += 1
                backoff = existing.retry_backoff_seconds * (
                    2 ** (existing.retry_attempt - 1)
                )
                existing.status = TaskStatus.ACTIVE
                existing.next_run_at = now + timedelta(seconds=backoff)
                session.commit()
                return True

            existing.retry_attempt = 0
            if existing.run_once:
                session.delete(existing)
                session.commit()
                return False

            existing.status = TaskStatus.ACTIVE
            interval = existing.interval_seconds
            if interval is None:  # pragma: no cover - defensive
                # add_task stores None only for run-once tasks, which
                # return above, and update_task cannot clear it.
                raise ConfigurationError(
                    f"Recurring task '{task_id}' has no interval."
                )
            if existing.fixed_interval:
                elapsed = (now - job_started_at).total_seconds()
                # floor+1, not ceil: when elapsed lands exactly on a
                # boundary (including 0 for sub-clock-resolution jobs),
                # ceil yields next_run_at == now and the task re-dispatches
                # immediately. The next run must be strictly in the future.
                periods = math.floor(elapsed / interval) + 1
                existing.next_run_at = job_started_at + timedelta(
                    seconds=periods * interval
                )
            else:
                existing.next_run_at = now + timedelta(seconds=interval)
            if existing.jitter_seconds > 0:
                existing.next_run_at += timedelta(
                    seconds=random.uniform(0, existing.jitter_seconds)
                )
            session.commit()
            return False

    def mark_job_running(self, job_id: str) -> None:
        """Transition a job to running state and set start time.

        Args:
            job_id (str): Job identifier (UUID string).

        Raises:
            JobNotFoundError: If job does not exist.
        """

        with self._write_lock, Session(self._engine) as session:
            job = session.get(Job, job_id)
            if job is None:
                raise JobNotFoundError(f"Job '{job_id}' should exist")
            job.status = JobStatus.RUNNING
            job.started_at = self._now_utc()
            session.commit()

    def finalize_job(
        self,
        job_id: str,
        status: str,
        duration_seconds: float | None = None,
        error_message: str | None = None,
    ) -> None:
        """Set final status and end timestamp for a job.

        Args:
            job_id (str): Job identifier (UUID string).
            status (str): Terminal job status.
            duration_seconds (float, Optional=None): Job duration in seconds.
            error_message (str, Optional=None): Error message if job failed.

        Raises:
            JobNotFoundError: If job does not exist.
        """

        with self._write_lock, Session(self._engine) as session:
            job = session.get(Job, job_id)
            if job is None:
                raise JobNotFoundError(f"Job '{job_id}' should exist")
            job.status = status
            job.ended_at = self._now_utc()
            job.duration_seconds = duration_seconds
            job.error_message = error_message
            session.commit()
