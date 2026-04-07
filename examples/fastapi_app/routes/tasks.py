from fastapi import APIRouter, HTTPException

from examples.fastapi_app.scheduler import scheduler

router = APIRouter(prefix="/tasks", tags=["tasks"])


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
        raise HTTPException(status_code=404, detail="Job not found or not running")
    return {"status": "cancelled"}
