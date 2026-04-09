from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import Enum
import pickle
import uuid
import logging
from typing import Any

from pydantic import BaseModel, field_serializer, model_validator
from sqlalchemy.orm import registry
from sqlmodel import Field, SQLModel

logger = logging.getLogger(__name__)

quiv_registry = registry()
"""Data models registry for Quiv scheduler tasks and jobs. \n
Keeps Quiv's internal SQLAlchemy metadata separate from user models"""


class QuivModelBase(SQLModel, registry=quiv_registry):
    """Base SQLModel class bound to Quiv's private SQLAlchemy registry.

    Attributes:
        metadata (Any): Registry metadata used for Quiv model table creation.
    """

    # metadata = quiv_registry.metadata

    @classmethod
    def set_timezone_to_utc(cls, value: datetime | None) -> datetime | None:
        """Normalize datetime values to timezone-aware UTC.

        If a datetime is naive (common with SQLite), it is treated as UTC.
        If it is timezone-aware, it is converted to UTC.
        """
        logger.debug(f"Normalizing datetime value '{value}' to UTC.")
        if value is None:
            return value
        if isinstance(value, datetime) and value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


def next_run_time() -> datetime:
    """Return a default next-run timestamp in UTC.

    Returns:
        datetime: A UTC datetime one second in the future.
    """

    return datetime.now(timezone.utc) + timedelta(seconds=1)


def get_current_time() -> datetime:
    """Return the current UTC datetime.

    Returns:
        datetime: Current timestamp in UTC.
    """

    return datetime.now(timezone.utc)


def id_generator() -> str:
    """Generate a unique task identifier.

    Returns:
        str: A UUID4 string.
    """

    return str(uuid.uuid4())


class Event(str, Enum):
    """Scheduler event types emitted during task and job lifecycle.

    Attributes:
        TASK_ADDED (str): Fired after a task is registered via ``add_task()``.
        TASK_REMOVED (str): Fired after a task is removed via ``remove_task()``.
        TASK_PAUSED (str): Fired after a task is paused via ``pause_task()``.
        TASK_RESUMED (str): Fired after a task is resumed via ``resume_task()``.
        JOB_STARTED (str): Fired when a job begins execution.
        JOB_COMPLETED (str): Fired when a job finishes successfully.
        JOB_FAILED (str): Fired when a job ends with an exception.
        JOB_CANCELLED (str): Fired when a job is cancelled via stop event.
    """

    TASK_ADDED = "task_added"
    TASK_REMOVED = "task_removed"
    TASK_PAUSED = "task_paused"
    TASK_RESUMED = "task_resumed"
    JOB_STARTED = "job_started"
    JOB_COMPLETED = "job_completed"
    JOB_FAILED = "job_failed"
    JOB_CANCELLED = "job_cancelled"


class TaskStatus(str, Enum):
    """Task status constants.

    Attributes:
        ACTIVE (str): Task is eligible for scheduling.
        RUNNING (str): Task is currently executing.
        PAUSED (str): Task is temporarily disabled.
    """

    ACTIVE = "active"
    RUNNING = "running"
    PAUSED = "paused"


class JobStatus(str, Enum):
    """Job lifecycle status constants.

    Attributes:
        SCHEDULED (str): Job is queued for execution.
        RUNNING (str): Job is currently executing.
        COMPLETED (str): Job finished successfully.
        CANCELLED (str): Job stopped due to cancellation signal.
        FAILED (str): Job ended with an exception.
    """

    SCHEDULED = "scheduled"
    RUNNING = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


class TaskDB(QuivModelBase, table=True):
    """Internal database model for scheduled tasks.

    This is an internal model used for persistence. Use :class:`Task` for
    public API interactions.

    Attributes:
        __tablename__ (str): Database table name for tasks.
        id (str, UUID): UUID task identifier string.
        task_name (str):
            User-facing display name for the task.
        args (bytes): Pickle-encoded positional arguments.
        kwargs (bytes): Pickle-encoded keyword arguments.
        interval_seconds (float): Interval between consecutive task runs.
        run_once (bool): Whether task should execute only once.
        status (str): Task status string.
        next_run_at (datetime): Next scheduled UTC run timestamp.
    """

    __tablename__: str = "quiv_task"  # type: ignore

    id: str = Field(default_factory=id_generator, primary_key=True)
    task_name: str
    args: bytes = Field(default_factory=lambda: pickle.dumps(()))
    kwargs: bytes = Field(default_factory=lambda: pickle.dumps({}))
    interval_seconds: float
    run_once: bool = False
    status: str = TaskStatus.ACTIVE
    next_run_at: datetime = Field(default_factory=next_run_time)

    @field_serializer("args", "kwargs")
    @classmethod
    def _unpickle_for_serialization(cls, value: bytes) -> Any:
        """Unpickle bytes to Python objects for model_dump() and JSON serialization. \n
        Falls back to a placeholder string if unpickling fails (e.g., corrupt data).
        """
        try:
            return pickle.loads(value)
        except Exception:  # pragma: no cover
            return f"<unserializable: {len(value)} bytes>"  # pragma: no cover

    # NOTE: This validator is effectively dead code for SQLModel table classes.
    # SQLAlchemy hydrates objects directly, bypassing Pydantic validators.
    # Datetime normalization is handled by the public Task model's validator instead.
    @model_validator(mode="before")
    def force_utc_on_load(self) -> TaskDB:  # pragma: no cover
        self.next_run_at = self.set_timezone_to_utc(self.next_run_at)  # type: ignore
        return self


