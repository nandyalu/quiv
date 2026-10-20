"""Handlers for the process-job tests.

A spawned child imports a handler by module name, so every handler here
is a module-level function, or a method on a module-level class. Tests
import this module by its bare name: ``tests/`` has no ``__init__.py``,
pytest puts it on ``sys.path``, and the child inherits that path.

Handlers take primitives and paths only. Their arguments are unpickled in
a fresh interpreter, so a class defined in a test function would not
import there.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
import types
from typing import Any

from quiv import run_on_main, run_subprocess
from quiv.exceptions import JobCancelledError


def noop() -> None:
    """Return at once."""


def sleep_seconds(seconds: float) -> None:
    """Hold the slot for a while, ignoring nothing in particular."""

    time.sleep(seconds)


def write_pid(path: str) -> None:
    """Write this process's pid to ``path``."""

    with open(path, "w", encoding="utf-8") as handle:
        handle.write(str(os.getpid()))


def raise_value_error() -> None:
    """Raise, so the parent sees an error message and a traceback."""

    raise ValueError("boom")


def raise_job_cancelled() -> None:
    """Raise JobCancelledError with no stop requested: an ordinary failure."""

    raise JobCancelledError("nobody asked")


def fail_until(counter_path: str, succeed_on: int) -> None:
    """Fail on every attempt before ``succeed_on``; count in a file."""

    attempt = 0
    if os.path.exists(counter_path):
        with open(counter_path, encoding="utf-8") as handle:
            attempt = int(handle.read() or 0)
    attempt += 1
    with open(counter_path, "w", encoding="utf-8") as handle:
        handle.write(str(attempt))
    if attempt < succeed_on:
        raise RuntimeError(f"attempt {attempt} fails")


def crash(code: int) -> None:
    """Leave without reporting anything, like a segfault would."""

    os._exit(code)


def kill_self() -> None:
    """Die by a signal, like the OOM killer would arrange."""

    os.kill(os.getpid(), getattr(signal, "SIGKILL", signal.SIGTERM))


def wait_for_stop(stop_event: Any) -> None:
    """Honour the stop event: a cooperative handler."""

    stop_event.wait(30)


def sleep_ignoring_stop(seconds: float) -> None:
    """Ignore the stop event, so the kill rule has to act."""

    time.sleep(seconds)


def report_progress(count: int, progress_hook: Any) -> None:
    """Send ``count`` progress payloads across the pipe."""

    for i in range(count):
        progress_hook(i, label=f"step-{i}")


def progress_unpicklable(progress_hook: Any) -> None:
    """Send a payload that cannot be pickled in the child."""

    progress_hook(lambda: None)


def progress_unpicklable_in_parent(progress_hook: Any) -> None:
    """Send a payload the child can pickle and the parent cannot unpickle.

    The class lives in a module made up here, so pickling by reference
    works in the child and the import fails in the parent.
    """

    module = types.ModuleType("quiv_test_child_only_module")
    cls = type("ChildOnly", (), {"__module__": module.__name__})
    setattr(module, "ChildOnly", cls)
    sys.modules[module.__name__] = module
    progress_hook(cls())
    # Give the parent time to fail on the message while this process is
    # still alive, so the parent has to terminate it.
    time.sleep(30)


def write_marker(path: str) -> None:
    """The ``run_on_main`` target: records which process ran it."""

    with open(path, "w", encoding="utf-8") as handle:
        handle.write(str(os.getpid()))


def call_run_on_main(marker_path: str) -> None:
    """Hand ``write_marker`` to the parent's main loop."""

    run_on_main(write_marker, marker_path)


def run_on_main_lambda() -> None:
    """A lambda cannot cross the pipe; quiv raises in the handler."""

    run_on_main(lambda: None)


async def async_noop() -> None:
    """An async handler, run in a fresh loop inside the child."""

    await asyncio.sleep(0)


def subprocess_until_stop() -> None:
    """Run a child of the child; the stop event must reach it."""

    run_subprocess(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        kill_grace=0.5,
    )


class Holder:
    """A class with the three kinds of method the validator must sort."""

    @staticmethod
    def static() -> None:
        """Importable by name through the class: accepted."""

    def method(self) -> None:
        """A bound method cannot be sent to a process: rejected."""

    @classmethod
    def cls_method(cls) -> None:
        """A classmethod is a bound method too: rejected."""
