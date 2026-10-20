from __future__ import annotations

import asyncio
from datetime import timedelta, datetime, timezone, tzinfo
import importlib
import logging
import math
from multiprocessing.connection import wait
import pickle
import threading
import time
from typing import TYPE_CHECKING, Any, Callable

from . import _worker
from .base import QuivBase
from .config import QuivConfig
from .context import _current_job_id, _current_quiv, run_on_main
from .exceptions import (
    ConfigurationError,
    HandlerRegistrationError,
    JobCancelledError,
    TaskNotActiveError,
    TaskNotFoundError,
)
from .models import (
    Event,
    Executor,
    Job,
    JobStatus,
    Task,
    TaskDB,
    TaskStatus,
    seconds_after,
)

if TYPE_CHECKING:
    # Annotations only; see the note on this import in base.py.
    from multiprocessing.synchronize import Event as ProcessEvent

_CLEANUP_INTERVAL_SECONDS = 60.0
_MIN_SLEEP_SECONDS = 0.01  # floor: never busy-spin
_MAX_SLEEP_SECONDS = 60.0  # ceiling: bounded staleness safety net
# How often the parent-side waiter of a process job looks at the stop
# event and the kill timer while it waits for the child.
_PROCESS_POLL_SECONDS = 0.05


class _ChildFailure(Exception):
    """A process job's handler raised, or its process died.

    ``str(e)`` is the child's ``str(exc)``, so ``error_message`` reads the
    same for both pools: the exception's message, not its type. The type
    is in ``traceback_text``, which goes to the log.
    """

    def __init__(
        self,
        message: str,
        traceback_text: str | None = None,
        cancelled: bool = False,
    ):
        super().__init__(message)
        self.traceback_text = traceback_text
        # The handler raised JobCancelledError: a quiv helper unwound it
        # because the stop event was set. The same clean stop as in a
        # thread job, once the parent confirms the stop was requested.
        self.cancelled = cancelled


class _Unset:
    """Sentinel for update_task parameters that were not passed."""

    def __repr__(self) -> str:  # pragma: no cover
        return "<UNSET>"


_UNSET = _Unset()


def _error_text(exc: BaseException) -> str:
    """Describe an exception for a log line or a job's ``error_message``.

    ``str()`` of ``CancelledError`` or ``KeyboardInterrupt`` is empty, so
    an exception that is not an ``Exception`` gets ``repr()``, which names
    the class.
    """

    return str(exc) if isinstance(exc, Exception) else repr(exc)


def _validate_task_name(value: str) -> None:
    if not value.strip():
        raise ConfigurationError("task_name must not be empty")


# Each numeric check below tests math.isnan first. NaN compares false with
# every number, so ``value <= 0`` alone let it through: a NaN interval then
# raised ValueError in finalize_task_after_job and left the task RUNNING.


def _validate_interval(value: float | None) -> None:
    # ``None`` is rejected here rather than by the caller so every entry
    # point reports a ConfigurationError instead of a TypeError from an
    # unguarded comparison.
    if value is None or math.isnan(value) or value <= 0:
        raise ConfigurationError("interval must be greater than 0")


def _validate_delay(value: float) -> None:
    if math.isnan(value) or value < 0:
        raise ConfigurationError("delay must be greater than or equal to 0")


def _to_utc(value: datetime) -> datetime:
    """Read a naive datetime as UTC, and convert an aware one to UTC.

    Naive input is read as UTC, not as the display timezone. The
    ``timezone`` setting only formats log output; every stored time is
    UTC.
    """

    if not isinstance(value, datetime):
        raise ConfigurationError("run_at must be a datetime")
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _validate_timeout(value: float | None) -> None:
    if value is not None and (math.isnan(value) or value <= 0):
        raise ConfigurationError("timeout must be greater than 0")


def _validate_max_retries(value: int) -> None:
    if value < 0:
        raise ConfigurationError(
            "max_retries must be greater than or equal to 0"
        )


def _validate_retry_backoff(value: float) -> None:
    if math.isnan(value) or value <= 0:
        raise ConfigurationError("retry_backoff must be greater than 0")


def _validate_jitter(value: float) -> None:
    if math.isnan(value) or value < 0:
        raise ConfigurationError("jitter must be greater than or equal to 0")


def _validate_args_type(value: tuple[Any, ...]) -> None:
    if not isinstance(value, tuple):
        raise ConfigurationError(
            f"args must be a tuple, got {type(value).__name__}"
        )


def _validate_kwargs_type(value: dict[str, Any]) -> None:
    if not isinstance(value, dict):
        raise ConfigurationError(
            f"kwargs must be a dict, got {type(value).__name__}"
        )


def _validate_no_legacy_params(legacy: frozenset[str]) -> None:
    # A handler written for 0.x keeps its default instead of being injected,
    # so cooperative cancellation and timeouts would stop working with no
    # error. Fail at registration, before the task row exists.
    if not legacy:
        return
    renames = ", ".join(f"'{name}' -> '{name[1:]}'" for name in sorted(legacy))
    raise ConfigurationError(
        f"Handler declares parameters that quiv renamed in 1.0.0: {renames}. "
        "Rename them in the handler signature. The behavior of each is "
        "unchanged."
    )


def _validate_no_injectable_clash(
    kwargs: dict[str, Any], injectable: frozenset[str]
) -> None:
    # An injected value overwrites a caller value of the same name, so the
    # caller would silently lose it. quiv cannot tell which value was meant.
    clash = sorted(injectable & kwargs.keys())
    if not clash:
        return
    names = ", ".join(f"'{name}'" for name in clash)
    raise ConfigurationError(
        f"kwargs keys {names} collide with the parameters quiv injects into "
        "this handler. Rename the keys, or remove the parameters from the "
        "handler signature."
    )


def _pickle_or_raise(value: Any, label: str) -> bytes:
    try:
        return pickle.dumps(value)
    except Exception as e:
        raise ConfigurationError(
            f"Failed to serialize task {label}: {e}"
        ) from e


