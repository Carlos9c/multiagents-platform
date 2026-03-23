from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models.task import (
    TASK_STATUS_AWAITING_VALIDATION,
    TASK_STATUS_COMPLETED,
    TASK_STATUS_FAILED,
    TASK_STATUS_PARTIAL,
    Task,
)
from app.schemas.code_execution import (
    CODE_EXECUTION_STATUS_AWAITING_VALIDATION,
    CODE_EXECUTION_STATUS_FAILED,
    CODE_EXECUTION_STATUS_REJECTED,
    CodeExecutorResult,
)
from app.schemas.code_validation import (
    CODE_VALIDATION_DECISION_COMPLETED,
    CODE_VALIDATION_DECISION_FAILED,
    CODE_VALIDATION_DECISION_PARTIAL,
    CodeValidationCheck,
    CodeValidationEvidence,
    CodeValidationResult,
)
from app.services.artifacts import create_artifact
from app.services.code_validation_client import (
    CodeValidatorClientError,
    evaluate_code_task_fulfillment,
)
from app.services.local_workspace_runtime import LocalWorkspaceRuntime
from app.services.project_memory_service import build_project_operational_context
from app.services.project_storage import CODE_DOMAIN, ProjectStorageService
from app.services.tasks import (
    mark_task_awaiting_validation,
    mark_task_completed,
    mark_task_failed,
    mark_task_partial,
)
from app.services.workspace_runtime import WorkspaceRuntimeError


CODE_VALIDATION_RESULT_ARTIFACT_TYPE = "code_validation_result"

NORMAL_VALIDATION_ALLOWED_EXECUTION_STATUSES = {
    CODE_EXECUTION_STATUS_AWAITING_VALIDATION,
}

TERMINAL_PREVALIDATION_EXECUTION_STATUSES = {
    CODE_EXECUTION_STATUS_FAILED,
    CODE_EXECUTION_STATUS_REJECTED,
}

SUPPORTED_VALIDATION_DECISIONS = {
    CODE_VALIDATION_DECISION_COMPLETED,
    CODE_VALIDATION_DECISION_PARTIAL,
    CODE_VALIDATION_DECISION_FAILED,
}


class CodeValidatorError(Exception):
    """Base error for code validation."""


class TaskValidationServiceError(Exception):
    """Base error for task validation service orchestration."""


@dataclass
class TaskValidationServiceResult:
    task_id: int
    execution_run_id: int
    validation_result: CodeValidationResult
    final_task_status: str


class BaseCodeValidator(ABC):
    @abstractmethod
    def validate(
        self,
        task: Task,
        execution_run_id: int,
        executor_result: CodeExecutorResult,
    ) -> CodeValidationResult:
        raise NotImplementedError


