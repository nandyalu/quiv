from __future__ import annotations

import inspect
import pickle
import weakref
from collections.abc import Awaitable
from typing import Any, Callable

from .exceptions import ConfigurationError

_INJECTABLE_KWARGS = frozenset({"_job_id", "_stop_event", "_progress_hook"})


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
        # WeakKeyDictionary so cached entries die with the handler — the
        # registry drops handlers on remove_task/run-once completion, and a
        # plain dict would leak them for the scheduler's lifetime.
        self._injectable_cache: (
            "weakref.WeakKeyDictionary[Callable[..., Any], frozenset[str]]"
        ) = weakref.WeakKeyDictionary()

    def _get_injectable_params(
        self, func: Callable[..., Any]
    ) -> frozenset[str]:
        """Return which injectable kwargs this callable accepts (cached).

        Args:
            func (Callable[..., Any]): Target callable.

        Returns:
            frozenset[str]: Accepted injectable keyword names.
        """

        try:
            cached = self._injectable_cache.get(func)
        except TypeError:  # unhashable callable — compute uncached
            cached = None
        if cached is not None:
            return cached
        accepted = self._compute_injectable_params(func)
        try:
            self._injectable_cache[func] = accepted
        except TypeError:  # unhashable or not weakref-able
            pass
        return accepted

    def _compute_injectable_params(
        self, func: Callable[..., Any]
    ) -> frozenset[str]:
        """Introspect which injectable kwargs a callable accepts.

        Args:
            func (Callable[..., Any]): Target callable.

        Returns:
            frozenset[str]: Accepted injectable keyword names; all of them
                when the callable takes ``**kwargs``.
        """

        try:
            signature = inspect.signature(func)
        except (ValueError, TypeError):
            return frozenset()
        accepted = set()
        for parameter in signature.parameters.values():
            if parameter.kind == parameter.VAR_KEYWORD:
                return _INJECTABLE_KWARGS  # **kwargs accepts everything
            if parameter.name in _INJECTABLE_KWARGS:
                accepted.add(parameter.name)
        return frozenset(accepted)

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

        injectable = self._get_injectable_params(func)

        if "_job_id" in injectable:
            f_kwargs["_job_id"] = job_id

        if "_stop_event" in injectable:
            f_kwargs["_stop_event"] = stop_event

        if "_progress_hook" in injectable:

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
