from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy.orm import Session

from app.models.artifact import Artifact
from app.models.task import Task
from app.schemas.code_execution import CodeExecutorResult
from app.schemas.code_validation import (
    CODE_VALIDATION_DECISION_COMPLETED,
    CODE_VALIDATION_DECISION_FAILED,
    CODE_VALIDATION_DECISION_PARTIAL,
    CodeValidationCheck,
    CodeValidationEvidence,
    CodeValidationFulfillmentDecision,
    CodeValidationResult,
)
from app.services.artifacts import create_artifact
from app.services.code_validation_client import evaluate_code_task_fulfillment
from app.services.local_workspace_runtime import LocalWorkspaceRuntime
from app.services.project_memory_service import build_project_operational_context
from app.services.project_storage import CODE_DOMAIN, ProjectStorageService
from app.services.workspace_runtime import WorkspaceRuntimeError


CODE_VALIDATION_RESULT_ARTIFACT_TYPE = "code_validation_result"


class CodeValidationServiceError(Exception):
    """Base exception for code validation service."""


def _serialize_validation_result(result: CodeValidationResult) -> str:
    return json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2)


def _collect_candidate_snapshot_paths(executor_result: CodeExecutorResult) -> list[str]:
    paths: list[str] = []

    for path in executor_result.input.primary_targets:
        if path not in paths:
            paths.append(path)

    for path in executor_result.input.related_files:
        if path not in paths:
            paths.append(path)

    for path in executor_result.input.reference_files:
        if path not in paths:
            paths.append(path)

    for path in executor_result.input.related_test_files:
        if path not in paths:
            paths.append(path)

    for change in executor_result.edit_plan.planned_changes:
        if change.path not in paths:
            paths.append(change.path)

    return paths


def _safe_read_workspace_file(
    workspace_runtime: LocalWorkspaceRuntime,
    workspace_dir: Path,
    path: str,
) -> str | None:
    try:
        if not workspace_runtime.file_exists(workspace_dir, path):
            return None
        return workspace_runtime.read_file(workspace_dir, path)
    except WorkspaceRuntimeError:
        return None


def _build_observed_changes(executor_result: CodeExecutorResult) -> list[str]:
    changes = executor_result.workspace_changes

    observed: list[str] = []

    for path in changes.created_files:
        observed.append(f"created:{path}")
    for path in changes.modified_files:
        observed.append(f"modified:{path}")
    for path in changes.deleted_files:
        observed.append(f"deleted:{path}")
    for path in changes.renamed_files:
        observed.append(f"renamed:{path}")

    return observed


def _build_validation_notes(
    task: Task,
    fulfillment_decision: CodeValidationFulfillmentDecision,
    evidence: CodeValidationEvidence,
) -> list[str]:
    notes: list[str] = []

    notes.append(f"Task {task.id} validation decision: {fulfillment_decision.decision}.")
    notes.append(fulfillment_decision.decision_reason)

    if evidence.resolved_execution_context.selection_rationale:
        notes.append(
            f"Selection rationale considered: {evidence.resolved_execution_context.selection_rationale}"
        )

    if evidence.resolved_execution_context.unresolved_questions:
        notes.append(
            "Context gaps considered during validation: "
            + "; ".join(evidence.resolved_execution_context.unresolved_questions[:6])
        )

    if fulfillment_decision.missing_requirements:
        notes.append(
            "Missing requirements: "
            + "; ".join(fulfillment_decision.missing_requirements[:10])
        )

    return notes


def build_code_validation_evidence(
    db: Session,
    task: Task,
    executor_result: CodeExecutorResult,
    execution_run_id: int,
    storage_service: ProjectStorageService | None = None,
    workspace_runtime: LocalWorkspaceRuntime | None = None,
) -> CodeValidationEvidence:
    resolved_storage_service = storage_service or ProjectStorageService()
    resolved_workspace_runtime = workspace_runtime or LocalWorkspaceRuntime(
        storage_service=resolved_storage_service
    )

    try:
        prepared_workspace = resolved_workspace_runtime.get_workspace(
            project_id=task.project_id,
            execution_run_id=execution_run_id,
            domain_name=CODE_DOMAIN,
        )
    except WorkspaceRuntimeError as exc:
        raise CodeValidationServiceError(
            f"Unable to access workspace for validation: {str(exc)}"
        ) from exc

    workspace_dir = prepared_workspace.workspace_dir

    checked_files = _collect_candidate_snapshot_paths(executor_result)
    final_file_snapshots: dict[str, str] = {}

    for path in checked_files:
        content = _safe_read_workspace_file(
            workspace_runtime=resolved_workspace_runtime,
            workspace_dir=workspace_dir,
            path=path,
        )
        if content is not None:
            final_file_snapshots[path] = content

    executed_checks: list[CodeValidationCheck] = []
    check_outputs: list[str] = []
    warnings: list[str] = []

    workspace_diff: str | None = None
    try:
        workspace_diff = resolved_workspace_runtime.generate_diff(
            project_id=task.project_id,
            execution_run_id=execution_run_id,
            domain_name=CODE_DOMAIN,
        )
    except WorkspaceRuntimeError:
        warnings.append("Workspace diff could not be generated during validation.")

    project_operational_context = build_project_operational_context(
        db=db,
        project_id=task.project_id,
    )

    return CodeValidationEvidence(
        checked_files=checked_files,
        observed_changes=_build_observed_changes(executor_result),
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


def validate_code_task_result(
    db: Session,
    task: Task,
    executor_result: CodeExecutorResult,
    execution_run_id: int,
    storage_service: ProjectStorageService | None = None,
    workspace_runtime: LocalWorkspaceRuntime | None = None,
) -> CodeValidationResult:
    evidence = build_code_validation_evidence(
        db=db,
        task=task,
        executor_result=executor_result,
        execution_run_id=execution_run_id,
        storage_service=storage_service,
        workspace_runtime=workspace_runtime,
    )

    try:
        fulfillment_decision = evaluate_code_task_fulfillment(
            task=task,
            executor_result=executor_result,
            evidence=evidence,
        )
    except Exception as exc:
        raise CodeValidationServiceError(
            f"Validator model failed: {str(exc)}"
        ) from exc

    if fulfillment_decision.decision not in {
        CODE_VALIDATION_DECISION_COMPLETED,
        CODE_VALIDATION_DECISION_PARTIAL,
        CODE_VALIDATION_DECISION_FAILED,
    }:
        raise CodeValidationServiceError(
            f"Unsupported validation decision returned by model: {fulfillment_decision.decision}"
        )

    validation_result = CodeValidationResult(
        task_id=task.id,
        decision=fulfillment_decision.decision,
        decision_reason=fulfillment_decision.decision_reason,
        missing_requirements=fulfillment_decision.missing_requirements,
        evidence_used=fulfillment_decision.evidence_used,
        validation_notes=_build_validation_notes(
            task=task,
            fulfillment_decision=fulfillment_decision,
            evidence=evidence,
        ),
        evidence=evidence,
    )

    create_artifact(
        db=db,
        project_id=task.project_id,
        task_id=task.id,
        artifact_type=CODE_VALIDATION_RESULT_ARTIFACT_TYPE,
        content=_serialize_validation_result(validation_result),
        created_by="code_validator",
    )

    return validation_result