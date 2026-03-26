from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.models.artifact import Artifact
from app.models.execution_run import ExecutionRun
from app.models.project import Project
from app.models.task import Task
from app.schemas.artifact import ArtifactRead
from app.schemas.execution_run import ExecutionRunRead
from app.schemas.project import ProjectCreate, ProjectRead
from app.schemas.task import TaskRead

router = APIRouter(prefix="/projects", tags=["projects"])


@router.post("", response_model=ProjectRead)
def create_project(payload: ProjectCreate, db: Session = Depends(get_db)):
    project = Project(
        name=payload.name,
        description=payload.description,
        enable_technical_refinement=payload.enable_technical_refinement,
        plan_version=1,
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


@router.get("", response_model=list[ProjectRead])
def list_projects(db: Session = Depends(get_db)):
    return db.query(Project).order_by(Project.id.asc()).all()


@router.get("/{project_id}", response_model=ProjectRead)
def get_project(project_id: int, db: Session = Depends(get_db)):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


@router.get("/{project_id}/tasks", response_model=list[TaskRead])
def list_project_tasks(
    project_id: int,
    planning_level: str | None = Query(default=None),
    task_type: str | None = Query(default=None),
    executor_type: str | None = Query(default=None),
    status: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    query = db.query(Task).filter(Task.project_id == project_id)

    if planning_level:
        query = query.filter(Task.planning_level == planning_level)

    if task_type:
        query = query.filter(Task.task_type == task_type)

    if executor_type:
        query = query.filter(Task.executor_type == executor_type)

    if status:
        query = query.filter(Task.status == status)

    return (
        query.order_by(
            Task.parent_task_id.asc().nullsfirst(),
            Task.sequence_order.asc().nullslast(),
            Task.id.asc(),
        ).all()
    )


@router.get("/{project_id}/artifacts", response_model=list[ArtifactRead])
def list_project_artifacts(project_id: int, db: Session = Depends(get_db)):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    return (
        db.query(Artifact)
        .filter(Artifact.project_id == project_id)
        .order_by(Artifact.id.asc())
        .all()
    )


@router.get("/{project_id}/execution-runs", response_model=list[ExecutionRunRead])
def list_project_execution_runs(project_id: int, db: Session = Depends(get_db)):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    return (
        db.query(ExecutionRun)
        .join(Task, ExecutionRun.task_id == Task.id)
        .filter(Task.project_id == project_id)
        .order_by(ExecutionRun.id.asc())
        .all()
    )