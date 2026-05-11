import dataclasses

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.models.artifact import Artifact
from app.models.execution_run import ExecutionRun
from app.models.project import Project
from app.models.task import Task
from app.schemas.analysis_read import CodebaseAnalysisRead
from app.schemas.artifact import ArtifactRead
from app.schemas.execution_run import ExecutionRunRead
from app.schemas.plan_history import PlanHistoryCycleRead
from app.schemas.project import ProjectCreate, ProjectRead, ProjectUpdate
from app.schemas.project_start import ProjectStartRequest, ProjectStartResponse
from app.schemas.task import TaskRead
from app.services.analysis import CodebaseAnalysisService
from app.services.plan_history_service import PlanHistoryService
from app.services.project_start_service import (
    ActiveTasksError,
    ProjectNotFoundError,
    ProjectStartService,
    SourcePathNotFoundError,
)
from app.services.project_storage import ProjectStorageService

router = APIRouter(prefix="/projects", tags=["projects"])

_start_service = ProjectStartService()


@router.post("/start", response_model=ProjectStartResponse)
def start_project(payload: ProjectStartRequest, db: Session = Depends(get_db)):
    try:
        return _start_service.start(db=db, request=payload)
    except (ProjectNotFoundError, SourcePathNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ActiveTasksError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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


@router.patch("/{project_id}", response_model=ProjectRead)
def update_project(project_id: int, payload: ProjectUpdate, db: Session = Depends(get_db)):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if payload.name is not None:
        project.name = payload.name
    if payload.description is not None:
        project.description = payload.description
    db.commit()
    db.refresh(project)
    return project


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

    return query.order_by(
        Task.parent_task_id.asc().nullsfirst(),
        Task.sequence_order.asc().nullslast(),
        Task.id.asc(),
    ).all()


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


@router.get("/{project_id}/analysis", response_model=CodebaseAnalysisRead)
def get_project_analysis(project_id: int, db: Session = Depends(get_db)):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    analysis = CodebaseAnalysisService().get_analysis(project_id)
    if analysis is None:
        raise HTTPException(status_code=404, detail="No analysis found for this project")

    return dataclasses.asdict(analysis)


@router.get("/{project_id}/plan-history", response_model=list[PlanHistoryCycleRead])
def get_project_plan_history(project_id: int, db: Session = Depends(get_db)):
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    paths = ProjectStorageService().get_project_paths(project_id)
    return PlanHistoryService().get_history(paths.project_meta_dir)
