from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.evaluation import EvaluationDecision, RecoveryContext


PostBatchStatus = Literal[
    "completed_without_checkpoint",
    "completed_with_evaluation",
    "checkpoint_blocked",
    "finalization_reopened",
    "finalization_guard_blocked",
    "project_stage_closed",
]


class PostBatchTaskRunSummary(BaseModel):
    task_id: int
    run_id: int | None = None
    run_status: str | None = None
    failure_type: str | None = None
    failure_code: str | None = None


class PostBatchResult(BaseModel):
    project_id: int
    plan_version: int
    batch_id: str
    checkpoint_id: str | None = None
    status: PostBatchStatus

    executed_task_ids: list[int] = Field(default_factory=list)
    successful_task_ids: list[int] = Field(default_factory=list)
    problematic_run_ids: list[int] = Field(default_factory=list)

    task_run_summaries: list[PostBatchTaskRunSummary] = Field(default_factory=list)

    recovery_context: RecoveryContext = Field(default_factory=RecoveryContext)
    evaluation_decision: EvaluationDecision | None = None

    continue_execution: bool
    requires_resequencing: bool = False
    requires_replanning: bool = False
    requires_manual_review: bool = False

    is_final_batch: bool = False
    finalization_iteration_count: int = 0
    max_finalization_iterations: int = 2
    finalization_guard_triggered: bool = False

    notes: str | None = None