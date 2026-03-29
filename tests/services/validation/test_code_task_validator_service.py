from app.services.validation.contracts import (
    ResolvedValidationIntent,
    TaskValidationInput,
    ValidationEvidenceItem,
    ValidationEvidencePackage,
    ValidationExecutionContext,
    ValidationRequestContext,
    ValidationTaskContext,
)
from app.services.validation.validators.code.service import (
    validate_code_task_with_llm,
)


class _FakeProvider:
    def __init__(self, payload):
        self._payload = payload

    def generate_structured(self, **kwargs):
        return self._payload


def test_validate_code_task_with_llm_returns_canonical_validation_result(monkeypatch):
    validation_input = TaskValidationInput(
        intent=ResolvedValidationIntent(
            validator_key="code_task_validator",
            discipline="code",
            validation_mode="post_execution",
            requires_workspace=True,
            requires_artifacts=True,
            requires_changed_files=True,
            requires_commands=True,
            requires_execution_context=True,
            requires_output_snapshot=True,
            requires_agent_sequence=True,
            requires_file_reading=True,
            notes=[],
        ),
        task=ValidationTaskContext(
            task_id=1,
            project_id=10,
            title="Implement endpoint and tests",
            description="Add endpoint and update tests.",
            summary="Repository implementation work.",
            objective="Deliver endpoint behavior.",
            acceptance_criteria="Endpoint exists and tests cover behavior.",
            technical_constraints="Keep project structure.",
            out_of_scope="No deployment.",
            task_type="implementation",
            planning_level="atomic",
            executor_type="execution_engine",
        ),
        execution=ValidationExecutionContext(
            execution_run_id=100,
            execution_status="succeeded",
            decision="completed",
            summary="Execution completed.",
            details="Updated code and tests.",
            completed_scope="Endpoint and tests updated.",
            remaining_scope=None,
            blockers_found=[],
            validation_notes=["Execution finished normally."],
            output_snapshot="done",
            execution_agent_sequence=["planner", "editor", "tester"],
        ),
        request_context=ValidationRequestContext(
            workspace_path="/tmp/workspace",
            source_path="/tmp/source",
            allowed_paths=["app/api.py", "tests/test_api.py"],
            relevant_files=["app/api.py", "tests/test_api.py"],
            key_decisions=["Preserve the current API shape."],
            related_task_ids=[2, 3],
        ),
        evidence_package=ValidationEvidencePackage(
            evidence_items=[
                ValidationEvidenceItem(
                    evidence_id="produced_file:app/api.py",
                    evidence_kind="produced_file",
                    media_type="text/plain",
                    representation_kind="full_text",
                    source="execution_workspace",
                    logical_name="api.py",
                    path="app/api.py",
                    change_type="modified",
                    content_text="def endpoint():\n    return {'ok': True}\n",
                ),
                ValidationEvidenceItem(
                    evidence_id="produced_file:tests/test_api.py",
                    evidence_kind="produced_file",
                    media_type="text/plain",
                    representation_kind="full_text",
                    source="execution_workspace",
                    logical_name="test_api.py",
                    path="tests/test_api.py",
                    change_type="modified",
                    content_text="def test_endpoint():\n    assert True\n",
                ),
                ValidationEvidenceItem(
                    evidence_id="command:0",
                    evidence_kind="command_output",
                    media_type="text/plain",
                    representation_kind="command_output",
                    source="execution_result",
                    logical_name="pytest -q",
                    content_text="$ pytest -q\n[exit_code=0]\n\nSTDOUT:\n2 passed\n\nSTDERR:\n",
                ),
                ValidationEvidenceItem(
                    evidence_id="artifact:42",
                    evidence_kind="persisted_artifact",
                    media_type="application/json",
                    representation_kind="artifact_preview",
                    source="persisted_artifact",
                    logical_name="code_validation_result",
                    artifact_id=42,
                    content_text='{"decision":"completed"}',
                ),
            ]
        ),
        metadata={
            "evidence_item_count": 4,
        },
    )

    raw_output = {
        "decision": "completed",
        "summary": "The task appears complete and the evidence supports closure.",
        "validated_scope": "The endpoint implementation and related tests were updated.",
        "missing_scope": None,
        "blockers": [],
        "findings": [
            {
                "severity": "info",
                "category": "acceptance_alignment",
                "message": "The updated files and test command output support the claimed implementation.",
                "evidence_refs": [
                    "produced_file:app/api.py",
                    "command:0",
                    "produced_file:tests/test_api.py",
                ],
                "file_paths": ["app/api.py", "tests/test_api.py"],
            }
        ],
        "manual_review_required": False,
        "confidence": "high",
        "reasoning_notes": [
            "The implementation evidence aligns with the task objective.",
            "The command output supports successful verification.",
        ],
    }

    monkeypatch.setattr(
        "app.services.validation.validators.code.service.get_llm_provider",
        lambda: _FakeProvider(raw_output),
    )

    result = validate_code_task_with_llm(
        validation_input=validation_input,
    )

    assert result.validator_key == "code_task_validator"
    assert result.discipline == "code"
    assert result.decision == "completed"
    assert result.summary == "The task appears complete and the evidence supports closure."
    assert result.validated_scope == "The endpoint implementation and related tests were updated."
    assert result.missing_scope is None
    assert result.blockers == []
    assert result.manual_review_required is False
    assert result.final_task_status == "completed"
    assert result.metadata["confidence"] == "high"
    assert len(result.findings) == 1
    assert result.findings[0].code == "acceptance_alignment"
    assert result.findings[0].file_path == "app/api.py"
    assert result.validated_evidence_ids == [
        "produced_file:app/api.py",
        "produced_file:tests/test_api.py",
        "command:0",
        "artifact:42",
    ]
    assert result.unconsumed_evidence_ids == []
    assert result.followup_validation_required is False
    assert result.recommended_next_validator_keys == []
    assert result.partial_validation_summary is None


