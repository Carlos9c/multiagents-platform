from pydantic import BaseModel, Field


CODE_GENERATION_DECISION_PROCEED = "proceed"
CODE_GENERATION_DECISION_REJECT = "reject"

VALID_CODE_GENERATION_DECISIONS = {
    CODE_GENERATION_DECISION_PROCEED,
    CODE_GENERATION_DECISION_REJECT,
}

CODE_GENERATION_ACTION_CREATE = "create"
CODE_GENERATION_ACTION_MODIFY = "modify"

VALID_CODE_GENERATION_ACTIONS = {
    CODE_GENERATION_ACTION_CREATE,
    CODE_GENERATION_ACTION_MODIFY,
}


class CodeGenerationPlanFileChange(BaseModel):
    path: str
    action: str
    purpose: str
    rationale: str


class CodeGenerationPlanResponse(BaseModel):
    """
    LLM output for the planning phase of the code executor.
    It may either:
    - proceed with a concrete edit plan
    - reject execution because the task is not safely executable as-is
    """

    decision: str
    summary: str
    planned_changes: list[CodeGenerationPlanFileChange] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    local_risks: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    rejection_reason: str | None = None
    remaining_scope: str | None = None
    blockers_found: str | None = None


class CodeGenerationFileContent(BaseModel):
    path: str
    action: str
    content: str
    rationale: str


class CodeGenerationFilesResponse(BaseModel):
    """
    LLM output for file materialization after an edit plan was approved.
    """

    summary: str
    generated_files: list[CodeGenerationFileContent] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)