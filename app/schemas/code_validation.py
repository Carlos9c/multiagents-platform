from pydantic import BaseModel, Field


CODE_VALIDATION_DECIDED_STATUS_COMPLETED = "completed"
CODE_VALIDATION_DECIDED_STATUS_PARTIAL = "partial"
CODE_VALIDATION_DECIDED_STATUS_FAILED = "failed"

VALID_CODE_VALIDATION_DECIDED_STATUSES = {
    CODE_VALIDATION_DECIDED_STATUS_COMPLETED,
    CODE_VALIDATION_DECIDED_STATUS_PARTIAL,
    CODE_VALIDATION_DECIDED_STATUS_FAILED,
}


class CodeValidationEvidence(BaseModel):
    """
    Structured evidence collected during validation.
    """

    checked_files: list[str] = Field(default_factory=list)
    observed_changes: list[str] = Field(default_factory=list)
    executed_checks: list[str] = Field(default_factory=list)
    check_outputs: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    workspace_diff: str | None = None

    final_file_snapshots: dict[str, str] = Field(default_factory=dict)
    execution_summary: str | None = None
    edit_plan_summary: str | None = None


class CodeValidationFulfillmentDecision(BaseModel):
    """
    Strict semantic decision produced by the fulfillment validator.

    It must answer only whether the produced resolution satisfies the task.
    """

    decided_task_status: str
    decision_reason: str
    missing_requirements: list[str] = Field(default_factory=list)
    evidence_used: list[str] = Field(default_factory=list)


class CodeValidationResult(BaseModel):
    """
    Final validation output for a code task.
    """

    task_id: int
    execution_run_id: int
    decided_task_status: str
    reasons: list[str] = Field(default_factory=list)
    unresolved_gaps: list[str] = Field(default_factory=list)
    evidence: CodeValidationEvidence