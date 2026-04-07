from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import Enum
import pickle
import uuid
import logging

from pydantic import model_validator
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


class Task(QuivModelBase, table=True):
    """Scheduled task persisted in the Quiv database.

    Attributes:
        __tablename__ (str): Database table name for tasks.
        id (str, UUID): UUID task identifier string.
        task_name (str):
            Unique User-facing task key mapped to a registered handler.
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

    # On load from DB, ensure next_run_at is timezone-aware UTC
    # Only model_validator in before mode works, as it runs on model instantiation
    @model_validator(mode="before")
    def force_utc_on_load(self) -> Task:
        self.next_run_at = self.set_timezone_to_utc(self.next_run_at)  # type: ignore
        return self


class Job(QuivModelBase, table=True):
    """Execution record for a single task run.

    Attributes:
        __tablename__ (str): Database table name for jobs.
        id (int, Optional=None): Auto-incrementing job identifier.
        task_id (str): Foreign key to the source task.
        status (str): Job status string.
        started_at (datetime): UTC start timestamp.
        ended_at (datetime, Optional=None): UTC end timestamp when available.
    """

    __tablename__: str = "quiv_job"  # type: ignore

    id: int | None = Field(default=None, primary_key=True)
    task_id: str = Field(foreign_key="quiv_task.id")
    status: str = JobStatus.SCHEDULED
    started_at: datetime = Field(default_factory=get_current_time)
    ended_at: datetime | None = None

    # Ensure started_at and ended_at are timezone-aware UTC on load from DB
    @model_validator(mode="before")
    def force_utc_on_load(self) -> Job:
        self.started_at = self.set_timezone_to_utc(self.started_at)  # type: ignore[assignment]
        self.ended_at = self.set_timezone_to_utc(self.ended_at)
        return self
