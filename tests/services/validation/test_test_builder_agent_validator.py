"""
Unit tests for TestBuilderAgentValidator.

Covers:
- completed decision when tests adequately cover acceptance_criteria
- partial with annotations when potential_implementation_gaps exist
- partial when acceptance criteria are uncovered
- failed for structurally broken tests
- coverage observation payload forwarded to LLM
- boundary sanitisation: forbidden text / forbidden categories stripped
- decision escalated to completed when negative signals are stripped
- metadata includes has_coverage_observation flag
"""

from __future__ import annotations

import pytest

from app.execution_engine.contracts import (
    CHANGE_TYPE_CREATED,
    OBSERVATION_TYPE_TEST_COVERAGE,
    ExecutionEvidence,
    ExecutionRequest,
    ExecutionResult,
    HistoricalExecutionContext,
    ProjectExecutionContext,
)
from app.services.validation.contracts import TaskValidationInput
from app.services.validation.validators.test_builder_agent_validator import (
    TestBuilderAgentValidator,
    TestBuilderAgentValidatorError,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _CapturingProvider:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls: list[dict] = []

    def generate_structured(self, **kwargs) -> dict:
        self.calls.append(kwargs)
        return self.payload


def _make_request(
    *,
    workspace_path: str,
    source_path: str,
    relevant_files: list[str] | None = None,
) -> ExecutionRequest:
    return ExecutionRequest(
        task_id=10,
        project_id=20,
        execution_run_id=30,
        task_title="Write unit tests for UserService",
        task_description="Cover create, read, update, delete operations.",
        task_summary="Testing task.",
        objective="Have all CRUD acceptance criteria covered by tests.",
        acceptance_criteria="All CRUD operations on users have at least one passing test.",
        tests_required="pytest tests for create, read, update, delete.",
        technical_constraints="Python 3.12, pytest.",
        out_of_scope="Frontend tests.",
        executor_type="execution_engine",
        task_type="testing",
        context=ProjectExecutionContext(
            project_id=20,
            workspace_path=workspace_path,
            source_path=source_path,
            relevant_files=list(relevant_files or []),
            key_decisions=[],
            related_tasks=[],
        ),
        historical_context=HistoricalExecutionContext(selected_task_runs=[]),
    )


def _make_evidence(
    *,
    test_files: list[str] | None = None,
    coverage_payload: dict | None = None,
) -> ExecutionEvidence:
    evidence = ExecutionEvidence()

    for path in test_files or []:
        evidence.add_changed_file(
            path=path,
            change_type=CHANGE_TYPE_CREATED,
            producer="test_builder_agent",
        )

    if coverage_payload is not None:
        evidence.add_observation(
            evidence_type=OBSERVATION_TYPE_TEST_COVERAGE,
            producer="test_builder_agent",
            summary="Test coverage assessment.",
            payload=coverage_payload,
        )

    return evidence


def _make_result(evidence: ExecutionEvidence) -> ExecutionResult:
    return ExecutionResult(
        task_id=10,
        decision="completed",
        summary="Tests materialised.",
        details="test_builder_agent wrote test files.",
        completed_scope="Test files written.",
        remaining_scope=None,
        blockers_found=[],
        validation_notes=[],
        output_snapshot="done",
        execution_agent_sequence=["context_selection_agent", "test_builder_agent"],
        evidence=evidence,
    )


def _make_validation_input(
    *,
    workspace_path: str,
    source_path: str,
    test_files: list[str] | None = None,
    coverage_payload: dict | None = None,
    relevant_files: list[str] | None = None,
) -> TaskValidationInput:
    request = _make_request(
        workspace_path=workspace_path,
        source_path=source_path,
        relevant_files=relevant_files,
    )
    evidence = _make_evidence(test_files=test_files, coverage_payload=coverage_payload)
    result = _make_result(evidence)
    return TaskValidationInput(
        execution_request=request,
        execution_result=result,
        intent=None,
        metadata={},
    )


def _llm_payload(
    *,
    decision: str = "completed",
    partial_annotations: list[dict] | None = None,
    missing_scope: str | None = None,
) -> dict:
    has_gap = decision == "partial"
    return {
        "decision": decision,
        "summary": f"Test validation decision={decision}.",
        "validated_scope": (
            "CRUD tests covering acceptance criteria." if decision != "failed" else None
        ),
        "missing_scope": missing_scope or ("Some criteria uncovered." if has_gap else None),
        "blockers": ["structural gap"] if decision == "failed" else [],
        "findings": [
            {
                "severity": "error" if decision == "failed" else "info",
                "category": "test_coverage",
                "message": "Tests evaluated against acceptance criteria.",
                "evidence_refs": [],
                "file_paths": [],
            }
        ],
        "manual_review_required": decision == "manual_review",
        "reasoning_notes": ["Based on test files and coverage observation."],
        "partial_annotations": partial_annotations or [],
    }


# ---------------------------------------------------------------------------
# completed decision
# ---------------------------------------------------------------------------


def test_validator_completed_decision(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    source = tmp_path / "source"
    workspace.mkdir()
    source.mkdir()

    test_file = workspace / "tests" / "test_user_service.py"
    test_file.parent.mkdir()
    test_file.write_text("def test_create(): assert True")

    validation_input = _make_validation_input(
        workspace_path=str(workspace),
        source_path=str(source),
        test_files=["tests/test_user_service.py"],
        coverage_payload={
            "covered_cases": ["create user", "delete user", "read user", "update user"],
            "uncovered_cases": [],
            "potential_implementation_gaps": [],
            "tested_against": "acceptance_criteria",
            "confidence": "high",
        },
    )

    provider = _CapturingProvider(_llm_payload(decision="completed"))
    monkeypatch.setattr(
        "app.services.validation.validators.test_builder_agent_validator.get_llm_provider",
        lambda **_kw: provider,
    )

    result = TestBuilderAgentValidator().validate(validation_input)

    assert result.decision == "completed"
    assert result.final_task_status == "completed"
    assert len(provider.calls) == 1


# ---------------------------------------------------------------------------
# partial with implementation gap annotations
# ---------------------------------------------------------------------------


def test_validator_partial_with_implementation_gap_annotations(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    source = tmp_path / "source"
    workspace.mkdir()
    source.mkdir()

    test_file = workspace / "tests" / "test_user_service.py"
    test_file.parent.mkdir()
    test_file.write_text("def test_delete(): pass")

    coverage = {
        "covered_cases": ["delete user"],
        "uncovered_cases": [],
        "potential_implementation_gaps": ["delete_user() is not implemented in UserService"],
        "tested_against": "acceptance_criteria",
        "confidence": "medium",
    }
    annotations = [
        {
            "file_path": "app/services/user_service.py",
            "issue_summary": "delete_user() method is missing",
            "required_action": "Implement delete_user(user_id) in UserService",
        }
    ]
    validation_input = _make_validation_input(
        workspace_path=str(workspace),
        source_path=str(source),
        test_files=["tests/test_user_service.py"],
        coverage_payload=coverage,
    )

    provider = _CapturingProvider(
        _llm_payload(
            decision="partial",
            partial_annotations=annotations,
            missing_scope="delete_user() is not implemented",
        )
    )
    monkeypatch.setattr(
        "app.services.validation.validators.test_builder_agent_validator.get_llm_provider",
        lambda **_kw: provider,
    )

    result = TestBuilderAgentValidator().validate(validation_input)

    assert result.decision == "partial"
    assert result.final_task_status == "partial"
    assert len(result.partial_annotations) == 1

    ann = result.partial_annotations[0]
    assert ann.file_path == "app/services/user_service.py"
    assert "delete_user" in ann.issue_summary
    assert "delete_user" in ann.required_action


# ---------------------------------------------------------------------------
# failed decision
# ---------------------------------------------------------------------------


def test_validator_failed_decision_for_broken_tests(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    source = tmp_path / "source"
    workspace.mkdir()
    source.mkdir()

    test_file = workspace / "tests" / "test_bad.py"
    test_file.parent.mkdir()
    test_file.write_text("def test_placeholder(): assert True  # skeleton")

    validation_input = _make_validation_input(
        workspace_path=str(workspace),
        source_path=str(source),
        test_files=["tests/test_bad.py"],
        coverage_payload=None,
    )

    provider = _CapturingProvider(_llm_payload(decision="failed"))
    monkeypatch.setattr(
        "app.services.validation.validators.test_builder_agent_validator.get_llm_provider",
        lambda **_kw: provider,
    )

    result = TestBuilderAgentValidator().validate(validation_input)

    assert result.decision == "failed"
    assert result.final_task_status == "failed"


# ---------------------------------------------------------------------------
# Coverage observation forwarded to LLM (prompt inspection)
# ---------------------------------------------------------------------------


def test_coverage_observation_included_in_prompt(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    source = tmp_path / "source"
    workspace.mkdir()
    source.mkdir()

    coverage = {
        "covered_cases": ["create user"],
        "uncovered_cases": ["delete user"],
        "potential_implementation_gaps": ["delete_user() is missing"],
        "tested_against": "acceptance_criteria",
        "confidence": "low",
    }
    validation_input = _make_validation_input(
        workspace_path=str(workspace),
        source_path=str(source),
        test_files=[],
        coverage_payload=coverage,
    )

    provider = _CapturingProvider(_llm_payload(decision="completed"))
    monkeypatch.setattr(
        "app.services.validation.validators.test_builder_agent_validator.get_llm_provider",
        lambda **_kw: provider,
    )

    TestBuilderAgentValidator().validate(validation_input)

    assert len(provider.calls) == 1
    prompt = provider.calls[0]["user_prompt"]
    assert "test_coverage_observation" in prompt
    assert "create user" in prompt
    assert "delete_user() is missing" in prompt


def test_no_coverage_observation_still_works(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    source = tmp_path / "source"
    workspace.mkdir()
    source.mkdir()

    validation_input = _make_validation_input(
        workspace_path=str(workspace),
        source_path=str(source),
        test_files=[],
        coverage_payload=None,
    )

    provider = _CapturingProvider(_llm_payload(decision="completed"))
    monkeypatch.setattr(
        "app.services.validation.validators.test_builder_agent_validator.get_llm_provider",
        lambda **_kw: provider,
    )

    result = TestBuilderAgentValidator().validate(validation_input)
    assert result.decision == "completed"

    prompt = provider.calls[0]["user_prompt"]
    assert "not available" in prompt


# ---------------------------------------------------------------------------
# metadata
# ---------------------------------------------------------------------------


def test_metadata_has_coverage_observation_flag_true(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    source = tmp_path / "source"
    workspace.mkdir()
    source.mkdir()

    validation_input = _make_validation_input(
        workspace_path=str(workspace),
        source_path=str(source),
        coverage_payload={
            "covered_cases": [],
            "uncovered_cases": [],
            "potential_implementation_gaps": [],
            "tested_against": "acceptance_criteria",
            "confidence": "high",
        },
    )

    provider = _CapturingProvider(_llm_payload(decision="completed"))
    monkeypatch.setattr(
        "app.services.validation.validators.test_builder_agent_validator.get_llm_provider",
        lambda **_kw: provider,
    )

    result = TestBuilderAgentValidator().validate(validation_input)
    assert result.metadata["has_coverage_observation"] is True


def test_metadata_has_coverage_observation_flag_false(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    source = tmp_path / "source"
    workspace.mkdir()
    source.mkdir()

    validation_input = _make_validation_input(
        workspace_path=str(workspace),
        source_path=str(source),
        coverage_payload=None,
    )

    provider = _CapturingProvider(_llm_payload(decision="completed"))
    monkeypatch.setattr(
        "app.services.validation.validators.test_builder_agent_validator.get_llm_provider",
        lambda **_kw: provider,
    )

    result = TestBuilderAgentValidator().validate(validation_input)
    assert result.metadata["has_coverage_observation"] is False


# ---------------------------------------------------------------------------
# Boundary sanitisation
# ---------------------------------------------------------------------------


def test_forbidden_finding_category_is_stripped(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    source = tmp_path / "source"
    workspace.mkdir()
    source.mkdir()

    payload = {
        "decision": "partial",
        "summary": "Partial — some criteria remain uncovered.",
        "validated_scope": None,
        "missing_scope": "Some criteria are uncovered.",
        "blockers": [],
        "findings": [
            {
                "severity": "warning",
                "category": "pipeline_consistency",  # forbidden
                "message": "The pipeline is inconsistent.",
                "evidence_refs": [],
                "file_paths": [],
            },
            {
                "severity": "warning",
                "category": "test_coverage",  # allowed
                "message": "Some tests are missing.",
                "evidence_refs": [],
                "file_paths": [],
            },
        ],
        "manual_review_required": False,
        "reasoning_notes": [],
        "partial_annotations": [
            {
                "file_path": None,
                "issue_summary": "Coverage gap",
                "required_action": "Cover the missing criteria",
            }
        ],
    }

    validation_input = _make_validation_input(
        workspace_path=str(workspace),
        source_path=str(source),
    )

    provider = _CapturingProvider(payload)
    monkeypatch.setattr(
        "app.services.validation.validators.test_builder_agent_validator.get_llm_provider",
        lambda **_kw: provider,
    )

    result = TestBuilderAgentValidator().validate(validation_input)

    codes = [f.code for f in result.findings]
    assert "pipeline_consistency" not in codes
    assert "test_coverage" in codes


def test_partial_escalates_to_completed_when_all_negatives_stripped(tmp_path, monkeypatch):
    """If a partial decision has all negative signals stripped, it must become completed."""
    workspace = tmp_path / "workspace"
    source = tmp_path / "source"
    workspace.mkdir()
    source.mkdir()

    payload = {
        "decision": "partial",
        "summary": "The orchestrator reported partial.",
        "validated_scope": None,
        "missing_scope": "The promotion did not occur.",  # forbidden text — will be stripped
        "blockers": ["execution evidence does not confirm"],  # forbidden text — will be stripped
        "findings": [
            {
                "severity": "warning",
                "category": "orchestrator_state",  # forbidden category — will be stripped
                "message": "Orchestrator said partial.",
                "evidence_refs": [],
                "file_paths": [],
            }
        ],
        "manual_review_required": False,
        "reasoning_notes": [],
        "partial_annotations": [],
    }

    validation_input = _make_validation_input(
        workspace_path=str(workspace),
        source_path=str(source),
    )

    provider = _CapturingProvider(payload)
    monkeypatch.setattr(
        "app.services.validation.validators.test_builder_agent_validator.get_llm_provider",
        lambda **_kw: provider,
    )

    result = TestBuilderAgentValidator().validate(validation_input)

    # All negative signals stripped → escalated to completed
    assert result.decision == "completed"
    assert result.final_task_status == "completed"


# ---------------------------------------------------------------------------
# Discipline
# ---------------------------------------------------------------------------


def test_discipline_defaults_to_testing_when_no_intent(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    source = tmp_path / "source"
    workspace.mkdir()
    source.mkdir()

    validation_input = _make_validation_input(
        workspace_path=str(workspace),
        source_path=str(source),
    )

    provider = _CapturingProvider(_llm_payload(decision="completed"))
    monkeypatch.setattr(
        "app.services.validation.validators.test_builder_agent_validator.get_llm_provider",
        lambda **_kw: provider,
    )

    result = TestBuilderAgentValidator().validate(validation_input)
    assert result.discipline == "testing"


# ---------------------------------------------------------------------------
# Validator key / producer key
# ---------------------------------------------------------------------------


def test_validator_and_producer_keys():
    v = TestBuilderAgentValidator()
    assert v.validator_key == "test_builder_agent_validator"
    assert v.producer_key == "test_builder_agent"


# ---------------------------------------------------------------------------
# Retry on invalid LLM output
# ---------------------------------------------------------------------------


def test_validator_retries_once_on_invalid_llm_output(tmp_path, monkeypatch):
    """First response invalid → retry → second response valid → decision returned."""
    workspace = tmp_path / "workspace"
    source = tmp_path / "source"
    workspace.mkdir()
    source.mkdir()

    validation_input = _make_validation_input(
        workspace_path=str(workspace),
        source_path=str(source),
    )

    invalid_raw = {"not_a_valid_field": "oops"}
    valid_payload = _llm_payload(decision="completed")
    responses = [invalid_raw, valid_payload]
    calls: list[dict] = []

    class _RetryProvider:
        def generate_structured(self, **kwargs):
            calls.append(kwargs)
            return responses.pop(0)

    monkeypatch.setattr(
        "app.services.validation.validators.test_builder_agent_validator.get_llm_provider",
        lambda **_kw: _RetryProvider(),
    )

    result = TestBuilderAgentValidator().validate(validation_input)

    assert result.decision == "completed"
    # Two calls: first (failed) + retry (succeeded)
    assert len(calls) == 2


def test_validator_raises_after_two_invalid_outputs(tmp_path, monkeypatch):
    """Both primary and retry responses are invalid → TestBuilderAgentValidatorError raised."""
    workspace = tmp_path / "workspace"
    source = tmp_path / "source"
    workspace.mkdir()
    source.mkdir()

    validation_input = _make_validation_input(
        workspace_path=str(workspace),
        source_path=str(source),
    )

    invalid_raw = {"not_a_valid_field": "oops"}

    class _AlwaysBadProvider:
        def generate_structured(self, **kwargs):
            return invalid_raw

    monkeypatch.setattr(
        "app.services.validation.validators.test_builder_agent_validator.get_llm_provider",
        lambda **_kw: _AlwaysBadProvider(),
    )

    with pytest.raises(TestBuilderAgentValidatorError):
        TestBuilderAgentValidator().validate(validation_input)
