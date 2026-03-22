from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.schemas.workflow import ProjectWorkflowResult
from app.services.project_workflow_service import (
    ProjectWorkflowServiceError,
    run_project_workflow,
)

router = APIRouter(
    prefix="/workflow",
    tags=["workflow"],
)


@router.post(
    "/projects/{project_id}/run",
    response_model=ProjectWorkflowResult,
)
def run_workflow_for_project(
    project_id: int,
    max_workflow_iterations: int = Query(
        default=5,
        ge=1,
        le=20,
        description="Maximum number of end-to-end workflow iterations before forcing manual review.",
    ),
    max_finalization_iterations: int = Query(
        default=2,
        ge=0,
        le=10,
        description="Maximum number of automatic finalization reopenings allowed before forcing manual review.",
    ),
    db: Session = Depends(get_db),
):
    try:
        return run_project_workflow(
            db=db,
            project_id=project_id,
            max_workflow_iterations=max_workflow_iterations,
            max_finalization_iterations=max_finalization_iterations,
        )
    except ProjectWorkflowServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Unexpected workflow execution error: {str(exc)}",
        ) from exc