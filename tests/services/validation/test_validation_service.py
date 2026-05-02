from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.execution_engine.contracts import (
    ExecutionEvidence,
    ExecutionRequest,
    ExecutionResult,
    HistoricalExecutionContext,
    ProjectExecutionContext,
)
from app.services.validation.aggregation import ValidationAggregationResult
from app.services.validation.contracts import TaskValidationInput, ValidationResult
from app.services.validation.selection import SelectedValidator, ValidationSelectionResult
from app.services.validation.service import (
    ValidationServiceError,
    validate_execution_result,
)


class _FakeValidator:
    def __init__(self, *, validator_key: str, producer_key: str, decision: str):
        self.validator_key = validator_key
        self.producer_key = producer_key
        self.decision = decision
        self.calls: list[TaskValidationInput] = []

    def validate(self, validation_input: TaskValidationInput) -> ValidationResult:
        self.calls.append(validation_input)
        return ValidationResult(
            validator_key=self.validator_key,
            discipline="code",
            decision=self.decision,
            summary=f"{self.validator_key} decided {self.decision}",
            findings=[],
            validated_scope=f"{self.validator_key} validated its scope",
            missing_scope=None if self.decision == "completed" else "missing scope",
            blockers=[] if self.decision == "completed" else ["blocker"],
            manual_review_required=self.decision == "manual_review",
            final_task_status=(
                "completed"
                if self.decision == "completed"
                else "partial" if self.decision == "partial" else "failed"
            ),
            artifacts_created=[],
            validated_evidence_ids=[],
            unconsumed_evidence_ids=[],
            followup_validation_required=False,
            recommended_next_validator_keys=[],
            partial_validation_summary=None,
            partial_reason="work_missing" if self.decision == "partial" else None,
            metadata={"validator_key": self.validator_key},
        )


class _FakeRegistry:
    def __init__(self, validators: list[_FakeValidator]):
        self._validators_by_key = {validator.validator_key: validator for validator in validators}

    def get_by_key(self, validator_key: str):
        return self._validators_by_key[validator_key]


@dataclass
class _FakeTask:
    id: int = 10
    project_id: int = 20


@dataclass
class _FakeExecutionRun:
    id: int = 30
    status: str = "succeeded"


def _make_execution_request() -> ExecutionRequest:
    return ExecutionRequest(
        task_id=10,
        project_id=20,
        execution_run_id=30,
        task_title="Implement settings workflow",
        task_description="Create the settings workflow and verify it locally.",
        task_summary="Vertical slice with operational verification.",
        objective="Deliver a working settings workflow backed by repository-local verification.",
        proposed_solution="Implement code and verify the critical path.",
        implementation_notes="Stay within the existing app structure.",
        implementation_steps="1. Add code 2. Add tests 3. Run verification",
        acceptance_criteria="The workflow exists and a relevant verification command passes.",
        tests_required="Repository-local verification should pass.",
        technical_constraints="Do not break unrelated flows.",
        out_of_scope="Do not modify unrelated features.",
        executor_type="execution_engine",
        success_criteria=["Workflow exists", "Verification passes"],
        constraints=["Stay within workspace"],
        allowed_paths=["app/screens/settings.py", "tests/test_settings.py"],
        blocked_paths=[],
        context=ProjectExecutionContext(
            project_id=20,
            workspace_path="/tmp/workspace",
            source_path="/tmp/source",
            relevant_files=["README.md"],
            key_decisions=["Prefer one narrow verification step."],
            related_tasks=[],
        ),
        historical_context=HistoricalExecutionContext(
            selected_task_runs=[],
        ),
    )


def _make_execution_result() -> ExecutionResult:
    return ExecutionResult(
        task_id=10,
        decision="partial",
        summary="Operational execution loop completed successfully.",
        details="Execution completed with evidence.",
        completed_scope="Implemented code and produced verification evidence.",
        remaining_scope="External validation is still pending.",
        blockers_found=[],
        validation_notes=["Execution orchestrator finished normally."],
        output_snapshot="done",
        execution_agent_sequence=["code_change_agent", "command_runner_agent"],
        evidence=ExecutionEvidence(),
    )


