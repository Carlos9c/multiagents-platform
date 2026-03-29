from fastapi import APIRouter, Depends, HTTPException
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.services.planner import generate_project_plan

router = APIRouter(prefix="/planner", tags=["planner"])


@router.post("/projects/{project_id}/plan")
def plan_project(project_id: int, db: Session = Depends(get_db)):
    try:
        return generate_project_plan(db, project_id)
    except ValidationError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Planner output validation failed: {str(exc)}",
        )
    except ValueError as exc:
        message = str(exc)

        if message.startswith("Project ") and message.endswith(" not found"):
            raise HTTPException(status_code=404, detail=message)

        raise HTTPException(status_code=500, detail=message)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Planner failed: {str(exc)}")
