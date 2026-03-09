# FastAPI Example App

A complete example showing quiv integrated into a multi-file FastAPI application
with task scheduling, runtime control via API endpoints, and WebSocket progress
updates.

## Run

From the **repository root**:

```bash
uv run uvicorn examples.fastapi_app.main:app --reload
```

## Endpoints

| Method | Path                        | Description                  |
|--------|-----------------------------|------------------------------|
| GET    | `/tasks/`                   | List all scheduled tasks     |
| GET    | `/tasks/jobs`               | List all jobs                |
| GET    | `/tasks/jobs?status=failed` | List jobs filtered by status |
| POST   | `/tasks/{task_name}/run`    | Trigger a task immediately   |
| POST   | `/tasks/{task_id}/pause`    | Pause a task                 |
| POST   | `/tasks/{task_id}/resume`   | Resume a paused task         |
| POST   | `/tasks/jobs/{job_id}/cancel` | Cancel a running job       |
| WS     | `/ws/progress`              | WebSocket for live progress  |

## Try it

1. Start the app (see above)
2. Open http://127.0.0.1:8000/docs for the interactive API docs
3. Trigger the report task immediately:
   ```bash
   curl -X POST http://127.0.0.1:8000/tasks/weekly-report/run
   ```
4. Watch progress via WebSocket:
   ```bash
   websocat ws://127.0.0.1:8000/ws/progress
   ```