def test_validate_execution_result_runs_selected_validators_in_order(monkeypatch):
    validator_a = _FakeValidator(
        validator_key="code_change_agent_validator",
        producer_key="code_change_agent",
        decision="completed",
    )
    validator_b = _FakeValidator(
        validator_key="command_runner_agent_validator",
        producer_key="command_runner_agent",
        decision="partial",
    )

    selection_result = ValidationSelectionResult(
        selected_validators=[
            SelectedValidator(
                producer_key="code_change_agent",
                validator_key="code_change_agent_validator",
                selection_reason="selected first",
            ),
            SelectedValidator(
                producer_key="command_runner_agent",
                validator_key="command_runner_agent_validator",
                selection_reason="selected second",
            ),
        ],
        skipped_producers=[],
        notes=["selection completed"],
    )

    final_result = ValidationResult(
        validator_key="validation_aggregator",
        discipline="code",
        decision="partial",
        summary="Aggregated validation decision is partial.",
        findings=[],
        validated_scope="Some validated scope",
        missing_scope="Some missing scope",
        blockers=["verification gap"],
        manual_review_required=False,
        final_task_status="partial",
        artifacts_created=[],
        validated_evidence_ids=[],
        unconsumed_evidence_ids=[],
        followup_validation_required=False,
        recommended_next_validator_keys=[],
        partial_validation_summary="Partial validation summary",
        partial_reason="work_missing",
        metadata={"aggregated": True},
    )

    validator_a_result = ValidationResult(
        validator_key="code_change_agent_validator",
        discipline="code",
        decision="completed",
        summary="code_change_agent_validator decided completed",
        findings=[],
        validated_scope="code_change_agent_validator validated its scope",
        missing_scope=None,
        blockers=[],
        manual_review_required=False,
        final_task_status="completed",
        artifacts_created=[],
        validated_evidence_ids=[],
        unconsumed_evidence_ids=[],
        followup_validation_required=False,
        recommended_next_validator_keys=[],
        partial_validation_summary=None,
        metadata={"validator_key": "code_change_agent_validator"},
    )

    validator_b_result = ValidationResult(
        validator_key="command_runner_agent_validator",
        discipline="code",
        decision="partial",
        summary="command_runner_agent_validator decided partial",
        findings=[],
        validated_scope="command_runner_agent_validator validated its scope",
        missing_scope="missing scope",
        blockers=["blocker"],
        manual_review_required=False,
        final_task_status="partial",
        artifacts_created=[],
        validated_evidence_ids=[],
        unconsumed_evidence_ids=[],
        followup_validation_required=False,
        recommended_next_validator_keys=[],
        partial_validation_summary=None,
        partial_reason="work_missing",
        metadata={"validator_key": "command_runner_agent_validator"},
    )

    aggregation_result = ValidationAggregationResult(
        final_result=final_result,
        validator_results=[
            validator_a_result,
            validator_b_result,
        ],
        winning_decision="partial",
        notes=["aggregation completed"],
    )
    monkeypatch.setattr(
        "app.services.validation.service.ValidationRegistry",
        lambda: _FakeRegistry([validator_a, validator_b]),
    )
    monkeypatch.setattr(
        "app.services.validation.service.select_validators_for_task",
        lambda **kwargs: selection_result,
    )
    monkeypatch.setattr(
        "app.services.validation.service.aggregate_validation_results",
        lambda **kwargs: aggregation_result,
    )

    result = validate_execution_result(
        task=_FakeTask(),
        execution_request=_make_execution_request(),
        execution_result=_make_execution_result(),
        execution_run=_FakeExecutionRun(),
        persisted_artifacts=[],
    )

    assert result.validation_result.decision == "partial"
    assert result.validation_result.validator_key == "validation_aggregator"
    assert result.selection_result == selection_result
    assert result.aggregation_result == aggregation_result

    assert len(validator_a.calls) == 1
    assert len(validator_b.calls) == 1
    assert validator_a.calls[0].execution_request.task_id == 10
    assert validator_b.calls[0].execution_request.task_id == 10

    assert [item.validator_key for item in result.validator_results] == [
        "code_change_agent_validator",
        "command_runner_agent_validator",
    ]


