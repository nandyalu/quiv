"""run_subprocess(): a child process that honours the stop event."""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from quiv import Event, Job, JobCancelledError, JobStatus, Quiv, Task, run_subprocess

PY = sys.executable
SLEEPER = [PY, "-c", "import time; time.sleep(30)"]


def test_returns_a_completed_process_with_output() -> None:
    result = run_subprocess(
        [PY, "-c", "import sys; print('out'); print('err', file=sys.stderr)"],
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "out"
    assert result.stderr.strip() == "err"


def test_check_raises_called_process_error() -> None:
    with pytest.raises(subprocess.CalledProcessError) as info:
        run_subprocess([PY, "-c", "raise SystemExit(3)"], check=True, timeout=10)
    assert info.value.returncode == 3


def test_capture_output_with_an_explicit_stream_raises_value_error() -> None:
    with pytest.raises(ValueError, match="capture_output"):
        run_subprocess(SLEEPER, capture_output=True, stdout=subprocess.PIPE)


def test_timeout_stops_the_child_and_raises_timeout_expired() -> None:
    before = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired) as info:
        run_subprocess(SLEEPER, timeout=0.3, capture_output=True)
    assert info.value.timeout == 0.3
    assert time.monotonic() - before < 5


def test_stop_event_terminates_the_child_and_raises_job_cancelled() -> None:
    stop = threading.Event()
    threading.Timer(0.3, stop.set).start()

    before = time.monotonic()
    with pytest.raises(JobCancelledError, match="stop event was set"):
        run_subprocess(SLEEPER, stop_event=stop, capture_output=True)
    assert time.monotonic() - before < 5


@pytest.mark.skipif(sys.platform == "win32", reason="terminate() is kill() on Windows")
def test_kills_after_the_grace_when_terminate_is_ignored(tmp_path: Path) -> None:
    ready = tmp_path / "ready"
    child = [
        PY,
        "-c",
        "import signal, time, sys\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"open({str(ready)!r}, 'w').close()\n"
        "time.sleep(30)\n",
    ]
    stop = threading.Event()

    def set_when_ready() -> None:
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        stop.set()

    threading.Thread(target=set_when_ready).start()

    before = time.monotonic()
    with pytest.raises(JobCancelledError):
        run_subprocess(child, stop_event=stop, kill_grace=0.3)
    elapsed = time.monotonic() - before

    assert elapsed >= 0.3, "the grace was not waited"
    assert elapsed < 10


def test_finds_the_stop_event_from_the_job_context(
    running_main_loop: asyncio.AbstractEventLoop,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A cancel reaches the child through the job, and it is a clean stop:
    the job is cancelled with no error message and no error in the log."""

    scheduler = Quiv(main_loop=running_main_loop)
    started: list[str] = []
    running = threading.Event()

    def on_started(event: Event, task: Task, job: Job) -> None:
        assert job.id is not None
        started.append(job.id)

    def handler() -> None:
        running.set()
        run_subprocess(SLEEPER)  # no stop_event argument: found via the job

    try:
        scheduler.add_listener(Event.JOB_STARTED, on_started)
        task_id = scheduler.add_task("child", handler, run_once=True)
        scheduler.start()
        assert running.wait(timeout=5)
        deadline = time.monotonic() + 5
        while not started:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        time.sleep(0.2)  # let the child start before it is stopped

        before = time.monotonic()
        with caplog.at_level(logging.INFO, logger="Quiv"):
            assert scheduler.cancel_job(started[0])
            job = scheduler.wait_for_task(task_id, timeout=10)

        assert job.status == JobStatus.CANCELLED
        assert job.error_message is None
        assert time.monotonic() - before < 5
        assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []
        assert any("stopped at" in r.message for r in caplog.records)
    finally:
        scheduler.shutdown()


def test_job_cancelled_error_without_a_stop_is_a_failure(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    """Raised by hand, with no stop requested, it is an ordinary exception."""

    scheduler = Quiv(main_loop=running_main_loop)

    def handler() -> None:
        raise JobCancelledError("raised by hand")

    try:
        task_id = scheduler.add_task("manual", handler, run_once=True)
        scheduler.start()
        job = scheduler.wait_for_task(task_id, timeout=5)

        assert job.status == JobStatus.FAILED
        assert job.error_message == "raised by hand"
    finally:
        scheduler.shutdown()


def test_input_is_sent_to_the_child() -> None:
    result = run_subprocess(
        [PY, "-c", "import sys; print(sys.stdin.read().upper())"],
        input="abc",
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.stdout.strip() == "ABC"


def test_input_survives_a_poll_timeout_retry() -> None:
    """communicate() is retried every poll slice. The unsent input must
    still reach a child that reads stdin only after the first slice."""

    result = run_subprocess(
        [PY, "-c", "import sys, time; time.sleep(0.4); print(sys.stdin.read().upper())"],
        input="late",
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.stdout.strip() == "LATE"


def test_input_with_an_explicit_stdin_raises_value_error() -> None:
    with pytest.raises(ValueError, match="stdin and input"):
        run_subprocess(SLEEPER, input="x", stdin=subprocess.PIPE)
