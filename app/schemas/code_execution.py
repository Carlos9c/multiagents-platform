from pydantic import BaseModel, Field


CODE_EXECUTION_STATUS_AWAITING_VALIDATION = "awaiting_validation"
CODE_EXECUTION_STATUS_FAILED = "failed"
CODE_EXECUTION_STATUS_REJECTED = "rejected"

VALID_CODE_EXECUTION_STATUSES = {
    CODE_EXECUTION_STATUS_AWAITING_VALIDATION,
    CODE_EXECUTION_STATUS_FAILED,
    CODE_EXECUTION_STATUS_REJECTED,
}

CODE_FILE_ROLE_TARGET = "target"
CODE_FILE_ROLE_RELATED = "related"
CODE_FILE_ROLE_REFERENCE = "reference"
CODE_FILE_ROLE_TEST = "test"

VALID_CODE_FILE_ROLES = {
    CODE_FILE_ROLE_TARGET,
    CODE_FILE_ROLE_RELATED,
    CODE_FILE_ROLE_REFERENCE,
    CODE_FILE_ROLE_TEST,
}

CODE_FILE_ACTION_CREATE = "create"
CODE_FILE_ACTION_MODIFY = "modify"

VALID_CODE_FILE_ACTIONS = {
    CODE_FILE_ACTION_CREATE,
    CODE_FILE_ACTION_MODIFY,
}


class CodeExecutorInput(BaseModel):
    """
    Resolved execution context for a code task.

    candidate_files is the broader candidate pool.
    The real semantic grouping used by build_working_set is:
    - primary_targets
    - related_files
    - reference_files
    - related_test_files
    """

    task_id: int
    project_id: int

    title: str
    description: str | None = None
    objective: str | None = None
    acceptance_criteria: str | None = None
    technical_constraints: str | None = None
    out_of_scope: str | None = None

    execution_goal: str
    repo_root: str

    relevant_decisions: list[str] = Field(default_factory=list)
    candidate_modules: list[str] = Field(default_factory=list)
    candidate_files: list[str] = Field(default_factory=list)

    primary_targets: list[str] = Field(default_factory=list)
    related_files: list[str] = Field(default_factory=list)
    reference_files: list[str] = Field(default_factory=list)
    related_test_files: list[str] = Field(default_factory=list)

    relevant_symbols: list[str] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)

    selection_rationale: str | None = None
    selection_confidence: float | None = None


class CodeFileContext(BaseModel):
    path: str
    role: str
    content: str | None = None
    summary: str | None = None
    relevant_snippets: list[str] = Field(default_factory=list)
    symbols: list[str] = Field(default_factory=list)


class CodeWorkingSet(BaseModel):
    """
    Concrete subset of repository context used by the code executor.
    """

    repo_root: str
    target_files: list[str] = Field(default_factory=list)
    related_files: list[str] = Field(default_factory=list)
    reference_files: list[str] = Field(default_factory=list)
    test_files: list[str] = Field(default_factory=list)
    files: list[CodeFileContext] = Field(default_factory=list)
    repo_guidance: list[str] = Field(default_factory=list)


class PlannedFileChange(BaseModel):
    path: str
    action: str
    purpose: str
    rationale: str


class CodeFileEditPlan(BaseModel):
    task_id: int
    summary: str
    planned_changes: list[PlannedFileChange] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    local_risks: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class WorkspaceChangeSet(BaseModel):
    created_files: list[str] = Field(default_factory=list)
    modified_files: list[str] = Field(default_factory=list)
    deleted_files: list[str] = Field(default_factory=list)
    renamed_files: list[str] = Field(default_factory=list)
    diff_summary: str | None = None
    impacted_areas: list[str] = Field(default_factory=list)


class ExecutionJournal(BaseModel):
    task_id: int
    summary: str
    local_decisions: list[str] = Field(default_factory=list)
    claimed_completed_scope: str | None = None
    claimed_remaining_scope: str | None = None
    encountered_uncertainties: list[str] = Field(default_factory=list)
    notes_for_validator: list[str] = Field(default_factory=list)


class CodeExecutorResult(BaseModel):
    task_id: int
    execution_status: str

    input: CodeExecutorInput
    working_set: CodeWorkingSet
    edit_plan: CodeFileEditPlan
    workspace_changes: WorkspaceChangeSet
    journal: ExecutionJournal

    output_snapshot: str | None = None