"""A child process that honours the job's stop event.

This module exposes :func:`run_subprocess`, a drop-in for
``subprocess.run`` inside a task handler. Cooperative cancellation cannot
reach a child process on its own: a handler that checks its stop event
between steps still waits for the running child to finish. This helper
watches the stop event while the child runs and stops the child when the
event is set, with the same rule quiv applies to a process job: terminate,
then kill after a grace.
"""

from __future__ import annotations

import subprocess
import threading
import time
from collections.abc import Sequence
from typing import Any

from .context import _current_stop_event
from .exceptions import JobCancelledError

# How often the stop event and the deadline are checked while the child runs.
_POLL_SECONDS = 0.1


def _stop_child(
    proc: subprocess.Popen[Any], kill_grace: float
) -> tuple[Any, Any]:
    """Terminate the child, kill it after the grace, and reap it.

    Returns whatever output ``communicate()`` collected, so a timeout can
    attach it to the exception it raises.
    """

    proc.terminate()
    try:
        return proc.communicate(timeout=kill_grace)
    except subprocess.TimeoutExpired:
        proc.kill()
        # Always wait after kill, so the child is reaped and never a zombie.
        return proc.communicate()


def run_subprocess(
    args: Sequence[str] | str,
    *,
    stop_event: threading.Event | None = None,
    timeout: float | None = None,
    kill_grace: float = 5.0,
    check: bool = False,
    capture_output: bool = False,
    **popen_kwargs: Any,
) -> subprocess.CompletedProcess[Any]:
    """Run a child process that honours the job's stop event.

    A drop-in for ``subprocess.run`` inside a handler. While the child
    runs, quiv watches the job's stop event. When the event is set, the
    child is terminated, killed after ``kill_grace`` seconds if it is
    still alive, and ``JobCancelledError`` is raised. When ``timeout``
    passes, the child is stopped the same way and
    ``subprocess.TimeoutExpired`` is raised, as ``subprocess.run`` does.

    The stop event is found through the job context, so code deep inside
    a service does not need ``stop_event`` in scope. The reach is the
    same as :func:`run_on_main`: nested sync calls, the event loops quiv
    creates on worker threads, ``asyncio.create_task`` and
    ``asyncio.to_thread``. It does not reach a ``threading.Thread`` the
    handler starts; pass ``stop_event`` there.

    The helper stops the direct child only. A child that starts children
    of its own can leave them running after it dies. Pass
    ``start_new_session=True`` to give the child its own process group if
    that matters to you.

    On Windows ``terminate()`` and ``kill()`` are the same call, so the
    grace has no effect there.

    Args:
        args (Sequence[str] | str): The command, as for ``subprocess.run``.
        stop_event (threading.Event, Optional=None): The event to watch.
            Defaults to the stop event of the job this code runs inside,
            or none outside a job.
        timeout (float, Optional=None): Seconds before the child is
            stopped and ``TimeoutExpired`` is raised.
        kill_grace (float, Optional=5.0): Seconds between ``terminate()``
            and ``kill()``. The same number as ``process_kill_grace``.
        check (bool, Optional=False): Raise ``CalledProcessError`` on a
            non-zero return code, as ``subprocess.run`` does.
        capture_output (bool, Optional=False): Collect stdout and stderr,
            as ``subprocess.run`` does.
        **popen_kwargs (Any): Passed to ``subprocess.Popen``.

    Returns:
        subprocess.CompletedProcess: As ``subprocess.run`` returns.

    Raises:
        JobCancelledError: If the stop event was set while the child ran.
            The child is stopped first. Let it propagate; quiv finalizes
            the job as ``CANCELLED``.
        subprocess.TimeoutExpired: If ``timeout`` passed first. The child
            is stopped first.
        subprocess.CalledProcessError: If ``check`` is true and the child
            returned non-zero.
        ValueError: If ``capture_output`` is combined with ``stdout`` or
            ``stderr``, as ``subprocess.run`` refuses too.
    """

    if capture_output:
        if "stdout" in popen_kwargs or "stderr" in popen_kwargs:
            raise ValueError(
                "stdout and stderr arguments may not be used with"
                " capture_output."
            )
        popen_kwargs["stdout"] = subprocess.PIPE
        popen_kwargs["stderr"] = subprocess.PIPE

    if stop_event is None:
        stop_event = _current_stop_event()

    name = args if isinstance(args, str) else " ".join(args[:1])
    deadline = None if timeout is None else time.monotonic() + timeout

    with subprocess.Popen(args, **popen_kwargs) as proc:
        while True:
            try:
                # After TimeoutExpired, a retry of communicate() loses no
                # output; the stdlib promises that.
                stdout, stderr = proc.communicate(timeout=_POLL_SECONDS)
                break
            except subprocess.TimeoutExpired:
                if stop_event is not None and stop_event.is_set():
                    _stop_child(proc, kill_grace)
                    raise JobCancelledError(
                        f"{name} was stopped: the job's stop event was set"
                    ) from None
                if deadline is not None and time.monotonic() >= deadline:
                    assert timeout is not None
                    stdout, stderr = _stop_child(proc, kill_grace)
                    raise subprocess.TimeoutExpired(
                        args, timeout, output=stdout, stderr=stderr
                    ) from None

    if check and proc.returncode:
        raise subprocess.CalledProcessError(
            proc.returncode, args, output=stdout, stderr=stderr
        )
    return subprocess.CompletedProcess(args, proc.returncode, stdout, stderr)
