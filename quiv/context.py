"""Context-aware main-loop dispatch for Quiv task handlers.

This module exposes :func:`run_on_main`. It sends a callable to the main
event loop of the active Quiv instance, then returns without a result.

The caller can sit at any depth in the call stack of a task. The caller can
run on a Quiv worker thread or on the thread of the main loop itself.

Resolution order for the active Quiv instance:

1. A :class:`contextvars.ContextVar` set by :meth:`Quiv._run_job` before
   invoking a task handler. This wins inside task execution.
2. A process-level fallback registered when :meth:`QuivBase.start` is
   called. This covers a caller that no task contains, such as a FastAPI
   route handler on the uvicorn loop that shares a utility with task
   code.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import threading
from collections.abc import Coroutine
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, Callable, cast

from .exceptions import MainLoopUnavailableError

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
    """Run ``func`` on the main event loop of Quiv and return at once.

    Call this from anywhere in the call stack of a task handler, or from
    code on the thread of the main loop. It sends a sync or async
    callable to the main event loop. You do not have to pass a parameter
    through the functions in between.

    Dispatch behavior:

    - If the caller is already on the main loop's thread, a sync ``func``
      runs inline on the call stack and an async ``func`` is scheduled
      with ``main_loop.create_task``.
    - If the caller is on another thread, such as a Quiv worker thread,
      quiv sends a sync ``func`` with ``call_soon_threadsafe`` and an
      async ``func`` with ``run_coroutine_threadsafe``.

    If ``func`` raises, quiv writes the error to the logger of the active
    instance and continues. The exception never reaches the caller.
    ``progress_hook`` and the event listeners behave the same way.

    The active Quiv instance reaches nested sync calls, the event loops
    that quiv creates on worker threads for async handlers, and a task
    started with ``asyncio.create_task`` inside an async handler. It does
    **not** reach a ``threading.Thread`` that you start yourself inside a
    handler.

    Args:
        func (Callable[..., Any]): Sync function or coroutine function
            to invoke on the main loop. Bare coroutine objects are not
            accepted — pass the function.
        *args (Any): Positional arguments forwarded to ``func``.
        **kwargs (Any): Keyword arguments forwarded to ``func``.

    Raises:
        MainLoopUnavailableError: If no active Quiv instance is
            registered, or if the active Quiv has no resolvable main
            loop. It subclasses both ``QuivError`` and ``RuntimeError``.
    """

    quiv = _get_active_quiv()
    if quiv is None:
        raise MainLoopUnavailableError(
            "run_on_main() called with no active Quiv instance. Ensure"
            " Quiv.start() has been called before dispatching to the main"
            " loop."
        )

    main_loop = quiv._resolve_main_loop()
    if main_loop is None:
        raise MainLoopUnavailableError(
            "run_on_main() requires a resolvable main event loop. Pass"
            " main_loop= to Quiv() or call start() from inside the main"
            " loop's thread."
        )

    logger = quiv._logger
    is_coro_fn = inspect.iscoroutinefunction(func)

    def _on_done(fut: Any) -> None:
        # Ask the future whether it was cancelled before asking for its
        # exception. A future from run_coroutine_threadsafe raises
        # concurrent.futures.CancelledError from exception(), a different
        # class from asyncio.CancelledError since Python 3.8, and an
        # uncaught error in a done-callback makes the futures module log a
        # traceback for every coroutine cancelled at shutdown.
        if fut.cancelled():
            return
        exc = fut.exception()
        if exc is None:
            return
        logger.error(
            f"run_on_main callable {func!r} failed: {exc}",
            exc_info=exc,
        )

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
            task = main_loop.create_task(coroutine)
            task.add_done_callback(_on_done)
            return
        try:
            result = func(*args, **kwargs)
            if asyncio.iscoroutine(result):
                task = main_loop.create_task(result)
                task.add_done_callback(_on_done)
        except Exception as e:
            logger.error(
                f"run_on_main callable {func!r} failed: {e}", exc_info=True
            )
        return

    if is_coro_fn:
        coroutine = cast(Coroutine[Any, Any, Any], func(*args, **kwargs))
        future = asyncio.run_coroutine_threadsafe(coroutine, main_loop)
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
