from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from app.execution_engine.contracts import (
    OBSERVATION_TYPE_IMAGE_GENERATED,
    ExecutionEvidence,
    ExecutionRequest,
    ExecutionResult,
    ProjectExecutionContext,
)
from app.services.validation.contracts import TaskValidationInput
from app.services.validation.validators.image_generation_agent_validator import (
    ImageGenerationAgentValidator,
)


def _make_png_bytes() -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"\x00" * 200


def _make_request(workspace_path: str) -> ExecutionRequest:
    return ExecutionRequest(
        task_id=1,
        project_id=1,
        execution_run_id=1,
        task_title="Generate app icon",
        task_description="Create a flat-style icon",
        acceptance_criteria="128×128 PNG icon",
        executor_type="execution_engine",
        context=ProjectExecutionContext(
            project_id=1,
            source_path="/source",
            workspace_path=workspace_path,
        ),
    )


def _make_result_with_image_evidence(image_path: str) -> ExecutionResult:
    evidence = ExecutionEvidence()
    evidence.add_changed_file(
        path=image_path, change_type="created", producer="image_generation_agent"
    )
    evidence.add_observation(
        evidence_type=OBSERVATION_TYPE_IMAGE_GENERATED,
        producer="image_generation_agent",
        summary="Generated icon",
        path=image_path,
        payload={
            "prompt_engineering": {
                "main_prompt": "flat icon",
                "style_directive": "flat icon",
                "design_rationale": "Blue for brand",
                "intended_colors": ["#2563EB"],
            },
            "generation": {"model": "dall-e-3"},
            "output": {
                "final_width": 128,
                "final_height": 128,
                "format": "png",
                "was_resized": True,
            },
        },
    )
    return ExecutionResult(
        task_id=1,
        decision="completed",
        summary="Image generated",
        evidence=evidence,
    )


# ── Technical failure: file missing ─────────────────────────────────────────


def test_validate_fails_when_image_missing(tmp_path: Path):
    request = _make_request(str(tmp_path))
    result = _make_result_with_image_evidence("assets/missing.png")
    validation_input = TaskValidationInput(execution_request=request, execution_result=result)

    validator = ImageGenerationAgentValidator()
    output = validator.validate(validation_input)

    assert output.decision == "failed"
    assert "not found" in output.summary.lower() or "not found" in (
        output.findings[0].message if output.findings else ""
    )


# ── Technical failure: no evidence ──────────────────────────────────────────


def test_validate_fails_when_no_evidence(tmp_path: Path):
    request = _make_request(str(tmp_path))
    result = ExecutionResult(
        task_id=1,
        decision="completed",
        summary="Done",
        evidence=ExecutionEvidence(),
    )
    validation_input = TaskValidationInput(execution_request=request, execution_result=result)

    validator = ImageGenerationAgentValidator()
    output = validator.validate(validation_input)

    assert output.decision == "failed"


# ── Technical failure: suspiciously small file ───────────────────────────────


def test_validate_fails_when_image_too_small(tmp_path: Path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    img_file = workspace / "icon.png"
    img_file.write_bytes(b"\x89PNG")  # < 100 bytes

    request = _make_request(str(workspace))
    result = _make_result_with_image_evidence("icon.png")
    validation_input = TaskValidationInput(execution_request=request, execution_result=result)

    validator = ImageGenerationAgentValidator()
    output = validator.validate(validation_input)

    assert output.decision == "failed"


# ── Semantic validation: vision LLM called when file exists ─────────────────


def test_validate_calls_vision_llm_when_file_exists(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    img_file = workspace / "icon.png"
    img_file.write_bytes(_make_png_bytes())

    llm = MagicMock()
    llm.generate_structured.return_value = {
        "decision": "completed",
        "summary": "Icon matches requirements",
        "validated_scope": "128×128 flat icon",
        "missing_scope": None,
        "blockers": [],
        "findings": [],
        "manual_review_required": False,
    }
    monkeypatch.setattr(
        "app.services.validation.validators.image_generation_agent_validator.get_llm_provider",
        lambda **_: llm,
    )

    request = _make_request(str(workspace))
    result = _make_result_with_image_evidence("icon.png")
    validation_input = TaskValidationInput(execution_request=request, execution_result=result)

    validator = ImageGenerationAgentValidator()
    output = validator.validate(validation_input)

    assert output.decision == "completed"
    assert llm.generate_structured.called
    # Verify images were passed to the LLM
    call_kwargs = llm.generate_structured.call_args[1]
    assert "images" in call_kwargs
    assert len(call_kwargs["images"]) == 1


# ── Semantic: partial decision propagated ────────────────────────────────────


def test_validate_maps_partial_decision(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "icon.png").write_bytes(_make_png_bytes())

    llm = MagicMock()
    llm.generate_structured.return_value = {
        "decision": "partial",
        "summary": "Icon is acceptable but color is off",
        "validated_scope": None,
        "missing_scope": "Exact brand color not matched",
        "blockers": [],
        "findings": [],
        "manual_review_required": False,
    }
    monkeypatch.setattr(
        "app.services.validation.validators.image_generation_agent_validator.get_llm_provider",
        lambda **_: llm,
    )

    request = _make_request(str(workspace))
    result = _make_result_with_image_evidence("icon.png")
    validation_input = TaskValidationInput(execution_request=request, execution_result=result)

    validator = ImageGenerationAgentValidator()
    output = validator.validate(validation_input)

    assert output.decision == "partial"
