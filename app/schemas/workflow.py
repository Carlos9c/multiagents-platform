from pydantic import BaseModel, Field


class WorkflowIterationSummary(BaseModel):
    iteration_number: int
    plan_version: int | None = None
    batch_ids_processed: list[str] = Field(default_factory=list)
    reopened_finalization: bool = False
    manual_review_required: bool = False
    notes: str | None = None


class ProjectWorkflowResult(BaseModel):
    project_id: int
    status: str
    planning_completed: bool
    refinement_completed: bool
    atomic_generation_completed: bool
    execution_plan_generated: bool
    plan_version: int | None = None
    completed_batches: list[str] = Field(default_factory=list)
    blocked_batches: list[str] = Field(default_factory=list)
    iterations: list[WorkflowIterationSummary] = Field(default_factory=list)
    manual_review_required: bool = False
    final_stage_closed: bool = False
    notes: str | None = None