from pydantic import BaseModel


class ArtifactRead(BaseModel):
    id: int
    task_id: int
    artifact_type: str
    content: str
    created_by: str

    model_config = {"from_attributes": True}