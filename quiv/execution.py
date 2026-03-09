from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable
from typing import Any, Callable


class ExecutionLayer:
    """Execution-focused utilities for preparing and running task handlers.

    Attributes:
        _run_async (Callable[[Callable[..., Awaitable[Any]], list | None, \
            dict | None], None]): 
            Callable used to run coroutine handlers in thread-local loops.
        _run_progress_callback (Callable[..., None]): 
            Callable used to dispatch progress updates.
    """

    def __init__(
        self,
        run_async: Callable[
            [Callable[..., Awaitable[Any]], list[Any] | None, dict[str, Any] | None], None
        ],
        run_progress_callback: Callable[..., None],
    ):
        """Initialize the execution layer.

        Args:
            run_async (Callable[[Callable[..., Awaitable[Any]], list | None, \
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
        task_name: str,
        func: Callable[..., Any],
        args_json: str,
        kwargs_json: str,
        stop_event: Any,
    ) -> tuple[list[Any], dict[str, Any]]:
        """Prepare runtime invocation arguments for a task handler.

        Args:
            task_name (str): Scheduled task name.
            func (Callable[..., Any]): Registered handler.
            args_json (str): JSON-encoded positional arguments.
            kwargs_json (str): JSON-encoded keyword arguments.
            stop_event (Any): Cancellation event to inject when supported.

        Returns:
            tuple[list, dict]: A tuple with decoded positional args and kwargs.
        """

        f_args = json.loads(args_json)
        f_kwargs = json.loads(kwargs_json)

        if self._accepts_keyword_arg(func, "_stop_event"):
            f_kwargs["_stop_event"] = stop_event

        if self._accepts_keyword_arg(func, "_progress_hook"):

            def _progress_hook(*progress_args: Any, **progress_kwargs: Any) -> None:
                self._run_progress_callback(
                    task_name, *progress_args, **progress_kwargs
                )

            f_kwargs["_progress_hook"] = _progress_hook

        return f_args, f_kwargs

    def run_callable(
        self, func: Callable[..., Any], args: list[Any], kwargs: dict[str, Any]
    ) -> None:
        """Run a handler function, supporting sync and async callables.

        Args:
            func (Callable[..., Any]): Handler callable.
            args (list): Positional arguments.
            kwargs (dict): Keyword arguments.
        """

        if inspect.iscoroutinefunction(func):
            self._run_async(func, args, kwargs)
            return
        func(*args, **kwargs)
