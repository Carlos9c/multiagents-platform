from __future__ import annotations

from pathlib import Path

from app.execution_engine.contracts import ExecutionRequest, ExecutionResult
from app.models.artifact import Artifact
from app.models.execution_run import ExecutionRun
from app.models.task import Task
from app.services.validation.contracts import (
    ResolvedValidationIntent,
    TaskValidationInput,
    ValidationEvidenceItem,
    ValidationEvidencePackage,
    ValidationExecutionContext,
    ValidationRequestContext,
    ValidationTaskContext,
)


class ValidationEvidenceBuilderError(Exception):
    """Raised when validation evidence cannot be assembled."""


def _read_text_file_if_exists(path: Path, *, max_chars: int = 40000) -> tuple[bool, str | None]:
    if not path.exists() or not path.is_file():
        return False, None

    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        content = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return False, None

    if len(content) > max_chars:
        content = content[:max_chars]

    return True, content


def _build_produced_file_evidence_item(
    *,
    relative_path: str,
    change_type: str,
    workspace_path: str | None,
    source_path: str | None,
) -> ValidationEvidenceItem:
    workspace_candidate = Path(workspace_path) / relative_path if workspace_path else None
    if workspace_candidate is not None:
        exists, content = _read_text_file_if_exists(workspace_candidate)
        if exists:
            return ValidationEvidenceItem(
                evidence_id=f"produced_file:{relative_path}",
                evidence_kind="produced_file",
                media_type="text/plain",
                representation_kind="full_text",
                source="execution_workspace",
                logical_name=Path(relative_path).name,
                path=relative_path,
                change_type=change_type,
                content_text=content,
                metadata={"exists": True},
            )

    source_candidate = Path(source_path) / relative_path if source_path else None
    if source_candidate is not None:
        exists, content = _read_text_file_if_exists(source_candidate)
        if exists:
            return ValidationEvidenceItem(
                evidence_id=f"produced_file:{relative_path}",
                evidence_kind="produced_file",
                media_type="text/plain",
                representation_kind="full_text",
                source="project_source",
                logical_name=Path(relative_path).name,
                path=relative_path,
                change_type=change_type,
                content_text=content,
                metadata={"exists": True},
            )

    return ValidationEvidenceItem(
        evidence_id=f"produced_file:{relative_path}",
        evidence_kind="produced_file",
        media_type="text/plain",
        representation_kind="binary_placeholder",
        source="missing",
        logical_name=Path(relative_path).name,
        path=relative_path,
        change_type=change_type,
        content_summary="The produced file could not be read from workspace or source.",
        metadata={"exists": False},
    )


def _build_command_evidence_item(
    *,
    index: int,
    command: str,
    exit_code: int,
    stdout: str | None,
    stderr: str | None,
) -> ValidationEvidenceItem:
    return ValidationEvidenceItem(
        evidence_id=f"command:{index}",
        evidence_kind="command_output",
        media_type="text/plain",
        representation_kind="command_output",
        source="execution_result",
        logical_name=command,
        content_text=(
            f"$ {command}\n"
            f"[exit_code={exit_code}]\n\n"
            f"STDOUT:\n{stdout or ''}\n\n"
            f"STDERR:\n{stderr or ''}"
        ),
        structured_content={
            "command": command,
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
        },
    )


def _build_persisted_artifact_evidence_item(
    *,
    artifact: Artifact,
) -> ValidationEvidenceItem:
    preview = artifact.content[:4000] if artifact.content else None
    return ValidationEvidenceItem(
        evidence_id=f"artifact:{artifact.id}",
        evidence_kind="persisted_artifact",
        media_type="application/json" if artifact.content and artifact.content.strip().startswith("{") else "text/plain",
        representation_kind="artifact_preview",
        source="persisted_artifact",
        logical_name=artifact.artifact_type,
        artifact_id=artifact.id,
        content_text=preview,
        metadata={
            "artifact_type": artifact.artifact_type,
            "task_id": artifact.task_id,
            "project_id": artifact.project_id,
        },
    )


