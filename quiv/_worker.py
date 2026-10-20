"""Child side of a process job. Runs in the spawned interpreter only.

The parent passes the handler as ``(module, qualname)`` strings and the
arguments as pickled bytes, so the child imports and unpickles nothing
until :func:`run_job` has set the worker flag. The handler's module, and
every module its arguments need, is therefore imported with the flag set,
and a module-level ``Quiv()`` in the application becomes inert.

Messages to the parent are tuples whose first item names the kind:

- ``("progress", args, kwargs)`` — the handler called ``progress_hook``.
- ``("run_on_main", func, args, kwargs)`` — the handler called
  ``run_on_main``.
- ``("done",)`` — the handler returned.
- ``("error", message, traceback_text, cancelled)`` — the handler raised;
  ``message`` is ``str(exc)`` and ``cancelled`` says whether it was a
  ``JobCancelledError``.
"""

from __future__ import annotations

import importlib
import traceback
from typing import TYPE_CHECKING, Any, Callable, cast

from .exceptions import JobCancelledError, QuivError
from .execution import ExecutionLayer, run_in_fresh_loop

if TYPE_CHECKING:
    from multiprocessing.connection import Connection

_in_worker = False
_channel: Connection | None = None
_stop_event: Any = None


def in_worker_process() -> bool:
    """Return ``True`` inside a quiv worker process."""

    return _in_worker


def channel() -> Connection | None:
    """Return the pipe to the parent, or ``None`` outside a worker."""

    return _channel


def stop_event() -> Any:
    """Return the job's stop event inside a worker, else ``None``.

    ``run_subprocess()`` and ``call_on_main()`` find the stop event through
    the job context in the parent. A worker has no job context, so they
    read it from here instead.
    """

    return _stop_event


def send(message: tuple[Any, ...], what: str) -> None:
    """Send one message to the parent.

    Raises:
        QuivError: If ``message`` cannot be pickled, or no channel exists.
    """

    if _channel is None:
        raise QuivError("no parent channel: not inside a quiv worker process")
    try:
        _channel.send(message)
    except Exception as e:
        raise QuivError(
            f"{what} must be picklable in a process job: {e}"
        ) from e


def _send_progress(task_id: str, *args: Any, **kwargs: Any) -> None:
    send(("progress", args, kwargs), "progress_hook payload")


def import_handler(module_name: str, qualname: str) -> Callable[..., Any]:
    """Import a handler by the reference the parent resolved for it."""

    target: Any = importlib.import_module(module_name)
    for part in qualname.split("."):
        target = getattr(target, part)
    return cast("Callable[..., Any]", target)


def run_job(
    conn: Connection,
    stop_event: Any,
    job_id: str,
    task_id: str,
    module_name: str,
    qualname: str,
    args_pickled: bytes,
    kwargs_pickled: bytes,
) -> None:
    """Process entry point: run one handler and report the outcome."""

    global _in_worker, _channel, _stop_event
    _in_worker = True  # before the handler's module is imported
    _channel = conn
    _stop_event = stop_event
    try:
        func = import_handler(module_name, qualname)
        layer = ExecutionLayer(run_in_fresh_loop, _send_progress)
        f_args, f_kwargs = layer.prepare_invocation(
            task_id=task_id,
            func=func,
            args_pickled=args_pickled,
            kwargs_pickled=kwargs_pickled,
            stop_event=stop_event,
            job_id=job_id,
        )
        layer.run_callable(func, f_args, f_kwargs)
    except Exception as e:
        conn.send(
            (
                "error",
                str(e),
                traceback.format_exc(),
                isinstance(e, JobCancelledError),
            )
        )
    else:
        conn.send(("done",))
    finally:
        conn.close()
