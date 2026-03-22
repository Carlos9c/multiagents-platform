from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.schemas.project_memory import ProjectOperationalContext


EvaluationDecisionType = Literal[
    "approve_continue",
    "request_corrections",
    "insert_new_tasks",
    "resequence_remaining_tasks",
    "replan_from_level",
    "manual_review",
]

EvaluationConfidence = Literal["low", "medium", "high"]
ImpactLevel = Literal["critical", "moderate", "low"]
SuggestedPosition = Literal[
    "before_next_batch",
    "inside_next_batch",
    "after_next_checkpoint",
    "future_backlog",
]
ReplanLevel = Literal["atomic", "refined", "high_level"]


class ExecutedTaskDelta(BaseModel):
    task_id: int
    title: str
    task_status: str
    last_run_status: str | None = None
    work_summary: str | None = None
    completed_scope: str | None = None
    remaining_scope: str | None = None
    blockers_found: str | None = None
    validation_notes: str | None = None


class ArtifactDelta(BaseModel):
    artifact_id: int
    artifact_type: str
    task_id: int | None = None
    summary: str


class NextBatchContext(BaseModel):
    batch_id: str
    name: str
    goal: str
    task_ids: list[int] = Field(default_factory=list)
    entry_conditions: list[str] = Field(default_factory=list)
    expected_outputs: list[str] = Field(default_factory=list)
    risk_level: str

    @field_validator("task_ids")
    @classmethod
    def validate_task_ids_not_empty(cls, value: list[int]) -> list[int]:
        if not value:
            raise ValueError("NextBatchContext.task_ids cannot be empty.")
        return value


class RemainingPlanSummary(BaseModel):
    pending_batch_ids: list[str] = Field(default_factory=list)
    pending_task_ids: list[int] = Field(default_factory=list)
    blocked_task_ids: list[int] = Field(default_factory=list)
    sequencing_rationale: str | None = None


class RecoveryDecisionSummary(BaseModel):
    source_task_id: int
    source_run_id: int | None = None
    run_status: str
    decision_type: str
    reason: str
    replacement_task_ids: list[int] = Field(default_factory=list)
    still_blocks_progress: bool
    covered_gap_summary: str


class RecoveryOpenIssue(BaseModel):
    task_id: int
    issue_type: str
    remaining_scope: str | None = None
    blockers_found: str | None = None
    why_it_still_matters: str


class RecoveryCreatedTaskSummary(BaseModel):
    task_id: int
    title: str
    objective: str | None = None
    origin: str = "recovery_agent"
    source_task_id: int


class RecoveryContext(BaseModel):
    recovery_decisions: list[RecoveryDecisionSummary] = Field(default_factory=list)
    open_issues: list[RecoveryOpenIssue] = Field(default_factory=list)
    recovery_created_tasks: list[RecoveryCreatedTaskSummary] = Field(default_factory=list)


class TaskArtifactEvidence(BaseModel):
    artifact_id: int
    artifact_type: str
    content_excerpt: str
    full_content_included: bool = False


class TaskExecutionEvidence(BaseModel):
    run_id: int | None = None
    run_status: str | None = None
    work_summary: str | None = None
    work_details: str | None = None
    completed_scope: str | None = None
    remaining_scope: str | None = None
    blockers_found: str | None = None
    validation_notes: str | None = None
    error_message: str | None = None


class EvaluatedTaskEvidence(BaseModel):
    task_id: int
    title: str
    description: str | None = None
    objective: str | None = None
    acceptance_criteria: str | None = None
    tests_required: str | None = None
    execution_evidence: TaskExecutionEvidence
    artifact_evidence: list[TaskArtifactEvidence] = Field(default_factory=list)


class ContentEvidence(BaseModel):
    evaluated_tasks: list[EvaluatedTaskEvidence] = Field(default_factory=list)


class EvaluationInput(BaseModel):
    project_id: int
    project_name: str
    project_goal: str
    current_execution_objective: str
    plan_version: int
    checkpoint_id: str
    checkpoint_name: str
    checkpoint_reason: str
    checkpoint_evaluation_goal: str
    project_operational_context: ProjectOperationalContext
    executed_tasks_since_last_checkpoint: list[ExecutedTaskDelta] = Field(default_factory=list)
    artifacts_since_last_checkpoint: list[ArtifactDelta] = Field(default_factory=list)
    current_project_state_summary: str
    next_batch: NextBatchContext | None = None
    remaining_plan: RemainingPlanSummary
    recovery_context: RecoveryContext = Field(default_factory=RecoveryContext)
    content_evidence: ContentEvidence = Field(default_factory=ContentEvidence)


class ProposedTaskImpact(BaseModel):
    impact_level: ImpactLevel
    impact_reason: str
    blocks_continuation: bool
    requires_resequencing: bool
    suggested_position: SuggestedPosition
    affected_task_ids: list[int] = Field(default_factory=list)
    risk_if_postponed: str


class ProposedTask(BaseModel):
    title: str
    description: str
    objective: str | None = None
    task_type: str = "implementation"
    priority: str = "medium"
    technical_constraints: str | None = None
    out_of_scope: str | None = None
    impact: ProposedTaskImpact


class EvaluationDecision(BaseModel):
    checkpoint_id: str
    decision_type: EvaluationDecisionType
    continue_execution: bool
    reason: str
    confidence: EvaluationConfidence
    project_state_summary: str
    detected_gaps: list[str] = Field(default_factory=list)
    proposed_new_tasks: list[ProposedTask] = Field(default_factory=list)
    resequence_remaining_tasks: bool = False
    updated_execution_guidance: str | None = None
    replan_from_level: ReplanLevel | None = None

    @field_validator("replan_from_level")
    @classmethod
    def validate_replan_from_level(cls, value: str | None, info) -> str | None:
        decision_type = info.data.get("decision_type")
        if decision_type == "replan_from_level" and not value:
            raise ValueError("replan_from_level is required when decision_type='replan_from_level'.")
        return value