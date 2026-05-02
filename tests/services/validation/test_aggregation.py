from __future__ import annotations

import pytest

from app.services.validation.aggregation import (
    ValidationAggregationError,
    aggregate_validation_results,
)
from app.services.validation.contracts import ValidationFinding, ValidationResult


def _make_result(
    *,
    validator_key: str,
    decision: str,
    summary: str | None = None,
    validated_scope: str | None = None,
    missing_scope: str | None = None,
    blockers: list[str] | None = None,
    manual_review_required: bool = False,
    final_task_status: str | None = None,
    artifacts_created: list[str] | None = None,
    validated_evidence_ids: list[str] | None = None,
    unconsumed_evidence_ids: list[str] | None = None,
    followup_validation_required: bool = False,
    recommended_next_validator_keys: list[str] | None = None,
    partial_validation_summary: str | None = None,
    findings: list[ValidationFinding] | None = None,
    metadata: dict | None = None,
    rejected_files: list[str] | None = None,
    partial_reason: str | None = None,
    missing_work_summary: str | None = None,
) -> ValidationResult:
    effective_partial_reason = partial_reason
    if decision == "partial" and effective_partial_reason is None:
        effective_partial_reason = "work_missing"
    return ValidationResult(
        validator_key=validator_key,
        discipline="code",
        decision=decision,
        summary=summary or f"{validator_key} decided {decision}",
        findings=list(findings or []),
        validated_scope=validated_scope,
        missing_scope=missing_scope,
        blockers=list(blockers or []),
        manual_review_required=manual_review_required,
        final_task_status=final_task_status,
        artifacts_created=list(artifacts_created or []),
        validated_evidence_ids=list(validated_evidence_ids or []),
        unconsumed_evidence_ids=list(unconsumed_evidence_ids or []),
        followup_validation_required=followup_validation_required,
        recommended_next_validator_keys=list(recommended_next_validator_keys or []),
        partial_validation_summary=partial_validation_summary,
        rejected_files=list(rejected_files or []),
        partial_reason=effective_partial_reason,
        missing_work_summary=missing_work_summary,
        metadata=dict(metadata or {}),
    )


def test_aggregate_validation_results_raises_on_empty_input():
    with pytest.raises(
        ValidationAggregationError,
        match="Cannot aggregate an empty validation result set",
    ):
        aggregate_validation_results(validator_results=[])


def test_aggregate_validation_results_returns_completed_when_all_are_completed():
    results = [
        _make_result(
            validator_key="code_change_agent_validator",
            decision="completed",
            validated_scope="Code contribution validated.",
            final_task_status="completed",
            validated_evidence_ids=["changed_file:0"],
        ),
        _make_result(
            validator_key="command_runner_agent_validator",
            decision="completed",
            validated_scope="Verification contribution validated.",
            final_task_status="completed",
            validated_evidence_ids=["command_execution:0"],
        ),
    ]

    aggregation = aggregate_validation_results(validator_results=results)

    assert aggregation.winning_decision == "completed"
    assert aggregation.final_result.decision == "completed"
    assert aggregation.final_result.validator_key == "validation_aggregator"
    assert aggregation.final_result.final_task_status == "completed"
    assert aggregation.final_result.manual_review_required is False
    assert aggregation.final_result.validated_evidence_ids == [
        "changed_file:0",
        "command_execution:0",
    ]


def test_aggregate_validation_results_prioritizes_partial_over_completed():
    results = [
        _make_result(
            validator_key="code_change_agent_validator",
            decision="completed",
            validated_scope="Code validated.",
            final_task_status="completed",
        ),
        _make_result(
            validator_key="command_runner_agent_validator",
            decision="partial",
            summary="Verification is only partial.",
            validated_scope="Verification partially validated.",
            missing_scope="A stronger verification signal is still missing.",
            blockers=["verification gap"],
            final_task_status="partial",
            partial_validation_summary="Command evidence is meaningful but incomplete.",
        ),
    ]

    aggregation = aggregate_validation_results(validator_results=results)

    assert aggregation.winning_decision == "partial"
    assert aggregation.final_result.decision == "partial"
    assert aggregation.final_result.final_task_status == "partial"
    assert aggregation.final_result.partial_validation_summary == (
        "Command evidence is meaningful but incomplete."
    )
    assert aggregation.final_result.blockers == ["verification gap"]
    assert aggregation.final_result.validated_scope == (
        "Code validated.\nVerification partially validated."
    )
    assert aggregation.final_result.missing_scope == (
        "A stronger verification signal is still missing."
    )


def test_aggregate_validation_results_prioritizes_manual_review_over_partial():
    results = [
        _make_result(
            validator_key="code_change_agent_validator",
            decision="partial",
            missing_scope="Some code scope remains unclear.",
            blockers=["code ambiguity"],
            final_task_status="partial",
        ),
        _make_result(
            validator_key="command_runner_agent_validator",
            decision="manual_review",
            summary="Verification evidence is too ambiguous for automatic closure.",
            blockers=["ambiguous verification outcome"],
            manual_review_required=True,
            final_task_status="failed",
        ),
    ]

    aggregation = aggregate_validation_results(validator_results=results)

    assert aggregation.winning_decision == "manual_review"
    assert aggregation.final_result.decision == "manual_review"
    assert aggregation.final_result.manual_review_required is True
    assert aggregation.final_result.final_task_status == "failed"
    assert aggregation.final_result.blockers == [
        "code ambiguity",
        "ambiguous verification outcome",
    ]