def _resolve_executor(value: Executor | str) -> Executor:
    try:
        return Executor(value)
    except ValueError:
        raise ConfigurationError(
            "executor must be 'thread' or 'process'"
        ) from None


def _validate_pool_available(
    executor: Executor, pool_size: int, process_pool_size: int
) -> None:
    if executor is Executor.THREAD and pool_size == 0:
        raise ConfigurationError("executor='thread' requires pool_size > 0")
    if executor is Executor.PROCESS and process_pool_size == 0:
        raise ConfigurationError(
            "executor='process' requires process_pool_size > 0"
        )


_PROCESS_HANDLER_HELP = (
    "executor='process' requires a handler that a new process can import"
    " by name: a module-level function or a staticmethod. A lambda, an"
    " inner function, a bound method, a functools.partial object, or a"
    " callable instance cannot be sent to a process."
)


def _resolve_handler_reference(func: Callable[..., Any]) -> tuple[str, str]:
    """Return ``(module, qualname)`` for a handler a child can import.

    The ``is not func`` check is what rejects bound methods and
    classmethods: the walk yields the plain function or a fresh
    bound-method object, never the object the caller passed. A
    staticmethod reached through its class yields the function itself and
    passes.
    """

    module_name = getattr(func, "__module__", None)
    qualname = getattr(func, "__qualname__", None)
    if (
        not isinstance(module_name, str)
        or not isinstance(qualname, str)
        or "<" in qualname  # "<lambda>" and "<locals>"
    ):
        raise ConfigurationError(_PROCESS_HANDLER_HELP)
    try:
        target: Any = importlib.import_module(module_name)
        for part in qualname.split("."):
            target = getattr(target, part)
    except (ImportError, AttributeError) as e:
        raise ConfigurationError(
            f"{_PROCESS_HANDLER_HELP} Cannot import"
            f" '{module_name}:{qualname}': {e}"
        ) from e
    if target is not func:
        raise ConfigurationError(_PROCESS_HANDLER_HELP)
    return module_name, qualname


