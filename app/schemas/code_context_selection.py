from pydantic import BaseModel, Field

from app.schemas.project_memory import ProjectOperationalContext


SELECTED_FILE_ROLE_TARGET = "target"
SELECTED_FILE_ROLE_RELATED = "related"
SELECTED_FILE_ROLE_REFERENCE = "reference"
SELECTED_FILE_ROLE_TEST = "test"

VALID_SELECTED_FILE_ROLES = {
    SELECTED_FILE_ROLE_TARGET,
    SELECTED_FILE_ROLE_RELATED,
    SELECTED_FILE_ROLE_REFERENCE,
    SELECTED_FILE_ROLE_TEST,
}


class CodeContextTaskPayload(BaseModel):
    task_id: int
    title: str
    description: str | None = None
    summary: str | None = None
    objective: str | None = None
    proposed_solution: str | None = None
    implementation_notes: str | None = None
    implementation_steps: str | None = None
    acceptance_criteria: str | None = None
    tests_required: str | None = None
    technical_constraints: str | None = None
    out_of_scope: str | None = None
    task_type: str
    priority: str
    planning_level: str
    executor_type: str
    status: str


class CodeContextTaskHierarchy(BaseModel):
    atomic_task_id: int
    atomic_title: str
    parent_refined_task_id: int | None = None
    parent_refined_title: str | None = None
    parent_refined_summary: str | None = None
    parent_refined_objective: str | None = None
    parent_high_level_task_id: int | None = None
    parent_high_level_title: str | None = None
    parent_high_level_summary: str | None = None
    parent_high_level_objective: str | None = None


class RelatedTaskContext(BaseModel):
    task_id: int
    title: str
    relationship_reason: str
    task_status: str
    planning_level: str
    last_run_status: str | None = None
    work_summary: str | None = None
    completed_scope: str | None = None
    remaining_scope: str | None = None
    blockers_found: str | None = None
    validation_notes: str | None = None
    artifact_summaries: list[str] = Field(default_factory=list)
    referenced_paths: list[str] = Field(default_factory=list)


class RepositoryFileDescriptor(BaseModel):
    path: str
    module_hint: str | None = None
    symbols: list[str] = Field(default_factory=list)
    summary: str | None = None


class RepositoryIndexSnapshot(BaseModel):
    repo_root: str
    total_files: int
    files: list[RepositoryFileDescriptor] = Field(default_factory=list)


class CandidatePathSignal(BaseModel):
    path: str
    score: float
    reasons: list[str] = Field(default_factory=list)
    source_types: list[str] = Field(default_factory=list)


class CodeContextSelectionConstraints(BaseModel):
    max_related_tasks: int = 10
    max_candidate_paths: int = 30
    max_primary_targets: int = 5
    max_related_files: int = 8
    max_reference_files: int = 4
    max_related_test_files: int = 4
    max_total_files: int = 16


class CodeContextSelectionInput(BaseModel):
    project_id: int
    task_id: int
    execution_run_id: int | None = None
    task: CodeContextTaskPayload
    hierarchy: CodeContextTaskHierarchy
    project_operational_context: ProjectOperationalContext
    related_tasks: list[RelatedTaskContext] = Field(default_factory=list)
    repository_index: RepositoryIndexSnapshot
    candidate_paths: list[CandidatePathSignal] = Field(default_factory=list)
    context_gaps: list[str] = Field(default_factory=list)
    constraints: CodeContextSelectionConstraints = Field(
        default_factory=CodeContextSelectionConstraints
    )


class SelectedCodeFile(BaseModel):
    path: str
    role: str
    why_selected: str
    selection_score: float = Field(ge=0.0, le=1.0)
    expected_usage: str | None = None
    derived_from: list[str] = Field(default_factory=list)


class CodeContextSourcesUsed(BaseModel):
    used_project_memory: bool = False
    related_task_ids: list[int] = Field(default_factory=list)
    candidate_paths_considered: int = 0
    repository_paths_considered: int = 0


class CodeContextEvidenceSummary(BaseModel):
    strengths: list[str] = Field(default_factory=list)
    caution_signals: list[str] = Field(default_factory=list)


class CodeContextSelectionResult(BaseModel):
    task_id: int
    project_id: int
    confidence_score: float = Field(ge=0.0, le=1.0)

    primary_targets: list[SelectedCodeFile] = Field(default_factory=list)
    related_files: list[SelectedCodeFile] = Field(default_factory=list)
    reference_files: list[SelectedCodeFile] = Field(default_factory=list)
    related_test_files: list[SelectedCodeFile] = Field(default_factory=list)

    candidate_file_pool: list[str] = Field(default_factory=list)
    relevant_symbols: list[str] = Field(default_factory=list)
    candidate_modules: list[str] = Field(default_factory=list)
    relevant_decisions: list[str] = Field(default_factory=list)
    context_gaps: list[str] = Field(default_factory=list)

    selection_rationale: str
    evidence_summary: CodeContextEvidenceSummary = Field(
        default_factory=CodeContextEvidenceSummary
    )
    context_sources_used: CodeContextSourcesUsed