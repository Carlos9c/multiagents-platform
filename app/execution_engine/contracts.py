from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

EXECUTION_DECISION_COMPLETED = "completed"
EXECUTION_DECISION_PARTIAL = "partial"
EXECUTION_DECISION_FAILED = "failed"
EXECUTION_DECISION_REJECTED = "rejected"

VALID_EXECUTION_DECISIONS = {
    EXECUTION_DECISION_COMPLETED,
    EXECUTION_DECISION_PARTIAL,
    EXECUTION_DECISION_FAILED,
    EXECUTION_DECISION_REJECTED,
}

CHANGE_TYPE_CREATED = "created"
CHANGE_TYPE_MODIFIED = "modified"
CHANGE_TYPE_DELETED = "deleted"

VALID_CHANGE_TYPES = {
    CHANGE_TYPE_CREATED,
    CHANGE_TYPE_MODIFIED,
    CHANGE_TYPE_DELETED,
}

ChangeType = Literal["created", "modified"]


class RelatedTaskSummary(BaseModel):
    task_id: int
    title: str
    status: str
    summary: str | None = None


class ProjectExecutionContext(BaseModel):
    project_id: int
    source_path: str
    workspace_path: str
    relevant_files: list[str] = Field(default_factory=list)
    key_decisions: list[str] = Field(default_factory=list)
    related_tasks: list[RelatedTaskSummary] = Field(default_factory=list)


class HistoricalTaskRunContext(BaseModel):
    task_id: int
    execution_run_id: int
    selection_rule: str
    selection_reason: str

    title: str
    description: str | None = None
    summary: str | None = None
    objective: str | None = None

    run_summary: str | None = None
    completed_scope: str | None = None
    validation_notes: list[str] = Field(default_factory=list)

    changed_files: list[str] = Field(default_factory=list)
    files_read: list[str] = Field(default_factory=list)
    change_dependencies: dict[str, list[str]] = Field(default_factory=dict)


class HistoricalExecutionContext(BaseModel):
    selected_task_runs: list[HistoricalTaskRunContext] = Field(default_factory=list)


class ExecutionRequest(BaseModel):
    task_id: int
    project_id: int
    execution_run_id: int

    task_title: str
    task_description: str | None = None
    task_summary: str | None = None
    objective: str | None = None
    proposed_solution: str | None = None
    implementation_notes: str | None = None
    implementation_steps: str | None = None
    acceptance_criteria: str | None = None
    tests_required: str | None = None
    technical_constraints: str | None = None
    out_of_scope: str | None = None

    executor_type: str

    success_criteria: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)

    allowed_paths: list[str] = Field(default_factory=list)
    blocked_paths: list[str] = Field(default_factory=list)

    context: ProjectExecutionContext
    historical_context: HistoricalExecutionContext | None = None


class ChangedFile(BaseModel):
    path: str
    change_type: ChangeType
    producer: str


class FileReadEvidence(BaseModel):
    path: str
    producer: str
    source: str | None = None


class ChangeDependencyEvidence(BaseModel):
    path: str
    depends_on: list[str] = Field(default_factory=list)
    producer: str


class CommandExecution(BaseModel):
    command: str
    producer: str
    cwd: str | None = None
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False

    verification_goal: str | None = None
    rationale: str | None = None
    validation_claims: list[str] = Field(default_factory=list)
    expected_exit_codes: list[int] = Field(default_factory=list)
    observed_outcome_summary: str | None = None


class NoteEvidence(BaseModel):
    message: str
    producer: str


class ArtifactCreatedEvidence(BaseModel):
    artifact_key: str
    producer: str