def _build_artifact_ref_evidence_item(
    *,
    index: int,
    artifact_ref: str,
) -> ValidationEvidenceItem:
    return ValidationEvidenceItem(
        evidence_id=f"artifact_ref:{index}",
        evidence_kind="artifact_reference",
        media_type="text/plain",
        representation_kind="summary",
        source="execution_result",
        logical_name=artifact_ref,
        content_summary=artifact_ref,
    )


def build_task_validation_input(
    *,
    task: Task,
    execution_request: ExecutionRequest,
    execution_result: ExecutionResult,
    execution_run: ExecutionRun,
    persisted_artifacts: list[Artifact] | None,
    intent: ResolvedValidationIntent,
) -> TaskValidationInput:
    persisted_artifacts = persisted_artifacts or []

    task_context = ValidationTaskContext(
        task_id=task.id,
        project_id=task.project_id,
        title=task.title,
        description=task.description,
        summary=task.summary,
        objective=task.objective,
        acceptance_criteria=task.acceptance_criteria,
        technical_constraints=task.technical_constraints,
        out_of_scope=task.out_of_scope,
        task_type=task.task_type,
        planning_level=task.planning_level,
        executor_type=task.executor_type,
    )

    execution_context = ValidationExecutionContext(
        execution_run_id=execution_run.id,
        execution_status=execution_run.status,
        decision=execution_result.decision,
        summary=execution_result.summary,
        details=execution_result.details,
        rejection_reason=execution_result.rejection_reason,
        completed_scope=execution_result.completed_scope,
        remaining_scope=execution_result.remaining_scope,
        blockers_found=list(execution_result.blockers_found or []),
        validation_notes=list(execution_result.validation_notes or []),
        output_snapshot=execution_result.output_snapshot,
        execution_agent_sequence=list(execution_result.execution_agent_sequence or []),
    )

    request_context = ValidationRequestContext(
        workspace_path=execution_request.context.workspace_path,
        source_path=execution_request.context.source_path,
        allowed_paths=list(execution_request.allowed_paths or []),
        relevant_files=list(execution_request.context.relevant_files or []),
        key_decisions=list(execution_request.context.key_decisions or []),
        related_task_ids=[
            item.task_id for item in (execution_request.context.related_tasks or [])
        ],
    )

    evidence_items: list[ValidationEvidenceItem] = []

    for changed_file in execution_result.evidence.changed_files:
        evidence_items.append(
            _build_produced_file_evidence_item(
                relative_path=changed_file.path,
                change_type=changed_file.change_type,
                workspace_path=execution_request.context.workspace_path,
                source_path=execution_request.context.source_path,
            )
        )

    for index, command in enumerate(execution_result.evidence.commands):
        evidence_items.append(
            _build_command_evidence_item(
                index=index,
                command=command.command,
                exit_code=command.exit_code,
                stdout=command.stdout,
                stderr=command.stderr,
            )
        )

    for artifact in persisted_artifacts:
        evidence_items.append(
            _build_persisted_artifact_evidence_item(
                artifact=artifact,
            )
        )

    for index, artifact_ref in enumerate(execution_result.evidence.artifacts_created or []):
        evidence_items.append(
            _build_artifact_ref_evidence_item(
                index=index,
                artifact_ref=artifact_ref,
            )
        )

    evidence_package = ValidationEvidencePackage(
        evidence_items=evidence_items,
    )

    metadata = {
        "evidence_item_count": len(evidence_items),
        "produced_file_count": len(execution_result.evidence.changed_files or []),
        "command_count": len(execution_result.evidence.commands or []),
        "persisted_artifact_count": len(persisted_artifacts),
    }

    return TaskValidationInput(
        intent=intent,
        task=task_context,
        execution=execution_context,
        request_context=request_context,
        evidence_package=evidence_package,
        metadata=metadata,
    )