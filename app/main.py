from fastapi import FastAPI

from app.api.artifacts import router as artifacts_router
from app.api.execution_runs import router as execution_runs_router
from app.api.projects import router as projects_router
from app.api.tasks import router as tasks_router
from app.api.planner import router as planner_router

app = FastAPI(title="Agente Desarrollador")

app.include_router(projects_router)
app.include_router(tasks_router)
app.include_router(execution_runs_router)
app.include_router(artifacts_router)
app.include_router(planner_router)


@app.get("/health")
def health():
    return {"status": "ok"}