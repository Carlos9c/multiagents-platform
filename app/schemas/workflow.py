from pydantic import BaseModel, Field


class WorkflowIterationSummary(BaseModel):
    iteration_number: int = Field(..., ge=1)

    plan_version: int = Field(..., ge=1)
    starting_plan_version: int = Field(..., ge=1)
    ending_plan_version: int = Field(..., ge=1)

    batch_ids_processed: list[str] = Field(default_factory=list)
    blocked_batch_ids_after_iteration: list[str] = Field(default_factory=list)

    reopened_finalization: bool = False
    manual_review_required: bool = False

    used_patched_plan: bool = False
    replan_triggered: bool = False

    notes: str


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