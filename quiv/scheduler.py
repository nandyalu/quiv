from __future__ import annotations

import asyncio
from datetime import timedelta, datetime, tzinfo
import json
import logging
import threading
import time
from typing import Any, Callable

from .base import QuivBase
from .config import QuivConfig
from .exceptions import ConfigurationError, HandlerNotRegisteredError
from .models import JobStatus, Task


class Quiv(QuivBase):
    """Public scheduler API and orchestration loop implementation."""

    def __init__(
        self,
        config: QuivConfig | None = None,
        pool_size: int = 10,
        history_retention_seconds: int = 86400,
        timezone_name: str | tzinfo = "UTC",
        *,
        logger: logging.Logger | None = None,
        main_loop: asyncio.AbstractEventLoop | None = None,
    ):
        """Initialize Quiv scheduler instance.

        Args:
            config (QuivConfig, Optional=None): Optional grouped scheduler configuration.
            pool_size (int, Optional=10): Thread-pool size when ``config`` is not provided.
            history_retention_seconds (int, Optional=86400): Job retention period when ``config`` is not provided.
            timezone_name (TimezoneInput, Optional="UTC"):
                Display timezone when ``config`` is not provided.
            logger (logging.Logger, Optional=None): Optional logger instance.
            main_loop (asyncio.AbstractEventLoop, Optional=None): Optional main event loop for progress callbacks.
        """

        super().__init__(
            config=config,
            pool_size=pool_size,
            history_retention_seconds=history_retention_seconds,
            timezone_name=timezone_name,
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
        args: list | None = None,
        kwargs: dict | None = None,
        progress_callback: Callable[..., Any] | None = None,
    ) -> str | None:
        """Schedule a callable to run at a fixed interval.

        Args:
            task_name (str): Unique name for this task.
            func (Callable[..., Any]): Function to execute as task (sync/async).
            interval (float): Interval in seconds between runs.
            delay (float, Optional=0): Initial delay before first run in seconds.
            run_once (bool, Optional=False): If ``True``, run task once and remove it.
            args (list, Optional=None): Positional arguments for handler.
            kwargs (dict, Optional=None): Keyword arguments for handler.
            progress_callback (Callable[..., Any], Optional=None): Optional progress callback executed on main loop.

        Raises:
            ConfigurationError: If scheduling parameters are invalid.
            HandlerNotRegisteredError: If handler registry insertion unexpectedly fails.
        Returns:
            str | None: Task id string (UUID) if scheduling succeeded, else ``None``.
        """

        if not task_name.strip():
            raise ConfigurationError("task_name must not be empty")
        if interval <= 0:
            raise ConfigurationError("interval must be greater than 0")
        if delay < 0:
            raise ConfigurationError(
                "delay must be greater than or equal to 0"
            )

        self.register_handler(task_name, func)
        self.register_progress_callback(task_name, progress_callback)
        if task_name not in self.registry:
            raise HandlerNotRegisteredError(
                f"Handler '{task_name}' not registered."
            )

        next_run = self._now_utc() + timedelta(seconds=delay)
        task_id = self.persistence.upsert_task(
            task_name=task_name,
            interval=interval,
            run_once=run_once,
            next_run_at=next_run,
            args_json=json.dumps(args or []),
            kwargs_json=json.dumps(kwargs or {}),
        )
        next_run_user_tz = self._to_display_timezone(next_run)
        self._logger.info(
            f"Task '{task_name}' added with interval {interval}s and delay"
            f" {delay}s (next run at {next_run_user_tz})"
        )
        return task_id

    def _loop(self) -> None:
        """Continuously dispatch due tasks until shutdown is requested."""

        while not getattr(self, "_initialized", False):
            time.sleep(0.1)

        self._logger.info("Scheduler loop starting")
        while not self._shutdown:
            try:
                all_jobs = self.persistence.get_all_jobs()
                self.persistence.cleanup_history(self.history_limit, all_jobs)

                now = self._now_utc()
                due_tasks = self.persistence.get_due_tasks(now)
                for task in due_tasks:
                    self._dispatch_due_task(task, now)

                time.sleep(1)
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
            args_json=task.args,
            kwargs_json=task.kwargs,
            stop_event=stop_event,
        )

        self._logger.info(
            f"Scheduling task '{task.task_name}' (Job ID: {job_id}) to run now"
        )
        self.executor.submit(self._run_job, job_id, func, f_args, f_kwargs)
        self.persistence.finalize_task_after_schedule(task.id, now)

        if task.run_once:
            self._logger.info(
                f"Task '{task.task_name}' is set to run once. Task completed."
            )
        else:
            self._logger.info(
                f"Next run for task '{task.task_name}' scheduled at"
                f" {now + timedelta(seconds=task.interval_seconds)}"
            )

    def _run_job(
        self, job_id: int, func: Callable[..., Any], args: list, kwargs: dict
    ) -> None:
        """Execute a single job and persist terminal status.

        Args:
            job_id (int): Job identifier.
            func (Callable[..., Any]): Handler callable.
            args (list): Positional arguments for handler.
            kwargs (dict): Keyword arguments for handler.
        """

        start_time = self._now_utc()
        self._logger.info(f"Job {job_id} started at {start_time}")
        self.persistence.mark_job_running(job_id)

        status = JobStatus.COMPLETED
        try:
            self.execution.run_callable(func, args, kwargs)
            end_time = self._now_utc()
            self._logger.info(
                f"Job {job_id} completed successfully at {end_time} (Duration:"
                f" {end_time - start_time})"
            )
        except Exception as e:
            self._logger.error(f"Job {job_id} failed: {e}")
            status = JobStatus.FAILED
        finally:
            stop_event = kwargs.get("_stop_event")
            if stop_event is not None and stop_event.is_set():
                status = JobStatus.CANCELLED
            self.persistence.finalize_job(job_id, status)
            self.stop_events.pop(job_id, None)