def test_aggregate_validation_results_prioritizes_failed_over_manual_review():
    results = [
        _make_result(
            validator_key="code_change_agent_validator",
            decision="manual_review",
            summary="Code evidence is ambiguous.",
            manual_review_required=True,
            final_task_status="failed",
        ),
        _make_result(
            validator_key="command_runner_agent_validator",
            decision="failed",
            summary="Verification directly contradicts task success.",
            blockers=["tests failed"],
            final_task_status="failed",
        ),
    ]

    aggregation = aggregate_validation_results(validator_results=results)

    assert aggregation.winning_decision == "failed"
    assert aggregation.final_result.decision == "failed"
    assert aggregation.final_result.final_task_status == "failed"
    assert "tests failed" in aggregation.final_result.blockers


def test_aggregate_validation_results_merges_findings_blockers_artifacts_and_evidence_ids():
    findings_a = [
        ValidationFinding(
            severity="warning",
            message="Code evidence is incomplete.",
            code="code_gap",
            file_path="app/screens/settings.py",
        )
    ]
    findings_b = [
        ValidationFinding(
            severity="error",
            message="Verification failed.",
            code="verification_failed",
            file_path=None,
        )
    ]

    results = [
        _make_result(
            validator_key="code_change_agent_validator",
            decision="partial",
            blockers=["shared blocker", "code blocker"],
            artifacts_created=["artifact-a"],
            validated_evidence_ids=["changed_file:0", "shared-evidence"],
            unconsumed_evidence_ids=["unconsumed-a", "shared-unconsumed"],
            recommended_next_validator_keys=["command_runner_agent_validator"],
            findings=findings_a,
            final_task_status="partial",
        ),
        _make_result(
            validator_key="command_runner_agent_validator",
            decision="failed",
            blockers=["shared blocker", "verification blocker"],
            artifacts_created=["artifact-b", "artifact-a"],
            validated_evidence_ids=["command_execution:0", "shared-evidence"],
            unconsumed_evidence_ids=["unconsumed-b", "shared-unconsumed"],
            recommended_next_validator_keys=["manual_review_validator"],
            findings=findings_b,
            final_task_status="failed",
        ),
    ]

    aggregation = aggregate_validation_results(validator_results=results)
    final_result = aggregation.final_result

    assert final_result.decision == "failed"

    assert final_result.blockers == [
        "shared blocker",
        "code blocker",
        "verification blocker",
    ]
    assert final_result.artifacts_created == [
        "artifact-a",
        "artifact-b",
    ]
    assert final_result.validated_evidence_ids == [
        "changed_file:0",
        "shared-evidence",
        "command_execution:0",
    ]
    assert final_result.unconsumed_evidence_ids == [
        "unconsumed-a",
        "shared-unconsumed",
        "unconsumed-b",
    ]
    assert final_result.recommended_next_validator_keys == [
        "command_runner_agent_validator",
        "manual_review_validator",
    ]
    assert len(final_result.findings) == 2
    assert final_result.findings[0].code == "code_gap"
    assert final_result.findings[1].code == "verification_failed"


def test_aggregate_validation_results_propagates_followup_validation_required():
    results = [
        _make_result(
            validator_key="code_change_agent_validator",
            decision="completed",
            followup_validation_required=False,
            final_task_status="completed",
        ),
        _make_result(
            validator_key="command_runner_agent_validator",
            decision="partial",
            followup_validation_required=True,
            final_task_status="partial",
        ),
    ]

    aggregation = aggregate_validation_results(validator_results=results)

    assert aggregation.final_result.followup_validation_required is True


def test_aggregate_validation_results_uses_winning_result_discipline_and_metadata_points_to_winner():
    results = [
        _make_result(
            validator_key="code_change_agent_validator",
            decision="completed",
            final_task_status="completed",
        ),
        _make_result(
            validator_key="command_runner_agent_validator",
            decision="partial",
            final_task_status="partial",
        ),
    ]

    aggregation = aggregate_validation_results(validator_results=results)
    final_result = aggregation.final_result

    assert final_result.discipline == "code"
    assert final_result.metadata["aggregated_validator_count"] == 2
    assert final_result.metadata["winning_validator_key"] == "command_runner_agent_validator"
    assert final_result.metadata["winning_decision"] == "partial"
    assert final_result.metadata["validator_keys"] == [
        "code_change_agent_validator",
        "command_runner_agent_validator",
    ]
    assert final_result.metadata["validator_decisions"] == {
        "code_change_agent_validator": "completed",
        "command_runner_agent_validator": "partial",
    }


def test_aggregate_validation_results_builds_summary_from_winning_result():
    results = [
        _make_result(
            validator_key="code_change_agent_validator",
            decision="completed",
            summary="Code validation passed.",
            final_task_status="completed",
        ),
        _make_result(
            validator_key="command_runner_agent_validator",
            decision="failed",
            summary="Verification failed on the main command.",
            final_task_status="failed",
        ),
    ]

    aggregation = aggregate_validation_results(validator_results=results)

    assert aggregation.final_result.summary == (
        "Aggregated validation decision is 'failed' based on 2 validator result(s): "
        "code_change_agent_validator, command_runner_agent_validator. "
        "Primary summary: Verification failed on the main command."
    )