def test_validate_execution_result_fails_when_no_validators_are_selected(monkeypatch):
    selection_result = ValidationSelectionResult(
        selected_validators=[],
        skipped_producers=["context_selection_agent"],
        notes=["nothing to validate"],
    )

    monkeypatch.setattr(
        "app.services.validation.service.ValidationRegistry",
        lambda: _FakeRegistry([]),
    )
    monkeypatch.setattr(
        "app.services.validation.service.select_validators_for_task",
        lambda **kwargs: selection_result,
    )

    with pytest.raises(ValidationServiceError, match="No validators were executed"):
        validate_execution_result(
            task=_FakeTask(),
            execution_request=_make_execution_request(),
            execution_result=_make_execution_result(),
            execution_run=_FakeExecutionRun(),
            persisted_artifacts=[],
        )


def test_validate_execution_result_fails_when_execution_order_does_not_match_selection(monkeypatch):
    validator_a = _FakeValidator(
        validator_key="code_change_agent_validator",
        producer_key="code_change_agent",
        decision="completed",
    )
    validator_b = _FakeValidator(
        validator_key="command_runner_agent_validator",
        producer_key="command_runner_agent",
        decision="completed",
    )

    selection_result = ValidationSelectionResult(
        selected_validators=[
            SelectedValidator(
                producer_key="code_change_agent",
                validator_key="code_change_agent_validator",
                selection_reason="selected first",
            ),
            SelectedValidator(
                producer_key="command_runner_agent",
                validator_key="command_runner_agent_validator",
                selection_reason="selected second",
            ),
        ],
        skipped_producers=[],
        notes=[],
    )

    final_result = ValidationResult(
        validator_key="validation_aggregator",
        discipline="code",
        decision="completed",
        summary="Aggregated validation decision is completed.",
        findings=[],
        validated_scope="All scope validated",
        missing_scope=None,
        blockers=[],
        manual_review_required=False,
        final_task_status="completed",
        artifacts_created=[],
        validated_evidence_ids=[],
        unconsumed_evidence_ids=[],
        followup_validation_required=False,
        recommended_next_validator_keys=[],
        partial_validation_summary=None,
        metadata={"aggregated": True},
    )

    aggregation_result = ValidationAggregationResult(
        final_result=final_result,
        validator_results=[],
        winning_decision="completed",
        notes=[],
    )

    class _MismatchingRegistry:
        def get_by_key(self, validator_key: str):
            if validator_key == "code_change_agent_validator":
                return validator_b
            return validator_a

    monkeypatch.setattr(
        "app.services.validation.service.ValidationRegistry",
        lambda: _MismatchingRegistry(),
    )
    monkeypatch.setattr(
        "app.services.validation.service.select_validators_for_task",
        lambda **kwargs: selection_result,
    )
    monkeypatch.setattr(
        "app.services.validation.service.aggregate_validation_results",
        lambda **kwargs: aggregation_result,
    )

    with pytest.raises(
        ValidationServiceError,
        match="Validator execution order does not match validator selection order",
    ):
        validate_execution_result(
            task=_FakeTask(),
            execution_request=_make_execution_request(),
            execution_result=_make_execution_result(),
            execution_run=_FakeExecutionRun(),
            persisted_artifacts=[],
        )


def _make_execution_request_and_result_input() -> TaskValidationInput:
    return TaskValidationInput(
        execution_request=_make_execution_request(),
        execution_result=_make_execution_result(),
        intent=None,
        metadata={},
    )
