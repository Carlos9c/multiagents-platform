from fastapi import APIRouter, Depends, HTTPException
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.services.technical_task_refiner import refine_high_level_task

router = APIRouter(
    prefix="/technical-task-refiner",
    tags=["technical-task-refiner"],
)


@router.post("/projects/{project_id}/tasks/{task_id}/refine")
def refine_task(
    project_id: int,
    task_id: int,
    db: Session = Depends(get_db),
):
    try:
        return refine_high_level_task(
            db,
            project_id=project_id,
            task_id=task_id,
        )
    except ValidationError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Technical refiner output validation failed: {str(exc)}",
        )
    except ValueError as exc:
        message = str(exc)
        if message.startswith("Project ") and message.endswith(" not found"):
            raise HTTPException(status_code=404, detail=message)
        if message.startswith("Task ") and message.endswith(" not found"):
            raise HTTPException(status_code=404, detail=message)
        raise HTTPException(status_code=400, detail=message)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Technical task refinement failed: {str(exc)}",
        )