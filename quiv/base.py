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
from .models import Event, Job, QuivModelBase, Task, TaskDB
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
            Mapping of task ids to handler callables.
        progress_callbacks (dict[str, Callable[..., Any]]):
            Mapping of task ids to progress callbacks.
        stop_events (dict[str, threading.Event]):
            Mapping of job ids (UUID strings) to cancellation events.
        persistence (PersistenceLayer): Persistence layer facade.
        execution (ExecutionLayer): Execution layer facade.
        _event_listeners (dict[Event, list[Callable[..., Any]]]):
            Mapping of event types to registered listener callables.
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
                session.exec(select(TaskDB).limit(1)).all()
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
        self.stop_events: dict[str, threading.Event] = {}
        self._event_listeners: dict[Event, list[Callable[..., Any]]] = {}
        self._event_listeners_lock = threading.Lock()
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

    def _register_handler(self, task_id: str, func: Callable[..., Any]) -> None:
        """Register a task handler callable.

        Args:
            task_id (str): Task identifier (UUID string).
            func (Callable[..., Any]): Handler callable.

        Raises:
            HandlerRegistrationError: If task_id or callable is invalid.
        """

        if not task_id.strip():
            raise HandlerRegistrationError("task_id must not be empty")
        if not callable(func):
            raise HandlerRegistrationError("func must be callable")
        self.registry[task_id] = func

    def _register_progress_callback(
        self, task_id: str, callback: Callable[..., Any] | None
    ) -> None:
        """Register or clear a progress callback for a task.

        Args:
            task_id (str): Task identifier (UUID string).
            callback (Callable[..., Any], Optional=None):
                Progress callback; ``None`` clears existing callback.

        Raises:
            HandlerRegistrationError: If callback is not callable.
        """

        if callback is None:
            self.progress_callbacks.pop(task_id, None)
            return
        if not callable(callback):
            raise HandlerRegistrationError(
                "progress callback must be callable"
            )
        self.progress_callbacks[task_id] = callback

    def add_listener(self, event: Event, callback: Callable[..., Any]) -> None:
        """Register an event listener for a scheduler lifecycle event.

        Listeners are dispatched on the main event loop when available
        (same behavior as progress callbacks). Multiple listeners can be
        registered for the same event.

        For ``TASK_*`` events the callback receives:

        - ``event`` (``Event``): The event type that was emitted.
        - ``task`` (``Task``): The task model object.

        For ``JOB_*`` events the callback receives:

        - ``event`` (``Event``): The event type that was emitted.
        - ``task`` (``Task``): The parent task model object.
        - ``job`` (``Job``): The job model object (includes
          ``duration_seconds`` and ``error_message`` when applicable).

        Args:
            event (Event): Event type to listen for.
            callback (Callable[..., Any]): Listener callable
                (sync or async).

        Raises:
            ConfigurationError: If event is not an ``Event`` enum member
                or callback is not callable.
        """

        if not isinstance(event, Event):
            raise ConfigurationError(
                "event must be an Event enum member, got"
                f" {type(event).__name__}"
            )
        if not callable(callback):
            raise ConfigurationError("callback must be callable")
        with self._event_listeners_lock:
            self._event_listeners.setdefault(event, []).append(callback)

    def remove_listener(
        self, event: Event, callback: Callable[..., Any]
    ) -> None:
        """Remove a previously registered event listener.

        Args:
            event (Event): Event type the callback was registered for.
            callback (Callable[..., Any]): The exact callback to remove.
        """

        with self._event_listeners_lock:
            listeners = self._event_listeners.get(event, [])
            try:
                listeners.remove(callback)
            except ValueError:
                pass

    def _emit_event(self, event: Event, *args: Any) -> None:
        """Dispatch all registered listeners for an event.

        Listeners are dispatched on the main event loop when available
        (via ``run_coroutine_threadsafe`` for async, ``call_soon_threadsafe``
        for sync). Without a loop, sync listeners run on the calling thread
        and async listeners run in a temporary event loop.

        Exceptions in listeners are logged and swallowed.

        For ``TASK_*`` events, listeners receive ``(event, task)``.
        For ``JOB_*`` events, listeners receive ``(event, task, job)``.

        Args:
            event (Event): The event being emitted.
            *args (Any): Model objects to pass to listeners
                (Task for task events; Task, Job for job events).
        """

        with self._event_listeners_lock:
            listeners = list(self._event_listeners.get(event, []))
        if not listeners:
            return

        loop = self._resolve_main_loop()

        for listener in listeners:
            try:
                self._dispatch_listener(listener, event, args, loop)
            except Exception as e:  # pragma: no cover
                self._logger.error(
                    f"Event listener for '{event.value}' failed: {e}"
                )

    def _dispatch_listener(
        self,
        listener: Callable[..., Any],
        event: Event,
        args: tuple[Any, ...],
        loop: asyncio.AbstractEventLoop | None,
    ) -> None:
        """Dispatch a single event listener callback.

        Args:
            listener (Callable[..., Any]): Listener callable.
            event (Event): The event being emitted.
            args (tuple[Any, ...]): Positional arguments after event.
            loop (asyncio.AbstractEventLoop | None): Main event loop.
        """

        def _on_listener_done(fut: Future[Any]) -> None:  # pragma: no cover
            exc = fut.exception()
            if exc is not None:
                self._logger.error(
                    f"Event listener for '{event.value}' failed: {exc}"
                )

        if inspect.iscoroutinefunction(listener):
            if loop is None:
                # No main loop available; run in temporary loop on this thread
                try:
                    new_loop = asyncio.new_event_loop()
                    try:
                        asyncio.set_event_loop(new_loop)
                        new_loop.run_until_complete(
                            listener(event, *args)
                        )
                    finally:
                        asyncio.set_event_loop(None)
                        new_loop.close()
                except Exception as e:
                    self._logger.error(
                        f"Event listener for '{event.value}' failed: {e}"
                    )
                return
            coroutine = cast(
                Coroutine[Any, Any, Any], listener(event, *args)
            )
            future = asyncio.run_coroutine_threadsafe(coroutine, loop)
            future.add_done_callback(_on_listener_done)
            return

        if loop is None:
            try:
                listener(event, *args)
            except Exception as e:  # pragma: no cover
                self._logger.error(
                    f"Event listener for '{event.value}' failed: {e}"
                )
            return

        def _call_sync_listener() -> None:
            try:
                result = listener(event, *args)
                if asyncio.iscoroutine(result):  # pragma: no cover
                    asyncio.ensure_future(result, loop=loop)
            except Exception as e:
                self._logger.error(
                    f"Event listener for '{event.value}' failed: {e}"
                )

        loop.call_soon_threadsafe(partial(_call_sync_listener))

    def _resolve_main_loop(self) -> asyncio.AbstractEventLoop | None:
        """Lazily resolve the main event loop.

        Returns:
            asyncio.AbstractEventLoop | None: The running event loop,
                or ``None`` if unavailable.
        """

        if self._main_loop is not None:
            if self._main_loop.is_closed():  # pragma: no cover
                return None
            return self._main_loop
        try:
            loop = asyncio.get_running_loop()
            self._main_loop = loop  # pragma: no cover
            return loop  # pragma: no cover
        except RuntimeError:
            return None

    def run_progress_callback(
        self, task_id: str, *args: Any, **kwargs: Any
    ) -> None:
        """Dispatch a progress callback, adapting to async availability.

        When a main event loop is available, async callbacks are dispatched
        via ``run_coroutine_threadsafe`` and sync callbacks via
        ``call_soon_threadsafe``. Without an event loop, async callbacks
        run in a temporary event loop on the calling thread and sync
        callbacks run directly.

        Args:
            task_id (str): Task id whose callback should be invoked.
            *args (Any): Positional payload values.
            **kwargs (Any): Keyword payload values.
        """

        callback = self.progress_callbacks.get(task_id)
        if callback is None:
            return

        loop = self._resolve_main_loop()

        def _on_progress_done(fut: Future[Any]) -> None:  # pragma: no cover
            exc = fut.exception()
            if exc is not None:
                self._logger.error(
                    f"Progress callback for task '{task_id}' failed: {exc}"
                )

        if inspect.iscoroutinefunction(callback):
            if loop is None:
                # No main loop available; run in temporary loop on this thread
                try:
                    new_loop = asyncio.new_event_loop()
                    try:
                        asyncio.set_event_loop(new_loop)
                        new_loop.run_until_complete(callback(*args, **kwargs))
                    finally:
                        asyncio.set_event_loop(None)
                        new_loop.close()
                except Exception as e:
                    self._logger.error(
                        f"Progress callback for task '{task_id}' failed: {e}"
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
                    f"Progress callback for task '{task_id}' failed: {e}"
                )
            return

        def _call_sync_callback() -> None:
            try:
                result = callback(*args, **kwargs)
                if asyncio.iscoroutine(result):  # pragma: no cover
                    asyncio.ensure_future(result, loop=loop)
            except Exception as e:
                self._logger.error(
                    f"Progress callback for task '{task_id}' failed: {e}"
                )

        loop.call_soon_threadsafe(partial(_call_sync_callback))

    def run_task_immediately(self, task_id: str) -> int:
        """Queue an existing scheduled task for immediate run.

        Args:
            task_id (str): Task id to enqueue.

        Returns:
            int: Number of queued task rows.

        Raises:
            HandlerNotRegisteredError: If no handler is registered for the id.
        """

        if task_id not in self.registry:
            raise HandlerNotRegisteredError(
                f"Handler for task '{task_id}' not registered."
            )
        count = self.persistence.queue_task_for_immediate_run(task_id)
        self._logger.info(
            f"Task '{task_id}' queued for immediate run via scheduler loop."
        )
        return count

    def start(self) -> None:
        """Start the scheduler background thread."""
        if self._main_loop is None:
            self._resolve_main_loop()  # pragma: no cover
        if not self.thread.is_alive():
            self.thread.start()
        return None

    def startup(self) -> None:  # pragma: no cover
        """Start the scheduler background thread.

        Alias for :meth:`start`. Pairs naturally with :meth:`shutdown`.
        """

        self.start()

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
            self._logger.debug(f"Cleaned up database file: {self._db_path}")
        except Exception as e:
            self._logger.warning(f"Could not cleanup database file: {e}")

    def stop(self) -> None:  # pragma: no cover
        """Stop scheduler loop, cancel jobs, and release resources.

        Alias for :meth:`shutdown`. Pairs naturally with :meth:`start`.
        """

        self.shutdown()

    def pause_task(self, task_id: str) -> None:
        """Pause a task by id.

        Args:
            task_id (str): Task id.

        Raises:
            TaskNotFoundError: If no task with that id exists.
        """

        self.persistence.pause_task(task_id)
        task = Task.model_validate(self.persistence.get_task(task_id))
        self._emit_event(Event.TASK_PAUSED, task)

    def resume_task(self, task_id: str, delay: int = 0) -> None:
        """Resume a paused task by id.

        Args:
            task_id (str): Task id.
            delay (int, Optional=0): Seconds to delay before next run.

        Raises:
            TaskNotFoundError: If no task with that id exists.
        """

        self.persistence.resume_task(task_id, delay=delay)
        task = Task.model_validate(self.persistence.get_task(task_id))
        self._emit_event(Event.TASK_RESUMED, task)

    def cancel_job(self, job_id: str) -> bool:
        """Signal cancellation for a running job.

        Args:
            job_id (str): Job identifier (UUID string).

        Returns:
            bool: ``True`` if a stop event existed and was set,
                otherwise ``False``.
        """

        if job_id in self.stop_events:
            self.stop_events[job_id].set()
            return True
        return False

    def get_task(self, task_id: str) -> Task:
        """Retrieve a single task by ID.

        Args:
            task_id (str): Task UUID.

        Returns:
            Task: The task record with unpickled args/kwargs.

        Raises:
            TaskNotFoundError: If no task with that ID exists.
        """

        task = self.persistence.get_task(task_id)
        return Task.model_validate(task)

    def get_job(self, job_id: str) -> Job:
        """Retrieve a single job by ID.

        Args:
            job_id (str): Job identifier (UUID string).

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
            list[Task]: List of tasks with unpickled args/kwargs.
        """

        tasks = self.persistence.get_all_tasks(
            include_run_once=include_run_once
        )
        return [Task.model_validate(t) for t in tasks]

    def get_all_jobs(self, status: str | None = None) -> list[Job]:
        """Retrieve persisted job records.

        Args:
            status (str, Optional=None): Optional status filter.

        Returns:
            list[Job]: List of jobs.
        """

        return self.persistence.get_all_jobs(status=status)
