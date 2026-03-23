from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.models.execution_run import ExecutionRun
from app.schemas.execution_run import ExecutionRunRead

router = APIRouter(prefix="/execution-runs", tags=["execution-runs"])


@router.get("", response_model=list[ExecutionRunRead])
def list_execution_runs(db: Session = Depends(get_db)):
    return db.query(ExecutionRun).order_by(ExecutionRun.id.asc()).all()