class LocalCodeValidator(BaseCodeValidator):
    def __init__(
        self,
        db: Session,
        storage_service: ProjectStorageService | None = None,
        workspace_runtime: LocalWorkspaceRuntime | None = None,
    ):
        self.db = db
        self.storage_service = storage_service or ProjectStorageService()
        self.workspace_runtime = workspace_runtime or LocalWorkspaceRuntime(
            storage_service=self.storage_service
        )

    def _persist_validation_artifact(
        self,
        task: Task,
        validation_result: CodeValidationResult,
    ) -> int:
        artifact = create_artifact(
            db=self.db,
            project_id=task.project_id,
            task_id=task.id,
            artifact_type=CODE_VALIDATION_RESULT_ARTIFACT_TYPE,
            content=validation_result.model_dump_json(indent=2),
            created_by="code_validator",
        )
        return artifact.id

    @staticmethod
    def _dedupe_keep_order(paths: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()

        for path in paths:
            if not path or path in seen:
                continue
            seen.add(path)
            result.append(path)

        return result

    def _collect_candidate_snapshot_paths(
        self,
        executor_result: CodeExecutorResult,
    ) -> list[str]:
        paths: list[str] = []

        paths.extend(executor_result.input.primary_targets)
        paths.extend(executor_result.input.related_files)
        paths.extend(executor_result.input.reference_files)
        paths.extend(executor_result.input.related_test_files)
        paths.extend(executor_result.working_set.target_files)
        paths.extend(executor_result.working_set.related_files)
        paths.extend(executor_result.working_set.reference_files)
        paths.extend(executor_result.working_set.test_files)
        paths.extend([item.path for item in executor_result.edit_plan.planned_changes])
        paths.extend(executor_result.workspace_changes.created_files)
        paths.extend(executor_result.workspace_changes.modified_files)

        return self._dedupe_keep_order(paths)

    def _read_final_file_snapshots(
        self,
        task: Task,
        execution_run_id: int,
        candidate_paths: list[str],
    ) -> dict[str, str]:
        workspace_paths = self.workspace_runtime.get_execution_workspace_paths(
            project_id=task.project_id,
            execution_run_id=execution_run_id,
        )
        workspace_dir = workspace_paths.workspace_dir

        snapshots: dict[str, str] = {}

        for path in candidate_paths:
            try:
                if self.workspace_runtime.file_exists(workspace_dir, path):
                    snapshots[path] = self.workspace_runtime.read_file(workspace_dir, path)
            except WorkspaceRuntimeError:
                continue

        return snapshots

    @staticmethod
    def _build_observed_changes(executor_result: CodeExecutorResult) -> list[str]:
        actual_changes = executor_result.workspace_changes
        observed: list[str] = []

        if actual_changes.created_files:
            observed.append(f"Created files: {', '.join(actual_changes.created_files)}")
        if actual_changes.modified_files:
            observed.append(f"Modified files: {', '.join(actual_changes.modified_files)}")
        if actual_changes.deleted_files:
            observed.append(f"Deleted files: {', '.join(actual_changes.deleted_files)}")
        if actual_changes.renamed_files:
            observed.append(f"Renamed files: {', '.join(actual_changes.renamed_files)}")
        if actual_changes.diff_summary:
            observed.append(f"Diff summary: {actual_changes.diff_summary}")

        return observed

    def _build_short_circuit_result(
        self,
        task: Task,
        executor_result: CodeExecutorResult,
        decision: str,
        decision_reason: str,
        warning: str,
        *,
        terminal_mode: str,
    ) -> CodeValidationResult:
        evidence = CodeValidationEvidence(
            checked_files=[],
            observed_changes=[],
            executed_checks=[
                CodeValidationCheck(
                    name="executor_status_short_circuit",
                    command=None,
                    status="completed",
                    output=warning,
                )
            ],
            check_outputs=[],
            warnings=[warning],
            workspace_diff=None,
            final_file_snapshots={},
            resolved_execution_context=executor_result.input,
            working_set=executor_result.working_set,
            edit_plan=executor_result.edit_plan,
            project_operational_context=build_project_operational_context(
                db=self.db,
                project_id=task.project_id,
            ),
        )

        validation_result = CodeValidationResult(
            task_id=task.id,
            decision=decision,
            decision_reason=decision_reason,
            missing_requirements=[],
            evidence_used=[
                "executor execution_status",
                "executor journal summary",
                "resolved execution context",
            ],
            validation_notes=[
                (
                    "Validation short-circuited because executor status was "
                    f"'{executor_result.execution_status}' in terminal_mode='{terminal_mode}'."
                ),
                decision_reason,
            ],
            evidence=evidence,
        )

        artifact_id = self._persist_validation_artifact(task, validation_result)
        validation_result.validation_notes.append(
            f"Structured validator artifact persisted as artifact_id={artifact_id}."
        )

        return validation_result

    def _build_evidence(
        self,
        task: Task,
        execution_run_id: int,
        executor_result: CodeExecutorResult,
    ) -> CodeValidationEvidence:
        checked_files = self._collect_candidate_snapshot_paths(executor_result)

        actual_changes = self.workspace_runtime.collect_changes(
            project_id=task.project_id,
            execution_run_id=execution_run_id,
            domain_name=CODE_DOMAIN,
        )
        workspace_diff = self.workspace_runtime.generate_diff(
            project_id=task.project_id,
            execution_run_id=execution_run_id,
            domain_name=CODE_DOMAIN,
        )

        final_file_snapshots = self._read_final_file_snapshots(
            task=task,
            execution_run_id=execution_run_id,
            candidate_paths=checked_files,
        )

        observed_changes = self._build_observed_changes(executor_result)

        executed_checks = [
            CodeValidationCheck(
                name="workspace_collect_changes",
                command=None,
                status="completed",
                output="Collected workspace change set successfully.",
            ),
            CodeValidationCheck(
                name="workspace_generate_diff",
                command=None,
                status="completed",
                output="Generated workspace diff successfully.",
            ),
            CodeValidationCheck(
                name="final_file_snapshot_read",
                command=None,
                status="completed",
                output=f"Collected {len(final_file_snapshots)} final file snapshots.",
            ),
            CodeValidationCheck(
                name="fulfillment_decision_llm",
                command=None,
                status="pending",
                output="Structured semantic fulfillment validation pending.",
            ),
        ]

        check_outputs: list[str] = []
        warnings: list[str] = []

        if not actual_changes.created_files and not actual_changes.modified_files:
            warnings.append(
                "No created or modified files were observed in the workspace change set."
            )

        project_operational_context = build_project_operational_context(
            db=self.db,
            project_id=task.project_id,
        )

        return CodeValidationEvidence(
            checked_files=checked_files,
            observed_changes=observed_changes,
            executed_checks=executed_checks,
            check_outputs=check_outputs,
            warnings=warnings,
            workspace_diff=workspace_diff,
            final_file_snapshots=final_file_snapshots,
            resolved_execution_context=executor_result.input,
            working_set=executor_result.working_set,
            edit_plan=executor_result.edit_plan,
            project_operational_context=project_operational_context,
        )

    def _build_validation_notes(
        self,
        task: Task,
        decision: str,
        decision_reason: str,
        missing_requirements: list[str],
        evidence: CodeValidationEvidence,
    ) -> list[str]:
        notes: list[str] = []

        notes.append(f"Task {task.id} validation decision: {decision}.")
        notes.append(decision_reason)

        if evidence.resolved_execution_context.selection_rationale:
            notes.append(
                "Selection rationale considered during validation: "
                f"{evidence.resolved_execution_context.selection_rationale}"
            )

        if evidence.resolved_execution_context.unresolved_questions:
            notes.append(
                "Context gaps considered during validation: "
                + "; ".join(evidence.resolved_execution_context.unresolved_questions[:8])
            )

        if missing_requirements:
            notes.append(
                "Missing requirements detected: "
                + "; ".join(missing_requirements[:10])
            )

        return notes

    def validate(
        self,
        task: Task,
        execution_run_id: int,
        executor_result: CodeExecutorResult,
    ) -> CodeValidationResult:
        if task.status != TASK_STATUS_AWAITING_VALIDATION:
            raise CodeValidatorError(
                f"Task {task.id} is not awaiting validation. Current status='{task.status}'."
            )

        if executor_result.execution_status in TERMINAL_PREVALIDATION_EXECUTION_STATUSES:
            raise CodeValidatorError(
                "Terminal executor statuses must use validate_terminal_result(), "
                f"received '{executor_result.execution_status}'."
            )

        if executor_result.execution_status not in NORMAL_VALIDATION_ALLOWED_EXECUTION_STATUSES:
            raise CodeValidatorError(
                f"Unsupported executor status '{executor_result.execution_status}' "
                "received by code validator."
            )

        try:
            evidence = self._build_evidence(
                task=task,
                execution_run_id=execution_run_id,
                executor_result=executor_result,
            )
        except WorkspaceRuntimeError as exc:
            evidence = CodeValidationEvidence(
                checked_files=[],
                observed_changes=[],
                executed_checks=[
                    CodeValidationCheck(
                        name="workspace_validation_failure",
                        command=None,
                        status="failed",
                        output=f"Workspace validation failed: {str(exc)}",
                    )
                ],
                check_outputs=[],
                warnings=[f"Workspace validation failed: {str(exc)}"],
                workspace_diff=None,
                final_file_snapshots={},
                resolved_execution_context=executor_result.input,
                working_set=executor_result.working_set,
                edit_plan=executor_result.edit_plan,
                project_operational_context=build_project_operational_context(
                    db=self.db,
                    project_id=task.project_id,
                ),
            )

            validation_result = CodeValidationResult(
                task_id=task.id,
                decision=CODE_VALIDATION_DECISION_FAILED,
                decision_reason=(
                    "Validation could not inspect the execution workspace safely enough to confirm task fulfillment."
                ),
                missing_requirements=[],
                evidence_used=[
                    "workspace inspection failure",
                    "executor journal summary",
                    "resolved execution context",
                ],
                validation_notes=[
                    "Workspace validation failed before semantic fulfillment could be evaluated.",
                    f"Workspace error: {str(exc)}",
                ],
                evidence=evidence,
            )
            artifact_id = self._persist_validation_artifact(task, validation_result)
            validation_result.validation_notes.append(
                f"Structured validator artifact persisted as artifact_id={artifact_id}."
            )
            return validation_result

        try:
            fulfillment_decision = evaluate_code_task_fulfillment(
                task=task,
                executor_result=executor_result,
                evidence=evidence,
            )
        except CodeValidatorClientError as exc:
            evidence.warnings.append(
                f"Semantic fulfillment validation failed: {str(exc)}"
            )
            validation_result = CodeValidationResult(
                task_id=task.id,
                decision=CODE_VALIDATION_DECISION_FAILED,
                decision_reason=(
                    "Validation could not reach a reliable fulfillment decision for the task."
                ),
                missing_requirements=[],
                evidence_used=[
                    "workspace diff",
                    "final file snapshots",
                    "resolved execution context",
                    "validator model failure",
                ],
                validation_notes=[
                    "Semantic fulfillment validation failed before a reliable decision could be produced.",
                    f"Validator client error: {str(exc)}",
                ],
                evidence=evidence,
            )
            artifact_id = self._persist_validation_artifact(task, validation_result)
            validation_result.validation_notes.append(
                f"Structured validator artifact persisted as artifact_id={artifact_id}."
            )
            return validation_result

        if fulfillment_decision.decision not in SUPPORTED_VALIDATION_DECISIONS:
            raise CodeValidatorError(
                f"Unsupported validation decision '{fulfillment_decision.decision}'."
            )

        for check in evidence.executed_checks:
            if check.name == "fulfillment_decision_llm" and check.status == "pending":
                check.status = "completed"
                check.output = "Structured semantic fulfillment validation completed."

        evidence.check_outputs.append(
            f"Fulfillment evidence used: {', '.join(fulfillment_decision.evidence_used)}"
        )

        validation_result = CodeValidationResult(
            task_id=task.id,
            decision=fulfillment_decision.decision,
            decision_reason=fulfillment_decision.decision_reason,
            missing_requirements=fulfillment_decision.missing_requirements,
            evidence_used=fulfillment_decision.evidence_used,
            validation_notes=self._build_validation_notes(
                task=task,
                decision=fulfillment_decision.decision,
                decision_reason=fulfillment_decision.decision_reason,
                missing_requirements=fulfillment_decision.missing_requirements,
                evidence=evidence,
            ),
            evidence=evidence,
        )

        artifact_id = self._persist_validation_artifact(task, validation_result)
        validation_result.validation_notes.append(
            f"Structured validator artifact persisted as artifact_id={artifact_id}."
        )

        return validation_result

    def validate_terminal_result(
        self,
        task: Task,
        execution_run_id: int,
        executor_result: CodeExecutorResult,
    ) -> CodeValidationResult:
        del execution_run_id

        if executor_result.execution_status == CODE_EXECUTION_STATUS_REJECTED:
            return self._build_short_circuit_result(
                task=task,
                executor_result=executor_result,
                decision=CODE_VALIDATION_DECISION_FAILED,
                decision_reason=(
                    "The executor rejected the task before producing a result that could satisfy the requested resolution."
                ),
                warning="Executor rejected the task before a valid operational execution pass.",
                terminal_mode="rejected_pre_validation",
            )

        if executor_result.execution_status == CODE_EXECUTION_STATUS_FAILED:
            return self._build_short_circuit_result(
                task=task,
                executor_result=executor_result,
                decision=CODE_VALIDATION_DECISION_FAILED,
                decision_reason=(
                    "The executor failed before producing a result that could satisfy the task."
                ),
                warning="Executor failed before a valid operational execution pass.",
                terminal_mode="failed_pre_validation",
            )

        raise CodeValidatorError(
            "validate_terminal_result() only accepts pre-validation terminal executor states. "
            f"Received '{executor_result.execution_status}'."
        )


def apply_validation_decision_to_task(
    db: Session,
    *,
    task_id: int,
    decision: str,
) -> str:
    if decision == CODE_VALIDATION_DECISION_COMPLETED:
        updated_task = mark_task_completed(db, task_id)
        final_status = TASK_STATUS_COMPLETED
    elif decision == CODE_VALIDATION_DECISION_PARTIAL:
        updated_task = mark_task_partial(db, task_id)
        final_status = TASK_STATUS_PARTIAL
    elif decision == CODE_VALIDATION_DECISION_FAILED:
        updated_task = mark_task_failed(db, task_id)
        final_status = TASK_STATUS_FAILED
    else:
        raise TaskValidationServiceError(
            f"Unsupported validation decision '{decision}'."
        )

    if updated_task is None:
        raise TaskValidationServiceError(
            f"Task {task_id} could not be updated after validation."
        )

    return final_status


def _get_task_or_raise(db: Session, task_id: int) -> Task:
    task = db.get(Task, task_id)
    if not task:
        raise TaskValidationServiceError(f"Task {task_id} not found")
    return task


def validate_code_task(
    db: Session,
    *,
    task_id: int,
    execution_run_id: int,
    executor_result: CodeExecutorResult,
    apply_final_status: bool = True,
) -> TaskValidationServiceResult:
    task = _get_task_or_raise(db, task_id)

    if executor_result.execution_status not in NORMAL_VALIDATION_ALLOWED_EXECUTION_STATUSES:
        raise TaskValidationServiceError(
            "validate_code_task() only supports normal post-execution validation for "
            f"statuses {sorted(NORMAL_VALIDATION_ALLOWED_EXECUTION_STATUSES)}. "
            f"Received '{executor_result.execution_status}'."
        )

    if task.status != TASK_STATUS_AWAITING_VALIDATION:
        mark_task_awaiting_validation(db, task_id)
        task = _get_task_or_raise(db, task_id)

    validator = LocalCodeValidator(db=db)

    try:
        validation_result = validator.validate(
            task=task,
            execution_run_id=execution_run_id,
            executor_result=executor_result,
        )
    except CodeValidatorError as exc:
        raise TaskValidationServiceError(
            f"Validation failed for task {task_id}: {str(exc)}"
        ) from exc

    if apply_final_status:
        final_task_status = apply_validation_decision_to_task(
            db=db,
            task_id=task_id,
            decision=validation_result.decision,
        )
    else:
        if validation_result.decision == CODE_VALIDATION_DECISION_COMPLETED:
            final_task_status = TASK_STATUS_COMPLETED
        elif validation_result.decision == CODE_VALIDATION_DECISION_PARTIAL:
            final_task_status = TASK_STATUS_PARTIAL
        elif validation_result.decision == CODE_VALIDATION_DECISION_FAILED:
            final_task_status = TASK_STATUS_FAILED
        else:
            raise TaskValidationServiceError(
                f"Unsupported validation decision '{validation_result.decision}'."
            )

    return TaskValidationServiceResult(
        task_id=task_id,
        execution_run_id=execution_run_id,
        validation_result=validation_result,
        final_task_status=final_task_status,
    )


def validate_terminal_code_task(
    db: Session,
    *,
    task_id: int,
    execution_run_id: int,
    executor_result: CodeExecutorResult,
    apply_final_status: bool = True,
) -> TaskValidationServiceResult:
    task = _get_task_or_raise(db, task_id)

    if executor_result.execution_status not in TERMINAL_PREVALIDATION_EXECUTION_STATUSES:
        raise TaskValidationServiceError(
            "validate_terminal_code_task() only supports terminal pre-validation statuses "
            f"{sorted(TERMINAL_PREVALIDATION_EXECUTION_STATUSES)}. "
            f"Received '{executor_result.execution_status}'."
        )

    validator = LocalCodeValidator(db=db)

    try:
        validation_result = validator.validate_terminal_result(
            task=task,
            execution_run_id=execution_run_id,
            executor_result=executor_result,
        )
    except CodeValidatorError as exc:
        raise TaskValidationServiceError(
            f"Terminal validation failed for task {task_id}: {str(exc)}"
        ) from exc

    if apply_final_status:
        final_task_status = apply_validation_decision_to_task(
            db=db,
            task_id=task_id,
            decision=validation_result.decision,
        )
    else:
        if validation_result.decision == CODE_VALIDATION_DECISION_COMPLETED:
            final_task_status = TASK_STATUS_COMPLETED
        elif validation_result.decision == CODE_VALIDATION_DECISION_PARTIAL:
            final_task_status = TASK_STATUS_PARTIAL
        elif validation_result.decision == CODE_VALIDATION_DECISION_FAILED:
            final_task_status = TASK_STATUS_FAILED
        else:
            raise TaskValidationServiceError(
                f"Unsupported validation decision '{validation_result.decision}'."
            )

    return TaskValidationServiceResult(
        task_id=task_id,
        execution_run_id=execution_run_id,
        validation_result=validation_result,
        final_task_status=final_task_status,
    )