class EvidenceItem(BaseModel):
    evidence_type: str
    producer: str
    summary: str
    path: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class ExecutionEvidence(BaseModel):
    """
    Accumulated execution evidence across all subagents involved in one execution run.

    Important:
    - This object is append-only during the orchestrator loop.
    - Callers should use the add_* methods instead of mutating the underlying lists directly.
    - Validation consumes the final accumulated state through ExecutionResult.evidence.
    """

    changed_files: list[ChangedFile] = Field(default_factory=list)
    files_read: list[FileReadEvidence] = Field(default_factory=list)
    change_dependencies: list[ChangeDependencyEvidence] = Field(default_factory=list)
    commands: list[CommandExecution] = Field(default_factory=list)
    notes: list[NoteEvidence] = Field(default_factory=list)
    artifacts_created: list[ArtifactCreatedEvidence] = Field(default_factory=list)

    def add_changed_file(
        self,
        *,
        path: str,
        change_type: ChangeType,
        producer: str,
    ) -> None:
        self.changed_files.append(
            ChangedFile(
                path=path,
                change_type=change_type,
                producer=producer,
            )
        )

    def add_file_read(
        self,
        *,
        path: str,
        producer: str,
        source: str | None = None,
    ) -> None:
        for item in self.files_read:
            if item.path == path and item.producer == producer and item.source == source:
                return

        self.files_read.append(
            FileReadEvidence(
                path=path,
                producer=producer,
                source=source,
            )
        )

    def add_change_dependency(
        self,
        *,
        path: str,
        depends_on: list[str],
        producer: str,
    ) -> None:
        normalized_depends_on = list(dict.fromkeys(dep for dep in depends_on if dep))

        for item in self.change_dependencies:
            if item.path == path and item.producer == producer:
                item.depends_on = list(dict.fromkeys(item.depends_on + normalized_depends_on))
                return

        self.change_dependencies.append(
            ChangeDependencyEvidence(
                path=path,
                depends_on=normalized_depends_on,
                producer=producer,
            )
        )

    def add_command_execution(
        self,
        *,
        command: str,
        producer: str,
        exit_code: int,
        stdout: str,
        stderr: str,
        cwd: str | None = None,
        timed_out: bool = False,
        verification_goal: str | None = None,
        rationale: str | None = None,
        validation_claims: list[str] | None = None,
        expected_exit_codes: list[int] | None = None,
        observed_outcome_summary: str | None = None,
    ) -> None:
        self.commands.append(
            CommandExecution(
                command=command,
                producer=producer,
                cwd=cwd,
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                timed_out=timed_out,
                verification_goal=verification_goal,
                rationale=rationale,
                validation_claims=list(validation_claims or []),
                expected_exit_codes=list(expected_exit_codes or []),
                observed_outcome_summary=observed_outcome_summary,
            )
        )

    def add_note(
        self,
        *,
        message: str,
        producer: str,
    ) -> None:
        self.notes.append(
            NoteEvidence(
                message=message,
                producer=producer,
            )
        )

    def add_artifact_created(
        self,
        *,
        artifact_key: str,
        producer: str,
    ) -> None:
        for item in self.artifacts_created:
            if item.artifact_key == artifact_key and item.producer == producer:
                return

        self.artifacts_created.append(
            ArtifactCreatedEvidence(
                artifact_key=artifact_key,
                producer=producer,
            )
        )

    def has_outputs(self) -> bool:
        return bool(
            self.changed_files
            or self.files_read
            or self.change_dependencies
            or self.commands
            or self.notes
            or self.artifacts_created
        )

    def to_evidence_items(self) -> list[EvidenceItem]:
        items: list[EvidenceItem] = []

        for item in self.changed_files:
            items.append(
                EvidenceItem(
                    evidence_type="changed_file",
                    producer=item.producer,
                    summary=f"{item.change_type} {item.path}",
                    path=item.path,
                    payload=item.model_dump(),
                )
            )

        for item in self.files_read:
            items.append(
                EvidenceItem(
                    evidence_type="file_read",
                    producer=item.producer,
                    summary=f"Read file {item.path}",
                    path=item.path,
                    payload=item.model_dump(),
                )
            )

        for item in self.change_dependencies:
            items.append(
                EvidenceItem(
                    evidence_type="change_dependency",
                    producer=item.producer,
                    summary=f"Registered dependencies for {item.path}",
                    path=item.path,
                    payload=item.model_dump(),
                )
            )

        for item in self.commands:
            items.append(
                EvidenceItem(
                    evidence_type="command_execution",
                    producer=item.producer,
                    summary=f"Executed '{item.command}' with exit code {item.exit_code}",
                    path=None,
                    payload=item.model_dump(),
                )
            )

        for item in self.notes:
            items.append(
                EvidenceItem(
                    evidence_type="note",
                    producer=item.producer,
                    summary=item.message,
                    path=None,
                    payload=item.model_dump(),
                )
            )

        for item in self.artifacts_created:
            items.append(
                EvidenceItem(
                    evidence_type="artifact_created",
                    producer=item.producer,
                    summary=f"Created artifact {item.artifact_key}",
                    path=None,
                    payload=item.model_dump(),
                )
            )

        return items


class ExecutionResult(BaseModel):
    task_id: int
    decision: Literal["completed", "partial", "failed", "rejected"]
    summary: str
    details: str | None = None
    rejection_reason: str | None = None
    completed_scope: str | None = None
    remaining_scope: str | None = None
    blockers_found: list[str] = Field(default_factory=list)
    validation_notes: list[str] = Field(default_factory=list)
    output_snapshot: str | None = None
    execution_agent_sequence: list[str] = Field(default_factory=list)
    evidence: ExecutionEvidence = Field(default_factory=ExecutionEvidence)
