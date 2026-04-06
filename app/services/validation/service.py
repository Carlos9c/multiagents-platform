from __future__ import annotations

from dataclasses import dataclass

from app.execution_engine.contracts import ExecutionRequest, ExecutionResult
from app.models.artifact import Artifact
from app.models.execution_run import ExecutionRun
from app.models.task import Task
from app.services.validation.aggregation import (
    ValidationAggregationResult,
    aggregate_validation_results,
)
from app.services.validation.contracts import TaskValidationInput, ValidationResult
from app.services.validation.registry import ValidationRegistry
from app.services.validation.selection import ValidationSelectionResult, select_validators_for_task


class ValidationServiceError(Exception):
    """Raised when validation service fails."""


@dataclass
class ValidationServiceResult:
    validator_input: TaskValidationInput
    selection_result: ValidationSelectionResult
    validator_results: list[ValidationResult]
    aggregation_result: ValidationAggregationResult
    validation_result: ValidationResult


def _build_validator_input(
    *,
    execution_request: ExecutionRequest,
    execution_result: ExecutionResult,
) -> TaskValidationInput:
    return TaskValidationInput(
        execution_request=execution_request,
        execution_result=execution_result,
        intent=None,
        metadata={},
    )


def _assert_validation_service_result_consistency(
    *,
    selection_result: ValidationSelectionResult,
    validator_results: list[ValidationResult],
    aggregation_result: ValidationAggregationResult,
) -> None:
    if not validator_results:
        raise ValidationServiceError("Validation completed without validator results.")

    selected_validator_keys = [item.validator_key for item in selection_result.selected_validators]
    result_validator_keys = [item.validator_key for item in validator_results]

    if len(selected_validator_keys) != len(result_validator_keys):
        raise ValidationServiceError(
            "Validator selection and validator execution counts do not match."
        )

    if selected_validator_keys != result_validator_keys:
        raise ValidationServiceError(
            "Validator execution order does not match validator selection order."
        )

    if aggregation_result.final_result.decision not in {
        "completed",
        "partial",
        "failed",
        "manual_review",
    }:
        raise ValidationServiceError(
            f"Unsupported aggregated validation decision "
            f"'{aggregation_result.final_result.decision}'."
        )


def validate_execution_result(
    *,
    task: Task,
    execution_request: ExecutionRequest,
    execution_result: ExecutionResult,
    execution_run: ExecutionRun,
    persisted_artifacts: list[Artifact] | None = None,
) -> ValidationServiceResult:
    del task
    del execution_run
    del persisted_artifacts

    try:
        validator_input = _build_validator_input(
            execution_request=execution_request,
            execution_result=execution_result,
        )

        registry = ValidationRegistry()

        selection_result = select_validators_for_task(
            validation_input=validator_input,
            registry=registry,
        )

        validator_results: list[ValidationResult] = []

        for selected_validator in selection_result.selected_validators:
            validator = registry.get_by_key(selected_validator.validator_key)
            validator_result = validator.validate(validator_input)
            validator_results.append(validator_result)

        if not validator_results:
            raise ValidationServiceError("No validators were executed for this task.")

        aggregation_result = aggregate_validation_results(
            validator_results=validator_results,
        )

        _assert_validation_service_result_consistency(
            selection_result=selection_result,
            validator_results=validator_results,
            aggregation_result=aggregation_result,
        )

        return ValidationServiceResult(
            validator_input=validator_input,
            selection_result=selection_result,
            validator_results=validator_results,
            aggregation_result=aggregation_result,
            validation_result=aggregation_result.final_result,
        )

    except ValidationServiceError:
        raise
    except Exception as exc:
        raise ValidationServiceError(str(exc)) from exc
