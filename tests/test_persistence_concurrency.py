from __future__ import annotations

import asyncio
import logging
import pickle
import threading
import time

from quiv import Quiv
from quiv.models import JobStatus, TaskStatus


def _join_all(threads: list[threading.Thread], timeout: float = 15.0) -> None:
    """Join threads with a shared deadline; fail instead of hanging CI."""
    deadline = time.monotonic() + timeout
    for t in threads:
        t.join(timeout=max(0.0, deadline - time.monotonic()))
    stuck = [t.name for t in threads if t.is_alive()]
    assert not stuck, f"threads did not finish within {timeout}s: {stuck}"


def _seed_task(scheduler: Quiv, name: str = "seed") -> str:
    return scheduler.persistence.create_task(
        task_name=name,
        interval=60,
        run_once=False,
        fixed_interval=True,
        next_run_at=scheduler._now_utc(),
        args_pickled=pickle.dumps(()),
        kwargs_pickled=pickle.dumps({}),
    )


def test_concurrent_writers_no_lost_updates(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    errors: list[Exception] = []
    try:
        def writer(tid: int) -> None:
            try:
                for i in range(25):
                    _seed_task(scheduler, name=f"writer-{tid}-{i}")
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=writer, args=(t,)) for t in range(8)
        ]
        for t in threads:
            t.start()
        _join_all(threads)

        assert not errors, f"writer threads raised: {errors[:3]}"
        tasks = scheduler.persistence.get_all_tasks()
        assert len(tasks) == 200
        assert len({task.id for task in tasks}) == 200
    finally:
        scheduler.shutdown()


def test_readers_during_writes_see_consistent_rows(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    errors: list[Exception] = []
    stop = threading.Event()
    known_job_ids: list[str] = []
    try:
        task_id = _seed_task(scheduler)
        persistence = scheduler.persistence
        known_job_ids.append(persistence.create_job(task_id, "seed"))

        def writer() -> None:
            try:
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    job_id = persistence.create_job(task_id, "seed")
                    known_job_ids.append(job_id)
                    persistence.mark_job_running(job_id)
                    persistence.finalize_job(
                        job_id, JobStatus.COMPLETED, duration_seconds=0.0
                    )
            except Exception as e:
                errors.append(e)
            finally:
                stop.set()

        def reader(tid: int) -> None:
            try:
                i = 0
                while not stop.is_set():
                    jobs = persistence.get_all_jobs()
                    for job in jobs:
                        if job.status == JobStatus.COMPLETED:
                            assert job.ended_at is not None
                        if job.status == JobStatus.RUNNING:
                            assert job.started_at is not None
                    job_id = known_job_ids[(tid + i) % len(known_job_ids)]
                    persistence.get_job(job_id)
                    i += 1
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer)] + [
            threading.Thread(target=reader, args=(t,)) for t in range(4)
        ]
        for t in threads:
            t.start()
        _join_all(threads)

        assert not errors, f"threads raised: {errors[:3]}"
    finally:
        scheduler.shutdown()


def test_full_scheduler_under_load(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    error_records: list[logging.LogRecord] = []

    class _ErrorCapture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if record.levelno >= logging.ERROR:
                error_records.append(record)

    capture = _ErrorCapture()
    quiv_logger = logging.getLogger("Quiv")
    quiv_logger.addHandler(capture)
    scheduler = Quiv(pool_size=32, main_loop=running_main_loop)
    errors: list[Exception] = []
    stop = threading.Event()
    try:
        def poll_jobs() -> None:
            try:
                while not stop.is_set():
                    scheduler.get_all_jobs()
                    time.sleep(0.01)
            except Exception as e:
                errors.append(e)

        poller = threading.Thread(target=poll_jobs)
        poller.start()

        for i in range(32):
            scheduler.add_task(
                task_name=f"load-{i}",
                func=lambda: time.sleep(0.05),
                interval=60,
                run_once=True,
                delay=0,
            )
        scheduler.start()

        deadline = time.monotonic() + 10
        completed = 0
        while time.monotonic() < deadline:
            completed = len(
                scheduler.get_all_jobs(status=JobStatus.COMPLETED)
            )
            if completed >= 32:
                break
            time.sleep(0.05)
        stop.set()
        _join_all([poller], timeout=5.0)

        assert completed == 32, f"only {completed}/32 jobs completed"
        failed = scheduler.get_all_jobs(status=JobStatus.FAILED)
        assert not failed, f"{len(failed)} jobs failed"
        assert not errors, f"poller thread raised: {errors[:3]}"
        assert not error_records, (
            f"errors logged: {[r.getMessage() for r in error_records[:3]]}"
        )
    finally:
        stop.set()
        scheduler.shutdown()
        quiv_logger.removeHandler(capture)


def test_pause_resume_race_yields_valid_status(
    running_main_loop: asyncio.AbstractEventLoop,
) -> None:
    scheduler = Quiv(main_loop=running_main_loop)
    errors: list[Exception] = []
    try:
        task_id = _seed_task(scheduler)
        persistence = scheduler.persistence

        def flipper(tid: int) -> None:
            try:
                for _ in range(25):
                    if tid % 2 == 0:
                        persistence.pause_task(task_id)
                    else:
                        persistence.resume_task(task_id)
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=flipper, args=(t,)) for t in range(8)
        ]
        for t in threads:
            t.start()
        _join_all(threads)

        assert not errors, f"flipper threads raised: {errors[:3]}"
        task = persistence.get_task(task_id)
        assert task.status in (TaskStatus.PAUSED, TaskStatus.ACTIVE)
    finally:
        scheduler.shutdown()
