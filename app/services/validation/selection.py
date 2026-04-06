from __future__ import annotations

from pydantic import BaseModel, Field

from app.services.validation.contracts import TaskValidationInput
from app.services.validation.helpers.evidence import (
    collect_all_producers,
    has_evidence_for_producer,
    order_producers_by_execution_sequence,
)
from app.services.validation.registry import ValidationRegistry

IGNORED_VALIDATION_PRODUCERS = {
    "context_selection_agent",
    "execution_orchestrator",
}


class SelectedValidator(BaseModel):
    producer_key: str
    validator_key: str
    selection_reason: str


class ValidationSelectionResult(BaseModel):
    selected_validators: list[SelectedValidator] = Field(default_factory=list)
    skipped_producers: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


def select_validators_for_task(
    *,
    validation_input: TaskValidationInput,
    registry: ValidationRegistry,
) -> ValidationSelectionResult:
    evidence = validation_input.execution_result.evidence
    available_producers = collect_all_producers(evidence)

    ordered_producers = order_producers_by_execution_sequence(
        execution_agent_sequence=validation_input.execution_result.execution_agent_sequence,
        available_producers=available_producers,
        ignored_producers=IGNORED_VALIDATION_PRODUCERS,
    )

    selected_validators: list[SelectedValidator] = []
    skipped_producers: list[str] = []
    notes: list[str] = []

    for producer_key in ordered_producers:
        if producer_key in IGNORED_VALIDATION_PRODUCERS:
            skipped_producers.append(producer_key)
            notes.append(f"Skipped producer '{producer_key}' because it is ignored by validation.")
            continue

        if not registry.has_producer(producer_key):
            skipped_producers.append(producer_key)
            notes.append(
                f"Skipped producer '{producer_key}' because no validator is registered for it."
            )
            continue

        if not has_evidence_for_producer(evidence, producer=producer_key):
            skipped_producers.append(producer_key)
            notes.append(
                f"Skipped producer '{producer_key}' because it has no evidence to validate."
            )
            continue

        validator = registry.get_by_producer(producer_key)
        selected_validators.append(
            SelectedValidator(
                producer_key=producer_key,
                validator_key=validator.validator_key,
                selection_reason=(
                    "Selected from execution_agent_sequence order with matching producer evidence."
                ),
            )
        )

    return ValidationSelectionResult(
        selected_validators=selected_validators,
        skipped_producers=skipped_producers,
        notes=notes,
    )