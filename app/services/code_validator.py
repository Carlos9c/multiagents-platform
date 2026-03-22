from __future__ import annotations

from abc import ABC, abstractmethod

from sqlalchemy.orm import Session

from app.models.task import TASK_STATUS_AWAITING_VALIDATION, Task
from app.schemas.code_execution import (
    CODE_EXECUTION_STATUS_AWAITING_VALIDATION,
    CODE_EXECUTION_STATUS_FAILED,
    CODE_EXECUTION_STATUS_REJECTED,
    CodeExecutorResult,
)
from app.schemas.code_validation import (
    CODE_VALIDATION_DECIDED_STATUS_COMPLETED,
    CODE_VALIDATION_DECIDED_STATUS_FAILED,
    CODE_VALIDATION_DECIDED_STATUS_PARTIAL,
    CodeValidationEvidence,
    CodeValidationResult,
)
from app.services.artifacts import create_artifact
from app.services.code_validator_client import (
    CodeValidatorClientError,
    evaluate_code_task_fulfillment,
)
from app.services.local_workspace_runtime import LocalWorkspaceRuntime
from app.services.project_storage import CODE_DOMAIN, ProjectStorageService
from app.services.workspace_runtime import WorkspaceRuntimeError


class CodeValidatorError(Exception):
    """Base error for code validation."""


class BaseCodeValidator(ABC):
    """
    Domain validator for code tasks.

    It decides the final terminal task status, but does not persist task-state transitions.
    """

    @abstractmethod
    def validate(
        self,
        task: Task,
        execution_run_id: int,
        executor_result: CodeExecutorResult,
    ) -> CodeValidationResult:
        raise NotImplementedError


