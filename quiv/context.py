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
import concurrent.futures
import inspect
import logging
import threading
from collections.abc import Coroutine
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, Callable, cast

from .exceptions import JobCancelledError, MainLoopUnavailableError

if TYPE_CHECKING:
    from .base import QuivBase


_logger = logging.getLogger("Quiv")

# How often call_on_main() looks at the job's stop event while it waits.
_CALL_POLL_SECONDS = 0.1

_current_quiv: ContextVar["QuivBase | None"] = ContextVar(
    "quiv_current_instance", default=None
)

_current_job_id: ContextVar["str | None"] = ContextVar(
    "quiv_current_job_id", default=None
)

_active_quiv_lock = threading.Lock()
_active_quiv: "QuivBase | None" = None


def _current_stop_event() -> threading.Event | None:
    """Return the stop event of the job this code runs inside, or None.

    Reads the context variables only, never the process-level fallback:
    the fallback is set by ``start()`` and reaches code that no job
    contains, and such code has no stop event.
    """

    quiv = _current_quiv.get()
    job_id = _current_job_id.get()
    if quiv is None or job_id is None:
        return None
    with quiv._registries_lock:
        return quiv.stop_events.get(job_id)


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

    **This is fire-and-forget, and quiv never waits for it.** The calling
    job finishes as soon as the work is handed over, so quiv counts that
    job as complete while the work has not started. ``Quiv.shutdown`` does
    not wait for it either, and does not cancel it: the loop belongs to the
    application, not to quiv. ``Quiv.pending_main_loop_work()`` reports how
    many callables are still outstanding, so the application can decide
    when its own loop may close.

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
        # Stop tracking first, whatever the outcome, so the pending count
        # never includes work that has already settled.
        quiv._untrack_main_loop_work(fut)
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
            quiv._track_main_loop_work(task)
            task.add_done_callback(_on_done)
            return
        try:
            result = func(*args, **kwargs)
            if asyncio.iscoroutine(result):
                task = main_loop.create_task(result)
                quiv._track_main_loop_work(task)
                task.add_done_callback(_on_done)
        except Exception as e:
            logger.error(
                f"run_on_main callable {func!r} failed: {e}", exc_info=True
            )
        return

    if is_coro_fn:
        coroutine = cast(Coroutine[Any, Any, Any], func(*args, **kwargs))
        # The loop can start the coroutine the instant it is scheduled, on
        # its own thread, before this one reaches the tracking call below.
        # A marker held across that gap keeps the count from reading zero
        # while the work is already running. On the two branches above we
        # are on the loop's own thread, so nothing can run until we yield
        # and there is no such gap.
        marker = object()
        quiv._track_main_loop_work(marker)
        try:
            future = asyncio.run_coroutine_threadsafe(coroutine, main_loop)
        except BaseException:
            quiv._untrack_main_loop_work(marker)
            # Nothing will ever await it now, and an unawaited coroutine
            # warns on garbage collection.
            coroutine.close()
            raise
        quiv._track_main_loop_work(future)
        future.add_done_callback(_on_done)
        quiv._untrack_main_loop_work(marker)
        return

    # A queued sync callable has no future to await, so a marker stands in
    # for it until it runs. Without one, the count would read zero
    # while the callback was still sitting in the loop's ready queue.
    marker = object()
    quiv._track_main_loop_work(marker)

    def _call_sync() -> None:
        try:
            result = func(*args, **kwargs)
            if asyncio.iscoroutine(result):
                task = asyncio.ensure_future(result, loop=main_loop)
                # Track the task before dropping the marker, so the work is
                # never momentarily invisible to a drain in progress.
                quiv._track_main_loop_work(task)
                task.add_done_callback(_on_done)
        except Exception as e:
            logger.error(
                f"run_on_main callable {func!r} failed: {e}", exc_info=True
            )
        finally:
            quiv._untrack_main_loop_work(marker)

    try:
        main_loop.call_soon_threadsafe(_call_sync)
    except BaseException:
        # The loop can close between _resolve_main_loop() above and this
        # call. Without this the marker would stay in the set for good:
        # the count would never return to zero, and
        # shutdown() would report work that was never queued. The
        # exception still reaches the caller, as it always did.
        quiv._untrack_main_loop_work(marker)
        raise


