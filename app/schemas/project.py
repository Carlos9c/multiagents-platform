from datetime import datetime

from pydantic import BaseModel, Field


class ProjectCreate(BaseModel):
    name: str
    description: str | None = None
    enable_technical_refinement: bool = Field(
        default=False,
        description=(
            "When true, the workflow inserts an intermediate refinement phase "
            "between high-level planning and atomic generation."
        ),
    )


class ProjectUpdate(BaseModel):
    name: str | None = None
    description: str | None = None


class ProjectRead(BaseModel):
    id: int
    name: str
    description: str | None = None
    enable_technical_refinement: bool
    plan_version: int
    last_planned_at: datetime | None = None

    model_config = {"from_attributes": True}
