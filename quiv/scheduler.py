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
from .context import _current_quiv
from .exceptions import (
    ConfigurationError,
    HandlerRegistrationError,
    TaskNotFoundError,
)
from .models import Event, JobStatus, TaskDB

_CLEANUP_INTERVAL_SECONDS = 60.0
_MIN_SLEEP_SECONDS = 0.01  # floor: never busy-spin
_MAX_SLEEP_SECONDS = 60.0  # ceiling: bounded staleness safety net


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
        fixed_interval: bool = True,
        *,
        timeout: float | None = None,
        max_retries: int = 0,
        retry_backoff: float = 30.0,
        jitter: float = 0.0,
        args: tuple[Any, ...] | None = None,
        kwargs: dict[str, Any] | None = None,
        progress_callback: Callable[..., Any] | None = None,
    ) -> str:
        """Schedule a callable to run at a fixed interval.

        Args:
            task_name (str): Display name for this task.
            func (Callable[..., Any]): Function to execute as task (sync/async).
            interval (float): Interval in seconds between runs.
            delay (float, Optional=0): Initial delay before first run in seconds.
            run_once (bool, Optional=False): If ``True``, run task once and remove it.
            fixed_interval (bool, Optional=True): If ``True``, next run is
                scheduled at fixed intervals from the job start time. If
                ``False``, next run is scheduled ``interval`` seconds after
                job completion.
            timeout (float, Optional=None): Cooperative per-job timeout in
                seconds. When a job exceeds it, quiv sets the job's stop
                event — exactly as ``cancel_job()`` would — and the job
                finalizes as ``cancelled`` with a timeout error message.
                Handlers that ignore their stop event keep occupying their
                pool thread (quiv never kills threads). ``None`` disables.
            max_retries (int, Optional=0): Maximum consecutive retries
                after failed jobs. Applies to ``failed`` jobs only —
                cancelled jobs (including timeouts) never retry.
            retry_backoff (float, Optional=30.0): Base delay in seconds
                for exponential retry backoff: first retry after
                ``retry_backoff``, then 2x, 4x, and so on.
            jitter (float, Optional=0.0): Adds ``uniform(0, jitter)``
                seconds to each recurring next-run time to de-synchronize
                tasks sharing interval boundaries. Not applied to the
                initial ``delay`` nor to retry backoff.
            args (tuple[Any, ...], Optional=None): Positional arguments for handler.
            kwargs (dict[str, Any], Optional=None): Keyword arguments for handler.
            progress_callback (Callable[..., Any], Optional=None): Optional progress callback executed on main loop.

        Raises:
            ConfigurationError: If scheduling parameters are invalid.
            HandlerRegistrationError: If ``func`` or ``progress_callback``
                is not callable.

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
        if timeout is not None and timeout <= 0:
            raise ConfigurationError("timeout must be greater than 0")
        if max_retries < 0:
            raise ConfigurationError(
                "max_retries must be greater than or equal to 0"
            )
        if retry_backoff <= 0:
            raise ConfigurationError("retry_backoff must be greater than 0")
        if jitter < 0:
            raise ConfigurationError(
                "jitter must be greater than or equal to 0"
            )
        # Validate registration inputs BEFORE persisting the task row —
        # failing later in _register_handler/_register_progress_callback
        # would leave an orphaned ACTIVE row that can never dispatch.
        if not callable(func):
            raise HandlerRegistrationError("func must be callable")
        if progress_callback is not None and not callable(progress_callback):
            raise HandlerRegistrationError(
                "progress callback must be callable"
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

        next_run = self._now_utc() + timedelta(seconds=delay)
        task_id = self.persistence.create_task(
            task_name=task_name,
            interval=interval,
            run_once=run_once,
            fixed_interval=fixed_interval,
            next_run_at=next_run,
            args_pickled=args_pickled,
            kwargs_pickled=kwargs_pickled,
            timeout=timeout,
            max_retries=max_retries,
            retry_backoff=retry_backoff,
            jitter=jitter,
        )

        self._register_handler(task_id, func)
        self._register_progress_callback(task_id, progress_callback)

        next_run_user_tz = self._to_display_timezone(next_run)
        self._logger.info(
            f"Task '{task_name}' added with interval {interval}s and delay"
            f" {delay}s (next run at {next_run_user_tz})"
        )
        task = self.get_task(task_id)
        self._emit_event(Event.TASK_ADDED, task)
        self._wake_loop()
        return task_id

    def remove_task(self, task_id: str) -> None:
        """Remove a scheduled task and its handler/callback registrations.

        If the task has a running job, its stop event is set to signal
        cancellation. The running job will finish on its own and clean
        up via ``_run_job``'s finally block.

        Args:
            task_id (str): Task id to remove.

        Raises:
            TaskNotFoundError: If no task with that id exists.
        """

        # Snapshot the task before deletion for the event listener
        task = self.get_task(task_id)

        # Cancel any running job for this task before deleting
        running_jobs = self.persistence.get_all_jobs(status=JobStatus.RUNNING)
        for job in running_jobs:
            if job.task_id != task_id or job.id is None:
                continue
            with self._registries_lock:
                stop_event = self.stop_events.get(job.id)
            if stop_event is not None:
                stop_event.set()
                self._logger.info(
                    f"Cancelled running job {job.id} for task '{task_id}'"
                )

        self.persistence.delete_task(task_id)
        with self._registries_lock:
            self.registry.pop(task_id, None)
            self.progress_callbacks.pop(task_id, None)
        self._logger.info(f"Task '{task_id}' removed")
        self._emit_event(Event.TASK_REMOVED, task)
        self._wake_loop()

    def _loop(self) -> None:
        """Continuously dispatch due tasks until shutdown is requested.

        Sleeps until the next due task (or the next history-cleanup
        deadline) on an interruptible wait; mutating API calls wake the
        loop early via ``_wake_loop()``.
        """

        while not getattr(self, "_initialized", False):
            time.sleep(0.1)

        self._logger.info("Scheduler loop starting")
        next_cleanup = time.monotonic()  # run cleanup on first iteration
        while not self._shutdown:
            try:
                if time.monotonic() >= next_cleanup:
                    self.persistence.cleanup_history(self.history_limit)
                    next_cleanup = (
                        time.monotonic() + _CLEANUP_INTERVAL_SECONDS
                    )

                self._enforce_timeouts()

                now = self._now_utc()
                if self._active_job_count < self._pool_size:
                    for task in self.persistence.get_due_tasks(now):
                        if (
                            self._active_job_count >= self._pool_size
                        ):  # pragma: no cover
                            break
                        self._dispatch_due_task(task, now)

                # Compute the sleep last — dispatching above changes
                # next_run_at (tasks go RUNNING and leave the due set).
                sleep_for = self._compute_sleep_seconds(next_cleanup)
                self._wake_event.wait(timeout=sleep_for)
                self._wake_event.clear()
            except Exception as e:
                self._logger.error(f"Error in scheduler loop: {e}")
                self._wake_event.wait(timeout=5)
                self._wake_event.clear()

    def _enforce_timeouts(self) -> None:
        """Set stop events for jobs past their deadline."""

        now = time.monotonic()
        with self._registries_lock:
            expired = [
                job_id
                for job_id, deadline in self._job_deadlines.items()
                if now >= deadline
            ]
            for job_id in expired:
                del self._job_deadlines[job_id]
                self._timed_out_jobs.add(job_id)
        for job_id in expired:
            if self.cancel_job(job_id):
                self._logger.warning(
                    f"Job {job_id} exceeded its timeout; stop event set."
                )

    def _compute_sleep_seconds(self, next_cleanup: float) -> float:
        """Seconds to sleep until the next scheduled wake-up.

        Args:
            next_cleanup (float): Monotonic deadline of the next history
                cleanup.

        Returns:
            float: Bounded sleep duration in seconds.
        """

        candidates = [next_cleanup - time.monotonic()]
        with self._registries_lock:
            soonest_deadline = (
                min(self._job_deadlines.values())
                if self._job_deadlines
                else None
            )
        if soonest_deadline is not None:
            candidates.append(soonest_deadline - time.monotonic())
        with self._job_count_lock:
            pool_full = self._active_job_count >= self._pool_size
        # When the pool is saturated, skip the next-due candidate: an
        # overdue task cannot dispatch anyway, and clamping its negative
        # delta to the sleep floor would busy-poll the DB at ~100 Hz for
        # the whole saturation window. A finishing job wakes the loop.
        if not pool_full:
            next_due = self.persistence.get_next_due_time()
            if next_due is not None:
                candidates.append(
                    (next_due - self._now_utc()).total_seconds()
                )
        return max(_MIN_SLEEP_SECONDS, min(min(candidates), _MAX_SLEEP_SECONDS))

    def _dispatch_due_task(self, task: TaskDB, now: datetime) -> None:
        """Create and dispatch execution for a due task.

        Args:
            task (TaskDB): Task record due for execution.
            now (datetime): Current UTC timestamp.
        """

        with self._registries_lock:
            func = self.registry.get(task.id)
        if func is None:
            # Task was removed between the due-query and dispatch.
            self._logger.warning(
                f"Skipping dispatch for task '{task.id}': handler no longer"
                " registered (task was likely removed)."
            )
            return

        try:
            self.persistence.mark_task_running(task.id)
            # Snapshot task for event listeners while it is guaranteed to
            # exist. The task row may be deleted by finalize_task_after_job
            # (run-once) or by remove_task() before _run_job emits its events.
            task_snapshot = self.get_task(task.id)
        except TaskNotFoundError:
            self._logger.warning(
                f"Skipping dispatch for task '{task.id}': task row was"
                " deleted before dispatch."
            )
            return

        job_id = self.persistence.create_job(
            task.id, task.task_name, attempt=task.retry_attempt + 1
        )
        stop_event = threading.Event()
        with self._registries_lock:
            self.stop_events[job_id] = stop_event
            if task.timeout_seconds is not None:
                self._job_deadlines[job_id] = (
                    time.monotonic() + task.timeout_seconds
                )

        f_args, f_kwargs = self.execution.prepare_invocation(
            task_id=task.id,
            func=func,
            args_pickled=task.args,
            kwargs_pickled=task.kwargs,
            stop_event=stop_event,
            job_id=job_id,
        )

        self._logger.info(
            f"Scheduling task '{task.task_name}' (Job ID: {job_id}) to run now"
        )

        with self._job_count_lock:
            self._active_job_count += 1
        self.executor.submit(
            self._run_job,
            job_id,
            task.id,
            task.task_name,
            task.run_once,
            now,
            task_snapshot,
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
        task_snapshot: Any,
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
            task_snapshot (Task): Pre-fetched task snapshot for event listeners.
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

        started_job = self.get_job(job_id)
        self._emit_event(Event.JOB_STARTED, task_snapshot, started_job)

        status = JobStatus.COMPLETED
        job_error: BaseException | None = None
        duration = timedelta()
        ctx_token = _current_quiv.set(self)
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
            _current_quiv.reset(ctx_token)
            with self._registries_lock:
                stop_event = self.stop_events.pop(job_id, None)
                self._job_deadlines.pop(job_id, None)
                timed_out = job_id in self._timed_out_jobs
                self._timed_out_jobs.discard(job_id)
            if stop_event is not None and stop_event.is_set():
                status = JobStatus.CANCELLED
            # Cancelled overrides failed (above) — a job that both raised
            # and was cancelled must not retry.
            job_failed = status == JobStatus.FAILED

            error_message = str(job_error) if job_error is not None else None
            if (
                timed_out
                and status == JobStatus.CANCELLED
                and error_message is None
            ):
                error_message = (
                    "Job exceeded timeout of"
                    f" {task_snapshot.timeout_seconds}s"
                )
            self.persistence.finalize_job(
                job_id,
                status,
                duration_seconds=duration.total_seconds(),
                error_message=error_message,
            )
            will_retry = self.persistence.finalize_task_after_job(
                task_id, start_time, job_failed
            )
            with self._job_count_lock:
                self._active_job_count -= 1
            # A freed slot lets deferred-due tasks dispatch, and the task's
            # freshly computed next_run_at may precede the loop's sleep.
            self._wake_loop()
            if run_once and not will_retry:
                with self._registries_lock:
                    self.registry.pop(task_id, None)
                    self.progress_callbacks.pop(task_id, None)

            finalized_job = self.get_job(job_id)
            event_map = {
                JobStatus.COMPLETED: Event.JOB_COMPLETED,
                JobStatus.FAILED: Event.JOB_FAILED,
                JobStatus.CANCELLED: Event.JOB_CANCELLED,
            }
            if status in event_map:
                self._emit_event(
                    event_map[status], task_snapshot, finalized_job
                )
            if will_retry:
                self._emit_event(
                    Event.JOB_RETRYING, task_snapshot, finalized_job
                )
