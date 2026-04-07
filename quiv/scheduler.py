from __future__ import annotations

import asyncio
from datetime import timedelta, datetime, tzinfo
import logging
import pickle
import threading
import time
from typing import Any, Callable

from .base import QuivBase
from .config import QuivConfig
from .exceptions import ConfigurationError
from .models import Event, JobStatus, Task


class Quiv(QuivBase):
    """Public scheduler API and orchestration loop implementation."""

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
        """Initialize Quiv scheduler instance.

        Args:
            config (QuivConfig, Optional=None): Optional grouped scheduler configuration.
            pool_size (int, Optional=10): Thread-pool size when ``config`` is not provided.
            history_retention_seconds (int, Optional=86400): Job retention period when ``config`` is not provided.
            timezone (str | tzinfo, Optional="UTC"):
                Display timezone when ``config`` is not provided.
            logger (logging.Logger | logging.LoggerAdapter[Any], Optional=None): Optional logger instance.
            main_loop (asyncio.AbstractEventLoop, Optional=None): Optional main event loop for progress callbacks.
        """

        super().__init__(
            config=config,
            pool_size=pool_size,
            history_retention_seconds=history_retention_seconds,
            timezone=timezone,
            logger=logger,
            main_loop=main_loop,
        )

    def add_task(
        self,
        task_name: str,
        func: Callable[..., Any],
        interval: float,
        delay: float = 0,
        run_once: bool = False,
        args: tuple[Any, ...] | None = None,
        kwargs: dict[str, Any] | None = None,
        progress_callback: Callable[..., Any] | None = None,
    ) -> str:
        """Schedule a callable to run at a fixed interval.

        Args:
            task_name (str): Unique name for this task.
            func (Callable[..., Any]): Function to execute as task (sync/async).
            interval (float): Interval in seconds between runs.
            delay (float, Optional=0): Initial delay before first run in seconds.
            run_once (bool, Optional=False): If ``True``, run task once and remove it.
            args (tuple[Any, ...], Optional=None): Positional arguments for handler.
            kwargs (dict[str, Any], Optional=None): Keyword arguments for handler.
            progress_callback (Callable[..., Any], Optional=None): Optional progress callback executed on main loop.

        Raises:
            ConfigurationError: If scheduling parameters are invalid or
                a task with the same name is already registered.

        Returns:
            str: Task id string (UUID).
        """

        if not task_name.strip():
            raise ConfigurationError("task_name must not be empty")
        if interval <= 0:
            raise ConfigurationError("interval must be greater than 0")
        if delay < 0:
            raise ConfigurationError(
                "delay must be greater than or equal to 0"
            )
        if task_name in self.registry:
            raise ConfigurationError(
                f"Task '{task_name}' is already registered. Call"
                " remove_task() first if you want to replace it."
            )

        resolved_args = args if args is not None else ()
        resolved_kwargs = kwargs if kwargs is not None else {}

        if not isinstance(resolved_args, tuple):
            raise ConfigurationError(
                f"args must be a tuple, got {type(resolved_args).__name__}"
            )
        if not isinstance(resolved_kwargs, dict):
            raise ConfigurationError(
                f"kwargs must be a dict, got {type(resolved_kwargs).__name__}"
            )

        try:
            args_pickled = pickle.dumps(resolved_args)
        except Exception as e:
            raise ConfigurationError(
                f"Failed to serialize task args: {e}"
            ) from e
        try:
            kwargs_pickled = pickle.dumps(resolved_kwargs)
        except Exception as e:  # pragma: no cover
            raise ConfigurationError(
                f"Failed to serialize task kwargs: {e}"
            ) from e

        self._register_handler(task_name, func)
        self._register_progress_callback(task_name, progress_callback)

        next_run = self._now_utc() + timedelta(seconds=delay)
        task_id = self.persistence.create_task(
            task_name=task_name,
            interval=interval,
            run_once=run_once,
            next_run_at=next_run,
            args_pickled=args_pickled,
            kwargs_pickled=kwargs_pickled,
        )
        next_run_user_tz = self._to_display_timezone(next_run)
        self._logger.info(
            f"Task '{task_name}' added with interval {interval}s and delay"
            f" {delay}s (next run at {next_run_user_tz})"
        )
        self._emit_event(
            Event.TASK_ADDED,
            {"task_name": task_name, "task_id": task_id},
        )
        return task_id

    def remove_task(self, task_name: str) -> None:
        """Remove a scheduled task and its handler/callback registrations.

        If the task has a running job, its stop event is set to signal
        cancellation. The running job will finish on its own and clean
        up via ``_run_job``'s finally block.

        After removal, the same ``task_name`` can be re-registered
        immediately with ``add_task()``.

        Args:
            task_name (str): Task name to remove.

        Raises:
            TaskNotFoundError: If no task with that name exists.
        """

        # Cancel any running job for this task before deleting
        running_jobs = self.persistence.get_all_jobs(status=JobStatus.RUNNING)
        task_id = self.persistence.get_task_id_by_name(task_name)
        for job in running_jobs:
            if job.task_id == task_id and job.id in self.stop_events:
                self.stop_events[job.id].set()
                self._logger.info(
                    f"Cancelled running job {job.id} for task '{task_name}'"
                )

        self.persistence.delete_task(task_name)
        self.registry.pop(task_name, None)
        self.progress_callbacks.pop(task_name, None)
        self._logger.info(f"Task '{task_name}' removed")
        self._emit_event(
            Event.TASK_REMOVED,
            {"task_name": task_name, "task_id": task_id},
        )

    def _loop(self) -> None:
        """Continuously dispatch due tasks until shutdown is requested."""

        while not getattr(self, "_initialized", False):
            time.sleep(0.1)

        self._logger.info("Scheduler loop starting")
        cleanup_interval = 60
        ticks_since_cleanup = cleanup_interval  # run on first iteration
        while not self._shutdown:
            try:
                if ticks_since_cleanup >= cleanup_interval:
                    self.persistence.cleanup_history(self.history_limit)
                    ticks_since_cleanup = 0

                now = self._now_utc()
                if self._active_job_count < self._pool_size:
                    due_tasks = self.persistence.get_due_tasks(now)
                    for task in due_tasks:
                        if self._active_job_count >= self._pool_size:  # pragma: no cover
                            break
                        self._dispatch_due_task(task, now)

                time.sleep(1)
                ticks_since_cleanup += 1
            except Exception as e:
                self._logger.error(f"Error in scheduler loop: {e}")
                time.sleep(5)

    def _dispatch_due_task(self, task: Task, now: datetime) -> None:
        """Create and dispatch execution for a due task.

        Args:
            task (Task): Task record due for execution.
            now (datetime): Current UTC timestamp.
        """

        job_id = self.persistence.create_job(task.id)
        stop_event = threading.Event()
        self.stop_events[job_id] = stop_event

        func = self.registry[task.task_name]
        f_args, f_kwargs = self.execution.prepare_invocation(
            task_name=task.task_name,
            func=func,
            args_pickled=task.args,
            kwargs_pickled=task.kwargs,
            stop_event=stop_event,
            job_id=job_id,
        )

        self._logger.info(
            f"Scheduling task '{task.task_name}' (Job ID: {job_id}) to run now"
        )
        self.persistence.mark_task_running(task.id)
        with self._job_count_lock:
            self._active_job_count += 1
        self.executor.submit(
            self._run_job,
            job_id,
            task.id,
            task.task_name,
            task.run_once,
            now,
            func,
            f_args,
            f_kwargs,
        )

    def _run_job(
        self,
        job_id: str,
        task_id: str,
        task_name: str,
        run_once: bool,
        scheduled_at: datetime,
        func: Callable[..., Any],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> None:
        """Execute a single job and persist terminal status.

        Args:
            job_id (str): Job identifier (UUID string).
            task_id (str): Source task identifier.
            task_name (str): Task name for logging.
            run_once (bool): Whether the task is single-run.
            scheduled_at (datetime): UTC time when the job was dispatched.
            func (Callable[..., Any]): Handler callable.
            args (tuple): Positional arguments for handler.
            kwargs (dict): Keyword arguments for handler.
        """

        start_time = self._now_utc()
        delay = start_time - scheduled_at
        if delay.total_seconds() > 2:
            self._logger.warning(
                f"'{task_name}' (Job {job_id}) started {delay} after scheduled"
                " time — threadpool was busy. Consider increasing pool_size."
            )
        self._logger.info(
            f"'{task_name}' (Job {job_id}) started at"
            f" {self._to_display_timezone(start_time)}"
        )
        self.persistence.mark_job_running(job_id)
        self._emit_event(
            Event.JOB_STARTED,
            {"task_name": task_name, "job_id": job_id},
        )

        status = JobStatus.COMPLETED
        job_error: BaseException | None = None
        duration = timedelta()
        try:
            self.execution.run_callable(func, args, kwargs)
            end_time = self._now_utc()
            duration = end_time - start_time
            self._logger.info(
                f"'{task_name}' (Job {job_id}) completed successfully at"
                f" {self._to_display_timezone(end_time)}"
                f" (Duration: {duration})"
            )
        except Exception as e:
            end_time = self._now_utc()
            duration = end_time - start_time
            job_error = e
            self._logger.exception(
                f"'{task_name}' (Job {job_id}) raised an exception at"
                f" {self._to_display_timezone(end_time)}"
                f" [runtime: {duration}]: {e}"
            )
            status = JobStatus.FAILED
        finally:
            stop_event = self.stop_events.pop(job_id, None)
            if stop_event is not None and stop_event.is_set():
                status = JobStatus.CANCELLED
            self.persistence.finalize_job(job_id, status)
            self.persistence.finalize_task_after_job(task_id)
            with self._job_count_lock:
                self._active_job_count -= 1
            if run_once:
                self.registry.pop(task_name, None)
                self.progress_callbacks.pop(task_name, None)

            if status == JobStatus.COMPLETED:
                self._emit_event(
                    Event.JOB_COMPLETED,
                    {
                        "task_name": task_name,
                        "job_id": job_id,
                        "duration": duration,
                    },
                )
            elif status == JobStatus.FAILED:
                self._emit_event(
                    Event.JOB_FAILED,
                    {
                        "task_name": task_name,
                        "job_id": job_id,
                        "error": job_error,
                        "duration": duration,
                    },
                )
            elif status == JobStatus.CANCELLED:
                self._emit_event(
                    Event.JOB_CANCELLED,
                    {"task_name": task_name, "job_id": job_id},
                )
