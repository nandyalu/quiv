from dataclasses import asdict

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from quiv.exceptions import ConfigurationError, TaskNotFoundError

from examples.fastapi_app.scheduler import scheduler

router = APIRouter(prefix="/tasks", tags=["tasks"])


class TaskUpdate(BaseModel):
    """PATCH body for updating a task; omitted fields stay unchanged."""

    interval: float | None = None
    jitter: float | None = None


@router.get("/stats")
def get_stats():
    """Point-in-time scheduler statistics snapshot."""
    return asdict(scheduler.stats())


@router.get("/{task_id}/jobs")
def list_task_jobs(task_id: str, limit: int | None = None, offset: int = 0):
    """List a single task's jobs, newest first, with pagination."""
    return scheduler.get_all_jobs(task_id=task_id, limit=limit, offset=offset)


@router.patch("/{task_id}")
def update_task(task_id: str, update: TaskUpdate):
    """Update a task's interval and/or jitter in place."""
    changes = update.model_dump(exclude_none=True)
    if not changes:
        raise HTTPException(
            status_code=422, detail="Provide at least one field to update"
        )
    try:
        task = scheduler.update_task(task_id, **changes)
    except TaskNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ConfigurationError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return task


@router.post("/{task_name}/run")
def run_task_now(task_name: str):
    """Trigger a scheduled task to run immediately."""
    try:
        count = scheduler.run_task_immediately(task_name)
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"queued": count}


@router.post("/{task_name}/pause")
def pause_task(task_name: str):
    """Pause a task by name."""
    try:
        scheduler.pause_task(task_name)
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"status": "paused"}


@router.post("/{task_name}/resume")
def resume_task(task_name: str, delay: int = 0):
    """Resume a paused task, optionally with a delay."""
    try:
        scheduler.resume_task(task_name, delay=delay)
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"status": "resumed"}


@router.get("/")
def list_tasks():
    """List all scheduled tasks."""
    return scheduler.get_all_tasks()


@router.get("/jobs")
def list_jobs(status: str | None = None):
    """List jobs, optionally filtered by status."""
    return scheduler.get_all_jobs(status=status)


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    """Cancel a running job."""
    cancelled = scheduler.cancel_job(job_id)
    if not cancelled:
        raise HTTPException(
            status_code=404, detail="Job not found or not running"
        )
    return {"status": "cancelled"}
