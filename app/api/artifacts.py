from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.models.artifact import Artifact
from app.schemas.artifact import ArtifactRead

router = APIRouter(prefix="/artifacts", tags=["artifacts"])


@router.get("", response_model=list[ArtifactRead])
def list_artifacts(db: Session = Depends(get_db)):
    return db.query(Artifact).all()