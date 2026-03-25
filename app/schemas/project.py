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


class ProjectRead(BaseModel):
    id: int
    name: str
    description: str | None = None
    enable_technical_refinement: bool

    model_config = {"from_attributes": True}