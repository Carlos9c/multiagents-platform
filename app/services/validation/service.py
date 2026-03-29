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
            execution_agent_sequence=list(
                execution_result.execution_agent_sequence or []
            ),
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

    validator_input = _build_validator_input(
        routing_decision=routing_decision,
        task=task,
        execution_request=execution_request,
        execution_result=execution_result,
        execution_run=execution_run,
        persisted_artifacts=persisted_artifacts,
    )

    validation_result = dispatch_validation(
        intent=routing_decision,
        validation_input=validator_input,
    )

    return ValidationServiceResult(
        routing_input=routing_input,
        routing_decision=routing_decision,
        validator_input=validator_input,
        validation_result=validation_result,
    )
