from __future__ import annotations

from app.services.validation.contracts import (
    ResolvedValidationIntent,
    TaskValidationInput,
    ValidationResult,
)
from app.services.validation.validators.code import validate_code_task_with_llm


class ValidationDispatcherError(Exception):
    """Raised when no validator can be resolved for a validation intent."""


def dispatch_validation(
    *,
    intent: ResolvedValidationIntent,
    validation_input: TaskValidationInput,
) -> ValidationResult:
    if intent.validator_key == "code_task_validator":
        return validate_code_task_with_llm(
            validation_input=validation_input,
        )

    raise ValidationDispatcherError(
        f"No validator registered for validator_key='{intent.validator_key}'."
    )
