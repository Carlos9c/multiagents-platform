from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.schemas.supervisor import SupervisorReportRead
from app.services.supervisor.supervisor_runner import run_supervisor

router = APIRouter(
    prefix="/supervisor",
    tags=["supervisor"],
)


@router.post(
    "/{project_id}/analyze",
    response_model=SupervisorReportRead,
    summary="Run full supervisor analysis for a project",
)
def analyze_project(
    project_id: int,
    db: Session = Depends(get_db),
) -> SupervisorReportRead:
    try:
        report = run_supervisor(db=db, project_id=project_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Supervisor analysis failed: {exc}",
        ) from exc
    return SupervisorReportRead.model_validate(report)
