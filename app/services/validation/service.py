from __future__ import annotations

from dataclasses import dataclass

from app.execution_engine.contracts import ExecutionRequest, ExecutionResult
from app.models.artifact import Artifact
from app.models.execution_run import ExecutionRun
from app.models.task import Task
from app.services.validation.contracts import (
    ResolvedValidationIntent,
    TaskValidationInput,
    ValidationResult,
)
from app.services.validation.dispatcher import dispatch_validation
from app.services.validation.evidence import build_task_validation_input
from app.services.validation.router import resolve_validation_route
from app.services.validation.router.schemas import (
    ValidationRoutingDecision,
    ValidationRoutingEvidenceSummary,
    ValidationRoutingExecutionSummary,
    ValidationRoutingInput,
    ValidationRoutingTaskContext,
)


class ValidationServiceError(Exception):
    """Base exception for validation orchestration failures."""


@dataclass
class ValidationServiceResult:
    routing_input: ValidationRoutingInput
    routing_decision: ValidationRoutingDecision
    validator_input: TaskValidationInput
    validation_result: ValidationResult


def build_validation_routing_input(
    *,
    task: Task,
    execution_request: ExecutionRequest,
    execution_result: ExecutionResult,
    execution_run: ExecutionRun,
) -> ValidationRoutingInput:
    return ValidationRoutingInput(
        task=ValidationRoutingTaskContext(
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
        ),
        execution=ValidationRoutingExecutionSummary(
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
        ),
        evidence=ValidationRoutingEvidenceSummary(
            changed_file_paths=[
                item.path for item in (execution_result.evidence.changed_files or [])
            ],
            command_count=len(execution_result.evidence.commands or []),
            artifact_refs=list(execution_result.evidence.artifacts_created or []),
            evidence_notes=list(execution_result.evidence.notes or []),
            relevant_files=list(execution_request.context.relevant_files or []),
            allowed_paths=list(execution_request.allowed_paths or []),
            key_decisions=list(execution_request.context.key_decisions or []),
            related_task_ids=[
                item.task_id for item in (execution_request.context.related_tasks or [])
            ],
        ),
    )


def _resolve_validation_intent(
    routing_decision: ValidationRoutingDecision,
) -> ResolvedValidationIntent:
    notes: list[str] = []

    if routing_decision.routing_rationale:
        notes.append(routing_decision.routing_rationale)

    notes.extend(item for item in routing_decision.validation_focus if item)
    notes.extend(item for item in routing_decision.open_questions if item)

    deduped_notes: list[str] = []
    seen: set[str] = set()
    for item in notes:
        normalized = item.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped_notes.append(normalized)

    return ResolvedValidationIntent(
        validator_key=routing_decision.validator_key,
        discipline=routing_decision.discipline,
        validation_mode=routing_decision.validation_mode,
        requires_workspace=routing_decision.requires_workspace,
        requires_artifacts=routing_decision.requires_artifacts,
        requires_changed_files=routing_decision.requires_changed_files,
        requires_commands=routing_decision.requires_command_results,
        requires_execution_context=True,
        requires_output_snapshot=routing_decision.requires_output_snapshot,
        requires_agent_sequence=routing_decision.requires_execution_agent_sequence,
        requires_file_reading=routing_decision.requires_file_reading,
        notes=deduped_notes,
    )


def _build_validator_input(
    *,
    routing_decision: ResolvedValidationIntent,
    task: Task,
    execution_request: ExecutionRequest,
    execution_result: ExecutionResult,
    execution_run: ExecutionRun,
    persisted_artifacts: list[Artifact],
) -> TaskValidationInput:
    if routing_decision.validator_key == "code_task_validator":
        return build_task_validation_input(
            intent=routing_decision,
            task=task,
            execution_request=execution_request,
            execution_result=execution_result,
            execution_run=execution_run,
            persisted_artifacts=persisted_artifacts,
        )

    raise ValidationServiceError(
        f"No validation input builder registered for validator_key='{routing_decision.validator_key}'."
    )


def _assert_validation_result_consistency(
    *,
    routing_decision: ResolvedValidationIntent,
    validation_result: ValidationResult,
) -> None:
    if validation_result.validator_key != routing_decision.validator_key:
        raise ValidationServiceError(
            "Validation result validator_key does not match the resolved validation route."
        )

    if validation_result.discipline != routing_decision.discipline:
        raise ValidationServiceError(
            "Validation result discipline does not match the resolved validation route."
        )

    decision = validation_result.decision
    final_task_status = validation_result.final_task_status

    if decision == "completed":
        if final_task_status != "completed":
            raise ValidationServiceError(
                "Validation result decision='completed' must produce final_task_status='completed'."
            )
        if validation_result.manual_review_required:
            raise ValidationServiceError(
                "Validation result decision='completed' cannot require manual review."
            )
        if validation_result.followup_validation_required:
            raise ValidationServiceError(
                "Validation result decision='completed' cannot require follow-up validation."
            )
        if validation_result.missing_scope:
            raise ValidationServiceError(
                "Validation result decision='completed' cannot report missing_scope."
            )

    elif decision == "partial":
        if final_task_status != "partial":
            raise ValidationServiceError(
                "Validation result decision='partial' must produce final_task_status='partial'."
            )

    elif decision == "failed":
        if final_task_status != "failed":
            raise ValidationServiceError(
                "Validation result decision='failed' must produce final_task_status='failed'."
            )

    elif decision == "manual_review":
        if final_task_status != "failed":
            raise ValidationServiceError(
                "Validation result decision='manual_review' must produce final_task_status='failed'."
            )
        if not validation_result.manual_review_required:
            raise ValidationServiceError(
                "Validation result decision='manual_review' must set manual_review_required=True."
            )

    else:
        raise ValidationServiceError(f"Unsupported validation decision '{decision}'.")


def validate_execution_result(
    *,
    task: Task,
    execution_request: ExecutionRequest,
    execution_result: ExecutionResult,
    execution_run: ExecutionRun,
    persisted_artifacts: list[Artifact] | None = None,
) -> ValidationServiceResult:
    persisted_artifacts = persisted_artifacts or []

    routing_input = build_validation_routing_input(
        task=task,
        execution_request=execution_request,
        execution_result=execution_result,
        execution_run=execution_run,
    )

    routing_decision = resolve_validation_route(
        routing_input=routing_input,
    )

    resolved_intent = _resolve_validation_intent(routing_decision)

    validator_input = _build_validator_input(
        routing_decision=resolved_intent,
        task=task,
        execution_request=execution_request,
        execution_result=execution_result,
        execution_run=execution_run,
        persisted_artifacts=persisted_artifacts,
    )

    validation_result = dispatch_validation(
        intent=resolved_intent,
        validation_input=validator_input,
    )

    _assert_validation_result_consistency(
        routing_decision=resolved_intent,
        validation_result=validation_result,
    )

    return ValidationServiceResult(
        routing_input=routing_input,
        routing_decision=routing_decision,
        validator_input=validator_input,
        validation_result=validation_result,
    )
