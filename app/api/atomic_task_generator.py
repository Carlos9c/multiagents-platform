from fastapi import APIRouter, Depends, HTTPException
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.services.atomic_task_generator import generate_atomic_tasks

router = APIRouter(
    prefix="/atomic-task-generator",
    tags=["atomic-task-generator"],
)


@router.post("/projects/{project_id}/tasks/{task_id}/generate")
def generate_atomic(
    project_id: int,
    task_id: int,
    db: Session = Depends(get_db),
):
    try:
        return generate_atomic_tasks(
            db,
            project_id=project_id,
            task_id=task_id,
        )
    except ValidationError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Atomic task generator output validation failed: {str(exc)}",
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
            detail=f"Atomic task generation failed: {str(exc)}",
        )