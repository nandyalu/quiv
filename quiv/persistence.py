from __future__ import annotations

import threading
from datetime import datetime, timedelta
from typing import Any, Callable

from sqlmodel import Session, select, col

from .exceptions import (
    JobNotFoundError,
    TaskNotFoundError,
    TaskNotScheduledError,
)
from .models import Job, JobStatus, TaskDB, TaskStatus


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
        self._lock = threading.Lock()

    def create_task(
        self,
        task_name: str,
        interval: float,
        run_once: bool,
        next_run_at: datetime,
        args_pickled: bytes,
        kwargs_pickled: bytes,
    ) -> str:
        """Insert a new scheduled task.

        Args:
            task_name (str): Unique task name.
            interval (float): Seconds between task runs.
            run_once (bool): Whether task should be single-run.
            next_run_at (datetime): Next UTC run timestamp.
            args_pickled (bytes): Pickle-encoded positional args.
            kwargs_pickled (bytes): Pickle-encoded keyword args.
        Returns:
            str: Task id string (UUID).
        """

        with self._lock, Session(self._engine) as session:
            task = TaskDB(
                task_name=task_name,
                interval_seconds=interval,
                next_run_at=next_run_at,
                run_once=run_once,
                args=args_pickled,
                kwargs=kwargs_pickled,
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

        with self._lock, Session(self._engine) as session:
            task = session.get(TaskDB, task_id)
            if task is None:
                raise TaskNotFoundError(f"Task '{task_id}' was not found")
            session.delete(task)
            session.commit()

    def get_all_tasks(self, include_run_once: bool = False) -> list[TaskDB]:
        """Fetch all persisted tasks.

        Args:
            include_run_once (bool, Optional=False):
                Include single-run tasks when ``True``.

        Returns:
            list[TaskDB]: A list of task records.
        """

        statement = select(TaskDB)
        if not include_run_once:
            statement = statement.where(TaskDB.run_once == False)
        with self._lock, Session(self._engine) as session:
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

        with self._lock, Session(self._engine) as session:
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

        with self._lock, Session(self._engine) as session:
            job = session.get(Job, job_id)
            if job is None:
                raise JobNotFoundError(f"Job '{job_id}' was not found")
            return job

    def get_all_jobs(self, status: str | None = None) -> list[Job]:
        """Fetch job records, optionally filtered by status.

        Args:
            status (str, Optional=None): Optional job status filter.

        Returns:
            list[Job]: A list of job records.
        """

        with self._lock, Session(self._engine) as session:
            statement = select(Job)
            if status:
                statement = statement.where(Job.status == status)
            return list(session.exec(statement).all())

    def queue_task_for_immediate_run(self, task_id: str) -> int:
        """Mark a scheduled task for immediate execution.

        Args:
            task_id (str): Task id to enqueue.

        Returns:
            int: Number of task rows updated.

        Raises:
            TaskNotScheduledError: If no scheduled task exists for the id.
        """

        with self._lock, Session(self._engine) as session:
            task = session.get(TaskDB, task_id)
            if task is None:
                raise TaskNotScheduledError(
                    f"Task '{task_id}' is not scheduled. Add it with"
                    " add_task before running immediately."
                )
            now = self._now_utc()
            task.status = TaskStatus.ACTIVE
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

        with self._lock, Session(self._engine) as session:
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

        with self._lock, Session(self._engine) as session:
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
        with self._lock, Session(self._engine) as session:
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

        with self._lock, Session(self._engine) as session:
            statement = select(TaskDB).where(TaskDB.next_run_at <= now)
            statement = statement.where(TaskDB.status == TaskStatus.ACTIVE)
            return list(session.exec(statement).all())

    def create_job(self, task_id: str, task_name: str) -> str:
        """Create a scheduled job record for a task.

        Args:
            task_id (str): Source task identifier.
            task_name (str): Name of the task.

        Returns:
            str: Newly created job id (UUID string).
        """

        with self._lock, Session(self._engine) as session:
            job = Job(task_id=task_id, task_name=task_name, status=JobStatus.SCHEDULED)
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

        with self._lock, Session(self._engine) as session:
            existing = session.get(TaskDB, task_id)
            if existing is None:
                raise TaskNotFoundError(f"Task '{task_id}' was not found")
            existing.status = TaskStatus.RUNNING
            session.commit()

    def finalize_task_after_job(self, task_id: str) -> None:
        """Update task state after job completion.

        For run-once tasks, deletes the task row. For recurring tasks,
        sets status back to active and schedules the next run.

        Args:
            task_id (str): Task identifier.
        """

        with self._lock, Session(self._engine) as session:
            existing = session.get(TaskDB, task_id)
            if existing is None:
                return  # run-once task already deleted, or task was removed
            if existing.run_once:
                session.delete(existing)
            else:
                existing.status = TaskStatus.ACTIVE
                existing.next_run_at = self._now_utc() + timedelta(
                    seconds=existing.interval_seconds
                )
            session.commit()

    def mark_job_running(self, job_id: str) -> None:
        """Transition a job to running state and set start time.

        Args:
            job_id (str): Job identifier (UUID string).

        Raises:
            JobNotFoundError: If job does not exist.
        """

        with self._lock, Session(self._engine) as session:
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

        with self._lock, Session(self._engine) as session:
            job = session.get(Job, job_id)
            if job is None:
                raise JobNotFoundError(f"Job '{job_id}' should exist")
            job.status = status
            job.ended_at = self._now_utc()
            job.duration_seconds = duration_seconds
            job.error_message = error_message
            session.commit()
