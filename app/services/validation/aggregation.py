from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.models.task import (
    TASK_STATUS_COMPLETED,
    TASK_STATUS_FAILED,
    TASK_STATUS_PARTIAL,
)
from app.services.validation.contracts import (
    VALIDATION_DECISION_COMPLETED,
    VALIDATION_DECISION_FAILED,
    VALIDATION_DECISION_MANUAL_REVIEW,
    VALIDATION_DECISION_PARTIAL,
    ValidationFinding,
    ValidationResult,
)

ValidationDecision = Literal["completed", "partial", "failed", "manual_review"]


class ValidationAggregationError(Exception):
    """Raised when validation aggregation cannot be completed."""


class ValidationAggregationResult(BaseModel):
    final_result: ValidationResult
    validator_results: list[ValidationResult] = Field(default_factory=list)
    winning_decision: ValidationDecision
    notes: list[str] = Field(default_factory=list)


def _decision_rank(decision: str) -> int:
    if decision == VALIDATION_DECISION_FAILED:
        return 4
    if decision == VALIDATION_DECISION_MANUAL_REVIEW:
        return 3
    if decision == VALIDATION_DECISION_PARTIAL:
        return 2
    if decision == VALIDATION_DECISION_COMPLETED:
        return 1
    raise ValidationAggregationError(f"Unknown validation decision: {decision}")


def _map_final_task_status(decision: str) -> str | None:
    if decision == VALIDATION_DECISION_COMPLETED:
        return TASK_STATUS_COMPLETED
    if decision == VALIDATION_DECISION_PARTIAL:
        return TASK_STATUS_PARTIAL
    if decision in {VALIDATION_DECISION_FAILED, VALIDATION_DECISION_MANUAL_REVIEW}:
        return TASK_STATUS_FAILED
    return None


def _unique_strings(values: list[str]) -> list[str]:
    ordered: list[str] = []
    for value in values:
        if not value:
            continue
        if value not in ordered:
            ordered.append(value)
    return ordered


def _merge_findings(results: list[ValidationResult]) -> list[ValidationFinding]:
    merged: list[ValidationFinding] = []

    for result in results:
        merged.extend(result.findings)

    return merged


def _merge_blockers(results: list[ValidationResult]) -> list[str]:
    merged: list[str] = []
    for result in results:
        merged.extend(result.blockers)
    return _unique_strings(merged)


def _merge_artifacts_created(results: list[ValidationResult]) -> list[str]:
    merged: list[str] = []
    for result in results:
        merged.extend(result.artifacts_created)
    return _unique_strings(merged)


def _merge_validated_evidence_ids(results: list[ValidationResult]) -> list[str]:
    merged: list[str] = []
    for result in results:
        merged.extend(result.validated_evidence_ids)
    return _unique_strings(merged)


def _merge_unconsumed_evidence_ids(results: list[ValidationResult]) -> list[str]:
    merged: list[str] = []
    for result in results:
        merged.extend(result.unconsumed_evidence_ids)
    return _unique_strings(merged)


def _merge_recommended_next_validator_keys(results: list[ValidationResult]) -> list[str]:
    merged: list[str] = []
    for result in results:
        merged.extend(result.recommended_next_validator_keys)
    return _unique_strings(merged)


def _pick_winning_result(results: list[ValidationResult]) -> ValidationResult:
    if not results:
        raise ValidationAggregationError("Cannot aggregate an empty validation result set.")

    return max(results, key=lambda item: _decision_rank(item.decision))


def _build_aggregate_summary(
    *,
    winning_result: ValidationResult,
    results: list[ValidationResult],
) -> str:
    validator_keys = ", ".join(result.validator_key for result in results)

    return (
        f"Aggregated validation decision is '{winning_result.decision}' based on "
        f"{len(results)} validator result(s): {validator_keys}. "
        f"Primary summary: {winning_result.summary}"
    )


def _build_aggregate_validated_scope(results: list[ValidationResult]) -> str | None:
    scopes = _unique_strings(
        [result.validated_scope for result in results if result.validated_scope]
    )
    if not scopes:
        return None
    return "\n".join(scopes)


def _build_aggregate_missing_scope(results: list[ValidationResult]) -> str | None:
    scopes = _unique_strings([result.missing_scope for result in results if result.missing_scope])
    if not scopes:
        return None
    return "\n".join(scopes)


def _build_partial_validation_summary(results: list[ValidationResult]) -> str | None:
    partial_summaries = _unique_strings(
        [
            result.partial_validation_summary or result.summary
            for result in results
            if result.decision == VALIDATION_DECISION_PARTIAL
        ]
    )
    if not partial_summaries:
        return None
    return "\n".join(partial_summaries)


def aggregate_validation_results(
    *,
    validator_results: list[ValidationResult],
) -> ValidationAggregationResult:
    if not validator_results:
        raise ValidationAggregationError("Cannot aggregate an empty validation result set.")

    winning_result = _pick_winning_result(validator_results)
    winning_decision = winning_result.decision

    followup_validation_required = any(
        result.followup_validation_required for result in validator_results
    )
    manual_review_required = any(
        result.manual_review_required or result.decision == VALIDATION_DECISION_MANUAL_REVIEW
        for result in validator_results
    )

    final_result = ValidationResult(
        validator_key="validation_aggregator",
        discipline=winning_result.discipline,
        decision=winning_decision,
        summary=_build_aggregate_summary(
            winning_result=winning_result,
            results=validator_results,
        ),
        findings=_merge_findings(validator_results),
        validated_scope=_build_aggregate_validated_scope(validator_results),
        missing_scope=_build_aggregate_missing_scope(validator_results),
        blockers=_merge_blockers(validator_results),
        manual_review_required=manual_review_required,
        final_task_status=_map_final_task_status(winning_decision),
        artifacts_created=_merge_artifacts_created(validator_results),
        validated_evidence_ids=_merge_validated_evidence_ids(validator_results),
        unconsumed_evidence_ids=_merge_unconsumed_evidence_ids(validator_results),
        followup_validation_required=followup_validation_required,
        recommended_next_validator_keys=_merge_recommended_next_validator_keys(validator_results),
        partial_validation_summary=_build_partial_validation_summary(validator_results),
        metadata={
            "aggregated_validator_count": len(validator_results),
            "winning_validator_key": winning_result.validator_key,
            "winning_decision": winning_decision,
            "validator_keys": [result.validator_key for result in validator_results],
            "validator_decisions": {
                result.validator_key: result.decision for result in validator_results
            },
        },
    )

    notes = [
        f"Winning validation decision: {winning_decision}",
        f"Winning validator: {winning_result.validator_key}",
    ]

    return ValidationAggregationResult(
        final_result=final_result,
        validator_results=validator_results,
        winning_decision=winning_decision,
        notes=notes,
    )