class Quiv(QuivBase):
    """Public scheduler API and orchestration loop implementation."""

    def __init__(
        self,
        config: QuivConfig | None = None,
        pool_size: int = 10,
        history_retention_seconds: int = 86400,
        timezone: str | tzinfo = "UTC",
        *,
        process_pool_size: int = 0,
        process_kill_grace: float = 5.0,
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
            process_pool_size (int, Optional=0): Keyword-only. Maximum
                process jobs at once when ``config`` is not provided; 0
                means no process pool.
            process_kill_grace (float, Optional=5.0): Keyword-only.
                Seconds a process job may ignore its stop event before
                quiv terminates the process, when ``config`` is not
                provided.
            logger (logging.Logger | logging.LoggerAdapter[Any], Optional=None): Optional logger instance.
            main_loop (asyncio.AbstractEventLoop, Optional=None): Optional main event loop for progress callbacks.
        """

        super().__init__(
            config=config,
            pool_size=pool_size,
            history_retention_seconds=history_retention_seconds,
            timezone=timezone,
            process_pool_size=process_pool_size,
            process_kill_grace=process_kill_grace,
            logger=logger,
            main_loop=main_loop,
        )

    def add_task(
        self,
        task_name: str,
        func: Callable[..., Any],
        interval: float | None = None,
        delay: float | None = None,
        run_once: bool = False,
        fixed_interval: bool = True,
        args: tuple[Any, ...] | None = None,
        kwargs: dict[str, Any] | None = None,
        progress_callback: Callable[..., Any] | None = None,
        *,
        run_at: datetime | None = None,
        timeout: float | None = None,
        max_retries: int = 0,
        retry_backoff: float = 30.0,
        jitter: float = 0.0,
        executor: Executor | str | None = None,
    ) -> str:
        """Schedule a callable to run at a fixed interval.

        Args:
            task_name (str): Display name for this task.
            func (Callable[..., Any]): Function to execute as task (sync/async).
            interval (float, Optional=None): Interval in seconds
                between runs. Required unless ``run_once=True``. A
                run-once task never repeats, so an interval given with
                ``run_once=True`` is ignored and stored as ``None``.
            delay (float, Optional=None): Initial delay before first run
                in seconds. Defaults to no delay. Mutually exclusive
                with ``run_at``.
            run_once (bool, Optional=False): If ``True``, run task once and remove it.
            fixed_interval (bool, Optional=True): If ``True``, next run is
                scheduled at fixed intervals from the job start time. If
                ``False``, next run is scheduled ``interval`` seconds after
                job completion.
            args (tuple[Any, ...], Optional=None): Positional arguments for handler.
            kwargs (dict[str, Any], Optional=None): Keyword arguments for handler.
            progress_callback (Callable[..., Any], Optional=None): Optional progress callback executed on main loop.
            run_at (datetime, Optional=None): Keyword-only. Absolute time
                of the first run, as an alternative to ``delay``. A naive
                datetime is read as UTC. A time already past runs at
                once, because the process may have been down when the
                time came due. Passing both ``run_at`` and ``delay``
                raises ``ConfigurationError``.
            timeout (float, Optional=None): Keyword-only. Cooperative
                per-job timeout in seconds. When a job exceeds it, quiv
                sets the job's stop event — exactly as ``cancel_job()``
                would — and the job finalizes as ``cancelled`` with a
                timeout error message. Handlers that ignore their stop
                event keep occupying their pool thread (quiv never kills
                threads). ``None`` disables.
            max_retries (int, Optional=0): Keyword-only. Maximum
                consecutive retries after failed jobs. Applies to
                ``failed`` jobs only — cancelled jobs (including
                timeouts) never retry.
            retry_backoff (float, Optional=30.0): Keyword-only. Base
                delay in seconds for exponential retry backoff: first
                retry after ``retry_backoff``, then 2x, 4x, and so on.
            jitter (float, Optional=0.0): Keyword-only. Adds
                ``uniform(0, jitter)`` seconds to each recurring next-run
                time to de-synchronize tasks sharing interval boundaries.
                Not applied to the initial ``delay`` nor to retry backoff.
            executor (Executor | str, Optional=None): Keyword-only. Which
                pool runs this task's jobs: ``"thread"`` or
                ``"process"``. ``None`` picks thread when the thread pool
                has a size above 0, else process. A process job runs in a
                new process that quiv spawns for that job alone and can
                terminate; its handler must be importable by name (a
                module-level function or a staticmethod), and its
                arguments are unpickled in that process.

        Raises:
            ConfigurationError: If scheduling parameters are invalid, if
                ``func`` declares a pre-1.0 injected parameter
                (``_job_id``, ``_stop_event``, ``_progress_hook``), if
                a key in ``kwargs`` collides with a parameter that quiv
                injects into ``func``, if ``executor`` names a pool of
                size 0, or if ``executor="process"`` and ``func`` cannot
                be imported by name in a new process.
            HandlerRegistrationError: If ``func`` or ``progress_callback``
                is not callable.

        Returns:
            str: Task id string (UUID).
        """

        self._ensure_live()
        _validate_task_name(task_name)
        if run_once:
            # A run-once task is deleted when it finishes, so its
            # interval is never read. Accept any value, keep none.
            interval = None
        else:
            _validate_interval(interval)
        if run_at is not None and delay is not None:
            raise ConfigurationError(
                "run_at and delay are mutually exclusive; pass one"
            )
        if delay is not None:
            _validate_delay(delay)
        _validate_timeout(timeout)
        _validate_max_retries(max_retries)
        _validate_retry_backoff(retry_backoff)
        _validate_jitter(jitter)
        # Validate registration inputs BEFORE persisting the task row —
        # failing later in _register_handler/_register_progress_callback
        # would leave an orphaned ACTIVE row that can never dispatch.
        if not callable(func):
            raise HandlerRegistrationError("func must be callable")
        _validate_no_legacy_params(self.execution._find_legacy_params(func))
        if progress_callback is not None and not callable(progress_callback):
            raise HandlerRegistrationError(
                "progress callback must be callable"
            )
        resolved_executor = (
            self._default_executor
            if executor is None
            else _resolve_executor(executor)
        )
        _validate_pool_available(
            resolved_executor, self._pool_size, self._process_pool_size
        )
        if resolved_executor is Executor.PROCESS:
            _resolve_handler_reference(func)

        resolved_args = args if args is not None else ()
        resolved_kwargs = kwargs if kwargs is not None else {}

        _validate_args_type(resolved_args)
        _validate_kwargs_type(resolved_kwargs)
        _validate_no_injectable_clash(
            resolved_kwargs, self.execution._get_injectable_params(func)
        )
        args_pickled = _pickle_or_raise(resolved_args, "args")
        kwargs_pickled = _pickle_or_raise(resolved_kwargs, "kwargs")

        now = self._now_utc()
        if run_at is not None:
            # A time already past runs at once rather than raising: the
            # process may have been down when it came due, and dropping
            # the task is worse than running it late.
            next_run = max(_to_utc(run_at), now)
        else:
            next_run = seconds_after(now, delay or 0)
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
            executor=resolved_executor.value,
        )

        self._register_handler(task_id, func)
        self._register_progress_callback(task_id, progress_callback)

        next_run_user_tz = self._to_display_timezone(next_run)
        schedule = (
            "as run-once" if run_once else f"with interval {interval}s"
        )
        where = (
            " in a new process"
            if resolved_executor is Executor.PROCESS
            else ""
        )
        self._logger.info(
            f"Task '{task_name}' added {schedule}{where}"
            f" (next run at {next_run_user_tz})"
        )
        task = self.get_task(task_id)
        self._emit_event(Event.TASK_ADDED, task)
        self._wake_loop()
        return task_id

    def update_task(
        self,
        task_id: str,
        *,
        task_name: str | _Unset = _UNSET,
        interval: float | _Unset = _UNSET,
        fixed_interval: bool | _Unset = _UNSET,
        args: tuple[Any, ...] | _Unset = _UNSET,
        kwargs: dict[str, Any] | _Unset = _UNSET,
        timeout: float | None | _Unset = _UNSET,
        max_retries: int | _Unset = _UNSET,
        retry_backoff: float | _Unset = _UNSET,
        jitter: float | _Unset = _UNSET,
        progress_callback: Callable[..., Any] | None | _Unset = _UNSET,
        run_at: datetime | _Unset = _UNSET,
        executor: Executor | str | _Unset = _UNSET,
    ) -> Task:
        """Mutate a scheduled task in place, preserving its ``task_id``.

        Only the parameters you pass change; everything else is
        untouched. If ``interval`` is changed, the next run is
        rescheduled to ``now + interval``; pass ``run_at`` instead to
        name that time yourself. Updating a ``RUNNING`` task is allowed
        — the changes take effect from the next run. Emits
        ``Event.TASK_UPDATED`` with the post-update :class:`Task`.

        ``run_at`` moves the task's **next scheduled run**, whether or
        not it has run before. A recurring task keeps its interval and
        simply runs next at the time given; the interval governs
        everything after that. A one-off alarm changes its time without
        being removed and added again, which would hand back a new
        ``task_id`` and leave a window with no task scheduled at all.

        **``run_at`` does not survive a task that is already RUNNING.**
        Finalizing a job rewrites the schedule: a run-once task has its
        row deleted, and a recurring task has ``next_run_at`` recomputed
        from its interval. Either way the new time is discarded when the
        job finishes, so the call looks applied and then is not. quiv
        logs a warning when it sees this, but the check is best-effort:
        a task can enter RUNNING between the write and the check. To
        move a recurring task reliably, wait for the job to finish, or
        change ``interval`` instead, which finalization does honour.

        Not updatable: ``run_once``, ``delay`` (initial delay is a
        creation-time concept), and the handler ``func`` (remove and
        re-add the task to change its handler).

        Args:
            task_id (str): Task identifier (UUID string).
            task_name (str, Optional): New display name.
            interval (float, Optional): New interval in seconds; also
                reschedules the next run to ``now + interval``.
            fixed_interval (bool, Optional): New scheduling mode.
            args (tuple[Any, ...], Optional): New positional arguments.
            kwargs (dict[str, Any], Optional): New keyword arguments.
            timeout (float | None, Optional): New cooperative timeout;
                ``None`` disables timeout enforcement.
            max_retries (int, Optional): New retry limit.
            retry_backoff (float, Optional): New base backoff delay.
            jitter (float, Optional): New jitter bound.
            progress_callback (Callable | None, Optional): New progress
                callback; ``None`` clears the existing one.
            run_at (datetime, Optional): Absolute time of the next run.
                Naive input is read as UTC, never as the display
                timezone. A time already past runs at once, matching
                ``add_task``. Mutually exclusive with ``interval``, and
                discarded if the task is already RUNNING (see above).
            executor (Executor | str, Optional): Which pool runs the
                task's jobs from the next dispatch on; a running job
                finishes where it started. ``"process"`` requires a
                handler that a new process can import by name.

        Raises:
            TaskNotFoundError: If no task with that id exists.
            ConfigurationError: If a provided value is invalid (same
                rules as ``add_task``), if a key in ``kwargs`` collides
                with a parameter that quiv injects into the registered
                handler, if ``run_at`` and ``interval`` are both
                passed, or if ``executor`` names a pool of size 0 or a
                process for a handler that cannot be imported by name.
            HandlerRegistrationError: If ``progress_callback`` is not
                callable.

        Returns:
            Task: The post-update task record.
        """

        self._ensure_live()
        if not isinstance(run_at, _Unset) and not isinstance(interval, _Unset):
            # persistence.update_task recomputes next_run_at from the new
            # interval, so it would silently overwrite this run_at. Refuse
            # rather than pick one, the same rule add_task applies to
            # run_at and delay.
            raise ConfigurationError(
                "run_at and interval are mutually exclusive; changing the"
                " interval already reschedules the next run"
            )

        updates: dict[str, Any] = {}
        if not isinstance(task_name, _Unset):
            _validate_task_name(task_name)
            updates["task_name"] = task_name
        if not isinstance(interval, _Unset):
            _validate_interval(interval)
            updates["interval_seconds"] = interval
        if not isinstance(run_at, _Unset):
            # A time already past runs at once rather than raising, the
            # same reason as in add_task: the process may have been down
            # when it came due, and dropping the run is worse than
            # running it late.
            updates["next_run_at"] = max(_to_utc(run_at), self._now_utc())
        if not isinstance(fixed_interval, _Unset):
            updates["fixed_interval"] = fixed_interval
        if not isinstance(args, _Unset):
            _validate_args_type(args)
            updates["args"] = _pickle_or_raise(args, "args")
        if not isinstance(kwargs, _Unset):
            _validate_kwargs_type(kwargs)
            # func is not updatable, so the registered handler decides which
            # names are injected. A handler missing from the registry is left
            # to the persistence call below, which reports the unknown id.
            with self._registries_lock:
                handler = self.registry.get(task_id)
            if handler is not None:
                _validate_no_injectable_clash(
                    kwargs, self.execution._get_injectable_params(handler)
                )
            updates["kwargs"] = _pickle_or_raise(kwargs, "kwargs")
        if not isinstance(timeout, _Unset):
            _validate_timeout(timeout)
            updates["timeout_seconds"] = timeout
        if not isinstance(max_retries, _Unset):
            _validate_max_retries(max_retries)
            updates["max_retries"] = max_retries
        if not isinstance(retry_backoff, _Unset):
            _validate_retry_backoff(retry_backoff)
            updates["retry_backoff_seconds"] = retry_backoff
        if not isinstance(jitter, _Unset):
            _validate_jitter(jitter)
            updates["jitter_seconds"] = jitter
        if not isinstance(executor, _Unset):
            resolved_executor = _resolve_executor(executor)
            _validate_pool_available(
                resolved_executor, self._pool_size, self._process_pool_size
            )
            if resolved_executor is Executor.PROCESS:
                # Must fail here, not at dispatch: a lambda handler moved
                # to the process pool would otherwise fail every job.
                with self._registries_lock:
                    handler = self.registry.get(task_id)
                if handler is not None:
                    _resolve_handler_reference(handler)
            updates["executor"] = resolved_executor.value
        if (
            not isinstance(progress_callback, _Unset)
            and progress_callback is not None
            and not callable(progress_callback)
        ):
            raise HandlerRegistrationError(
                "progress callback must be callable"
            )

        if updates:
            self.persistence.update_task(task_id, **updates)
        else:
            # Nothing to write — still surface unknown ids.
            self.persistence.get_task(task_id)
        if not isinstance(progress_callback, _Unset):
            self._register_progress_callback(task_id, progress_callback)

        self._wake_loop()
        task = self.get_task(task_id)
        if (
            not isinstance(run_at, _Unset)
            and task.status == TaskStatus.RUNNING
        ):
            # Finalizing this job rewrites the schedule — the row is
            # deleted for a run-once task, and next_run_at is recomputed
            # from the interval for a recurring one — so the time just
            # written is about to be discarded. Best-effort: the task can
            # enter RUNNING after this check, and then there is nothing
            # to warn from.
            self._logger.warning(
                f"Task '{task_id}' is running, so the run_at just set will"
                " be overwritten when its job finishes. Move it once the"
                " job has completed."
            )
        updated = ", ".join(updates) if updates else "progress_callback"
        self._logger.info(f"Task '{task_id}' updated ({updated})")
        self._emit_event(Event.TASK_UPDATED, task)
        return task

    def remove_task(self, task_id: str) -> None:
        """Remove a scheduled task and its handler/callback registrations.

        If the task has a job in flight, scheduled or running, its stop
        event is set to signal cancellation. That job finishes on its
        own and cleans up via ``_run_job``'s finally block, and it
        resolves the task's waiters. With nothing in flight the waiters
        learn the task is gone.

        Args:
            task_id (str): Task id to remove.

        Raises:
            TaskNotFoundError: If no task with that id exists.
        """

        self._ensure_live()
        # Snapshot the task before deletion for the event listener
        task = self.get_task(task_id)

        # Signal every job of this task that is in flight. SCHEDULED
        # counts: _dispatch_due_task created and submitted it, and its
        # stop event already exists, so the job sees the cancellation
        # the moment _run_job starts it.
        in_flight = False
        for job_status in (JobStatus.SCHEDULED, JobStatus.RUNNING):
            for job in self.get_all_jobs(status=job_status, task_id=task_id):
                if job.id is None:  # pragma: no cover - a stored job has an id
                    continue
                in_flight = True
                with self._registries_lock:
                    stop_event = self.stop_events.get(job.id)
                if stop_event is not None:
                    stop_event.set()
                    self._logger.info(
                        f"Cancelled running job {job.id} for task '{task_id}'"
                    )

        # The delete and the dispatch's mark-running serialize on the
        # persistence write lock, so the status the row had at deletion
        # is exact. RUNNING means a dispatch marked it and will either
        # create the job, which resolves the waiters, or find the row
        # gone and fail them itself in _dispatch_due_task.
        if self.persistence.delete_task(task_id) == TaskStatus.RUNNING:
            in_flight = True
        with self._registries_lock:
            self.registry.pop(task_id, None)
            self.progress_callbacks.pop(task_id, None)
        if not in_flight:
            # A job in flight finalizes on its own and resolves the task's
            # waiters with that job. With nothing in flight, no job will
            # ever come, so the waiters learn the task is gone.
            self._fail_waiters(
                self._task_waiters,
                task_id,
                TaskNotFoundError(
                    f"Task '{task_id}' was removed before a job of it ran."
                ),
            )
        self._logger.info(f"Task '{task_id}' removed")
        self._emit_event(Event.TASK_REMOVED, task)
        self._wake_loop()

    def _loop(self) -> None:
        """Continuously dispatch due tasks until shutdown is requested.

        Sleeps until the next due task (or the next history-cleanup
        deadline) on an interruptible wait; mutating API calls wake the
        loop early via ``_wake_loop()``.
        """

        # Unreachable as written: __init__ sets _initialized as its last
        # statement, and the thread only starts in start(), which a caller
        # reaches after __init__ returns. Kept as a guard in case that
        # ordering ever changes.
        while not getattr(self, "_initialized", False):  # pragma: no cover
            time.sleep(0.1)  # pragma: no cover

        self._logger.info("Scheduler loop starting")
        next_cleanup = time.monotonic()  # run cleanup on first iteration
        while not self._shutdown:
            try:
                if time.monotonic() >= next_cleanup:
                    # Advance the deadline first and catch here: a failing
                    # cleanup must cost one cleanup, not every dispatch
                    # until the process restarts.
                    next_cleanup = (
                        time.monotonic() + _CLEANUP_INTERVAL_SECONDS
                    )
                    try:
                        self.persistence.cleanup_history(self.history_limit)
                    except Exception as e:
                        self._logger.error(f"Job history cleanup failed: {e}")

                self._enforce_timeouts()

                now = self._now_utc()
                if self._pool_has_slot(Executor.THREAD) or self._pool_has_slot(
                    Executor.PROCESS
                ):
                    for task in self.persistence.get_due_tasks(now):
                        # Backpressure is per pool. A full thread pool
                        # must not hold back a due process task, so a
                        # task whose pool is full is skipped, not the
                        # rest of the batch.
                        if not self._pool_has_slot(task.executor):
                            continue
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
        # When a pool is saturated, skip its next-due candidate: an overdue
        # task of that pool cannot dispatch anyway, and clamping its
        # negative delta to the sleep floor would busy-poll the DB at
        # ~100 Hz for the whole saturation window. A finishing job wakes
        # the loop. Per pool, so a full thread pool with an overdue thread
        # task does not busy-poll while the process pool is free.
        open_pools = [
            executor
            for executor in (Executor.THREAD, Executor.PROCESS)
            if self._pool_has_slot(executor)
        ]
        if len(open_pools) == 2:
            next_due = self.persistence.get_next_due_time()
        elif len(open_pools) == 1:
            next_due = self.persistence.get_next_due_time(
                executor=open_pools[0].value
            )
        else:
            next_due = None
        if next_due is not None:
            candidates.append((next_due - self._now_utc()).total_seconds())
        return max(_MIN_SLEEP_SECONDS, min(min(candidates), _MAX_SLEEP_SECONDS))

    def _pool_has_slot(self, executor: str) -> bool:
        """Whether the pool named by ``executor`` can take one more job."""

        with self._job_count_lock:
            if executor == Executor.PROCESS:
                return self._active_process_count < self._process_pool_size
            return self._active_job_count < self._pool_size

    def _dispatch_due_task(self, task: TaskDB, now: datetime) -> None:
        """Create and dispatch execution for a due task.

        Args:
            task (TaskDB): Task record due for execution.
            now (datetime): Current UTC timestamp.
        """

        with self._registries_lock:
            func = self.registry.get(task.id)
            if func is not None:
                # Before the mark, so that resume_task() sees the job for
                # its whole life. _run_job discards it after the task is
                # finalized.
                self._tasks_in_flight.add(task.id)
        if func is None:
            # Task was removed between the due-query and dispatch.
            self._logger.warning(
                f"Skipping dispatch for task '{task.id}': handler no longer"
                " registered (task was likely removed)."
            )
            return

        marked = False
        try:
            self.persistence.mark_task_running(task.id)
            marked = True
            # Snapshot task for event listeners while it is guaranteed to
            # exist. The task row may be deleted by finalize_task_after_job
            # (run-once) or by remove_task() before _run_job emits its events.
            task_snapshot = self.get_task(task.id)
            job_id = self.persistence.create_job(
                task.id,
                task.task_name,
                attempt=task.retry_attempt + 1,
                executor=task.executor,
            )
        except TaskNotFoundError:
            with self._registries_lock:
                self._tasks_in_flight.discard(task.id)
            self._logger.warning(
                f"Skipping dispatch for task '{task.id}': task row was"
                " deleted before dispatch."
            )
            self._fail_removed_task_waiters(task.id)
            return
        except TaskNotActiveError as e:
            # pause_task() landed after the due query. The pause wins:
            # nothing was marked, and no job starts.
            with self._registries_lock:
                self._tasks_in_flight.discard(task.id)
            self._logger.info(f"Skipping dispatch for task '{task.id}': {e}")
            return
        except Exception:
            with self._registries_lock:
                self._tasks_in_flight.discard(task.id)
            if marked:
                # No job exists to finalize the task. Left RUNNING, it
                # never comes back from get_due_tasks; ACTIVE and still
                # due, the next pass dispatches it again. Conditional, so
                # a pause that landed after the mark is not undone.
                try:
                    self.persistence.unmark_task_running(task.id)
                except TaskNotFoundError:
                    self._fail_removed_task_waiters(task.id)
                except Exception as e:
                    self._logger.error(
                        f"Task '{task.id}' could not be returned to the"
                        f" schedule after a failed dispatch: {e}"
                    )
            raise

        is_process = task.executor == Executor.PROCESS
        stop_event: threading.Event | ProcessEvent = (
            self._mp_context.Event() if is_process else threading.Event()
        )
        with self._registries_lock:
            self.stop_events[job_id] = stop_event
            if task.timeout_seconds is not None:
                self._job_deadlines[job_id] = (
                    time.monotonic() + task.timeout_seconds
                )

        if is_process:
            # prepare_invocation is not called here. The child does it
            # after its worker flag is set, so the argument classes are
            # imported there, and a module-level Quiv() in the
            # application is inert when that import runs.
            self._logger.info(
                f"Scheduling task '{task.task_name}' (Job ID: {job_id}) to"
                " run now in a new process"
            )
            with self._job_count_lock:
                self._active_process_count += 1
            self._process_waiters.submit(
                self._run_process_job,
                job_id,
                task.id,
                task.task_name,
                task.run_once,
                now,
                task_snapshot,
                func,
                task.args,
                task.kwargs,
                stop_event,
            )
            return

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

    def _fail_removed_task_waiters(self, task_id: str) -> None:
        """Tell a task's waiters that its row went before a job started.

        When ``remove_task()`` deletes the row after a dispatch marked it
        RUNNING, it leaves the task's waiters for the job it expected. No
        job comes from that dispatch, so the dispatch tells them.
        """

        self._fail_waiters(
            self._task_waiters,
            task_id,
            TaskNotFoundError(
                f"Task '{task_id}' was removed before its job started."
            ),
        )

    def _stop_requested(self, job_id: str) -> bool:
        """Whether the job's stop event is set."""

        with self._registries_lock:
            stop_event = self.stop_events.get(job_id)
        return stop_event is not None and stop_event.is_set()

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
        """Execute a single thread job and persist terminal status.

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

        def run() -> str | None:
            self.execution.run_callable(func, args, kwargs)
            return None

        self._execute_job(
            job_id,
            task_id,
            task_name,
            run_once,
            scheduled_at,
            task_snapshot,
            Executor.THREAD,
            run,
        )

    def _run_process_job(
        self,
        job_id: str,
        task_id: str,
        task_name: str,
        run_once: bool,
        scheduled_at: datetime,
        task_snapshot: Any,
        func: Callable[..., Any],
        args_pickled: bytes,
        kwargs_pickled: bytes,
        stop_event: Any,
    ) -> None:
        """Run a single job in a spawned process and persist terminal status.

        Runs on a thread of ``_process_waiters``: the parent-side half of
        the job. Same arguments as ``_run_job``, except that the handler
        arguments stay pickled, because the child unpickles them.
        """

        def run() -> str | None:
            return self._run_in_child(
                job_id, task_id, func, args_pickled, kwargs_pickled, stop_event
            )

        self._execute_job(
            job_id,
            task_id,
            task_name,
            run_once,
            scheduled_at,
            task_snapshot,
            Executor.PROCESS,
            run,
        )

    def _execute_job(
        self,
        job_id: str,
        task_id: str,
        task_name: str,
        run_once: bool,
        scheduled_at: datetime,
        task_snapshot: Any,
        executor: Executor,
        run: Callable[[], str | None],
    ) -> None:
        """Run one job through ``run`` and finalize job and task state.

        Args:
            job_id (str): Job identifier (UUID string).
            task_id (str): Source task identifier.
            task_name (str): Task name for logging.
            run_once (bool): Whether the task is single-run.
            scheduled_at (datetime): UTC time when the job was dispatched.
            task_snapshot (Task): Pre-fetched task snapshot for event listeners.
            executor (Executor): Which pool the job occupies.
            run (Callable[[], str | None]): Does the work. Returns a note
                for ``error_message``, or ``None``. Raises
                ``_ChildFailure`` when a process job's handler raised or
                its process died.
        """

        start_time = self._now_utc()
        delay = start_time - scheduled_at
        if delay.total_seconds() > 2:
            busy = (
                "process pool was busy. Consider increasing"
                " process_pool_size."
                if executor is Executor.PROCESS
                else "threadpool was busy. Consider increasing pool_size."
            )
            self._logger.warning(
                f"'{task_name}' (Job {job_id}) started {delay} after scheduled"
                f" time — {busy}"
            )
        self._logger.info(
            f"'{task_name}' (Job {job_id}) started at"
            f" {self._to_display_timezone(start_time)}"
        )

        status = JobStatus.COMPLETED
        job_error: BaseException | None = None
        note: str | None = None
        duration = timedelta()
        handler_started = False
        ctx_token = _current_quiv.set(self)
        job_token = _current_job_id.set(job_id)
        try:
            # Inside the try, so that an error here (a locked or full temp
            # database) finalizes the job as failed and returns the task to
            # ACTIVE. Outside it, the task stayed RUNNING for the life of
            # the process and the job kept its pool slot.
            self.persistence.mark_job_running(job_id)
            started_job = self.get_job(job_id)
            self._emit_event(Event.JOB_STARTED, task_snapshot, started_job)

            handler_started = True
            note = run()
            end_time = self._now_utc()
            duration = end_time - start_time
            if note is None:
                self._logger.info(
                    f"'{task_name}' (Job {job_id}) completed successfully at"
                    f" {self._to_display_timezone(end_time)}"
                    f" (Duration: {duration})"
                )
            else:
                # quiv terminated the process. The stop event is set, so
                # the finally below finalizes as CANCELLED.
                self._logger.info(
                    f"'{task_name}' (Job {job_id}) stopped at"
                    f" {self._to_display_timezone(end_time)}"
                    f" (Duration: {duration}): {note}"
                )
        except _ChildFailure as e:
            end_time = self._now_utc()
            duration = end_time - start_time
            if e.cancelled and self._stop_requested(job_id):
                # The same clean stop as the JobCancelledError branch
                # below, reported by the child instead of raised here.
                self._logger.info(
                    f"'{task_name}' (Job {job_id}) stopped at"
                    f" {self._to_display_timezone(end_time)}"
                    f" (Duration: {duration})"
                )
            else:
                job_error = e
                # Not logger.exception: the traceback on this side is the
                # waiter's, not the handler's. The child's text is what
                # the log needs.
                detail = f"\n{e.traceback_text}" if e.traceback_text else ""
                self._logger.error(
                    f"'{task_name}' (Job {job_id}) failed in its process at"
                    f" {self._to_display_timezone(end_time)}"
                    f" [runtime: {duration}]: {e}{detail}"
                )
                status = JobStatus.FAILED
        except BaseException as e:
            # BaseException, not Exception: asyncio.run() raises
            # CancelledError when the handler's main task is cancelled, and
            # the finally below then recorded the job as COMPLETED.
            end_time = self._now_utc()
            duration = end_time - start_time
            if not handler_started:
                job_error = e
                self._logger.exception(
                    f"'{task_name}' (Job {job_id}) could not start: {e}"
                )
                status = JobStatus.FAILED
            elif isinstance(e, JobCancelledError) and self._stop_requested(
                job_id
            ):
                # A quiv helper (call_on_main, run_subprocess) unwound the
                # handler because the job's stop event was set. That is
                # the cancellation the caller asked for, not a failure:
                # no traceback, no error message. The finally block below
                # sees the stop event and finalizes as CANCELLED.
                self._logger.info(
                    f"'{task_name}' (Job {job_id}) stopped at"
                    f" {self._to_display_timezone(end_time)}"
                    f" (Duration: {duration})"
                )
            else:
                job_error = e
                self._logger.exception(
                    f"'{task_name}' (Job {job_id}) raised an exception at"
                    f" {self._to_display_timezone(end_time)}"
                    f" [runtime: {duration}]: {_error_text(e)}"
                )
                status = JobStatus.FAILED
            if isinstance(e, (KeyboardInterrupt, SystemExit)):
                # Recorded as failed, and finalized by the finally below,
                # but not swallowed: these two ask the process to stop.
                raise
        finally:
            _current_job_id.reset(job_token)
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

            # run() returns a note only when it did not raise, so at most
            # one of the two is set.
            error_message = (
                _error_text(job_error) if job_error is not None else note
            )
            if timed_out and status == JobStatus.CANCELLED:
                # The timeout message always leads so timed-out jobs stay
                # distinguishable from manual cancellations and failures,
                # even when the handler also raised or the process was
                # terminated.
                timeout_message = (
                    "Job exceeded timeout of"
                    f" {task_snapshot.timeout_seconds}s"
                )
                timeout_detail = (
                    f"handler raised: {error_message}"
                    if job_error is not None
                    else note
                )
                error_message = (
                    timeout_message
                    if timeout_detail is None
                    else f"{timeout_message} ({timeout_detail})"
                )
            # Each write has its own guard, so one failing does not skip
            # the other or the slot release below. An exception from here
            # would only reach the executor future, which nothing reads.
            finalize_error: Exception | None = None
            try:
                self.persistence.finalize_job(
                    job_id,
                    status,
                    duration_seconds=duration.total_seconds(),
                    error_message=error_message,
                )
            except Exception as e:
                finalize_error = e
                self._logger.exception(
                    f"'{task_name}' (Job {job_id}) could not record its"
                    f" end status '{status.value}': {e}"
                )
            will_retry = False
            try:
                will_retry = self.persistence.finalize_task_after_job(
                    task_id, start_time, job_failed
                )
            except Exception as e:
                self._logger.exception(
                    f"'{task_name}' (Job {job_id}) could not return task"
                    f" '{task_id}' to the schedule: {e}"
                )
            # After the task is finalized, so resume_task() never finds a
            # task with a job in flight to be idle.
            with self._registries_lock:
                self._tasks_in_flight.discard(task_id)
            with self._job_count_lock:
                if executor is Executor.PROCESS:
                    self._active_process_count -= 1
                else:
                    self._active_job_count -= 1
            # A freed slot lets deferred-due tasks dispatch, and the task's
            # freshly computed next_run_at may precede the loop's sleep.
            self._wake_loop()
            if run_once and not will_retry:
                with self._registries_lock:
                    self.registry.pop(task_id, None)
                    self.progress_callbacks.pop(task_id, None)

            # The JOB_* event and the waiters get the finished job, never
            # one that has not ended. After a failed end-status write the
            # row still says RUNNING, so it is not read back. Then, and
            # when the read itself fails, they get a Job made from the
            # values quiv tried to write: the outcome of the handler is
            # known even when the database is not.
            finalized_job: Job | None = None
            if finalize_error is None:
                try:
                    finalized_job = self.get_job(job_id)
                except Exception as e:
                    self._logger.exception(
                        f"'{task_name}' (Job {job_id}) could not be read"
                        f" back after it finished: {e}"
                    )
            if finalized_job is None:
                finalized_job = Job(
                    id=job_id,
                    task_id=task_id,
                    task_name=task_name,
                    status=status,
                    started_at=start_time,
                    ended_at=start_time + duration,
                    duration_seconds=duration.total_seconds(),
                    error_message=error_message,
                    attempt=task_snapshot.retry_attempt + 1,
                    executor=executor.value,
                )
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
            # After the events, so the JOB_* event is emitted before a
            # waiter wakes; the wait methods promise that order.
            self._resolve_waiters(self._job_waiters, job_id, finalized_job)
            self._resolve_waiters(self._task_waiters, task_id, finalized_job)

    def _run_in_child(
        self,
        job_id: str,
        task_id: str,
        func: Callable[..., Any],
        args_pickled: bytes,
        kwargs_pickled: bytes,
        stop_event: Any,
    ) -> str | None:
        """Run the handler in a spawned process and relay its messages.

        Spawns the child, relays its progress and ``run_on_main`` messages,
        runs the kill timer, and reaps the process. The kill rule: a stop
        signal the child ignores for ``process_kill_grace`` seconds becomes
        ``terminate()``. Timeout, ``cancel_job()``, ``remove_task()``, and
        ``shutdown()`` all set the same stop event, so one rule covers them.

        Returns:
            str | None: A note for ``error_message`` when quiv terminated
                the process, else ``None``.

        Raises:
            _ChildFailure: When the handler raised or the process died on
                its own.
        """

        module_name, qualname = _resolve_handler_reference(func)
        parent_conn, child_conn = self._mp_context.Pipe(duplex=False)
        process = self._mp_context.Process(
            target=_worker.run_job,
            args=(
                child_conn,
                stop_event,
                job_id,
                task_id,
                module_name,
                qualname,
                args_pickled,
                kwargs_pickled,
            ),
            name=f"quiv-job-{job_id}",
            daemon=True,
        )
        try:
            process.start()
        except BaseException:
            parent_conn.close()
            child_conn.close()
            raise
        # Drop the parent's copy of the child's end, or EOF never arrives
        # when the child exits.
        child_conn.close()
        with self._registries_lock:
            self._job_processes[job_id] = process
        if process.pid is not None:
            self.persistence.set_job_pid(job_id, process.pid)

        outcome: tuple[Any, ...] | None = None
        kill_at: float | None = None
        reason: str | None = None
        try:
            while True:
                # The kill timer runs every iteration, before the wait, so
                # a child that sends progress without pause cannot starve
                # it, and a grace of 0 terminates on the next tick.
                if outcome is None and stop_event.is_set():
                    if kill_at is None:
                        kill_at = time.monotonic() + self._process_kill_grace
                    elif time.monotonic() >= kill_at:
                        self._terminate_job_process(
                            job_id,
                            process,
                            "ignored its stop event for"
                            f" {self._process_kill_grace}s",
                        )
                        kill_at = None
                ready = wait(
                    [parent_conn, process.sentinel],
                    timeout=_PROCESS_POLL_SECONDS,
                )
                if parent_conn in ready:
                    # The pipe first, and again before the sentinel is
                    # looked at, so nothing the child sent is left behind.
                    # When the child closes its end, by returning or by
                    # dying, the pipe reads EOF and the loop ends here.
                    try:
                        message = parent_conn.recv()
                    except EOFError:
                        break
                    outcome = (
                        self._handle_child_message(task_id, message)
                        or outcome
                    )
                    if outcome is not None:
                        kill_at = None  # the handler finished; exiting
                    continue
                if process.sentinel in ready:  # pragma: no cover
                    # The process is gone and its pipe end is still open,
                    # which only a grandchild that inherited the end can
                    # cause. Nothing more can arrive on it.
                    break
        except BaseException:
            # An error on this side, such as a message the parent cannot
            # unpickle, must not leave the child running.
            if process.is_alive():
                process.terminate()
            raise
        finally:
            process.join()
            parent_conn.close()
            with self._registries_lock:
                self._job_processes.pop(job_id, None)
                reason = self._terminated_jobs.pop(job_id, None)

        if outcome is not None and outcome[0] == "error":
            raise _ChildFailure(
                outcome[1], outcome[2], cancelled=bool(outcome[3])
            )
        if outcome is not None:
            return None
        if reason is not None:
            return f"process terminated: {reason}"
        # Windows reports a large positive code after terminate(), so
        # "terminated" is never read from the code; it came from
        # _terminated_jobs above.
        code = process.exitcode
        if code is None or code >= 0:
            raise _ChildFailure(
                f"process ended with exit code {code} before reporting a"
                " result"
            )
        raise _ChildFailure(f"process killed by signal {-code}")

    def _handle_child_message(
        self, task_id: str, message: tuple[Any, ...]
    ) -> tuple[Any, ...] | None:
        """Relay one child message. Returns the message when it is final."""

        kind = message[0]
        if kind == "progress":
            # The same path a thread job's progress takes: the main-loop
            # dispatch and its fallbacks.
            self.run_progress_callback(task_id, *message[1], **message[2])
            return None
        if kind == "run_on_main":
            # This runs inside _execute_job, where _current_quiv is set,
            # so run_on_main finds this instance the ordinary way. The
            # child cannot see an error here: its message is already sent.
            try:
                run_on_main(message[1], *message[2], **message[3])
            except Exception as e:
                self._logger.error(
                    f"run_on_main() from a process job of task '{task_id}'"
                    f" failed: {e}"
                )
            return None
        return message
