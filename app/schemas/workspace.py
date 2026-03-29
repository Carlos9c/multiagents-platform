from pydantic import BaseModel, Field


class WorkspaceChangeSet(BaseModel):
    created_files: list[str] = Field(default_factory=list)
    modified_files: list[str] = Field(default_factory=list)
    deleted_files: list[str] = Field(default_factory=list)
    renamed_files: list[str] = Field(default_factory=list)
    diff_summary: str | None = None
    impacted_areas: list[str] = Field(default_factory=list)
