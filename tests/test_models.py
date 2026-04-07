from __future__ import annotations

from datetime import datetime, timedelta, timezone

from quiv.models import (
    Job,
    QuivModelBase,
    Task,
    TaskDB,
    get_current_time,
    id_generator,
    next_run_time,
)


def test_set_timezone_to_utc_on_naive_datetime() -> None:
    value = QuivModelBase.set_timezone_to_utc(datetime.now())
    assert value is not None
    assert value.tzinfo is not None
    assert value.utcoffset() == timedelta(0)


def test_set_timezone_to_utc_converts_aware_datetime() -> None:
    local_dt = datetime(
        2026, 3, 4, 12, 0, tzinfo=timezone(timedelta(hours=5, minutes=30))
    )
    value = QuivModelBase.set_timezone_to_utc(local_dt)
    assert value is not None
    assert value.tzinfo is not None
    assert value.utcoffset() == timedelta(0)


def test_job_validator_handles_none_end_time() -> None:
    job = Job(task_id="task-1", ended_at=None)
    assert job.ended_at is None


def test_set_timezone_to_utc_with_none() -> None:
    assert QuivModelBase.set_timezone_to_utc(None) is None


def test_task_model_validator_normalizes_naive_next_run() -> None:
    task_db = TaskDB(
        task_name="demo",
        interval_seconds=10,
        next_run_at=datetime(2026, 1, 1, 12, 0, 0),
    )
    # Convert to public Task model (which normalizes datetime)
    validated = Task.model_validate(task_db)
    assert validated.next_run_at.tzinfo is not None
    assert validated.next_run_at.utcoffset() == timedelta(0)


def test_job_model_validator_normalizes_naive_fields() -> None:
    job = Job(
        task_id="task-1",
        started_at=datetime(2026, 1, 1, 0, 0, 0),
        ended_at=datetime(2026, 1, 1, 0, 0, 2),
    )
    validated = Job.model_validate(job)
    assert validated.started_at.tzinfo is not None
    assert validated.started_at.utcoffset() == timedelta(0)
    assert validated.ended_at is not None
    assert validated.ended_at.tzinfo is not None
    assert validated.ended_at.utcoffset() == timedelta(0)


def test_task_serializes_args_kwargs_as_unpickled_values() -> None:
    import pickle

    task_db = TaskDB(
        task_name="serialize-check",
        interval_seconds=60,
        args=pickle.dumps((1, "hello", [3, 4])),
        kwargs=pickle.dumps({"key": "value", "num": 42}),
    )
    # Convert to public Task model
    task = Task.model_validate(task_db)
    assert task.args == (1, "hello", [3, 4])
    assert task.kwargs == {"key": "value", "num": 42}

    # Also verify JSON serialization works (what FastAPI uses)
    json_str = task.model_dump_json()
    assert '"hello"' in json_str
    assert '"key"' in json_str


def test_time_helpers_and_id_generator() -> None:
    nr = next_run_time()
    now = get_current_time()
    generated_id = id_generator()
    assert nr.tzinfo is not None
    assert now.tzinfo is not None
    assert nr > now
    assert isinstance(generated_id, str)
    assert len(generated_id) > 10