def test_validate_code_task_with_llm_reports_unconsumed_evidence_for_future_orchestration(
    monkeypatch,
):
    validation_input = TaskValidationInput(
        intent=ResolvedValidationIntent(
            validator_key="code_task_validator",
            discipline="code",
            validation_mode="post_execution",
            requires_workspace=True,
            requires_artifacts=True,
            requires_changed_files=True,
            requires_commands=True,
            requires_execution_context=True,
            requires_output_snapshot=True,
            requires_agent_sequence=True,
            requires_file_reading=True,
            notes=[],
        ),
        task=ValidationTaskContext(
            task_id=1,
            project_id=10,
            title="Create copy and image",
        ),
        execution=ValidationExecutionContext(
            execution_run_id=100,
            execution_status="succeeded",
            decision="completed",
            summary="Execution completed.",
        ),
        request_context=ValidationRequestContext(),
        evidence_package=ValidationEvidencePackage(
            evidence_items=[
                ValidationEvidenceItem(
                    evidence_id="produced_file:campaign.md",
                    evidence_kind="produced_file",
                    media_type="text/plain",
                    representation_kind="full_text",
                    source="execution_workspace",
                    path="campaign.md",
                    content_text="# Campaign copy",
                ),
                ValidationEvidenceItem(
                    evidence_id="generated_image:hero.png",
                    evidence_kind="generated_image",
                    media_type="image/png",
                    representation_kind="binary_placeholder",
                    source="execution_workspace",
                    path="hero.png",
                    content_summary="Generated hero image.",
                ),
            ]
        ),
        metadata={},
    )

    raw_output = {
        "decision": "partial",
        "summary": "The textual part can be validated, but additional evidence remains outside this validator's capabilities.",
        "validated_scope": "The generated campaign copy was reviewed.",
        "missing_scope": "The generated image was not validated by this validator.",
        "blockers": [],
        "findings": [],
        "manual_review_required": False,
        "confidence": "medium",
        "reasoning_notes": ["Text evidence was validated, but image evidence was not consumed."],
    }

    monkeypatch.setattr(
        "app.services.validation.validators.code.service.get_llm_provider",
        lambda: _FakeProvider(raw_output),
    )

    result = validate_code_task_with_llm(
        validation_input=validation_input,
    )

    assert result.validated_evidence_ids == ["produced_file:campaign.md"]
    assert result.unconsumed_evidence_ids == ["generated_image:hero.png"]
    assert result.followup_validation_required is True
    assert result.recommended_next_validator_keys == ["image_task_validator"]
    assert result.partial_validation_summary is not None
