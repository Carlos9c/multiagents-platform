from __future__ import annotations

from pydantic import BaseModel, Field


class ProjectStartRequest(BaseModel):
    name: str | None = Field(
        default=None,
        description="Required when creating a new project (no project_id).",
    )
    description: str | None = None
    enable_technical_refinement: bool = False
    source_path: str | None = None
    project_id: int | None = None
    manual_update: bool = True
    use_existing_source: bool = Field(
        default=False,
        description=(
            "When True with project_id, skips source import and reuses the existing "
            "source_dir with cached analysis. Used for disruptive-change restarts."
        ),
    )


class ProjectStartResponse(BaseModel):
    project_id: int
    plan_summary: str
    artifact_id: int
    tasks_created: int
    analysis_performed: bool
    cycle_number: int
