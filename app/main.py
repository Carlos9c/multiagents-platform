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
from app.api.qa import router as qa_router
from app.api.supervisor import router as supervisor_router
from app.api.supervisor_aggregate import router as supervisor_aggregate_router
from app.api.tasks import router as tasks_router
from app.api.technical_task_refiner import router as technical_task_refiner_router
from app.api.workflow import router as workflow_router
from app.api.ws.connection_manager import manager as ws_manager
from app.api.ws.router import router as ws_router
from app.db.session import SessionLocal
from app.models.conversation import (
    CONVERSATION_PHASE_EXECUTING,
    CONVERSATION_PHASE_PAUSED,
    CONVERSATION_STATUS_ACTIVE,
    Conversation,
)
from app.models.task import TASK_STATUS_PENDING, Task

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

    # Startup recovery: conversations left in EXECUTING state (e.g. after a crash or
    # restart) are transitioned to PAUSED so the user can decide whether to resume.
    # Workflows are NEVER restarted automatically — resumption is always a user action.
    db = SessionLocal()
    try:
        stuck_conversations = (
            db.query(Conversation)
            .filter(
                Conversation.phase == CONVERSATION_PHASE_EXECUTING,
                Conversation.status == CONVERSATION_STATUS_ACTIVE,
            )
            .all()
        )
        for conv in stuck_conversations:
            has_pending = (
                db.query(Task)
                .filter(Task.project_id == conv.project_id, Task.status == TASK_STATUS_PENDING)
                .first()
            ) is not None
            if has_pending:
                conv.phase = CONVERSATION_PHASE_PAUSED
                logging.getLogger(__name__).info(
                    "startup_recovery_paused project_id=%s conversation_id=%s",
                    conv.project_id,
                    conv.id,
                )
        db.commit()
    except Exception as exc:
        logging.getLogger(__name__).warning("startup_recovery_failed error=%s", exc)
    finally:
        db.close()

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
app.include_router(qa_router)
app.include_router(supervisor_router)
app.include_router(supervisor_aggregate_router)


@app.get("/health")
def health():
    return {"status": "ok"}
