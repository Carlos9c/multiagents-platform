from sqlalchemy.orm import Session

from app.models.artifact import Artifact


def create_artifact(
    db: Session,
    project_id: int,
    artifact_type: str,
    content: str,
    created_by: str,
    task_id: int | None = None,
) -> Artifact:
    artifact = Artifact(
        project_id=project_id,
        task_id=task_id,
        artifact_type=artifact_type,
        content=content,
        created_by=created_by,
    )
    db.add(artifact)
    db.commit()
    db.refresh(artifact)
    return artifact