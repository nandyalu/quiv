from __future__ import annotations

import asyncio
import os
import threading
import time
from collections.abc import Generator

import pytest


@pytest.fixture
def leftover_db_paths() -> Generator[list[str], None, None]:
    """Delete a temp database that a test leaves behind on purpose.

    ``shutdown()`` deletes the database file, so nearly every test needs
    nothing here. Two kinds of test are different, and both leave a file
    in the temp directory on every run without this fixture:

    - A test that abandons a thread. ``shutdown(timeout=...)`` deletes the
      file and leaves a job that missed the deadline running on its daemon
      thread. That job still holds its connection, so the write it makes
      as it finishes recreates the file that shutdown just removed.
    - A test that makes deletion fail, to reach the warning branch in
      ``shutdown()``. Nothing deletes the file in that case, which is the
      point of the test.

    Append ``scheduler._db_path`` to the yielded list before shutting the
    scheduler down.

    The real ``os.remove`` and ``os.path.exists`` are captured here, at
    setup. ``quiv.base.os`` is the ``os`` module itself, so a test that
    patches ``quiv.base.os.remove`` patches it everywhere, and teardown
    order does not guarantee that ``monkeypatch`` has undone that before
    this fixture runs.
    """

    real_remove = os.remove
    real_exists = os.path.exists

    paths: list[str] = []
    yield paths

    # A thread abandoned by shutdown(timeout=...) recreates the file when
    # it finishes. Wait for that write, so the file is deleted after it
    # reappears rather than before.
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if any(real_exists(p) for p in paths):
            break
        time.sleep(0.1)
    for path in paths:
        for suffix in ("", "-wal", "-shm"):
            candidate = path + suffix
            if real_exists(candidate):
                real_remove(candidate)


@pytest.fixture
def running_main_loop() -> Generator[asyncio.AbstractEventLoop, None, None]:
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        yield loop
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
        loop.close()
