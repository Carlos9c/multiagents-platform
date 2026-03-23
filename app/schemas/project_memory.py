from pydantic import BaseModel, Field


class ProjectMemoryTaskSummary(BaseModel):
    task_id: int
    parent_task_id: int | None = None
    title: str
    planning_level: str
    status: str
    priority: str
    task_type: str
    objective: str | None = None
    last_run_id: int | None = None
    last_run_status: str | None = None
    work_summary: str | None = None
    completed_scope: str | None = None
    remaining_scope: str | None = None
    blockers_found: str | None = None
    validation_notes: str | None = None
    artifact_types: list[str] = Field(default_factory=list)


class ProjectMemoryPathSignal(BaseModel):
    path: str
    mention_count: int = 0
    source_task_ids: list[int] = Field(default_factory=list)
    source_run_ids: list[int] = Field(default_factory=list)
    source_artifact_ids: list[int] = Field(default_factory=list)


class ProjectMemoryDecisionSignal(BaseModel):
    source_type: str
    source_id: int
    task_id: int | None = None
    summary: str


class ProjectMemoryArtifactSummary(BaseModel):
    artifact_id: int
    artifact_type: str
    task_id: int | None = None
    summary: str


class ProjectOperationalContext(BaseModel):
    project_id: int
    project_name: str
    project_goal: str

    total_tasks: int = 0
    pending_task_ids: list[int] = Field(default_factory=list)
    active_task_ids: list[int] = Field(default_factory=list)
    completed_task_ids: list[int] = Field(default_factory=list)
    failed_task_ids: list[int] = Field(default_factory=list)
    blocked_task_ids: list[int] = Field(default_factory=list)

    active_workstreams: list[str] = Field(default_factory=list)
    recent_completed_work: list[str] = Field(default_factory=list)
    recent_failure_learnings: list[str] = Field(default_factory=list)
    validation_learnings: list[str] = Field(default_factory=list)
    recovery_learnings: list[str] = Field(default_factory=list)
    open_gaps: list[str] = Field(default_factory=list)

    key_decisions: list[ProjectMemoryDecisionSignal] = Field(default_factory=list)
    referenced_paths: list[ProjectMemoryPathSignal] = Field(default_factory=list)
    recent_artifact_summaries: list[ProjectMemoryArtifactSummary] = Field(default_factory=list)
    task_memory: list[ProjectMemoryTaskSummary] = Field(default_factory=list)

    summary: str