from __future__ import annotations

from pydantic import BaseModel, Field


class WorkflowIterationTrace(BaseModel):
    project_id: int = Field(..., gt=0)
    plan_version: int = Field(..., ge=1)

    batch_internal_id: str = Field(..., min_length=1)
    batch_id: str = Field(..., min_length=1)
    batch_index: int = Field(..., ge=1)
    checkpoint_id: str = Field(..., min_length=1)

    executed_task_ids: list[int] = Field(default_factory=list)
    successful_task_ids: list[int] = Field(default_factory=list)
    problematic_run_ids: list[int] = Field(default_factory=list)
    created_recovery_task_ids: list[int] = Field(default_factory=list)
    source_run_ids_with_recovery: list[int] = Field(default_factory=list)

    resolved_action: str | None = None
    decision_signals_used: list[str] = Field(default_factory=list)

    mutation_kind: str | None = None
    patched_plan_version: int | None = Field(default=None, ge=1)
    assigned_task_ids: list[int] = Field(default_factory=list)
    unassigned_task_ids: list[int] = Field(default_factory=list)

    preexisting_pending_valid_task_count: int = Field(default=0, ge=0)
    new_recovery_pending_task_count: int = Field(default=0, ge=0)

    continue_execution: bool
    requires_resequencing: bool
    requires_replanning: bool
    requires_manual_review: bool

    is_final_batch: bool
    finalization_iteration_count: int = Field(..., ge=0)
    max_finalization_iterations: int = Field(..., ge=0)
    finalization_guard_triggered: bool = False

    notes: str = Field(..., min_length=5)