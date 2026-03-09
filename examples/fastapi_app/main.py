import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from examples.fastapi_app.routes.tasks import router as tasks_router
from examples.fastapi_app.scheduler import scheduler
from examples.fastapi_app.tasks.cleanup import cleanup_stale_records
from examples.fastapi_app.tasks.report import generate_report

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


# ---------- WebSocket connection manager ----------


class ConnectionManager:
    """Track active WebSocket connections for progress broadcasts."""

    def __init__(self):
        self.connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        self.connections.remove(websocket)

    async def broadcast(self, message: dict):
        for ws in self.connections:
            try:
                await ws.send_json(message)
            except Exception:
                pass


ws_manager = ConnectionManager()


# ---------- Progress callback ----------


async def on_report_progress(**payload):
    """Forward task progress to all connected WebSocket clients."""
    logger.info("Report progress: %s", payload)
    await ws_manager.broadcast({"event": "progress", "data": payload})


# ---------- Lifespan ----------


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler.add_task(
        task_name="db-cleanup",
        func=cleanup_stale_records,
        interval=3600,
        kwargs={"days": 30},
    )
    scheduler.add_task(
        task_name="weekly-report",
        func=generate_report,
        interval=604800,
        delay=10,
        kwargs={"report_type": "weekly-summary"},
        progress_callback=on_report_progress,
    )
    scheduler.start()
    yield
    scheduler.shutdown()


app = FastAPI(lifespan=lifespan)
app.include_router(tasks_router)


# ---------- WebSocket endpoint ----------


@app.websocket("/ws/progress")
async def progress_websocket(websocket: WebSocket):
    await ws_manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)