def call_on_main(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run ``func`` on the main event loop of Quiv and wait for its result.

    The sibling of :func:`run_on_main`: the same dispatch, the same
    active instance, the same reach. The difference is that this one
    waits. The result comes back to the caller, an exception raised by
    ``func`` reaches the caller, and the job's stop event ends the wait.

    A handler that hands all its work to the main loop and returns gives
    quiv nothing to record: a one-millisecond success, whatever the work
    did. With ``call_on_main`` the job carries the real duration and the
    real failure, ``JOB_FAILED`` fires, ``max_retries`` applies, and the
    task's ``timeout`` cancels the coroutine.

    Dispatch behavior:

    - If the caller is on the main loop's thread, a sync ``func`` runs
      inline and its result is returned. An async ``func`` cannot be
      waited for there, because waiting would block the loop that must
      run it; that raises ``MainLoopUnavailableError``. Await it instead.
    - If the caller is on another thread, such as a Quiv worker thread,
      ``func`` runs on the main loop and this thread blocks until it
      finishes. Sync and async targets are handled the same way.

    Every keyword goes to ``func``, so this function has no options of
    its own. Cancellation comes from the job's stop event, and a deadline
    from the task's ``timeout``. Outside a job there is no stop event,
    and the wait has no limit.

    The work counts in ``Quiv.pending_main_loop_work()`` while it runs.

    Args:
        func (Callable[..., Any]): Sync function or coroutine function
            to invoke on the main loop.
        *args (Any): Positional arguments forwarded to ``func``.
        **kwargs (Any): Keyword arguments forwarded to ``func``.

    Returns:
        Any: What ``func`` returned, or what its coroutine returned.

    Raises:
        JobCancelledError: If the job's stop event was set while waiting.
            The coroutine on the main loop is cancelled. Let it
            propagate; quiv finalizes the job as ``CANCELLED``.
        MainLoopUnavailableError: If no active Quiv instance is
            registered, if the active Quiv has no resolvable main loop,
            or if an async ``func`` is passed from the main loop's own
            thread.
        Exception: Whatever ``func`` raised.
    """

    quiv = _get_active_quiv()
    if quiv is None:
        raise MainLoopUnavailableError(
            "call_on_main() called with no active Quiv instance. Ensure"
            " Quiv.start() has been called before dispatching to the main"
            " loop."
        )

    main_loop = quiv._resolve_main_loop()
    if main_loop is None:
        raise MainLoopUnavailableError(
            "call_on_main() requires a resolvable main event loop. Pass"
            " main_loop= to Quiv() or call start() from inside the main"
            " loop's thread."
        )

    try:
        current_loop: asyncio.AbstractEventLoop | None = (
            asyncio.get_running_loop()
        )
    except RuntimeError:
        current_loop = None

    if current_loop is main_loop:
        if inspect.iscoroutinefunction(func):
            raise MainLoopUnavailableError(
                "call_on_main() cannot wait for an async target from the"
                " main loop's own thread; await it there instead."
            )
        result = func(*args, **kwargs)
        if asyncio.iscoroutine(result):
            # A sync callable that returned a coroutine is the same case.
            result.close()
            raise MainLoopUnavailableError(
                "call_on_main() cannot wait for a coroutine from the main"
                " loop's own thread; await it there instead."
            )
        return result

    async def _invoke() -> Any:
        result = func(*args, **kwargs)
        if inspect.isawaitable(result):
            result = await result
        return result

    # The same marker-then-future tracking run_on_main uses, so the work
    # counts in pending_main_loop_work() from the moment it is handed over.
    coroutine = _invoke()
    marker = object()
    quiv._track_main_loop_work(marker)
    try:
        future = asyncio.run_coroutine_threadsafe(coroutine, main_loop)
    except BaseException:
        quiv._untrack_main_loop_work(marker)
        coroutine.close()
        raise
    quiv._track_main_loop_work(future)
    # Untrack only. The outcome is the caller's, not the log's.
    future.add_done_callback(quiv._untrack_main_loop_work)
    quiv._untrack_main_loop_work(marker)

    stop_event = _current_stop_event()
    while True:
        try:
            return future.result(timeout=_CALL_POLL_SECONDS)
        except concurrent.futures.TimeoutError:
            if stop_event is not None and stop_event.is_set():
                # Threadsafe: cancels the task on the loop at its next await.
                future.cancel()
                raise JobCancelledError(
                    f"call_on_main({func!r}) was stopped: the job's stop"
                    " event was set"
                ) from None
