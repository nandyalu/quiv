from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Coroutine
from datetime import datetime, timezone, tzinfo
from functools import partial
import inspect
import logging
import os
import tempfile
import threading
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, cast

from sqlmodel import Session, create_engine, select

from .config import QuivConfig, resolve_timezone
from .exceptions import (
    ConfigurationError,
    DatabaseInitializationError,
    HandlerNotRegisteredError,
    HandlerRegistrationError,
)
from .execution import ExecutionLayer
from .models import Job, QuivModelBase, Task
from .persistence import PersistenceLayer


class QuivBase(ABC):
    """Base scheduler runtime with lifecycle, execution, and callback plumbing.

    Attributes:
        _logger (logging.Logger | logging.LoggerAdapter[Any]): Logger instance used by the scheduler.
        _timezone (tzinfo): Configured display timezone.
        _main_loop (asyncio.AbstractEventLoop):
            Main application event loop for progress callbacks.
        _db_path (str): Temporary sqlite path used by the scheduler.
        _engine (Any): SQLModel engine backing task/job persistence.
        executor (ThreadPoolExecutor): Thread pool used for task execution.
        history_limit (int): Job history retention period in seconds.
        registry (dict[str, Callable[..., Any]]):
            Mapping of task names to handler callables.
        progress_callbacks (dict[str, Callable[..., Any]]):
            Mapping of task names to progress callbacks.
        stop_events (dict[int, threading.Event]):
            Mapping of job ids to cancellation events.
        persistence (PersistenceLayer): Persistence layer facade.
        execution (ExecutionLayer): Execution layer facade.
        _shutdown (bool): Loop shutdown flag.
        thread (threading.Thread): Background scheduler thread.
        _initialized (bool): Initialization completion flag.
    """

    def __init__(
        self,
        config: QuivConfig | None = None,
        pool_size: int = 10,
        history_retention_seconds: int = 86400,
        timezone: str | tzinfo = "UTC",
        *,
        logger: logging.Logger | logging.LoggerAdapter[Any] | None = None,
        main_loop: asyncio.AbstractEventLoop | None = None,
    ):
        """Initialize shared runtime components.

        Args:
            config (QuivConfig, Optional=None): Grouped scheduler configuration.
            pool_size (int, Optional=10):
                Thread-pool size when ``config`` is not provided.
            history_retention_seconds (int, Optional=86400):
                Job retention period when ``config`` is not provided.
            timezone (str | tzinfo, Optional="UTC"):
                Display timezone when ``config`` is not provided.
            logger (logging.Logger | logging.LoggerAdapter[Any], Optional=None): Optional logger instance.
            main_loop (asyncio.AbstractEventLoop, Optional=None):
                Optional main event loop for progress callbacks.

        Raises:
            ConfigurationError: If configuration values are invalid.
            DatabaseInitializationError: If database initialization fails.
        """

        if config is not None:
            if (
                pool_size != 10
                or history_retention_seconds != 86400
                or timezone != "UTC"
            ):
                raise ConfigurationError(
                    "When 'config' is provided, do not pass"
                    " pool_size/history_retention_seconds/timezone"
                    " explicitly."
                )
            pool_size = config.pool_size
            history_retention_seconds = config.history_retention_seconds
            timezone = config.timezone

        if pool_size <= 0:
            raise ConfigurationError("pool_size must be greater than 0")
        if history_retention_seconds < 0:
            raise ConfigurationError(
                "history_retention_seconds must be greater than or equal to 0"
            )

        if not logger:
            logger = logging.getLogger("Quiv")
        self._logger = logger
        self._timezone: tzinfo = resolve_timezone(timezone)

        self._main_loop = main_loop

        temp_dir = tempfile.gettempdir()
        db_name = f"quiv_{uuid.uuid4().hex[:8]}.db"
        self._db_path = os.path.join(temp_dir, db_name)
        self._engine = create_engine(
            f"sqlite:///{self._db_path}",
            connect_args={"check_same_thread": False, "timeout": 10},
            pool_pre_ping=True,
        )

        from sqlalchemy import event

        @event.listens_for(self._engine, "connect")
        def _set_sqlite_wal(dbapi_conn: Any, connection_record: Any) -> None:
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

        try:
            QuivModelBase.metadata.create_all(self._engine)
            with Session(self._engine) as session:
                session.exec(select(Task).limit(1)).all()
                session.exec(select(Job).limit(1)).all()
            self._logger.debug(
                "Database tables created and verified successfully."
            )
        except Exception as e:
            self._logger.error(f"Failed to create database tables: {e}")
            raise DatabaseInitializationError(
                "Failed to initialize scheduler database"
            ) from e

        self._pool_size = pool_size
        self.executor = ThreadPoolExecutor(max_workers=pool_size)
        self.history_limit = history_retention_seconds
        self.registry: dict[str, Callable[..., Any]] = {}
        self.progress_callbacks: dict[str, Callable[..., Any]] = {}
        self.stop_events: dict[int, threading.Event] = {}
        self._active_job_count = 0
        self._job_count_lock = threading.Lock()

        self.persistence = PersistenceLayer(self._engine, self._now_utc)
        self.execution = ExecutionLayer(
            self.run_async, self.run_progress_callback
        )

        self._shutdown = False
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self._initialized = True

    def _now_utc(self) -> datetime:
        """Return current UTC timestamp.

        Returns:
            datetime: Current UTC datetime.
        """

        return datetime.now(timezone.utc)

    def _to_display_timezone(self, value: datetime) -> datetime:
        """Convert a datetime to the configured display timezone.

        Args:
            value (datetime): Datetime value to convert.

        Returns:
            datetime: Datetime converted to configured timezone.
        """

        return value.astimezone(self._timezone)

    @abstractmethod
    def _loop(self) -> None:
        """Run the scheduler loop.

        Implemented by concrete scheduler classes.
        """

        raise NotImplementedError

    def run_async(
        self,
        task: Callable[..., Awaitable[Any]],
        args: tuple[Any, ...] | None = None,
        kwargs: dict[str, Any] | None = None,
    ) -> None:
        """Run an async callable in a thread-local event loop.

        Args:
            task (Callable[..., Awaitable[Any]]): Coroutine function to execute.
            args (tuple, Optional=None): Positional arguments for the callable.
            kwargs (dict, Optional=None): Keyword arguments for the callable.
        """

        new_loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(new_loop)
            new_loop.run_until_complete(task(*(args or ()), **(kwargs or {})))
        finally:
            asyncio.set_event_loop(None)
            new_loop.close()

    def _register_handler(self, name: str, func: Callable[..., Any]) -> None:
        """Register a task handler callable.

        Args:
            name (str): Unique task name.
            func (Callable[..., Any]): Handler callable.

        Raises:
            HandlerRegistrationError: If name or callable is invalid.
        """

        if not name.strip():
            raise HandlerRegistrationError("task name must not be empty")
        if not callable(func):
            raise HandlerRegistrationError("func must be callable")
        self.registry[name] = func

    def _register_progress_callback(
        self, name: str, callback: Callable[..., Any] | None
    ) -> None:
        """Register or clear a progress callback for a task.

        Args:
            name (str): Task name; has to be unique.
            callback (Callable[..., Any], Optional=None):
                Progress callback; ``None`` clears existing callback.

        Raises:
            HandlerRegistrationError: If callback is not callable.
        """

        if callback is None:
            self.progress_callbacks.pop(name, None)
            return
        if not callable(callback):
            raise HandlerRegistrationError(
                "progress callback must be callable"
            )
        self.progress_callbacks[name] = callback

    def _resolve_main_loop(self) -> asyncio.AbstractEventLoop | None:
        """Lazily resolve the main event loop.

        Returns:
            asyncio.AbstractEventLoop | None: The running event loop,
                or ``None`` if unavailable.
        """

        if self._main_loop is not None:
            if self._main_loop.is_closed():
                return None
            return self._main_loop
        try:
            loop = asyncio.get_running_loop()
            self._main_loop = loop
            return loop
        except RuntimeError:
            return None

    def run_progress_callback(
        self, task_name: str, *args: Any, **kwargs: Any
    ) -> None:
        """Dispatch a progress callback, adapting to async availability.

        When a main event loop is available, async callbacks are dispatched
        via ``run_coroutine_threadsafe`` and sync callbacks via
        ``call_soon_threadsafe``. Without an event loop, sync callbacks
        run directly on the calling thread and async callbacks are skipped
        with a warning.

        Args:
            task_name (str): Task name whose callback should be invoked.
            *args (Any): Positional payload values.
            **kwargs (Any): Keyword payload values.
        """

        callback = self.progress_callbacks.get(task_name)
        if callback is None:
            return

        loop = self._resolve_main_loop()

        def _on_progress_done(fut: Future[Any]) -> None:
            exc = fut.exception()
            if exc is not None:
                self._logger.error(
                    f"Progress callback for task '{task_name}' failed: {exc}"
                )

        if inspect.iscoroutinefunction(callback):
            if loop is None:
                self._logger.warning(
                    f"Async progress callback for task '{task_name}' skipped"
                    " because no event loop is available."
                )
                return
            coroutine = cast(
                Coroutine[Any, Any, Any], callback(*args, **kwargs)
            )
            future = asyncio.run_coroutine_threadsafe(coroutine, loop)
            future.add_done_callback(_on_progress_done)
            return

        if loop is None:
            try:
                callback(*args, **kwargs)
            except Exception as e:
                self._logger.error(
                    f"Progress callback for task '{task_name}' failed: {e}"
                )
            return

        def _call_sync_callback() -> None:
            try:
                result = callback(*args, **kwargs)
                if asyncio.iscoroutine(result):
                    asyncio.ensure_future(result, loop=loop)
            except Exception as e:
                self._logger.error(
                    f"Progress callback for task '{task_name}' failed: {e}"
                )

        loop.call_soon_threadsafe(partial(_call_sync_callback))

    def run_task_immediately(self, task_name: str) -> int:
        """Queue an existing scheduled task for immediate run.

        Args:
            task_name (str): Task name to enqueue.

        Returns:
            int: Number of queued task rows.

        Raises:
            HandlerNotRegisteredError: If no handler exists for the name.
        """

        if task_name not in self.registry:
            raise HandlerNotRegisteredError(
                f"Handler '{task_name}' not registered."
            )
        count = self.persistence.queue_task_for_immediate_run(task_name)
        self._logger.info(
            f"Task '{task_name}' queued for immediate run via scheduler loop."
        )
        return count

    def start(self) -> None:
        """Start the scheduler background thread."""
        if not self.thread.is_alive():
            self.thread.start()
        return None

    def shutdown(self) -> None:
        """Stop scheduler loop, cancel jobs, and release resources."""

        self._shutdown = True
        all_jobs = self.get_all_jobs()
        for job in all_jobs:
            if job.id is not None:
                self.cancel_job(job.id)
        if self.thread.is_alive():
            self.thread.join()
        self.executor.shutdown(wait=True)

        try:
            self._engine.dispose()
            for suffix in ("", "-wal", "-shm"):
                path = self._db_path + suffix
                if os.path.exists(path):
                    os.remove(path)
            self._logger.info(f"Cleaned up database file: {self._db_path}")
        except Exception as e:
            self._logger.warning(f"Could not cleanup database file: {e}")

    def pause_task(self, task_name: str) -> None:
        """Pause a task by name.

        Args:
            task_name (str): Task name.

        Raises:
            TaskNotFoundError: If no task with that name exists.
        """

        task_id = self.persistence.get_task_id_by_name(task_name)
        self.persistence.pause_task(task_id)

    def resume_task(self, task_name: str, delay: int = 0) -> None:
        """Resume a paused task by name.

        Args:
            task_name (str): Task name.
            delay (int, Optional=0): Seconds to delay before next run.

        Raises:
            TaskNotFoundError: If no task with that name exists.
        """

        task_id = self.persistence.get_task_id_by_name(task_name)
        self.persistence.resume_task(task_id, delay=delay)

    def cancel_job(self, job_id: int) -> bool:
        """Signal cancellation for a running job.

        Args:
            job_id (int): Job identifier.

        Returns:
            bool: ``True`` if a stop event existed and was set,
                otherwise ``False``.
        """

        if job_id in self.stop_events:
            self.stop_events[job_id].set()
            return True
        return False

    def get_task(self, task_name: str) -> Task:
        """Retrieve a single task by name.

        Args:
            task_name (str): Task name.

        Returns:
            Task: The task record.

        Raises:
            TaskNotFoundError: If no task with that name exists.
        """

        return self.persistence.get_task_by_name(task_name)

    def get_task_by_id(self, task_id: str) -> Task:
        """Retrieve a single task by ID.

        Args:
            task_id (str): Task UUID.

        Returns:
            Task: The task record.

        Raises:
            TaskNotFoundError: If no task with that ID exists.
        """

        return self.persistence.get_task_by_id(task_id)

    def get_job(self, job_id: int) -> Job:
        """Retrieve a single job by ID.

        Args:
            job_id (int): Job identifier.

        Returns:
            Job: The job record.

        Raises:
            JobNotFoundError: If no job with that ID exists.
        """

        return self.persistence.get_job(job_id)

    def get_all_tasks(self, include_run_once: bool = False) -> list[Task]:
        """Retrieve persisted task records.

        Args:
            include_run_once (bool, Optional=False):
                Include single-run tasks when ``True``.

        Returns:
            list[Task]: List of tasks.
        """

        return self.persistence.get_all_tasks(
            include_run_once=include_run_once
        )

    def get_all_jobs(self, status: str | None = None) -> list[Job]:
        """Retrieve persisted job records.

        Args:
            status (str, Optional=None): Optional status filter.

        Returns:
            list[Job]: List of jobs.
        """

        return self.persistence.get_all_jobs(status=status)
