from __future__ import annotations

import inspect
import pickle
from collections.abc import Awaitable
from typing import Any, Callable

from .exceptions import ConfigurationError


class ExecutionLayer:
    """Execution-focused utilities for preparing and running task handlers.

    Attributes:
        _run_async (Callable[[Callable[..., Awaitable[Any]], tuple | None, \
            dict | None], None]):
            Callable used to run coroutine handlers in thread-local loops.
        _run_progress_callback (Callable[..., None]):
            Callable used to dispatch progress updates.
    """

    def __init__(
        self,
        run_async: Callable[
            [Callable[..., Awaitable[Any]], tuple[Any, ...] | None, dict[str, Any] | None], None
        ],
        run_progress_callback: Callable[..., None],
    ):
        """Initialize the execution layer.

        Args:
            run_async (Callable[[Callable[..., Awaitable[Any]], tuple | None, \
                dict | None], None]):
                Callback that executes async handlers.
            run_progress_callback (Callable[..., None]): 
                Callback for progress hook dispatch.
        """

        self._run_async = run_async
        self._run_progress_callback = run_progress_callback

    def _accepts_keyword_arg(
        self, func: Callable[..., Any], keyword: str
    ) -> bool:
        """Check whether a callable accepts a specific keyword argument.

        Args:
            func (Callable[..., Any]): Target callable.
            keyword (str): Keyword name to validate.

        Returns:
            bool: ``True`` if the callable accepts the keyword directly \
                or via ``**kwargs``.
        """

        try:
            signature = inspect.signature(func)
        except (ValueError, TypeError):
            return False
        for parameter in signature.parameters.values():
            if parameter.kind == parameter.VAR_KEYWORD:
                return True
            if parameter.name == keyword:
                return True
        return False

    def prepare_invocation(
        self,
        task_id: str,
        func: Callable[..., Any],
        args_pickled: bytes,
        kwargs_pickled: bytes,
        stop_event: Any,
        job_id: str,
    ) -> tuple[tuple[Any, ...], dict[str, Any]]:
        """Prepare runtime invocation arguments for a task handler.

        Args:
            task_id (str): Task identifier (UUID string).
            func (Callable[..., Any]): Registered handler.
            args_pickled (bytes): Pickle-encoded positional arguments.
            kwargs_pickled (bytes): Pickle-encoded keyword arguments.
            stop_event (Any): Cancellation event to inject when supported.
            job_id (str): Job identifier (UUID string) to inject when supported.

        Returns:
            tuple[tuple, dict]: A tuple with decoded positional args and kwargs.
        """

        try:
            raw_args = pickle.loads(args_pickled)
            f_args = tuple(raw_args)
        except Exception as e:
            raise ConfigurationError(
                f"Failed to deserialize task args: {e}"
            ) from e

        try:
            f_kwargs = pickle.loads(kwargs_pickled)
        except Exception as e:
            raise ConfigurationError(
                f"Failed to deserialize task kwargs: {e}"
            ) from e

        if not isinstance(f_kwargs, dict):
            raise ConfigurationError(
                f"Expected kwargs to be a dict, got {type(f_kwargs).__name__}"
            )

        if self._accepts_keyword_arg(func, "_job_id"):
            f_kwargs["_job_id"] = job_id

        if self._accepts_keyword_arg(func, "_stop_event"):
            f_kwargs["_stop_event"] = stop_event

        if self._accepts_keyword_arg(func, "_progress_hook"):

            def _progress_hook(*progress_args: Any, **progress_kwargs: Any) -> None:
                self._run_progress_callback(
                    task_id, *progress_args, **progress_kwargs
                )

            f_kwargs["_progress_hook"] = _progress_hook

        return f_args, f_kwargs

    def run_callable(
        self, func: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> None:
        """Run a handler function, supporting sync and async callables.

        Args:
            func (Callable[..., Any]): Handler callable.
            args (tuple): Positional arguments.
            kwargs (dict): Keyword arguments.
        """

        if inspect.iscoroutinefunction(func):
            self._run_async(func, args, kwargs)
            return
        func(*args, **kwargs)
