from typing import Literal

from pydantic import BaseModel, Field, field_validator


RecoveryDecisionType = Literal[
    "retry_same_atomic",
    "replace_atomic_task",
    "re_atomize_from_parent",
    "send_to_technical_refiner",
    "manual_review",
    "mark_obsolete",
]

RecoveryConfidence = Literal["low", "medium", "high"]
ReplacementTaskStrategy = Literal[
    "none",
    "single_replacement",
    "multiple_replacements",
]


class RecoveryTaskContext(BaseModel):
    task_id: int
    title: str
    description: str | None = None
    summary: str | None = None
    objective: str | None = None
    task_type: str
    priority: str
    planning_level: str
    executor_type: str
    status: str
    parent_task_id: int | None = None
    parent_refined_title: str | None = None
    parent_high_level_title: str | None = None
    implementation_steps: str | None = None
    acceptance_criteria: str | None = None
    tests_required: str | None = None
    technical_constraints: str | None = None
    out_of_scope: str | None = None


class RecoveryExecutionRunContext(BaseModel):
    run_id: int
    attempt_number: int
    status: str
    input_snapshot: str | None = None
    output_snapshot: str | None = None
    error_message: str | None = None
    failure_type: str | None = None
    failure_code: str | None = None
    recovery_action: str | None = None
    work_summary: str | None = None
    work_details: str | None = None
    artifacts_created: str | None = None
    completed_scope: str | None = None
    remaining_scope: str | None = None
    blockers_found: str | None = None
    validation_notes: str | None = None


class RecoveryArtifactSummary(BaseModel):
    artifact_id: int
    artifact_type: str
    task_id: int | None = None
    summary: str


class RecoveryRecentRunSummary(BaseModel):
    run_id: int
    attempt_number: int
    status: str
    failure_type: str | None = None
    failure_code: str | None = None
    work_summary: str | None = None
    completed_scope: str | None = None
    remaining_scope: str | None = None
    blockers_found: str | None = None


class RecoveryProjectContext(BaseModel):
    project_id: int
    project_name: str
    project_goal: str
    current_execution_objective: str


class RecoveryInput(BaseModel):
    project_context: RecoveryProjectContext
    task: RecoveryTaskContext
    execution_run: RecoveryExecutionRunContext
    recent_runs_for_task: list[RecoveryRecentRunSummary] = Field(default_factory=list)
    relevant_artifacts: list[RecoveryArtifactSummary] = Field(default_factory=list)
    next_batch_summary: str | None = None
    remaining_plan_summary: str | None = None
    coordination_rules_summary: str


class RecoveryProposedTask(BaseModel):
    title: str
    description: str
    objective: str | None = None
    task_type: str = "implementation"
    priority: str = "medium"
    technical_constraints: str | None = None
    out_of_scope: str | None = None


class RecoveryDecision(BaseModel):
    source_task_id: int
    source_run_id: int
    decision_type: RecoveryDecisionType
    confidence: RecoveryConfidence
    reason: str
    still_blocks_progress: bool
    covered_gap_summary: str
    replacement_task_strategy: ReplacementTaskStrategy = "none"
    proposed_tasks: list[RecoveryProposedTask] = Field(default_factory=list)
    should_mark_source_task_obsolete: bool = False
    evaluation_guidance: str
    execution_guidance: str

    @field_validator("proposed_tasks")
    @classmethod
    def validate_proposed_tasks_consistency(
        cls,
        value: list[RecoveryProposedTask],
        info,
    ) -> list[RecoveryProposedTask]:
        strategy = info.data.get("replacement_task_strategy", "none")
        if strategy == "none" and value:
            raise ValueError("proposed_tasks must be empty when replacement_task_strategy='none'.")
        if strategy != "none" and not value:
            raise ValueError("proposed_tasks must not be empty when replacement_task_strategy is not 'none'.")
        return value