class Task(BaseModel):
    """Scheduled task model returned by public API methods.

    The public API methods ``get_task()`` and ``get_all_tasks()`` return
    ``Task`` objects directly.

    Example::

        task_id = scheduler.add_task("my-task", handler, interval=60)
        task = scheduler.get_task(task_id)  # Returns Task
        tasks = scheduler.get_all_tasks()   # Returns list[Task]

    Attributes:
        id (str): UUID task identifier string.
        task_name (str): User-facing task key.
        args (tuple[Any, ...]): Positional arguments.
        kwargs (dict[str, Any]): Keyword arguments.
        interval_seconds (float): Interval between consecutive task runs.
        run_once (bool): Whether task executes only once.
        status (str): Task status string.
        next_run_at (datetime): Next scheduled UTC run timestamp.
    """

    model_config = {"from_attributes": True}

    id: str
    task_name: str
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    interval_seconds: float
    run_once: bool
    status: str
    next_run_at: datetime

    @model_validator(mode="before")
    @classmethod
    def _convert_from_task_db(cls, data: Any) -> Any:
        """Convert TaskDB object to dict, unpickling args/kwargs."""
        # If it's already a dict, check if args/kwargs need unpickling
        if isinstance(data, dict):
            for field, default in (("args", ()), ("kwargs", {})):
                if field in data and isinstance(data[field], bytes):
                    try:
                        data[field] = pickle.loads(data[field])
                    except Exception:
                        logger.warning(
                            f"Failed to unpickle {field}, using empty default"
                        )
                        data[field] = default
            # Ensure next_run_at is UTC-aware
            if "next_run_at" in data:
                data["next_run_at"] = QuivModelBase.set_timezone_to_utc(
                    data["next_run_at"]
                )
            return data

        # If it's a TaskDB object, extract and unpickle
        if hasattr(data, "args") and hasattr(data, "kwargs"):
            try:
                args = pickle.loads(data.args)
            except Exception:
                logger.warning("Failed to unpickle args, using empty tuple")
                args = ()
            try:
                kwargs = pickle.loads(data.kwargs)
            except Exception:
                logger.warning("Failed to unpickle kwargs, using empty dict")
                kwargs = {}

            return {
                "id": data.id,
                "task_name": data.task_name,
                "args": args,
                "kwargs": kwargs,
                "interval_seconds": data.interval_seconds,
                "run_once": data.run_once,
                "status": data.status,
                "next_run_at": QuivModelBase.set_timezone_to_utc(
                    data.next_run_at
                ),
            }

        return data


class Job(QuivModelBase, table=True):
    """Execution record for a single task run.

    Attributes:
        __tablename__ (str): Database table name for jobs.
        id (str, UUID): UUID job identifier string.
        task_id (str): Foreign key to the source task.
        task_name (str): Name of the task that spawned this job.
        status (str): Job status string.
        started_at (datetime): UTC start timestamp.
        ended_at (datetime, Optional=None): UTC end timestamp when available.
        duration_seconds (float, Optional=None): Job duration in seconds.
        error_message (str, Optional=None): Error message if job failed.
    """

    __tablename__: str = "quiv_job"  # type: ignore

    id: str = Field(default_factory=id_generator, primary_key=True)
    task_id: str = Field(foreign_key="quiv_task.id")
    task_name: str
    status: str = JobStatus.SCHEDULED
    started_at: datetime = Field(default_factory=get_current_time)
    ended_at: datetime | None = None
    duration_seconds: float | None = None
    error_message: str | None = None

    # Ensure started_at and ended_at are timezone-aware UTC on load from DB
    @model_validator(mode="before")
    def force_utc_on_load(self) -> Job:
        self.started_at = self.set_timezone_to_utc(self.started_at)  # type: ignore[assignment]
        self.ended_at = self.set_timezone_to_utc(self.ended_at)
        return self
