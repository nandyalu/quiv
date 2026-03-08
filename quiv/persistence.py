from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import Callable

from sqlmodel import Session, select

from .exceptions import (
    JobNotFoundError,
    TaskNotFoundError,
    TaskNotScheduledError,
)
from .models import Job, JobStatus, Task, TaskStatus


class PersistenceLayer:
    """Persistence operations for tasks and jobs.

    Attributes:
        _engine (Any): SQLModel engine used for sessions.
        _now_utc (Callable[[], datetime]):
            Callable that returns current UTC datetime.
    """

    def __init__(self, engine, now_utc: Callable[[], datetime]):
        """Initialize the persistence layer.

        Args:
            engine (Any): SQLModel/SQLAlchemy engine instance.
            now_utc (Callable[[], datetime]):
                Function returning the current UTC datetime.
        """

        self._engine = engine
        self._now_utc = now_utc
        self._lock = threading.Lock()

    def upsert_task(
        self,
        task_name: str,
        interval: float,
        run_once: bool,
        next_run_at: datetime,
        args_json: str,
        kwargs_json: str,
    ) -> str | None:
        """Insert or update a scheduled task.

        Args:
            task_name (str): Unique task name.
            interval (float): Seconds between task runs.
            run_once (bool): Whether task should be single-run.
            next_run_at (datetime): Next UTC run timestamp.
            args_json (str): JSON-encoded positional args.
            kwargs_json (str): JSON-encoded keyword args.
        Returns:
            str: Task id string (UUID) if created/updated successfully,
                else ``None``.
        """

        with self._lock, Session(self._engine) as session:
            task = Task(
                task_name=task_name,
                interval_seconds=interval,
                next_run_at=next_run_at,
                run_once=run_once,
                args=args_json,
                kwargs=kwargs_json,
            )
            session.merge(task)
            session.commit()
            return task.id
        return None  # pragma: no cover

    def get_all_tasks(self, include_run_once: bool = False) -> list[Task]:
        """Fetch all persisted tasks.

        Args:
            include_run_once (bool, Optional=False):
                Include single-run tasks when ``True``.

        Returns:
            list[Task]: A list of task records.
        """

        statement = select(Task)
        if not include_run_once:
            statement = statement.where(Task.run_once == False)
        with self._lock, Session(self._engine) as session:
            tasks = list(session.exec(statement).all())
            return tasks

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

    def queue_task_for_immediate_run(self, task_name: str) -> int:
        """Mark scheduled task rows for immediate execution.

        Args:
            task_name (str): Task name to enqueue.

        Returns:
            int: Number of task rows updated.

        Raises:
            TaskNotScheduledError: If no scheduled task exists for the name.
        """

        with self._lock, Session(self._engine) as session:
            tasks = session.exec(
                select(Task).where(Task.task_name == task_name)
            ).all()
            if not tasks:
                raise TaskNotScheduledError(
                    f"Task '{task_name}' is not scheduled. Add it with"
                    " add_task before running immediately."
                )
            now = self._now_utc()
            for task in tasks:
                task.status = TaskStatus.ACTIVE
                task.next_run_at = now
            session.commit()
            return len(tasks)

    def pause_task(self, task_id: str) -> None:
        """Pause a task so it will not be dispatched.

        Args:
            task_id (str): Task identifier.

        Raises:
            TaskNotFoundError: If task does not exist.
        """

        with self._lock, Session(self._engine) as session:
            task = session.get(Task, task_id)
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
            task = session.get(Task, task_id)
            if task is None:
                raise TaskNotFoundError(f"Task '{task_id}' was not found")
            task.status = TaskStatus.ACTIVE
            task.next_run_at = self._now_utc() + timedelta(seconds=delay)
            session.commit()

    def cleanup_history(
        self, history_limit_seconds: int, all_jobs: list[Job]
    ) -> None:
        """Delete old finished jobs based on retention configuration.

        Args:
            history_limit_seconds (int): Retention window in seconds.
            all_jobs (list[Job]): Candidate jobs to evaluate for deletion.
        """

        cutoff = self._now_utc().timestamp() - history_limit_seconds
        with self._lock, Session(self._engine) as session:
            for job in all_jobs:
                if job.ended_at and job.ended_at.timestamp() < cutoff:
                    existing = session.get(Job, job.id)
                    if existing is not None:
                        session.delete(existing)
            session.commit()

    def get_due_tasks(self, now: datetime) -> list[Task]:
        """Return tasks that are due for execution.

        Args:
            now (datetime): Current UTC timestamp used for due comparison.

        Returns:
            list[Task]: A list of active due tasks.
        """

        with self._lock, Session(self._engine) as session:
            statement = select(Task).where(Task.next_run_at <= now)
            statement = statement.where(Task.status == TaskStatus.ACTIVE)
            return list(session.exec(statement).all())

    def create_job(self, task_id: str) -> int:
        """Create a scheduled job record for a task.

        Args:
            task_id (str): Source task identifier.

        Returns:
            int: Newly created job id.

        Raises:
            JobNotFoundError: If a job id could not be assigned.
        """

        with self._lock, Session(self._engine) as session:
            job = Job(task_id=task_id, status=JobStatus.SCHEDULED)
            session.add(job)
            session.commit()
            if job.id is None:
                raise JobNotFoundError(
                    "Job ID should be set after commit"
                )  # pragma: no cover
            return job.id

    def finalize_task_after_schedule(
        self, task_id: str, now: datetime
    ) -> None:
        """Update task scheduling state after dispatch.

        Args:
            task_id (str): Task identifier.
            now (datetime): Current UTC timestamp.

        Raises:
            TaskNotFoundError: If task does not exist.
        """

        with self._lock, Session(self._engine) as session:
            existing = session.get(Task, task_id)
            if existing is None:
                raise TaskNotFoundError(f"Task '{task_id}' was not found")
            if existing.run_once:
                existing.status = TaskStatus.PAUSED
                existing.next_run_at = datetime.max.replace(
                    tzinfo=timezone.utc
                )
                session.delete(existing)
            else:
                existing.next_run_at = now + timedelta(
                    seconds=existing.interval_seconds
                )
            session.commit()

    def mark_job_running(self, job_id: int) -> None:
        """Transition a job to running state and set start time.

        Args:
            job_id (int): Job identifier.

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

    def finalize_job(self, job_id: int, status: str) -> None:
        """Set final status and end timestamp for a job.

        Args:
            job_id (int): Job identifier.
            status (str): Terminal job status.

        Raises:
            JobNotFoundError: If job does not exist.
        """

        with self._lock, Session(self._engine) as session:
            job = session.get(Job, job_id)
            if job is None:
                raise JobNotFoundError(f"Job '{job_id}' should exist")
            job.status = status
            job.ended_at = self._now_utc()
            session.commit()
