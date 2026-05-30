from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.schemas.supervisor_aggregate import AggregateReportRead, AggregateRequest
from app.services.supervisor.aggregate_filter import TooFewProjectsError
from app.services.supervisor.aggregate_runner import run_aggregate

router = APIRouter(
    prefix="/supervisor",
    tags=["supervisor"],
)


@router.post(
    "/aggregate",
    response_model=AggregateReportRead,
    summary="Run cross-project aggregate supervisor analysis",
)
def aggregate(
    request: AggregateRequest,
    db: Session = Depends(get_db),
) -> AggregateReportRead:
    try:
        report = run_aggregate(
            db,
            version=request.version,
            date_from=request.date_from,
            date_to=request.date_to,
            project_ids=request.project_ids,
            limit=request.limit,
            include_dirty=request.include_dirty,
        )
    except TooFewProjectsError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Aggregate analysis failed: {exc}",
        ) from exc
    return AggregateReportRead.model_validate(report)
