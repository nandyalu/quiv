"""Context-aware main-loop dispatch for Quiv task handlers.

This module exposes :func:`run_on_main`, a fire-and-forget helper that
dispatches a callable onto the active Quiv instance's main event loop —
regardless of how deeply nested in a task's call stack the caller sits, and
regardless of whether it is invoked from a Quiv worker thread or from the
main loop's own thread.

Resolution order for the active Quiv instance:

1. A :class:`contextvars.ContextVar` set by :meth:`Quiv._run_job` before
   invoking a task handler. This wins inside task execution.
2. A process-level fallback registered when :meth:`QuivBase.start` is
   called. This covers callers that are not inside a task context — for
   example, FastAPI route handlers running on uvicorn's loop that share
   utilities with task code.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import threading
from collections.abc import Coroutine
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, Callable, cast

if TYPE_CHECKING:
    from .base import QuivBase


_logger = logging.getLogger("Quiv")

_current_quiv: ContextVar["QuivBase | None"] = ContextVar(
    "quiv_current_instance", default=None
)

_active_quiv_lock = threading.Lock()
_active_quiv: "QuivBase | None" = None


def _register_active(instance: "QuivBase") -> None:
    """Register a Quiv instance as the process-level active fallback.

    Args:
        instance (QuivBase): Instance to register.
    """

    global _active_quiv
    with _active_quiv_lock:
        if _active_quiv is not None and _active_quiv is not instance:
            _logger.warning(
                "Multiple Quiv instances are active; run_on_main() will use"
                " the most recently started instance when called outside a"
                " task context."
            )
        _active_quiv = instance


def _unregister_active(instance: "QuivBase") -> None:
    """Clear the process-level fallback if it points at ``instance``.

    Args:
        instance (QuivBase): Instance to unregister.
    """

    global _active_quiv
    with _active_quiv_lock:
        if _active_quiv is instance:
            _active_quiv = None


def _get_active_quiv() -> "QuivBase | None":
    """Return the active Quiv instance, contextvar winning over fallback."""

    ctx = _current_quiv.get()
    if ctx is not None:
        return ctx
    return _active_quiv


def run_on_main(
    func: Callable[..., Any], *args: Any, **kwargs: Any
) -> None:
    """Run ``func`` on Quiv's main event loop, fire-and-forget.

    Use this from anywhere in a task handler's call stack (or from code
    on the main loop's thread itself) to dispatch a sync or async callable
    onto the main event loop without threading a parameter through the
    intermediate call layers.

    Dispatch behavior:

    - If the caller is already on the main loop's thread, a sync ``func``
      runs inline on the call stack and an async ``func`` is scheduled
      with ``main_loop.create_task``.
    - If the caller is on a different thread (e.g., a Quiv worker thread),
      sync ``func`` is dispatched via ``call_soon_threadsafe`` and async
      ``func`` via ``run_coroutine_threadsafe``.

    Exceptions raised by ``func`` are logged via the active Quiv's logger
    and swallowed — they will not propagate back to the caller. This
    mirrors ``_progress_hook`` and event-listener semantics.

    Context propagation: the active Quiv instance propagates into nested
    sync calls, into the thread-local event loops created for async
    handlers, and into ``asyncio.create_task`` invocations made inside an
    async handler. It does **not** propagate into ``threading.Thread``
    instances manually started from inside a handler.

    Args:
        func (Callable[..., Any]): Sync function or coroutine function
            to invoke on the main loop. Bare coroutine objects are not
            accepted — pass the function.
        *args (Any): Positional arguments forwarded to ``func``.
        **kwargs (Any): Keyword arguments forwarded to ``func``.

    Raises:
        RuntimeError: If no active Quiv instance is registered, or if
            the active Quiv has no resolvable main loop.
    """

    quiv = _get_active_quiv()
    if quiv is None:
        raise RuntimeError(
            "run_on_main() called with no active Quiv instance. Ensure"
            " Quiv.start() has been called before dispatching to the main"
            " loop."
        )

    main_loop = quiv._resolve_main_loop()
    if main_loop is None:
        raise RuntimeError(
            "run_on_main() requires a resolvable main event loop. Pass"
            " main_loop= to Quiv() or call start() from inside the main"
            " loop's thread."
        )

    logger = quiv._logger
    is_coro_fn = inspect.iscoroutinefunction(func)

    try:
        current_loop: asyncio.AbstractEventLoop | None = (
            asyncio.get_running_loop()
        )
    except RuntimeError:
        current_loop = None

    if current_loop is main_loop:
        if is_coro_fn:
            coroutine = cast(
                Coroutine[Any, Any, Any], func(*args, **kwargs)
            )
            main_loop.create_task(coroutine)
            return
        try:
            result = func(*args, **kwargs)
            if asyncio.iscoroutine(result):
                main_loop.create_task(result)
        except Exception as e:
            logger.error(
                f"run_on_main callable {func!r} failed: {e}", exc_info=True
            )
        return

    if is_coro_fn:
        coroutine = cast(Coroutine[Any, Any, Any], func(*args, **kwargs))
        future = asyncio.run_coroutine_threadsafe(coroutine, main_loop)

        def _on_done(fut: Any) -> None:
            exc = fut.exception()
            if exc is not None:
                logger.error(
                    f"run_on_main callable {func!r} failed: {exc}",
                    exc_info=exc,
                )

        future.add_done_callback(_on_done)
        return

    def _call_sync() -> None:
        try:
            result = func(*args, **kwargs)
            if asyncio.iscoroutine(result):
                asyncio.ensure_future(result, loop=main_loop)
        except Exception as e:
            logger.error(
                f"run_on_main callable {func!r} failed: {e}", exc_info=True
            )

    main_loop.call_soon_threadsafe(_call_sync)