class LocalCodeValidator(BaseCodeValidator):
    """
    Runtime-backed validator focused on fulfillment of the task request.

    It gathers real workspace evidence, then asks a strict fulfillment validator
    to decide only whether the result satisfies the task:
      - completed
      - partial
      - failed
    """

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
            artifact_type="code_validation_result",
            content=validation_result.model_dump_json(indent=2),
            created_by="code_validator",
        )
        return artifact.id

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

        for path in sorted(set(candidate_paths)):
            try:
                if self.workspace_runtime.file_exists(workspace_dir, path):
                    snapshots[path] = self.workspace_runtime.read_file(workspace_dir, path)
            except WorkspaceRuntimeError:
                continue

        return snapshots

    def _build_evidence(
        self,
        task: Task,
        execution_run_id: int,
        executor_result: CodeExecutorResult,
    ) -> CodeValidationEvidence:
        checked_files = sorted(
            set(
                executor_result.working_set.target_files
                + executor_result.workspace_changes.created_files
                + executor_result.workspace_changes.modified_files
                + [item.path for item in executor_result.edit_plan.planned_changes]
            )
        )

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

        observed_changes: list[str] = []
        if actual_changes.created_files:
            observed_changes.append(
                f"Created files: {', '.join(actual_changes.created_files)}"
            )
        if actual_changes.modified_files:
            observed_changes.append(
                f"Modified files: {', '.join(actual_changes.modified_files)}"
            )
        if actual_changes.deleted_files:
            observed_changes.append(
                f"Deleted files: {', '.join(actual_changes.deleted_files)}"
            )

        candidate_snapshot_paths = (
            actual_changes.created_files
            + actual_changes.modified_files
            + [item.path for item in executor_result.edit_plan.planned_changes]
        )

        final_file_snapshots = self._read_final_file_snapshots(
            task=task,
            execution_run_id=execution_run_id,
            candidate_paths=candidate_snapshot_paths,
        )

        return CodeValidationEvidence(
            checked_files=checked_files,
            observed_changes=observed_changes,
            executed_checks=[
                "workspace_collect_changes",
                "workspace_generate_diff",
                "final_file_snapshot_read",
                "fulfillment_decision_llm",
            ],
            check_outputs=[],
            warnings=[],
            workspace_diff=workspace_diff,
            final_file_snapshots=final_file_snapshots,
            execution_summary=executor_result.journal.summary,
            edit_plan_summary=executor_result.edit_plan.summary,
        )

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

        if executor_result.execution_status == CODE_EXECUTION_STATUS_REJECTED:
            evidence = CodeValidationEvidence(
                checked_files=[],
                observed_changes=[],
                executed_checks=["executor_status_short_circuit"],
                check_outputs=[],
                warnings=["Executor rejected the task before a valid operational execution pass."],
                workspace_diff=None,
                final_file_snapshots={},
                execution_summary=executor_result.journal.summary,
                edit_plan_summary=executor_result.edit_plan.summary,
            )

            validation_result = CodeValidationResult(
                task_id=task.id,
                execution_run_id=execution_run_id,
                decided_task_status=CODE_VALIDATION_DECIDED_STATUS_FAILED,
                reasons=[
                    "The task was rejected by the executor and therefore does not satisfy the requested resolution.",
                ],
                unresolved_gaps=[],
                evidence=evidence,
            )
            artifact_id = self._persist_validation_artifact(task, validation_result)
            validation_result.evidence.warnings.append(
                f"Structured validator artifact persisted as artifact_id={artifact_id}."
            )
            return validation_result

        if executor_result.execution_status == CODE_EXECUTION_STATUS_FAILED:
            evidence = CodeValidationEvidence(
                checked_files=[],
                observed_changes=[],
                executed_checks=["executor_status_short_circuit"],
                check_outputs=[],
                warnings=["Executor failed before a valid operational execution pass."],
                workspace_diff=None,
                final_file_snapshots={},
                execution_summary=executor_result.journal.summary,
                edit_plan_summary=executor_result.edit_plan.summary,
            )

            validation_result = CodeValidationResult(
                task_id=task.id,
                execution_run_id=execution_run_id,
                decided_task_status=CODE_VALIDATION_DECIDED_STATUS_FAILED,
                reasons=[
                    "The executor failed before producing a result that could satisfy the task.",
                ],
                unresolved_gaps=[],
                evidence=evidence,
            )
            artifact_id = self._persist_validation_artifact(task, validation_result)
            validation_result.evidence.warnings.append(
                f"Structured validator artifact persisted as artifact_id={artifact_id}."
            )
            return validation_result

        if executor_result.execution_status != CODE_EXECUTION_STATUS_AWAITING_VALIDATION:
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
                    "workspace_collect_changes",
                    "workspace_generate_diff",
                ],
                check_outputs=[],
                warnings=[f"Workspace validation failed: {str(exc)}"],
                workspace_diff=None,
                final_file_snapshots={},
                execution_summary=executor_result.journal.summary,
                edit_plan_summary=executor_result.edit_plan.summary,
            )

            validation_result = CodeValidationResult(
                task_id=task.id,
                execution_run_id=execution_run_id,
                decided_task_status=CODE_VALIDATION_DECIDED_STATUS_FAILED,
                reasons=[
                    "Validation could not inspect the execution workspace safely enough to confirm task fulfillment.",
                ],
                unresolved_gaps=[],
                evidence=evidence,
            )
            artifact_id = self._persist_validation_artifact(task, validation_result)
            validation_result.evidence.warnings.append(
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
                execution_run_id=execution_run_id,
                decided_task_status=CODE_VALIDATION_DECIDED_STATUS_FAILED,
                reasons=[
                    "Validation could not reach a reliable fulfillment decision for the task.",
                ],
                unresolved_gaps=[],
                evidence=evidence,
            )
            artifact_id = self._persist_validation_artifact(task, validation_result)
            validation_result.evidence.warnings.append(
                f"Structured validator artifact persisted as artifact_id={artifact_id}."
            )
            return validation_result

        decided_status = fulfillment_decision.decided_task_status
        if decided_status not in {
            CODE_VALIDATION_DECIDED_STATUS_COMPLETED,
            CODE_VALIDATION_DECIDED_STATUS_PARTIAL,
            CODE_VALIDATION_DECIDED_STATUS_FAILED,
        }:
            raise CodeValidatorError(
                f"Unsupported semantic decided_task_status '{decided_status}'."
            )

        validation_result = CodeValidationResult(
            task_id=task.id,
            execution_run_id=execution_run_id,
            decided_task_status=decided_status,
            reasons=[fulfillment_decision.decision_reason],
            unresolved_gaps=fulfillment_decision.missing_requirements,
            evidence=evidence,
        )
        validation_result.evidence.check_outputs.append(
            f"Fulfillment evidence used: {fulfillment_decision.evidence_used}"
        )

        artifact_id = self._persist_validation_artifact(task, validation_result)
        validation_result.evidence.warnings.append(
            f"Structured validator artifact persisted as artifact_id={artifact_id}."
        )

        return validation_result