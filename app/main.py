import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.artifacts import router as artifacts_router
from app.api.atomic_task_generator import router as atomic_task_generator_router
from app.api.execution_runs import router as execution_runs_router
from app.api.planner import router as planner_router
from app.api.projects import router as projects_router
from app.api.tasks import router as tasks_router
from app.api.technical_task_refiner import router as technical_task_refiner_router
from app.api.workflow import router as workflow_router
from app.api.ws.connection_manager import manager as ws_manager
from app.api.ws.router import router as ws_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)

logging.getLogger("app.execution_engine").setLevel(logging.INFO)
logging.getLogger("app.execution_engine.orchestrator").setLevel(logging.INFO)
logging.getLogger("app.services.task_execution_service").setLevel(logging.INFO)
logging.getLogger("sqlalchemy.engine.Engine").setLevel(logging.WARNING)
logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
logging.getLogger("sqlalchemy.pool").setLevel(logging.WARNING)
logging.getLogger("sqlalchemy.dialects").setLevel(logging.WARNING)
logging.getLogger("sqlalchemy").propagate = False

@asynccontextmanager
async def _lifespan(application: FastAPI):
    ws_manager.set_event_loop(asyncio.get_running_loop())
    yield


app = FastAPI(title="Agente Desarrollador", lifespan=_lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(ws_router)
app.include_router(projects_router)
app.include_router(tasks_router)
app.include_router(execution_runs_router)
app.include_router(artifacts_router)
app.include_router(planner_router)
app.include_router(technical_task_refiner_router)
app.include_router(atomic_task_generator_router)
app.include_router(workflow_router)


@app.get("/health")
def health():
    return {"status": "ok"}
