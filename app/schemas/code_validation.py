from pydantic import BaseModel, Field

from app.schemas.code_execution import CodeExecutorInput, CodeFileEditPlan, CodeWorkingSet
from app.schemas.project_memory import ProjectOperationalContext


CODE_VALIDATION_DECISION_COMPLETED = "completed"
CODE_VALIDATION_DECISION_PARTIAL = "partial"
CODE_VALIDATION_DECISION_FAILED = "failed"

VALID_CODE_VALIDATION_DECISIONS = {
    CODE_VALIDATION_DECISION_COMPLETED,
    CODE_VALIDATION_DECISION_PARTIAL,
    CODE_VALIDATION_DECISION_FAILED,
}


class CodeValidationCheck(BaseModel):
    name: str
    command: str | None = None
    status: str
    output: str | None = None


class CodeValidationEvidence(BaseModel):
    checked_files: list[str] = Field(default_factory=list)
    observed_changes: list[str] = Field(default_factory=list)
    executed_checks: list[CodeValidationCheck] = Field(default_factory=list)
    check_outputs: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    workspace_diff: str | None = None
    final_file_snapshots: dict[str, str] = Field(default_factory=dict)

    resolved_execution_context: CodeExecutorInput
    working_set: CodeWorkingSet
    edit_plan: CodeFileEditPlan
    project_operational_context: ProjectOperationalContext


class CodeValidationFulfillmentDecision(BaseModel):
    decision: str
    decision_reason: str
    missing_requirements: list[str] = Field(default_factory=list)
    evidence_used: list[str] = Field(default_factory=list)


class CodeValidationResult(BaseModel):
    task_id: int
    decision: str
    decision_reason: str
    missing_requirements: list[str] = Field(default_factory=list)
    evidence_used: list[str] = Field(default_factory=list)
    validation_notes: list[str] = Field(default_factory=list)
    evidence: CodeValidationEvidence